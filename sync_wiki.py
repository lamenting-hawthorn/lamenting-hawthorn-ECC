#!/usr/bin/env python3
"""
sync_wiki.py — Phase 2: Sync Obsidian vault to typed_memory.
=============================================================

Usage:
    # Full sync
    python sync_wiki.py --vault ~/knowledge-base

    # Dry run (don't write, just show what would change)
    python sync_wiki.py --vault ~/knowledge-base --dry-run

    # Sync a single file
    python sync_wiki.py --vault ~/knowledge-base --file architecture/event-pipeline.md

    # Continuous watch mode (requires watchdog)
    python sync_wiki.py --vault ~/knowledge-base --watch

Config:
    Set OWNER_USER_ID and DATABASE_URL in environment or pass --user / --db-url.

Pipeline:
    .md file → parse frontmatter → chunk by ## headings → embed each chunk
    → write to typed_memory with visibility='owner_only'
    → create graph edges for wiki [[links]]
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import asyncpg
except ImportError:
    print("Install asyncpg: pip install asyncpg")
    sys.exit(1)

try:
    import httpx
except ImportError:
    print("Install httpx: pip install httpx")
    sys.exit(1)

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

OWNER_USER_ID = os.environ.get("WIKI_OWNER_USER_ID", "u_owner")
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql:///agent_memory",
)
EMBEDDING_API_URL = os.environ.get(
    "EMBEDDING_API_URL",
    "https://api.openai.com/v1/embeddings",
)
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "1536"))
VAULT_PATH = os.path.expanduser(os.environ.get("WIKI_VAULT_PATH", "~/knowledge-base"))


def encode_pgvector(embedding: Optional[List[float]]) -> Optional[str]:
    """asyncpg needs pgvector values encoded unless a pgvector codec is registered."""
    if embedding is None:
        return None
    return "[" + ",".join(str(float(x)) for x in embedding) + "]"


# ---------------------------------------------------------------------------
# SYNC LOGIC
# ---------------------------------------------------------------------------

class WikiSyncer:
    """Syncs an Obsidian vault to the typed_memory table."""

    def __init__(
        self,
        vault_path: str,
        user_id: str = OWNER_USER_ID,
        db_url: str = DATABASE_URL,
        dry_run: bool = False,
    ):
        self.vault_path = Path(vault_path).expanduser().resolve()
        self.user_id = user_id
        self.db_url = db_url
        self.dry_run = dry_run
        self._db = None
        self._http = httpx.AsyncClient(timeout=30)

        self.stats = {
            "files_scanned": 0,
            "chunks_found": 0,
            "chunks_unchanged": 0,
            "chunks_written": 0,
            "edges_created": 0,
            "errors": 0,
        }

    async def __aenter__(self):
        self._db = await asyncpg.connect(self.db_url)
        await self._db.execute("SELECT set_config('app.current_role', 'service', false)")
        return self

    async def __aexit__(self, *args):
        if self._db:
            await self._db.close()
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Markdown parsing
    # ------------------------------------------------------------------

    def parse_frontmatter(self, content: str) -> Tuple[Dict, str]:
        """Extract YAML frontmatter from markdown content.

        Returns (frontmatter_dict, body_without_frontmatter).
        """
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
        if not match:
            return {}, content

        raw_yaml = match.group(1)
        body = match.group(2)

        # Minimal YAML parsing (handles tags, aliases, dates)
        fm: Dict = {}
        for line in raw_yaml.split("\n"):
            line = line.strip()
            if not line:
                continue
            # tags: [tag1, tag2] or tags: tag1
            if line.startswith("tags:"):
                tags_val = line.split(":", 1)[1].strip()
                if tags_val.startswith("[") and tags_val.endswith("]"):
                    fm["tags"] = [t.strip().strip('"').strip("'")
                                  for t in tags_val[1:-1].split(",")]
                else:
                    fm["tags"] = [t.strip() for t in tags_val.split(",") if t.strip()]
            # aliases: [alias1, alias2]
            elif line.startswith("aliases:"):
                aliases_val = line.split(":", 1)[1].strip()
                if aliases_val.startswith("["):
                    fm["aliases"] = [a.strip().strip('"').strip("'")
                                     for a in aliases_val[1:-1].split(",")]
                else:
                    fm["aliases"] = [a.strip() for a in aliases_val.split(",") if a.strip()]
            # Simple key: value pairs
            elif ":" in line:
                key, val = line.split(":", 1)
                fm[key.strip()] = val.strip().strip('"').strip("'")

        return fm, body

    def chunk_markdown(self, content: str, file_path: str) -> List[Dict]:
        """Split markdown into chunks by ## headings.

        Each chunk gets:
          - heading: the section heading text
          - content: the section body
          - links: list of [[wiki links]] found in the section
          - level: heading level (1, 2, 3)
        """
        # Extract frontmatter
        fm, body = self.parse_frontmatter(content)

        chunks = []
        lines = body.split("\n")
        current_heading = "Overview"
        current_level = 1
        current_lines: List[str] = []
        current_links: List[str] = []

        def flush_section():
            text = "\n".join(current_lines).strip()
            if text:
                chunks.append({
                    "heading": current_heading,
                    "level": current_level,
                    "content": text,
                    "links": current_links,
                    "frontmatter": fm,
                })

        for line in lines:
            heading_match = re.match(r"^(#{1,3})\s+(.+)$", line)
            if heading_match:
                flush_section()
                current_level = len(heading_match.group(1))
                current_heading = heading_match.group(2).strip()
                current_lines = []
                current_links = []
            else:
                current_lines.append(line)
                # Extract wiki links [[target]] or [[target|display]]
                for link_match in re.finditer(r"\[\[([^\]]+)\]\]", line):
                    target = link_match.group(1).split("|")[0].split("#")[0].strip()
                    if target and target not in current_links:
                        current_links.append(target)

        flush_section()

        return chunks

    def compute_checksum(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    async def embed(self, text: str) -> Optional[List[float]]:
        """Generate embedding via API. Returns None on failure."""
        if not EMBEDDING_API_KEY and "openai" in EMBEDDING_API_URL.lower():
            print("  [WARN] No EMBEDDING_API_KEY set. Skipping embedding.")
            return None

        try:
            resp = await self._http.post(
                EMBEDDING_API_URL,
                headers={
                    "Authorization": f"Bearer {EMBEDDING_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": EMBEDDING_MODEL,
                    "input": text[:8000],  # truncate to avoid token limits
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            # Handle OpenAI format
            if "data" in data and len(data["data"]) > 0:
                return data["data"][0]["embedding"]
            # Handle other providers
            if isinstance(data, list) and len(data) > 0:
                return data[0] if isinstance(data[0], list) else data[0].get("embedding")

            print(f"  [ERROR] Unexpected embedding response: {data.keys() if isinstance(data, dict) else type(data)}")
            return None

        except httpx.HTTPStatusError as e:
            print(f"  [ERROR] Embedding API {e.response.status_code}: {e.response.text[:200]}")
            return None
        except Exception as e:
            print(f"  [ERROR] Embedding request failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Database operations
    # ------------------------------------------------------------------

    async def chunk_exists(self, checksum: str, file_path: str, heading: str) -> bool:
        """Check if a chunk with this checksum already exists (unchanged)."""
        row = await self._db.fetchval(
            """
            SELECT id FROM memory.typed_memory
            WHERE user_id = $1
              AND category = 'knowledge_base'
              AND metadata->>'source_file' = $2
              AND metadata->>'heading' = $3
              AND metadata->>'checksum' = $4
            LIMIT 1
            """,
            self.user_id, file_path, heading, checksum,
        )
        return row is not None

    async def upsert_chunk(
        self,
        chunk: Dict,
        file_path: str,
        checksum: str,
        embedding: Optional[List[float]],
    ) -> Optional[str]:
        """Write or update a chunk in typed_memory. Returns memory id."""
        frontmatter = chunk.get("frontmatter", {})
        tags = frontmatter.get("tags", [])
        aliases = frontmatter.get("aliases", [])

        metadata = json.dumps({
            "source_file": file_path,
            "heading": chunk["heading"],
            "level": chunk["level"],
            "tags": tags,
            "aliases": aliases,
            "links": chunk["links"],
            "checksum": checksum,
            "last_synced": datetime.now(timezone.utc).isoformat(),
        })

        content_text = chunk["content"]
        summary = chunk["heading"]

        if self.dry_run:
            print(f"  [DRY-RUN] Would write: {file_path} → {summary}")
            return None

        try:
            # Try to find existing record by source_file + heading
            existing_id = await self._db.fetchval(
                """
                SELECT id FROM memory.typed_memory
                WHERE user_id = $1
                  AND category = 'knowledge_base'
                  AND metadata->>'source_file' = $2
                  AND metadata->>'heading' = $3
                LIMIT 1
                """,
                self.user_id, file_path, summary,
            )

            if existing_id:
                # Update
                await self._db.execute(
                    """
                    UPDATE memory.typed_memory
                    SET content = $1,
                        summary = $2,
                        embedding = $3,
                        metadata = $4,
                        updated_at = now()
                    WHERE id = $5
                    """,
                    content_text, summary, encode_pgvector(embedding), metadata, existing_id,
                )
                return existing_id
            else:
                # Insert
                new_id = await self._db.fetchval(
                    """
                    INSERT INTO memory.typed_memory
                        (memory_type, category, content, summary, user_id, session_id,
                         visibility, confidence, source, embedding, metadata, expires_at)
                    VALUES ('semantic', 'knowledge_base', $1, $2, $3, 'wiki_sync',
                            'owner_only', 0.9, 'knowledge_base_import', $4, $5, NULL)
                    RETURNING id
                    """,
                    content_text, summary, self.user_id, encode_pgvector(embedding), metadata,
                )
                return new_id

        except Exception as e:
            print(f"  [ERROR] DB write failed for {file_path}: {e}")
            self.stats["errors"] += 1
            return None

    async def create_graph_edges(self, memory_id: str, links: List[str], file_path: str):
        """Create 'references' edges from this memory to linked wiki pages."""
        for link_target in links:
            # Find the target memory by heading match
            target_id = await self._db.fetchval(
                """
                SELECT id FROM memory.typed_memory
                WHERE user_id = $1
                  AND category = 'knowledge_base'
                  AND (
                      summary ILIKE $2
                      OR metadata->>'source_file' ILIKE $3
                  )
                LIMIT 1
                """,
                self.user_id, f"%{link_target}%", f"%{link_target}%",
            )

            if target_id:
                try:
                    await self._db.execute(
                        """
                        INSERT INTO memory.memory_edges
                            (source_id, target_id, edge_type, weight, created_by, metadata)
                        VALUES ($1, $2, 'references', 0.8, 'system', $3)
                        ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
                        """,
                        memory_id, target_id,
                        json.dumps({"source_file": file_path}),
                    )
                    self.stats["edges_created"] += 1
                except Exception as e:
                    print(f"  [WARN] Edge creation failed ({link_target}): {e}")

    # ------------------------------------------------------------------
    # File sync
    # ------------------------------------------------------------------

    async def sync_file(self, file_path: str) -> bool:
        """Sync a single markdown file. Returns True if any changes were made."""
        full_path = self.vault_path / file_path
        if not full_path.exists():
            print(f"  [ERROR] File not found: {full_path}")
            self.stats["errors"] += 1
            return False

        with open(full_path, "r") as f:
            content = f.read()

        rel_path = str(full_path.relative_to(self.vault_path))
        checksum = self.compute_checksum(content)
        chunks = self.chunk_markdown(content, rel_path)

        self.stats["files_scanned"] += 1
        self.stats["chunks_found"] += len(chunks)

        any_changed = False

        for chunk in chunks:
            # Check if unchanged
            if await self.chunk_exists(checksum, rel_path, chunk["heading"]):
                self.stats["chunks_unchanged"] += 1
                continue

            print(f"  {rel_path} → {chunk['heading']}")

            # Generate embedding
            embedding = await self.embed(chunk["content"])

            # Write to database
            memory_id = await self.upsert_chunk(chunk, rel_path, checksum, embedding)
            if memory_id:
                self.stats["chunks_written"] += 1
                any_changed = True

                # Create graph edges for [[links]]
                if chunk["links"]:
                    await self.create_graph_edges(memory_id, chunk["links"], rel_path)

        return any_changed

    async def sync_all(self, file_filter: Optional[str] = None):
        """Sync all markdown files in the vault."""
        pattern = "*.md"
        md_files = sorted(self.vault_path.rglob(pattern))

        # Filter out hidden dirs and common exclusions
        md_files = [
            f for f in md_files
            if not any(part.startswith(".") for part in f.relative_to(self.vault_path).parts)
            and f.name != "_index.md"
            and not f.name.startswith("_")
        ]

        if file_filter:
            md_files = [f for f in md_files if file_filter in str(f)]

        print(f"Scanning {len(md_files)} markdown files in {self.vault_path}...")

        for md_file in md_files:
            rel = str(md_file.relative_to(self.vault_path))
            await self.sync_file(rel)

    def print_stats(self):
        """Print sync statistics."""
        print()
        print("=" * 60)
        print("Sync Complete")
        print("=" * 60)
        print(f"  Files scanned:    {self.stats['files_scanned']}")
        print(f"  Chunks found:     {self.stats['chunks_found']}")
        print(f"  Unchanged (skip): {self.stats['chunks_unchanged']}")
        print(f"  Written:          {self.stats['chunks_written']}")
        print(f"  Edges created:    {self.stats['edges_created']}")
        print(f"  Errors:           {self.stats['errors']}")
        print("=" * 60)


# ---------------------------------------------------------------------------
# WATCH MODE (optional)
# ---------------------------------------------------------------------------

async def watch_mode(vault_path: str, user_id: str, db_url: str):
    """Watch the vault for changes and auto-sync."""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("Install watchdog for watch mode: pip install watchdog")
        sys.exit(1)

    class WikiFileHandler(FileSystemEventHandler):
        def __init__(self, vault, user_id, db_url):
            self.vault = vault
            self.user_id = user_id
            self.db_url = db_url

        async def _sync_if_md(self, path):
            if not path.endswith(".md"):
                return
            # Debounce: wait 1s for file to finish writing
            await asyncio.sleep(1)
            async with WikiSyncer(self.vault, self.user_id, self.db_url) as syncer:
                rel = str(Path(path).relative_to(Path(self.vault).resolve()))
                await syncer.sync_file(rel)
                if syncer.stats["chunks_written"] > 0:
                    print(f"  Auto-synced: {rel}")

        def on_modified(self, event):
            if not event.is_directory and event.src_path.endswith(".md"):
                asyncio.create_task(self._sync_if_md(event.src_path))

    event_handler = WikiFileHandler(vault_path, user_id, db_url)
    observer = Observer()
    observer.schedule(event_handler, path=vault_path, recursive=True)
    observer.start()

    print(f"Watching {vault_path} for changes... (Ctrl+C to stop)")
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(
        description="Sync Obsidian vault to typed_memory (Phase 2)",
    )
    parser.add_argument("--vault", default=VAULT_PATH,
                        help=f"Path to Obsidian vault (default: {VAULT_PATH})")
    parser.add_argument("--user", default=OWNER_USER_ID,
                        help=f"Owner user ID (default: {OWNER_USER_ID})")
    parser.add_argument("--db-url", default=DATABASE_URL,
                        help="Postgres connection string")
    parser.add_argument("--file", default=None,
                        help="Sync only this specific file (relative path)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't write to database, just show what would change")
    parser.add_argument("--watch", action="store_true",
                        help="Watch mode: auto-sync on file changes")
    args = parser.parse_args()

    if args.watch:
        await watch_mode(args.vault, args.user, args.db_url)
        return

    async with WikiSyncer(args.vault, args.user, args.db_url, args.dry_run) as syncer:
        if args.file:
            await syncer.sync_file(args.file)
        else:
            await syncer.sync_all()
        syncer.print_stats()


if __name__ == "__main__":
    asyncio.run(main())
