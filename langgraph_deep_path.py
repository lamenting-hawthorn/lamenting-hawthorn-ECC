"""
LangGraph Deep Path — Production Agent State Machine
=====================================================
This is the core state graph for complex queries in the revised architecture.
Hermes remains the interface + tool layer. LangGraph owns the orchestration.

Design principles from 2026 production research:
- Query-adaptive routing (tier 1/2/3)
- Multi-layer verification with feedback loops
- Structured memory (episodic / semantic / procedural)
- Bounded retries, circuit breakers, audit trails
"""

from typing import TypedDict, Annotated, List, Dict, Any, Optional, Literal
from dataclasses import dataclass, field
from datetime import datetime, timezone
import operator

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres import PostgresSaver
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

# ---------------------------------------------------------------------------
# 1. STATE SCHEMA — everything that flows through the graph
# ---------------------------------------------------------------------------

class VerificationResult(TypedDict):
    layer: str                          # "tool_contract" | "grounding" | "guardrail" | "self_critique"
    passed: bool
    score: float                        # 0.0 - 1.0
    feedback: str                       # correction prompt on failure
    evidence: List[str]                 # what supported/rejected the claim

class MemoryEntry(TypedDict):
    memory_type: Literal["episodic", "semantic", "procedural"]
    category: Literal["fact", "preference", "action_item", "correction", "procedure"]
    content: str
    confidence: float                   # 0.0 - 1.0
    source: str                         # "user_utterance" | "tool_result" | "agent_inference"
    expiry: Optional[str]             # ISO date or null for permanent
    user_id: str
    session_id: str
    created_at: str

class AgentState(TypedDict):
    # Input / identity
    user_id: str
    session_id: str
    org_id: Optional[str]
    original_query: str
    messages: Annotated[List[BaseMessage], operator.add]

    # Memory loaded at start
    episodic_context: str               # summarized session history
    semantic_memories: List[Dict]     # facts, preferences from vector+graph store
    procedural_skills: List[str]      # relevant hermes skill names

    # Query classification
    query_tier: Literal["1_cache", "2_hybrid", "3_multihop"]
    query_complexity_score: float     # 0.0 - 1.0

    # Retrieval
    retrieved_chunks: List[Dict]      # {text, source, score, retrieval_method}
    retrieval_confidence: float

    # Agent execution scratchpad
    agent_scratchpad: str
    tool_calls: List[Dict]            # {tool_name, args, result, timestamp}
    pending_tool_calls: List[Dict]    # queue for parallel execution

    # Verification
    verification_results: List[VerificationResult]
    overall_verdict: Literal["PASS", "FAIL", "UNCERTAIN"]
    retry_count: int
    max_retries: int

    # Memory write-back (populated during execution)
    memories_to_write: List[MemoryEntry]

    # Output
    final_answer: Optional[str]
    answer_confidence: float
    answer_grounding: List[str]       # sources cited
    latency_ms: int
    error: Optional[str]

    # Observability
    trace_spans: List[Dict]           # OpenTelemetry-style spans
    audit_log: List[Dict]

# ---------------------------------------------------------------------------
# 2. NODE IMPLEMENTATIONS
# ---------------------------------------------------------------------------

async def load_memory(state: AgentState, config: RunnableConfig) -> Dict:
    """Node 1: Load all three memory tiers.
    - Episodic: last N messages + summary from Redis/session store
    - Semantic: facts + preferences from hybrid vector+graph (Mem0/Zep/Cognee)
    - Procedural: relevant skill IDs from skill registry
    """
    user_id = state["user_id"]
    session_id = state["session_id"]

    # Episodic — fast, from Redis or SQLite
    episodic = await get_episodic_memory(user_id, session_id, max_tokens=2000)

    # Semantic — hybrid retrieve from pgvector + knowledge graph
    semantic = await get_semantic_memory(
        user_id=user_id,
        query=state["original_query"],
        top_k=10,
        min_confidence=0.7
    )

    # Procedural — match query to skill registry
    skills = await rank_relevant_skills(state["original_query"], top_k=3)

    return {
        "episodic_context": episodic,
        "semantic_memories": semantic,
        "procedural_skills": skills,
        "trace_spans": [{"node": "load_memory", "ts": now(), "duration_ms": 45}],
    }


async def classify_query(state: AgentState, config: RunnableConfig) -> Dict:
    """Node 2: Adaptive query routing.
    Uses a small, fast model to classify before paying for heavy retrieval.
    """
    query = state["original_query"]

    # Fast classifier (can be a small model, regex, or heuristic ensemble)
    classification = await classify_query_complexity(query)
    tier = classification["tier"]       # "1_cache" | "2_hybrid" | "3_multihop"
    score = classification["score"]

    return {
        "query_tier": tier,
        "query_complexity_score": score,
        "trace_spans": [{"node": "classify_query", "tier": tier, "score": score}],
    }


def route_by_tier(state: AgentState) -> str:
    """Conditional edge: where to go after classification.
    Tier 1 (cache/direct): skip retrieval entirely.
    Tier 2 (hybrid): single-hop vector + FTS.
    Tier 3 (multihop): full agentic RAG with planning.
    """
    if state["query_tier"] == "1_cache":
        return "execute"      # go straight to agent with memory only
    elif state["query_tier"] == "2_hybrid":
        return "retrieve"
    else:
        return "plan_multihop"


async def retrieve(state: AgentState, config: RunnableConfig) -> Dict:
    """Node 3a: Tier 2 hybrid retrieval (pgvector + FTS + reranker).
    """
    query = state["original_query"]

    # Parallel: dense (pgvector) + sparse (Postgres FTS)
    dense_results = await vector_search(query, top_k=20)
    sparse_results = await full_text_search(query, top_k=20)

    # Fuse and rerank (Voyage AI rerank-2.5 or Cohere v3.5)
    fused = reciprocal_rank_fusion(dense_results, sparse_results, k=60)
    reranked = await rerank(query, fused, top_k=5)

    confidence = compute_retrieval_confidence(reranked)

    return {
        "retrieved_chunks": reranked,
        "retrieval_confidence": confidence,
        "trace_spans": [{"node": "retrieve", "method": "hybrid", "chunks": len(reranked)}],
    }


async def plan_multihop(state: AgentState, config: RunnableConfig) -> Dict:
    """Node 3b: Tier 3 multi-hop planning.
    Decomposes complex query into sub-queries, each with its own retrieval.
    """
    plan = await llm_plan_decomposition(
        query=state["original_query"],
        memories=state["semantic_memories"],
        context=state["episodic_context"]
    )

    # Execute sub-queries sequentially (or in parallel if independent)
    all_chunks = []
    for sub_query in plan["sub_queries"]:
        chunks = await hybrid_retrieve(sub_query)
        all_chunks.extend(chunks)

    # Deduplicate and rerank across all hops
    reranked = await rerank(state["original_query"], dedupe(all_chunks), top_k=8)

    return {
        "retrieved_chunks": reranked,
        "retrieval_confidence": plan["confidence"],
        "trace_spans": [{"node": "plan_multihop", "sub_queries": len(plan["sub_queries"])}],
    }


async def execute(state: AgentState, config: RunnableConfig) -> Dict:
    """Node 4: Agent reasoning + tool calling.
    This is where the LLM thinks, calls Hermes tools, and builds an answer.
    NOT the Hermes loop — this is a single LangGraph node that calls tools.
    """
    # Build the prompt from all gathered context
    prompt = build_agent_prompt(
        query=state["original_query"],
        messages=state["messages"],
        episodic=state["episodic_context"],
        semantic=state["semantic_memories"],
        retrieved=state["retrieved_chunks"],
        skills=state["procedural_skills"],
        scratchpad=state.get("agent_scratchpad", ""),
        prior_feedback=extract_verification_feedback(state["verification_results"]),
    )

    # Call the LLM with tool schemas
    response = await llm_chat_with_tools(
        messages=prompt,
        tools=hermes_tool_schemas(),   # registered Hermes tools as LangChain tools
    )

    # Handle tool calls
    tool_calls = []
    if response.tool_calls:
        for tc in response.tool_calls:
            result = await dispatch_hermes_tool(
                tool_name=tc["name"],
                args=tc["args"],
                user_id=state["user_id"],
                org_id=state.get("org_id"),
            )
            tool_calls.append({
                "tool_name": tc["name"],
                "args": tc["args"],
                "result": result,
                "timestamp": now(),
            })

        # Re-invoke LLM with tool results (single turn — LangGraph handles looping)
        follow_up = await llm_chat_with_tools(
            messages=prompt + [response] + [tool_result_to_message(tc) for tc in tool_calls],
            tools=hermes_tool_schemas(),
        )
        answer = follow_up.content
    else:
        answer = response.content

    # Extract candidate memories from the interaction
    candidate_memories = await extract_memory_candidates(
        query=state["original_query"],
        answer=answer,
        tool_calls=tool_calls,
        semantic_memories=state["semantic_memories"],
    )

    return {
        "messages": [HumanMessage(content=state["original_query"]), AIMessage(content=answer)],
        "agent_scratchpad": state.get("agent_scratchpad", "") + f"\nTurn: {answer}",
        "tool_calls": state.get("tool_calls", []) + tool_calls,
        "memories_to_write": candidate_memories,
        "trace_spans": [{"node": "execute", "tool_calls": len(tool_calls)}],
    }


async def verify(state: AgentState, config: RunnableConfig) -> Dict:
    """Node 5: Multi-layer verification subgraph (compressed into one node here).
    Each layer can fail independently. Any failure triggers retry with feedback.
    """
    answer = state["messages"][-1].content if state["messages"] else ""
    retrieved = state["retrieved_chunks"]
    tool_calls = state.get("tool_calls", [])

    results: List[VerificationResult] = []

    # Layer 1: Tool contract check — did all tool calls respect pre/post conditions?
    tool_contract_ok = verify_tool_contracts(tool_calls)
    results.append({
        "layer": "tool_contract",
        "passed": tool_contract_ok,
        "score": 1.0 if tool_contract_ok else 0.0,
        "feedback": "" if tool_contract_ok else "Tool results violated post-conditions. Re-check with corrected parameters.",
        "evidence": [tc["tool_name"] for tc in tool_calls],
    })

    # Layer 2: Grounding check — is every claim in the answer supported by retrieved context?
    grounding = await verify_grounding(answer, retrieved)
    results.append({
        "layer": "grounding",
        "passed": grounding["passed"],
        "score": grounding["score"],
        "feedback": grounding.get("feedback", ""),
        "evidence": grounding["cited_sources"],
    })

    # Layer 3: Rule-based guardrails — deterministic checks (PII, policy, safety)
    guardrails = run_guardrails(answer, user_id=state["user_id"], org_id=state.get("org_id"))
    results.append({
        "layer": "guardrail",
        "passed": guardrails["passed"],
        "score": 1.0 if guardrails["passed"] else 0.0,
        "feedback": guardrails.get("blocked_reason", ""),
        "evidence": guardrails.get("matched_rules", []),
    })

    # Layer 4: Self-critique — LLM evaluates its own answer for coherence, completeness
    critique = await self_critique(answer, query=state["original_query"], context=retrieved)
    results.append({
        "layer": "self_critique",
        "passed": critique["passed"],
        "score": critique["score"],
        "feedback": critique.get("feedback", ""),
        "evidence": critique.get("reasoning", ""),
    })

    # Aggregate verdict
    all_pass = all(r["passed"] for r in results)
    any_fail = any(not r["passed"] for r in results)
    overall = "PASS" if all_pass else "FAIL" if any_fail else "UNCERTAIN"

    return {
        "verification_results": results,
        "overall_verdict": overall,
        "trace_spans": [{"node": "verify", "verdict": overall, "layers": len(results)}],
    }


def route_after_verify(state: AgentState) -> str:
    """Conditional edge: retry, pass, or fail closed."""
    if state["overall_verdict"] == "PASS":
        return "memory_writeback"

    if state["retry_count"] >= state.get("max_retries", 2):
        # Fail closed: return a safe fallback, log for human review
        return "fail_closed"

    # Increment retry and feed back specific correction instructions
    return "execute"


async def memory_writeback(state: AgentState, config: RunnableConfig) -> Dict:
    """Node 6: Conditional, structured memory persistence.
    Only writes entries that pass the salience filter.
    """
    to_write = []
    for mem in state.get("memories_to_write", []):
        # Salience filter: only durable, high-confidence, non-trivial entries
        if should_persist_memory(mem):
            # Enrich with metadata
            mem["user_id"] = state["user_id"]
            mem["session_id"] = state["session_id"]
            mem["created_at"] = now()
            to_write.append(mem)

    # Batch write to appropriate stores
    semantic_written = []
    episodic_written = []
    for mem in to_write:
        if mem["memory_type"] == "semantic":
            await write_semantic_memory(mem)
            semantic_written.append(mem)
        elif mem["memory_type"] == "episodic":
            await write_episodic_memory(mem)
            episodic_written.append(mem)
        # Procedural memory goes to skill store (rare, high bar)

    # Audit trail
    audit = {
        "event": "memory_writeback",
        "user_id": state["user_id"],
        "session_id": state["session_id"],
        "entries_written": len(to_write),
        "semantic": [m["content"][:100] for m in semantic_written],
        "timestamp": now(),
    }

    return {
        "final_answer": state["messages"][-1].content if state["messages"] else None,
        "answer_confidence": compute_answer_confidence(state),
        "answer_grounding": [c["source"] for c in state["retrieved_chunks"]],
        "audit_log": [audit],
        "trace_spans": [{"node": "memory_writeback", "written": len(to_write)}],
    }


async def fail_closed(state: AgentState, config: RunnableConfig) -> Dict:
    """Final node when verification fails after max retries.
    Returns a safe, honest response and flags for human review.
    """
    feedback = extract_verification_feedback(state["verification_results"])

    safe_answer = (
        f"I wasn't able to verify my answer with sufficient confidence. "
        f"Here's what I found: {state.get('agent_scratchpad', '')}\n\n"
        f"I can try again with more specific guidance, or a human can review."
    )

    return {
        "final_answer": safe_answer,
        "answer_confidence": 0.0,
        "error": f"Verification failed after {state['retry_count']} retries. Feedback: {feedback}",
        "audit_log": [{
            "event": "fail_closed",
            "user_id": state["user_id"],
            "reason": feedback,
            "timestamp": now(),
        }],
    }


# ---------------------------------------------------------------------------
# 3. GRAPH ASSEMBLY
# ---------------------------------------------------------------------------

def build_deep_path_graph(checkpointer: PostgresSaver) -> StateGraph:
    """Assemble the deep-path state graph."""
    workflow = StateGraph(AgentState)

    # Nodes
    workflow.add_node("load_memory", load_memory)
    workflow.add_node("classify_query", classify_query)
    workflow.add_node("retrieve", retrieve)
    workflow.add_node("plan_multihop", plan_multihop)
    workflow.add_node("execute", execute)
    workflow.add_node("verify", verify)
    workflow.add_node("memory_writeback", memory_writeback)
    workflow.add_node("fail_closed", fail_closed)

    # Edges
    workflow.set_entry_point("load_memory")
    workflow.add_edge("load_memory", "classify_query")

    # Conditional: route by query tier
    workflow.add_conditional_edges(
        "classify_query",
        route_by_tier,
        {
            "execute": "execute",
            "retrieve": "retrieve",
            "plan_multihop": "plan_multihop",
        },
    )

    workflow.add_edge("retrieve", "execute")
    workflow.add_edge("plan_multihop", "execute")
    workflow.add_edge("execute", "verify")

    # Conditional: retry loop or proceed
    workflow.add_conditional_edges(
        "verify",
        route_after_verify,
        {
            "execute": "execute",           # retry with feedback
            "memory_writeback": "memory_writeback",
            "fail_closed": "fail_closed",
        },
    )

    workflow.add_edge("memory_writeback", END)
    workflow.add_edge("fail_closed", END)

    # Compile with Postgres checkpointing for durable execution
    return workflow.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 4. HERMES INTEGRATION — how tools are called
# ---------------------------------------------------------------------------

async def dispatch_hermes_tool(tool_name: str, args: Dict, user_id: str, org_id: Optional[str]) -> Any:
    """
    Hermes tools are exposed as a JSON-RPC or MCP interface.
    LangGraph nodes call them directly — no need for Hermes's conversation loop.
    Options:
      A) MCP server: call via mcp client (native in Hermes)
      B) Internal API: POST to Hermes tool dispatcher
      C) Direct import: if running in same Python process
    """
    # Option A — MCP (recommended for decoupling)
    from hermes_tools import mcp_call_tool
    return await mcp_call_tool(server="hermes_tools", tool=tool_name, args=args)


def hermes_tool_schemas() -> List[Dict]:
    """Return Hermes tool schemas in OpenAI-compatible format for LLM tool calling."""
    # This can be dynamically discovered from Hermes's tool registry
    return [
        {"type": "function", "function": {"name": "terminal", "description": "...", "parameters": {}}},
        {"type": "function", "function": {"name": "read_file", "description": "...", "parameters": {}}},
        {"type": "function", "function": {"name": "web_search", "description": "...", "parameters": {}}},
        # ... all Hermes tools
    ]


# ---------------------------------------------------------------------------
# 5. FAST PATH — for simple queries that skip the graph entirely
# ---------------------------------------------------------------------------

async def fast_path_handler(query: str, user_id: str, session_id: str) -> Dict:
    """
    Simple queries (chit-chat, commands, known patterns) bypass LangGraph.
    Uses Hermes's native loop or a lightweight direct response.
    This keeps latency < 200ms for the common case.
    """
    # Check cache first
    cached = await check_response_cache(query, user_id)
    if cached:
        return {"answer": cached, "source": "cache", "latency_ms": 5}

    # Lightweight single-turn with episodic memory only
    context = await get_episodic_memory(user_id, session_id, max_tokens=1000)
    answer = await llm_single_turn(query, context=context)

    # Maybe write to episodic, skip semantic
    await write_episodic_memory({
        "memory_type": "episodic",
        "category": "interaction",
        "content": f"Q: {query}\nA: {answer}",
        "user_id": user_id,
        "session_id": session_id,
    })

    return {"answer": answer, "source": "fast_path", "latency_ms": 150}


# ---------------------------------------------------------------------------
# 6. ENTRY POINT — the router that decides fast vs deep
# ---------------------------------------------------------------------------

async def handle_user_message(
    query: str,
    user_id: str,
    session_id: str,
    org_id: Optional[str] = None,
    checkpointer: Optional[PostgresSaver] = None,
) -> Dict:
    """Main entry. Hermes gateway calls this for every incoming message."""

    # Pre-classify (cheap, fast)
    tier = await quick_classify(query)

    if tier == "1_cache" or tier == "2_hybrid_simple":
        # Fast path — no LangGraph overhead
        return await fast_path_handler(query, user_id, session_id)

    # Deep path — full LangGraph state machine
    graph = build_deep_path_graph(checkpointer)

    initial_state: AgentState = {
        "user_id": user_id,
        "session_id": session_id,
        "org_id": org_id,
        "original_query": query,
        "messages": [HumanMessage(content=query)],
        "episodic_context": "",
        "semantic_memories": [],
        "procedural_skills": [],
        "query_tier": tier,
        "query_complexity_score": 0.0,
        "retrieved_chunks": [],
        "retrieval_confidence": 0.0,
        "agent_scratchpad": "",
        "tool_calls": [],
        "pending_tool_calls": [],
        "verification_results": [],
        "overall_verdict": "UNCERTAIN",
        "retry_count": 0,
        "max_retries": 2,
        "memories_to_write": [],
        "final_answer": None,
        "answer_confidence": 0.0,
        "answer_grounding": [],
        "latency_ms": 0,
        "error": None,
        "trace_spans": [],
        "audit_log": [],
    }

    # Run the graph — checkpointing happens automatically
    result = await graph.ainvoke(
        initial_state,
        config={"configurable": {"thread_id": session_id}},
    )

    return {
        "answer": result["final_answer"],
        "confidence": result["answer_confidence"],
        "grounding": result["answer_grounding"],
        "latency_ms": sum(s.get("duration_ms", 0) for s in result["trace_spans"]),
        "audit": result["audit_log"],
    }


# ---------------------------------------------------------------------------
# Helpers (stub implementations)
# ---------------------------------------------------------------------------

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

async def get_episodic_memory(user_id: str, session_id: str, max_tokens: int) -> str: ...
async def get_semantic_memory(user_id: str, query: str, top_k: int, min_confidence: float) -> List[Dict]: ...
async def rank_relevant_skills(query: str, top_k: int) -> List[str]: ...
async def classify_query_complexity(query: str) -> Dict: ...
async def vector_search(query: str, top_k: int) -> List[Dict]: ...
async def full_text_search(query: str, top_k: int) -> List[Dict]: ...
async def rerank(query: str, chunks: List[Dict], top_k: int) -> List[Dict]: ...
def reciprocal_rank_fusion(dense, sparse, k: int) -> List[Dict]: ...
def compute_retrieval_confidence(chunks: List[Dict]) -> float: ...
def build_memory_prompt(episodic, semantic, skills) -> str: ...
def build_agent_prompt(query, messages, episodic, semantic, retrieved, skills, scratchpad, prior_feedback) -> List[BaseMessage]: ...
async def llm_chat_with_tools(messages, tools): ...
async def llm_plan_decomposition(query, memories, context) -> Dict: ...
async def hybrid_retrieve(query) -> List[Dict]: ...
def dedupe(chunks: List[Dict]) -> List[Dict]: ...
async def extract_memory_candidates(query, answer, tool_calls, semantic_memories) -> List[MemoryEntry]: ...
def extract_verification_feedback(results: List[VerificationResult]) -> str: ...
async def verify_grounding(answer: str, chunks: List[Dict]) -> Dict: ...
def verify_tool_contracts(tool_calls: List[Dict]) -> bool: ...
def run_guardrails(answer: str, user_id: str, org_id: Optional[str]) -> Dict: ...
async def self_critique(answer: str, query: str, context: List[Dict]) -> Dict: ...
def should_persist_memory(mem: MemoryEntry) -> bool: ...
async def write_semantic_memory(mem: MemoryEntry): ...
async def write_episodic_memory(mem: MemoryEntry): ...
def compute_answer_confidence(state: AgentState) -> float: ...
async def check_response_cache(query: str, user_id: str) -> Optional[str]: ...
async def llm_single_turn(query: str, context: str) -> str: ...
async def quick_classify(query: str) -> str: ...
def tool_result_to_message(tc: Dict) -> ToolMessage: ...
