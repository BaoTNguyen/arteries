"""Human-reviewed document ingestion.

Walks a repo's Markdown, proposes candidate facts with source spans, and
imports the ones a human accepts into persistent memory. Was the evergreen
bootstrap; the tier is gone, the mechanism is worth keeping.

Two-step flow:
    art docs extract --project . --out docs_review.md
    art docs import --review docs_review.md --write

The review file is meant for humans to edit. The sidecar JSON preserves
source spans and original extracted text so edits can still be connected
back to source files.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from arteries import storage
from arteries.config import PROJECT_ID

DEFAULT_INCLUDE = ["AGENTS.md", "README.md", "*.md", "*.txt", "*.rst"]
IGNORE_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", "target"}
DOMAINS = ["technical", "AI", "business", "strategy", "product", "finance", "career", "learning", "personal", "writing", "intent"]
DOMAIN_KEYWORDS = {
    "technical": ["code", "api", "database", "schema", "test", "agent", "hook", "cli", "memory", "postgres", "integration"],
    "AI": ["ai", "llm", "model", "embedding", "prompt", "rlvr", "reward", "retrieval", "memoryframe"],
    "business": ["business", "customer", "revenue", "market", "sales"],
    "strategy": ["strategy", "roadmap", "goal", "decision", "approach"],
    "product": ["product", "feature", "user", "ux", "workflow"],
    "finance": ["finance", "cost", "pricing", "budget", "valuation"],
    "career": ["career", "job", "interview", "resume"],
    "learning": ["learn", "study", "teach", "course"],
    "personal": ["prefer", "preference", "habit", "personal"],
    "writing": ["write", "docs", "documentation", "draft"],
    "intent": ["prefer", "always", "never", "want", "need", "should", "must"],
}


@dataclass
class SourceSpan:
    path: str
    line_start: int
    line_end: int
    digest: str


@dataclass
class CandidateMemory:
    memory_id: str
    fact: str
    domains: list[str]
    confidence: float
    source: SourceSpan | None


def sidecar_path(review_path: Path) -> Path:
    if review_path.suffix:
        return review_path.with_suffix(".meta.json")
    return review_path.with_name(review_path.name + ".meta.json")


def discover_files(project: Path, includes: list[str]) -> list[Path]:
    files: list[Path] = []
    for path in project.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(project).parts
        if any(part in IGNORE_DIRS for part in rel_parts):
            continue
        rel = path.relative_to(project).as_posix()
        if any(fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(path.name, pattern) for pattern in includes):
            files.append(path)
    return sorted(files)


def extract_candidates(project: Path, includes: list[str] | None = None) -> list[CandidateMemory]:
    includes = includes or DEFAULT_INCLUDE
    candidates: list[CandidateMemory] = []
    seen: set[str] = set()
    for path in discover_files(project, includes):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        digest = _digest(text)
        for fact, line_start, line_end in _extract_facts(text):
            normalized = _normalize_fact(fact)
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(CandidateMemory(
                memory_id=f"mem_{len(candidates) + 1:03d}",
                fact=fact,
                domains=_infer_domains(fact),
                confidence=0.85,
                source=SourceSpan(
                    path=path.relative_to(project).as_posix(),
                    line_start=line_start,
                    line_end=line_end,
                    digest=digest,
                ),
            ))
    return candidates


def write_review(project: Path, out_path: Path, candidates: list[CandidateMemory], import_id: str | None = None) -> None:
    import_id = import_id or f"docs-{uuid4()}"
    lines = [
        "---",
        f"import_id: {import_id}",
        f"project_root: {project.resolve()}",
        "status: pending_review",
        "---",
        "",
        "# Document Review",
        "",
        "Delete memories you do not want imported. Edit wording freely.",
        "Move memories under `Rejected Memories` to keep feedback without importing them.",
        "Keep the `Source:` line when you want provenance preserved.",
        "",
        "## Accepted Memories",
        "",
    ]
    for candidate in candidates:
        lines.extend(_format_memory_block(candidate))
    lines.extend([
        "## Rejected Memories",
        "",
        "Move memories here if they should not be imported.",
        "",
    ])
    out_path.write_text("\n".join(lines), encoding="utf-8")
    meta = {
        "import_id": import_id,
        "project_root": str(project.resolve()),
        "memories": [asdict(candidate) for candidate in candidates],
    }
    sidecar_path(out_path).write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")


def parse_review(review_path: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    text = review_path.read_text(encoding="utf-8")
    header, body = _split_frontmatter(text)
    section = "unknown"
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    fact_lines: list[str] = []

    def finish() -> None:
        nonlocal current, fact_lines
        if not current:
            return
        current["fact"] = _clean_fact("\n".join(fact_lines))
        if current["fact"]:
            blocks.append(current)
        current = None
        fact_lines = []

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            finish()
            heading = line[3:].strip().lower()
            if heading == "accepted memories":
                section = "accept"
            elif heading == "rejected memories":
                section = "reject"
            else:
                section = "unknown"
            continue
        if line.startswith("### "):
            finish()
            current = {"memory_id": line[4:].strip(), "status": section, "domains": [], "confidence": 1.0, "source": None}
            continue
        if not current:
            continue
        if line.startswith("Source:"):
            current["source"] = _parse_source(line.removeprefix("Source:").strip())
            continue
        if line.startswith("Domains:"):
            value = line.removeprefix("Domains:").strip()
            current["domains"] = [part.strip() for part in value.split(",") if part.strip()]
            continue
        if line.startswith("Confidence:"):
            try:
                current["confidence"] = float(line.removeprefix("Confidence:").strip())
            except ValueError:
                current["confidence"] = 1.0
            continue
        if line.startswith("Reason:"):
            current["reason"] = line.removeprefix("Reason:").strip()
            continue
        if line or fact_lines:
            fact_lines.append(line)
    finish()
    return header, blocks


def import_review(review_path: Path, write: bool = False) -> dict[str, Any]:
    _header, blocks = parse_review(review_path)
    duplicate_ids = _duplicate_memory_ids(blocks)
    meta = _load_meta(review_path)
    originals = {item["memory_id"]: item for item in meta.get("memories", [])}
    accepted = [block for block in blocks if block["status"] == "accept" and block["fact"]]
    rejected = [block for block in blocks if block["status"] == "reject"]
    edited = [block for block in accepted if block["memory_id"] in originals and block["fact"] != originals[block["memory_id"]]["fact"]]
    manual = [block for block in accepted if block["memory_id"] not in originals]
    duplicate_count = 0
    inserted: list[str] = []
    errors: list[str] = []

    if duplicate_ids:
        errors.append("Duplicate memory IDs found: " + ", ".join(duplicate_ids))

    if write and not duplicate_ids:
        existing = {_normalize_fact(row["fact"]) for row in storage.get_persistent(PROJECT_ID, limit=1000)}
        for block in accepted:
            normalized = _normalize_fact(block["fact"])
            if normalized in existing:
                duplicate_count += 1
                continue
            source_meta = _source_meta(block, originals.get(block["memory_id"]), meta.get("import_id"))
            inserted.append(storage.insert_persistent(
                project_id=PROJECT_ID,
                fact=block["fact"],
                domains=block["domains"] or _infer_domains(block["fact"]),
                confidence=block["confidence"],
                origin="reviewed",
                source_meta=source_meta,
            ))
            existing.add(normalized)

    return {
        "accepted": len(accepted),
        "rejected": len(rejected),
        "edited": len(edited),
        "manual": len(manual),
        "duplicates": duplicate_count,
        "duplicate_ids": duplicate_ids,
        "errors": errors,
        "inserted": len(inserted),
        "inserted_ids": inserted,
    }


def _duplicate_memory_ids(blocks: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    for block in blocks:
        memory_id = block["memory_id"]
        counts[memory_id] = counts.get(memory_id, 0) + 1
    return sorted(memory_id for memory_id, count in counts.items() if count > 1)


def _format_memory_block(candidate: CandidateMemory) -> list[str]:
    source = candidate.source
    source_line = "Source: manual"
    if source:
        source_line = f"Source: {source.path}:{source.line_start}-{source.line_end}"
    return [
        f"### {candidate.memory_id}",
        "",
        candidate.fact,
        "",
        source_line,
        f"Domains: {', '.join(candidate.domains)}",
        f"Confidence: {candidate.confidence:.2f}",
        "",
    ]


def _extract_facts(text: str) -> list[tuple[str, int, int]]:
    facts: list[tuple[str, int, int]] = []
    paragraph: list[str] = []
    start_line = 1

    def flush(end_line: int) -> None:
        nonlocal paragraph, start_line
        if not paragraph:
            return
        fact = _clean_fact(" ".join(paragraph))
        if _is_candidate(fact):
            facts.append((fact, start_line, end_line))
        paragraph = []

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            flush(line_no - 1)
            continue
        if stripped.startswith("#"):
            flush(line_no - 1)
            continue
        if stripped.startswith("|"):
            flush(line_no - 1)
            continue
        item = re.sub(r"^[-*+]\s+", "", stripped)
        item = re.sub(r"^\d+[.)]\s+", "", item)
        if not paragraph:
            start_line = line_no
        paragraph.append(item)
    flush(len(text.splitlines()))
    return facts


def _is_candidate(fact: str) -> bool:
    words = fact.split()
    if len(words) < 6 or len(words) > 80:
        return False
    lower = fact.lower()
    if lower.startswith(("todo", "maybe", "wip", "draft")):
        return False
    signals = ["must", "should", "always", "never", "use", "uses", "prefer", "prefers", "owns", "is ", "are ", "not "]
    return any(signal in lower for signal in signals)


def _clean_fact(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip("` ")


def _normalize_fact(value: str) -> str:
    return re.sub(r"\W+", " ", value.casefold()).strip()


def _infer_domains(fact: str) -> list[str]:
    lower = fact.lower()
    scored: list[tuple[int, str]] = []
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword in lower)
        if score:
            scored.append((score, domain))
    if not scored:
        return ["technical"]
    scored.sort(reverse=True)
    domains = [domain for _score, domain in scored[:2]]
    return [domain for domain in DOMAINS if domain in domains]


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    header: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            header[key.strip()] = value.strip()
    return header, text[end + 5:].lstrip("\n")


def _parse_source(value: str) -> dict[str, Any] | None:
    if value == "manual":
        return None
    match = re.match(r"(.+):(\d+)-(\d+)$", value)
    if not match:
        return {"path": value}
    return {"path": match.group(1), "line_start": int(match.group(2)), "line_end": int(match.group(3))}


def _load_meta(review_path: Path) -> dict[str, Any]:
    path = sidecar_path(review_path)
    if not path.exists():
        return {"memories": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _source_meta(block: dict[str, Any], original: dict[str, Any] | None, import_id: str | None = None) -> dict[str, Any]:
    meta = {
        "source_type": "bootstrap_import",
        "review_id": import_id,
        "review_memory_id": block["memory_id"],
        "review_status": block["status"],
    }
    source = None
    if original:
        meta["original_fact"] = original.get("fact")
        meta["edited"] = block["fact"] != original.get("fact")
        source = original.get("source")
    elif block.get("source"):
        source = block["source"]
    else:
        meta["source_type"] = "user_review"

    if source:
        meta["source"] = source
        meta["source_file"] = source.get("path")
        meta["source_hash"] = source.get("digest")
    return meta


def _print_summary(summary: dict[str, Any]) -> None:
    print("Document import preview")
    print(f"Accepted: {summary['accepted']}")
    print(f"Rejected: {summary['rejected']}")
    print(f"Edited: {summary['edited']}")
    print(f"New manual memories: {summary['manual']}")
    print(f"Existing fact duplicates: {summary['duplicates']}")
    if summary.get("duplicate_ids"):
        print("Duplicate memory IDs: " + ", ".join(summary["duplicate_ids"]))
    if summary.get("errors"):
        for error in summary["errors"]:
            print(f"Error: {error}")
    print(f"Inserted: {summary['inserted']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="art docs", description="Extract and import reviewed facts from a repo's documentation.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract", help="write editable Markdown review file")
    extract_parser.add_argument("--project", required=True, type=Path)
    extract_parser.add_argument("--out", required=True, type=Path)
    extract_parser.add_argument("--include", action="append", default=[])

    import_parser = subparsers.add_parser("import", help="preview or write accepted memories from review file")
    import_parser.add_argument("--review", required=True, type=Path)
    import_parser.add_argument("--write", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "extract":
        includes = args.include or DEFAULT_INCLUDE
        candidates = extract_candidates(args.project, includes)
        write_review(args.project, args.out, candidates)
        print(f"Wrote {len(candidates)} candidate memories to {args.out}")
        print(f"Wrote metadata to {sidecar_path(args.out)}")
        return 0
    if args.command == "import":
        summary = import_review(args.review, write=args.write)
        _print_summary(summary)
        if summary.get("errors") and args.write:
            print("Fix the review file before writing to the database.")
            return 1
        if not args.write:
            if summary.get("errors"):
                print("Fix the review file before running with --write.")
            else:
                print("Run again with --write to insert accepted memories.")
        return 0
    return 2
