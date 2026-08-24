"""Inspect recorded agent runs."""

from __future__ import annotations

import argparse
import json
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

    ns = parser.parse_args(argv)
    if ns.command == "start":
        print(json.dumps(runlog.start_run(_project(ns), ns.agent, ns.cli, ns.repo), indent=2, sort_keys=True))
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
