"""Short CLI entry point for arteries: `art`."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from arteries import doctor, evergreen, inspect, packet, runs, setup_cli, setup_db, trace
from arteries.eval import evaluate


COMMANDS = ("setup", "evergreen", "setup-db", "eval", "inspect", "runs", "doctor", "packet", "trace")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="art",
        description="Arteries CLI shortcut.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=COMMANDS,
        help="command to run: setup, evergreen, setup-db, eval, inspect, runs, doctor, packet, trace",
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)

    if ns.command is None:
        parser.print_help()
        return 0

    if ns.command == "setup":
        return setup_cli.main(ns.args)
    if ns.command == "evergreen":
        return evergreen.main(ns.args)
    if ns.command == "setup-db":
        setup_db.setup()
        return 0
    if ns.command == "eval":
        if not ns.args:
            parser.error("eval requires a prompt")
        prompt = " ".join(ns.args)
        result = asyncio.run(evaluate(prompt))
        if result:
            print(result)
        return 0
    if ns.command == "inspect":
        return inspect.main(ns.args)
    if ns.command == "runs":
        return runs.main(ns.args)
    if ns.command == "doctor":
        return doctor.main(ns.args)
    if ns.command == "packet":
        return packet.main(list(ns.args))
    if ns.command == "trace":
        return trace.main(ns.args)

    parser.error(f"unknown command: {ns.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
