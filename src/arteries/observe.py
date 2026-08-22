"""The inbound path for things that are not a person at a CLI.

Arteries observes through hooks on user turns, which is the right shape for a
coding CLI and the wrong shape for everything else in the stack. Heart
orchestrates agents, plexus decomposes goals, marrow trains -- none of them type
into a prompt, so none of them produce a turn for a hook to catch.

This is the write path they use instead. It puts an observation into ephemeral,
where the ordinary compile pass picks it up, so a fact heart discovered is
promoted, deduped, and graphed by exactly the same machinery as one you typed.

    echo '{"text": "chose sequential retries over a backoff pool"}' \\
        | art observe --source heart --kind decision

    art observe --source plexus --project arteries "decomposed goal into 4 steps"

Callers are stdlib-only and do not talk to Postgres; that is the whole reason
this exists as a command rather than an import.
"""

from __future__ import annotations

import argparse
import json
import sys

from arteries import runlog, scope, storage
from arteries.config import PARENT_AGENT_ID
from arteries.extract import MIN_EXTRACTABLE_WORDS, _infer_domains

# Where an observation came from. `user` and `assistant` are the hook path;
# these are the programmatic ones, and the compiler applies a different bar to
# each because a plan is not a finding and a reward is not a claim.
SOURCES = ("heart", "plexus", "marrow", "external")

# What kind of thing is being reported. Passed through to the compiler as a
# hint; `kind` on the resulting persistent row is still the compiler's call.
KINDS = ("observation", "decision", "outcome", "plan")


def observe(
    text: str,
    *,
    source: str = "external",
    kind: str = "observation",
    project_id: str | None = None,
    agent_id: str | None = None,
    metadata: dict | None = None,
) -> str | None:
    """Store one observation as ephemeral. Returns its id, or None if refused."""
    text = (text or "").strip()
    if len(text.split()) < MIN_EXTRACTABLE_WORDS:
        return None
    if source not in SOURCES:
        source = "external"
    if kind not in KINDS:
        kind = "observation"

    # Same opt-in rule as the hook path: an unregistered project is not
    # observed, whoever is doing the observing. An explicit --project is checked
    # on its own registration -- falling back to the caller's cwd would let a
    # programmatic sender write into any project name it liked from a tracked
    # directory, which is the opposite of opt-in.
    if project_id:
        project, tracked = project_id, bool(scope.scope_for(project_id))
    else:
        project, tracked = scope.current_project(), scope.is_tracked()
    if not tracked:
        runlog.log_event("observe.skipped_untracked", "arteries",
                         {"project": project, "source": source})
        return None

    # A stable bucket per source, NOT the caller's pid. `art observe` is a
    # one-shot process, so defaulting to AGENT_PROCESS_ID would file every
    # observation under a dead pid and the compile claim -- which matches on
    # agent_process_id -- would never see any of them again. Same orphaning that
    # stranded raw-pid rows before scopes existed.
    body = f"[{kind}] {text}" if kind != "observation" else text
    row_id = storage.insert_ephemeral(
        project_id=project,
        agent_process_id=agent_id or f"{source}-observer",
        fact=body,
        domains=_infer_domains(body.lower()),
        parent_agent_id=PARENT_AGENT_ID,
        source=source,
    )
    runlog.log_event("observe.recorded", "arteries",
                     {"source": source, "kind": kind, "chars": len(text),
                      **(metadata or {})}, project_id=project)
    return row_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="art observe", description=__doc__)
    parser.add_argument("text", nargs="*", help="observation text; omit to read stdin")
    parser.add_argument("--source", default="external", choices=SOURCES)
    parser.add_argument("--kind", default="observation", choices=KINDS)
    parser.add_argument("--project", default=None)
    parser.add_argument("--agent", default=None)
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="stdin is JSONL: one {text, kind, metadata} per line")
    args = parser.parse_args(argv)

    if args.as_json:
        recorded = 0
        for line in sys.stdin.read().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                print(f"skipped unparseable line: {line[:60]}", file=sys.stderr)
                continue
            if observe(rec.get("text", ""), source=rec.get("source", args.source),
                       kind=rec.get("kind", args.kind), project_id=args.project,
                       agent_id=args.agent, metadata=rec.get("metadata")):
                recorded += 1
        print(f"recorded {recorded} observations")
        return 0

    text = " ".join(args.text).strip() or sys.stdin.read().strip()
    row_id = observe(text, source=args.source, kind=args.kind,
                     project_id=args.project, agent_id=args.agent)
    if not row_id:
        print("not recorded: too short, or this project is not in a scope",
              file=sys.stderr)
        return 1
    print(row_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
