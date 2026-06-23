"""Phase 1-4 LangGraph workflow.

Flow:
resolve_identity -> retrieve_memory -> call_model -> salience_gate -> write_memory
"""

from __future__ import annotations

import os
from typing import Annotated, Any, TypedDict
import operator
from time import perf_counter

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

try:
    from .checkpoints import CheckpointerHandle, build_checkpointer
    from .durable_memory import DurableMemoryStore, MemoryInput
    from .hermes_native_memory import HermesNativeMemoryStore
    from .memory import build_memory_write, retrieve_fake_memory, should_propose_memory_write
    from .tracing import TraceEventInput, TraceStore, new_trace_id
except ImportError:  # Allows `python src/test.py` without installing as a package.
    from checkpoints import CheckpointerHandle, build_checkpointer
    from durable_memory import DurableMemoryStore, MemoryInput
    from hermes_native_memory import HermesNativeMemoryStore
    from memory import build_memory_write, retrieve_fake_memory, should_propose_memory_write
    from tracing import TraceEventInput, TraceStore, new_trace_id


SYSTEM_PROMPT = (
    "You are Mo Memory, a permission-safe assistant.\n"
    "Use retrieved context if relevant.\n"
    "Never claim memory unless it appears in retrieved context."
)


class MoMemoryState(TypedDict, total=False):
    user_text: str
    actor: dict[str, str]
    session_id: str
    retrieved_context: str
    trace_id: str
    messages: Annotated[list[BaseMessage], operator.add]
    assistant_response: str
    memory_writes: list[dict[str, Any]]
    written_memory_ids: list[str]


def resolve_identity(state: MoMemoryState) -> dict[str, Any]:
    trace_id = state.get("trace_id") or new_trace_id("mo-memory")
    if state.get("actor"):
        return {"actor": state["actor"], "trace_id": trace_id}

    raise ValueError("actor is required for runtime graph invocation")


def retrieve_memory(state: MoMemoryState) -> dict[str, Any]:
    """Retrieve memories from Hermes native + Postgres in parallel.

    Optimized: Hermes SQLite FTS5 and Postgres hybrid search run simultaneously
    via ThreadPoolExecutor. Local embeddings avoid API latency where configured,
    and repeated-query caching stays inside the retrieval backend.
    """
    import concurrent.futures

    actor = state["actor"]
    session_id = state.get("session_id", "phase4-graph-session")
    trace_id = state.get("trace_id") or new_trace_id("mo-memory")
    trace_store = TraceStore()
    query = state["user_text"]

    native_results: list[dict] = []
    postgres_results: list[dict] = []
    native_latency = 0
    postgres_latency = 0
    postgres_error: str | None = None

    use_postgres = os.environ.get("MEMORY_BACKEND", "fake").lower() in (
        "postgres", "durable"
    )

    def _search_hermes():
        nonlocal native_latency
        start = perf_counter()
        try:
            res = HermesNativeMemoryStore().search(
                query,
                actor_id=actor["actor_id"],
                org_id=actor["org_id"],
                limit=5,
            )
            native_latency = int((perf_counter() - start) * 1000)
            return res
        except Exception:
            native_latency = int((perf_counter() - start) * 1000)
            return []

    def _search_postgres():
        nonlocal postgres_latency, postgres_error
        if not use_postgres:
            return []
        start = perf_counter()
        try:
            from src.hybrid_retrieval import HybridMemoryStore
            res = HybridMemoryStore().hybrid_search(
                query,
                user_id=actor["actor_id"],
                org_id=actor["org_id"],
                session_id=session_id,
                limit=5,
                trace_id=trace_id,
            )
            postgres_latency = int((perf_counter() - start) * 1000)
            return res
        except Exception as exc:
            postgres_error = str(exc)[:500]
            try:
                res = DurableMemoryStore().search_memory_basic(
                    query,
                    user_id=actor["actor_id"],
                    org_id=actor["org_id"],
                    session_id=session_id,
                    limit=5,
                    trace_id=trace_id,
                )
            except Exception as fallback_exc:
                postgres_error = str(fallback_exc)[:500]
                res = []
            postgres_latency = int((perf_counter() - start) * 1000)
            return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        hermes_future = executor.submit(_search_hermes)
        postgres_future = executor.submit(_search_postgres)
        native_results = hermes_future.result()
        postgres_results = postgres_future.result()

    trace_store.log_event(TraceEventInput(
        trace_id=trace_id,
        step_name="agent_memory_retrieval",
        status="ok",
        source="hermes_native_memory",
        user_id=actor["actor_id"],
        org_id=actor.get("org_id"),
        session_id=session_id,
        latency_ms=native_latency,
        results_count=len(native_results),
    ))

    if use_postgres:
        trace_store.log_event(TraceEventInput(
            trace_id=trace_id,
            step_name="postgres_retrieval",
            status="fallback" if postgres_error else "ok",
            source="direct_lookup" if postgres_error else "hybrid",
            user_id=actor["actor_id"],
            org_id=actor.get("org_id"),
            session_id=session_id,
            latency_ms=postgres_latency,
            results_count=len(postgres_results),
            error_message=postgres_error,
        ))

    if not use_postgres:
        if native_results:
            context = "\n".join(f"- [Hermes native] {row['content']}" for row in native_results)
            return {"retrieved_context": context, "trace_id": trace_id}
        return {"retrieved_context": retrieve_fake_memory(), "trace_id": trace_id}

    if not postgres_results and not native_results:
        return {"retrieved_context": retrieve_fake_memory(), "trace_id": trace_id}

    # Phase 6: graph expansion — add related memories via graph edges
    try:
        from src.graph_memory import GraphMemory
        gm = GraphMemory()
        seed_ids = [str(r["id"]) for r in postgres_results]
        related = gm.expand_graph(
            seed_ids,
            depth=1,
            limit=3,
            user_id=actor["actor_id"],
            org_id=actor.get("org_id"),
        )
        postgres_results = postgres_results + related
    except Exception:
        pass  # Graph expansion is best-effort

    context_parts = [f"- [Hermes native] {row['content']}" for row in native_results]
    context_parts.extend(f"- [Postgres] {row['content']}" for row in postgres_results[:8])
    context = "\n".join(context_parts)
    return {"retrieved_context": context, "trace_id": trace_id}


def retrieve_fake_memory_node(state: MoMemoryState) -> dict[str, Any]:
    return {"retrieved_context": retrieve_fake_memory()}


def call_model(state: MoMemoryState) -> dict[str, Any]:
    """Call the LLM (DeepSeek) with retrieved context."""
    user_text = state["user_text"]
    retrieved_context = state["retrieved_context"]
    actor = state["actor"]
    trace_id = state.get("trace_id") or new_trace_id("mo-memory")
    session_id = state.get("session_id", "phase4-graph-session")
    trace_store = TraceStore()

    # Try real LLM first
    model_start = perf_counter()
    model_status = "ok"
    model_error: str | None = None
    try:
        from src.llm_client import LLMClient
        client = LLMClient()
        response = client.chat_with_memory(user_text, retrieved_context=retrieved_context)
    except Exception as exc:
        model_status = "fallback"
        model_error = str(exc)[:500]
        # Fallback for tests / when API key not configured
        if retrieved_context == "No durable memory retrieved yet.":
            response = (
                "I can handle that locally for this run, but I do not have "
                "durable memory connected yet."
            )
        else:
            response = f"Using retrieved context: {retrieved_context}"
    trace_store.log_event(
        TraceEventInput(
            trace_id=trace_id,
            step_name="model_answer",
            status=model_status,
            source=os.environ.get("LLM_MODEL", "local_fallback"),
            user_id=actor["actor_id"],
            org_id=actor.get("org_id"),
            session_id=session_id,
            latency_ms=int((perf_counter() - model_start) * 1000),
            error_message=model_error,
            details={
                "context_used": retrieved_context != retrieve_fake_memory(),
                "empty_answer": not bool(response.strip()),
            },
        )
    )

    messages: list[BaseMessage] = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=f"Retrieved context: {retrieved_context}\n\nUser: {user_text}"),
        AIMessage(content=response),
    ]

    return {
        "messages": messages,
        "assistant_response": response,
        "trace_id": trace_id,
    }


def salience_gate(state: MoMemoryState) -> dict[str, Any]:
    if not should_propose_memory_write(state["user_text"]):
        return {"memory_writes": []}

    return {
        "memory_writes": [
            build_memory_write(state["actor"], state["user_text"]),
        ]
    }


def write_memory(state: MoMemoryState) -> dict[str, Any]:
    actor = state["actor"]
    native_store = HermesNativeMemoryStore()
    written_ids: list[str] = []
    for proposed in state.get("memory_writes", []):
        native_store.write(
            actor_id=actor["actor_id"],
            org_id=actor["org_id"],
            role=actor["role"],
            content=proposed["text"],
            memory_type=proposed.get("memory_type", "semantic"),
            category=proposed.get("category", "fact"),
            visibility="owner_only",
            confidence=float(proposed.get("confidence", 0.8)),
            metadata={"source": "salience_gate"},
        )

    if os.environ.get("MEMORY_BACKEND", "fake").lower() not in ("postgres", "durable"):
        return {"written_memory_ids": []}

    store = DurableMemoryStore()
    for proposed in state.get("memory_writes", []):
        memory_id = store.insert_memory(
            MemoryInput(
                content=proposed["text"],
                user_id=actor["actor_id"],
                session_id=state.get("session_id", "phase4-graph-session"),
                org_id=actor["org_id"],
                role=actor["role"],
                memory_type=proposed.get("memory_type", "semantic"),
                category=proposed.get("category", "fact"),
                visibility="owner_only",
                confidence=float(proposed.get("confidence", 0.8)),
                source="user_utterance",
                metadata={"source": proposed.get("source", "salience_gate")},
            )
        )
        written_ids.append(memory_id)

        # Phase 6: extract entities and link via graph edges
        try:
            from src.graph_memory import GraphMemory
            gm = GraphMemory()
            gm.extract_and_link(memory_id, proposed["text"])
        except Exception:
            pass  # Graph linking is best-effort

    return {"written_memory_ids": written_ids}


def build_graph(checkpointer: Any | None = None):
    workflow = StateGraph(MoMemoryState)
    workflow.add_node("resolve_identity", resolve_identity)
    workflow.add_node("retrieve_memory", retrieve_memory)
    workflow.add_node("call_model", call_model)
    workflow.add_node("salience_gate", salience_gate)
    workflow.add_node("write_memory", write_memory)

    workflow.add_edge(START, "resolve_identity")
    workflow.add_edge("resolve_identity", "retrieve_memory")
    workflow.add_edge("retrieve_memory", "call_model")
    workflow.add_edge("call_model", "salience_gate")
    workflow.add_edge("salience_gate", "write_memory")
    workflow.add_edge("write_memory", END)

    if checkpointer is None:
        checkpointer = build_checkpointer("memory").checkpointer

    return workflow.compile(checkpointer=checkpointer)


def build_graph_with_checkpointer(mode: str | None = None, *, setup: bool = False) -> tuple[Any, CheckpointerHandle]:
    handle = build_checkpointer(mode, setup=setup)
    return build_graph(handle.checkpointer), handle
