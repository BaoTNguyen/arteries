"""Documents into chunks, chunks into claims, claims into the graph.

`art docs` mines a repo's Markdown for sentences a human then approves. That is
a good bootstrap and a poor way to read a design document: it flattens structure
into disconnected statements, so a plan that says step 3 touches compile.py and
depends on step 2 arrives as three unrelated facts.

This path keeps the structure. A document is stored, split into chunks, and each
chunk goes through the same compile call the conversation path uses -- so it
produces entities, relations, and decisions, and every claim carries a
`derived_from` edge back to the chunk and document it came from.

    art ingest plan.md --kind plan
    art ingest docs/ --include '*.md'
    plexus plan | art ingest - --kind plan --name plexus:goal/x

Re-ingesting an unchanged file does nothing; the digest is the guard.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from dataclasses import dataclass
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

from arteries import graph, runlog, scope
from arteries.config import DB_CONFIG

# Chunks are paragraph-grouped rather than fixed-width. A design document's unit
# of meaning is the paragraph or the list under a heading, and splitting mid-
# sentence to hit a token count is how you get claims with no subject.
TARGET_CHARS = 1400
MAX_CHARS = 2600

# A plan is read for its parts, not its gist. Measured: a four-section plan fit
# in one chunk under the document sizes and came back as a single claim -- the
# dependency between steps and the rationale for the decision were summarised
# away. Smaller chunks make each step its own unit, so each survives as its own
# claim with its own entities.
PLAN_TARGET_CHARS = 400
PLAN_MAX_CHARS = 900


@dataclass
class Chunk:
    ord: int
    text: str
    line_start: int
    line_end: int


def split(text: str, kind: str = "document") -> list[Chunk]:
    """Group paragraphs into chunks, breaking at headings and never mid-block."""
    target = PLAN_TARGET_CHARS if kind == "plan" else TARGET_CHARS
    cap = PLAN_MAX_CHARS if kind == "plan" else MAX_CHARS
    chunks: list[Chunk] = []
    buf: list[str] = []
    start = 1
    n = 0

    def flush(end: int) -> None:
        nonlocal buf, start, n
        body = "\n".join(buf).strip()
        if body:
            chunks.append(Chunk(n, body, start, end))
            n += 1
        buf = []

    lines = text.splitlines()
    for i, raw in enumerate(lines, start=1):
        # A heading starts a new chunk: it is the document telling you where the
        # topic changes, which is better than any length heuristic.
        if raw.startswith("#") and buf and len("\n".join(buf)) > target // 2:
            flush(i - 1)
            start = i
        if not buf:
            start = i
        buf.append(raw)
        if len("\n".join(buf)) >= cap:
            flush(i)
    flush(len(lines))
    return chunks


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def ingest_file(path: Path, project: str, kind: str = "document") -> dict:
    """Store one document from disk and compile its chunks into claims."""
    return await ingest_text(
        path.read_text(encoding="utf-8", errors="replace"),
        name=str(path), project=project, kind=kind,
    )


async def ingest_text(text: str, *, name: str, project: str,
                      kind: str = "document") -> dict:
    """Same pipeline, for content that was never a file.

    Plexus plans and heart's episode notes are generated in memory, not written
    to disk, and requiring a tempfile just to get provenance would be silly.
    `name` is the identity the document is stored under -- reuse it and the
    digest guard works exactly as it does for a path.
    """
    from arteries import compile as compiler
    from arteries.embed import embed_texts_sync

    digest = _digest(text)
    scope_id = scope.scope_for(project) or project
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, digest FROM arteries.documents "
                        "WHERE project_id = %s AND path = %s", (project, name))
            row = cur.fetchone()
            if row and row[1] == digest:
                return {"path": name, "status": "unchanged"}

            cur.execute(
                """
                INSERT INTO arteries.documents (project_id, path, digest, kind)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (project_id, path) DO UPDATE
                    SET digest = EXCLUDED.digest, ingested_at = now()
                RETURNING id
                """,
                (project, name, digest, kind),
            )
            doc_id = str(cur.fetchone()[0])
            # A changed document replaces its chunks; stale passages would keep
            # answering queries about text that no longer exists. Retire the
            # edges with them -- memory_edges can carry no foreign key, so
            # nothing else will, and a claim citing a deleted chunk is a
            # provenance trail that goes nowhere.
            cur.execute(
                """
                UPDATE arteries.memory_edges SET valid_until = now()
                WHERE valid_until IS NULL
                  AND ((src_kind = 'chunk' AND src_id IN
                        (SELECT id::text FROM arteries.chunks WHERE document_id = %s))
                    OR (dst_kind = 'chunk' AND dst_id IN
                        (SELECT id::text FROM arteries.chunks WHERE document_id = %s)))
                """,
                (doc_id, doc_id),
            )
            cur.execute("DELETE FROM arteries.chunks WHERE document_id = %s", (doc_id,))

            chunks = split(text, kind)
            vectors = embed_texts_sync([c.text for c in chunks])
            chunk_ids = []
            for c, vec in zip(chunks, vectors):
                cur.execute(
                    """
                    INSERT INTO arteries.chunks
                        (document_id, project_id, ord, text, line_start, line_end, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
                    RETURNING id
                    """,
                    (doc_id, project, c.ord, c.text, c.line_start, c.line_end, vec),
                )
                cid = str(cur.fetchone()[0])
                chunk_ids.append(cid)
                graph.add_edge(cur, project, "chunk", cid, "is_part_of", "document", doc_id)
            conn.commit()

        claims = 0
        for c, cid in zip(chunks, chunk_ids):
            # One compile call per chunk. Unlike conversation turns, a document
            # chunk is self-contained -- which is exactly why cognee can chunk
            # per passage and why batching turns was the right call there and
            # the wrong one here.
            record = [{"id": cid, "fact": c.text, "domains": [], "source": kind,
                       "parent_agent_id": None}]
            ctx = compiler._load_persistent_context(conn, record, project)
            result = await compiler._llm_compile(record, ctx)
            if compiler.validate_response(result):
                runlog.log_event("docs.chunk.invalid", "arteries",
                                 {"path": name, "ord": c.ord}, project_id=project)
                continue
            written = compiler._write_results(conn, result, [], project)
            claims += written["new"]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM arteries.persistent
                    WHERE project_id = %s ORDER BY source_ts DESC LIMIT %s
                    """, (project, written["new"]))
                for (pid,) in cur.fetchall():
                    graph.add_edge(cur, project, "persistent", str(pid),
                                   graph.DERIVED_FROM, "chunk", cid)
                conn.commit()

        return {"path": name, "status": "ingested",
                "chunks": len(chunks), "claims": claims, "scope": scope_id}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="art ingest", description=__doc__)
    parser.add_argument("target", help="a file, a directory, or - for stdin")
    parser.add_argument("--name",
                        help="identity to store stdin under; reuse it so re-sends dedupe")
    parser.add_argument("--include", action="append", default=None,
                        help="glob for directory mode (default *.md)")
    parser.add_argument("--kind", default="document",
                        help="document | plan | spec -- recorded on the document row")
    parser.add_argument("--project", default=None)
    args = parser.parse_args(argv)

    project = args.project or scope.current_project()
    if not scope.scope_for(project) and not scope.is_tracked():
        print(f"{project} is not in a scope; run `art scope add <group> <path>` first")
        return 1

    if args.target == "-":
        text = sys.stdin.read()
        if not text.strip():
            print("nothing on stdin")
            return 1
        name = args.name or f"stdin:{args.kind}:{_digest(text)[:12]}"
        result = asyncio.run(ingest_text(text, name=name, project=project, kind=args.kind))
        print(f"  {result['status']:<10} {name}"
              + (f"  ({result['chunks']} chunks, {result['claims']} claims)"
                 if result["status"] == "ingested" else ""))
        return 0

    target = Path(args.target)
    if target.is_dir():
        patterns = args.include or ["*.md"]
        files = sorted({p for pat in patterns for p in target.rglob(pat) if p.is_file()})
    else:
        files = [target]
    if not files:
        print("no matching files")
        return 1

    for path in files:
        result = asyncio.run(ingest_file(path.resolve(), project, args.kind))
        if result["status"] == "unchanged":
            print(f"  unchanged  {path}")
        else:
            print(f"  ingested   {path}  ({result['chunks']} chunks, {result['claims']} claims)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
