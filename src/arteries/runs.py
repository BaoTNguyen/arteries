"""Inspect recorded agent runs."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from arteries import runlog
from arteries.config import AGENT_PROCESS_ID


def _project(ns) -> str:
    """--project if given, else the project owning the cwd.

    The default used to be config.PROJECT_ID, bound at import: "default" for
    anyone not running under a hook, so `art runs recent` reported on a project
    that holds nothing.
    """
    from arteries import scope
    return ns.project or scope.current_project()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect arteries run events.")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="start a new session run")
    start.add_argument("--project", default=None)
    start.add_argument("--agent", default=AGENT_PROCESS_ID)
    start.add_argument("--cli", default=None)
    start.add_argument("--repo", type=Path, default=Path.cwd())
    start.add_argument("--force", action="store_true",
                       help="mint a new run even if this session already has one")

    end = sub.add_parser("end", help="close a run (heart calls this at episode.finished)")
    end.add_argument("run_id", nargs="?", default=None,
                     help="run to close; omit for the current one")
    end.add_argument("--reason", default="explicit")
    end.add_argument("--repo", type=Path, default=Path.cwd())

    sub.add_parser("sweep", help="close runs idle past their threshold")

    recent = sub.add_parser("recent", help="show recent events for a project")
    recent.add_argument("--project", default=None)
    recent.add_argument("--limit", type=int, default=25)
    recent.add_argument("--repo", type=Path, default=Path.cwd())

    show = sub.add_parser("show", help="show events for a run id")
    show.add_argument("run_id")
    show.add_argument("--limit", type=int, default=100)
    show.add_argument("--repo", type=Path, default=Path.cwd())

    summary = sub.add_parser("summary", help="summarize recent activity for a project")
    summary.add_argument("--project", default=None)
    summary.add_argument("--limit", type=int, default=100)
    summary.add_argument("--repo", type=Path, default=Path.cwd())

    # Bare `art runs` used to exit with a usage dump, alone among the CLI's
    # commands -- `art doctor`, `art packet` and the rest all do something
    # useful with no arguments. "recent" is the one a bare invocation means.
    if not (argv if argv is not None else sys.argv[1:]):
        argv = ["recent"]
    ns = parser.parse_args(argv)
    if ns.command == "start":
        print(json.dumps(runlog.start_run(_project(ns), ns.agent, ns.cli, ns.repo, force=ns.force),
                         indent=2, sort_keys=True))
        return 0
    if ns.command == "end":
        run_id = ns.run_id or runlog.current_run(repo_path=ns.repo)["run_id"]
        closed = runlog.end_run(run_id, ns.reason)
        print(json.dumps({"run_id": run_id, "closed": closed, "reason": ns.reason},
                         indent=2, sort_keys=True))
        return 0
    if ns.command == "sweep":
        print(json.dumps({"closed": runlog.sweep_runs()}, indent=2, sort_keys=True))
        return 0
    if ns.command == "recent":
        print(json.dumps(runlog.recent_events(_project(ns), ns.limit, repo_path=ns.repo), indent=2, sort_keys=True))
        return 0
    if ns.command == "show":
        print(json.dumps(runlog.show_run(ns.run_id, ns.limit, repo_path=ns.repo), indent=2, sort_keys=True))
        return 0
    if ns.command == "summary":
        print(json.dumps(runlog.summarize(_project(ns), ns.limit, repo_path=ns.repo), indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
