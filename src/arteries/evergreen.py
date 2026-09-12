"""The evergreen tier: what a scope knows, rather than what a project recorded.

Promotion from persistent asks one question -- how incremental is this claim to
the whole project group? -- and answers it with four measurable terms rather than
a model call, because the answer has to be stable enough to argue with.

    novelty    = 1 - max_cosine(fact, live evergreen in scope)
    reach      = min(1, distinct_entities / 3)
    durability = 0 if superseded within DURABILITY_DAYS else 1
    use        = min(1, access_count / 3)

    incrementality = 0.4*novelty + 0.2*reach + 0.2*durability + 0.2*use

Novelty carries the most weight because it is the only term that measures the
claim against the tier it wants to join. The other three are evidence that the
claim survived contact with the project: it named things, nothing overwrote it,
and something read it.

`core` bypasses the score entirely. Core rows are the project's own
specification -- they seed the graph before any conversation happens, and
scoring a design document against an empty tier would just measure emptiness.

Thresholds are guesses calibrated against no data, the same honesty
`compile.DUPLICATE_SIM` states about itself. `art benchmark` re-derives them once
there is a month of tier to derive from.

Promotion goes one level at a time: ephemeral to persistent to evergreen. An
authored write may enter at any tier, because the operator names it and a human
is the gate, but nothing skips a level unattended.
"""

from __future__ import annotations

import os
from typing import Any

import psycopg2
import psycopg2.extras

from arteries import degrade, promote, runlog
from arteries.config import AGENT_PROCESS_ID, DB_CONFIG, PROJECT_ID

# Below this a claim stays persistent. 0.6 sits above what a claim scores on
# novelty alone (0.4), so nothing reaches evergreen purely by being unlike what
# is already there -- it also has to have named something, survived, or been
# read.
EVERGREEN_THRESHOLD = float(os.getenv("ARTERIES_EVERGREEN_THRESHOLD", "0.6"))

# A claim superseded inside two weeks was a working assumption, not knowledge.
DURABILITY_DAYS = int(os.getenv("ARTERIES_DURABILITY_DAYS", "14"))

# How many activity days a claim must survive before it is even scored.
#
# Two of the four terms are meaningless on a fresh claim. `durability` is "not
# superseded within DURABILITY_DAYS", which a claim written this turn satisfies
# by not having existed long enough to be contradicted -- not-yet-disproven
# wearing the costume of survived. `use` is access_count/3, and a claim written
# this turn has been read zero times.
#
# So a brand-new claim can reach the 0.6 threshold on novelty and reach alone,
# with no evidence for half its score. Waiting is what makes the other half mean
# something: promotion is consolidation, not a fourth step of ingestion.
#
# Activity days rather than calendar days, for the same reason eviction uses
# them: three weeks away should not age a claim into the graph any more than it
# should age one out.
MIN_MATURITY_DAYS = int(os.getenv("ARTERIES_EVERGREEN_MATURITY_DAYS", "5"))

WEIGHTS = {"novelty": 0.4, "reach": 0.2, "durability": 0.2, "use": 0.2}

# Kinds that describe how the project is *run* rather than what is in it. These
# are scope-wide by nature -- a constraint recorded in arteries binds heart --
# so they promote on the score alone, without needing breadth.
SCOPE_WIDE_KINDS = ("decision", "constraint", "preference")

# Breadth for everything else: a claim naming two or more distinct entities is
# about how parts of the system relate, which is what a graph is for. A fact
# about one file with no reuse stays persistent, and that is the point.
MIN_REACH = 2 / 3


def score(novelty: float, reach: float, durability: float, use: float) -> float:
    """The weighted sum, clamped. Separate from the queries so it is testable
    without a database and arguable without a rerun."""
    total = (WEIGHTS["novelty"] * _clamp(novelty)
             + WEIGHTS["reach"] * _clamp(reach)
             + WEIGHTS["durability"] * _clamp(durability)
             + WEIGHTS["use"] * _clamp(use))
    return round(total, 4)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value or 0.0)))


def eligible(incrementality: float, kind: str, reach: float) -> bool:
    """Whether a scored claim may promote.

    Two doors rather than one threshold. A decision or a constraint is scope-wide
    by kind and does not need to name three things to prove it; a plain fact
    does, because the tier is a graph and an unconnected node is a row in the
    wrong table.
    """
    if incrementality < EVERGREEN_THRESHOLD:
        return False
    return kind in SCOPE_WIDE_KINDS or reach >= MIN_REACH


def measure(conn, row: dict[str, Any], scope_id: str) -> dict[str, float]:
    """The four terms for one persistent row."""
    return {
        "novelty": _novelty(conn, row, scope_id),
        "reach": _reach(conn, row),
        "durability": _durability(conn, row),
        "use": min(1.0, float(row.get("access_count") or 0) / 3.0),
    }


def _novelty(conn, row: dict[str, Any], scope_id: str) -> float:
    """1 - the closest thing evergreen already has.

    An empty tier scores 1.0, which is correct rather than generous: the first
    claim about anything is maximally incremental to a graph with nothing in it.
    """
    if not row.get("embedding"):
        return 0.0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT max(1 - (embedding <=> %(q)s::vector))
            FROM arteries.evergreen
            WHERE scope_id = %(scope)s AND valid_until IS NULL
              AND embedding IS NOT NULL
            """,
            {"q": row["embedding"], "scope": scope_id},
        )
        closest = cur.fetchone()[0]
    return 1.0 if closest is None else _clamp(1.0 - float(closest))


def _reach(conn, row: dict[str, Any]) -> float:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(DISTINCT dst_id) FROM arteries.memory_edges
            WHERE src_kind = 'persistent' AND src_id = %s
              AND rel = 'mentions' AND valid_until IS NULL
            """,
            (str(row["id"]),),
        )
        entities = cur.fetchone()[0]
    return min(1.0, entities / 3.0)


def _durability(conn, row: dict[str, Any]) -> float:
    """0 if something replaced this within DURABILITY_DAYS, else 1.

    A claim overwritten inside two weeks was a working assumption. One that
    survived is either right or unexamined, and the other three terms are what
    tell those apart.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS(
                SELECT 1 FROM arteries.memory_edges
                WHERE rel = 'supersedes' AND dst_kind = 'persistent'
                  AND dst_id = %s
                  AND created_at < %s + (%s || ' days')::interval
            )
            """,
            (str(row["id"]), row.get("valid_from"), DURABILITY_DAYS),
        )
        superseded = cur.fetchone()[0]
    return 0.0 if superseded else 1.0


def candidates(conn, project_id: str, limit: int) -> list[dict[str, Any]]:
    """Live persistent claims old enough to score, not already promoted.

    One query, used by the real pass and by --dry-run, so a verdict printed by
    one means the same thing in the other. Written separately once, and the
    dry-run reported "hold" for rows that had already been promoted -- true of
    the score, misleading about the outcome.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT p.id, p.fact, p.kind, p.domains, p.confidence, p.embedding,
                   p.access_count, p.valid_from, p.project_id,
                   p.episode_id, p.task_id
            FROM arteries.persistent p
            WHERE p.project_id = %(project)s
              AND p.valid_until IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM arteries.evergreen e
                  WHERE e.valid_until IS NULL AND p.id = ANY(e.parent_ids)
              )
              -- Old enough for `durability` and `use` to carry information.
              -- Counted against project_activity rather than the calendar, so a
              -- fortnight away does not mature anything.
              --
              -- Or written before the clock existed. The clock started on
              -- 2026-09-11 and the store has claims from August; those have
              -- months of real survival behind them, and holding them back
              -- because nothing was recording days yet would measure the
              -- clock's age rather than the claim's.
              AND (
                  (SELECT count(*) FROM arteries.project_activity a
                   WHERE a.project_id = p.project_id
                     AND a.day >= p.valid_from::date) >= %(maturity)s
                  OR p.valid_from::date < (
                      SELECT min(day) FROM arteries.project_activity a2
                      WHERE a2.project_id = p.project_id)
              )
            ORDER BY p.access_count DESC, p.valid_from ASC
            LIMIT %(limit)s
            """,
            {"project": project_id, "maturity": MIN_MATURITY_DAYS, "limit": limit},
        )
        rows = [dict(r) for r in cur.fetchall()]

    # The same filter the write path applies, applied again here. It is cheap,
    # it is the same question, and the store predates it: 566 live claims were
    # written before `worth_keeping` existed, and the first dry run wanted to
    # promote "User intends to write Plexus-related context into a markdown
    # file" into the graph. A claim that would not be written today should not
    # be consolidated today, and nothing else in the pipeline re-asks.
    return [r for r in rows if promote.worth_keeping(r.get("fact", "")) is None]


def promote_once(project_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Score live persistent claims and promote the ones that clear the bar.

    Reads exactly one table and writes exactly one, which is how the one-level
    rule is enforced rather than merely stated.
    """
    from arteries import scope

    project_id = project_id or PROJECT_ID
    scope_id = scope.scope_for(project_id) or project_id
    conn = psycopg2.connect(**DB_CONFIG)
    promoted, considered = [], 0
    try:
        rows = candidates(conn, project_id, limit)

        for row in rows:
            considered += 1
            terms = measure(conn, row, scope_id)
            value = score(**terms)
            if not eligible(value, row.get("kind") or "fact", terms["reach"]):
                continue
            if _insert(conn, row, scope_id, value):
                promoted.append(str(row["id"]))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        # Classified rather than swallowed: a database being down and a NameError
        # in the scorer are different problems and only one of them is fine.
        return {"status": degrade.note(exc, "evergreen promotion"),
                "promoted": 0, "considered": considered}
    finally:
        conn.close()

    if promoted:
        runlog.log_event("memory.evergreen.promoted", "arteries",
                         {"count": len(promoted), "ids": promoted[:5],
                          "considered": considered},
                         project_id=project_id, agent_id=AGENT_PROCESS_ID)
    return {"status": "ok", "promoted": len(promoted), "considered": considered}


def _insert(conn, row: dict[str, Any], scope_id: str, value: float) -> bool:
    """Write one evergreen row. False if it was already there."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arteries.evergreen
                (scope_id, fact, kind, domains, confidence, embedding,
                 source_project_id, parent_ids, episode_id, task_id,
                 incrementality)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s::uuid[], %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (scope_id, row["fact"], row.get("kind") or "fact",
             psycopg2.extras.Json(row.get("domains") or []),
             row.get("confidence") or 1.0, row.get("embedding"),
             row.get("project_id"), [str(row["id"])],
             row.get("episode_id"), row.get("task_id"), value),
        )
        return cur.fetchone() is not None


def seed(conn, scope_id: str, fact: str, *, kind: str = "fact",
         domains: list | None = None, embedding: list | None = None) -> str | None:
    """Write a core row directly.

    The one write that skips the score, because a project's own specification is
    not a claim that earned its way in -- it is the thing everything else is
    measured against. `incrementality` stays NULL: the row never scored, and a
    zero would read as "scored badly and got in anyway".
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arteries.evergreen
                (scope_id, fact, kind, domains, embedding, core, origin)
            VALUES (%s, %s, %s, %s::jsonb, %s, true, 'authored')
            RETURNING id
            """,
            (scope_id, fact, kind, psycopg2.extras.Json(domains or []), embedding),
        )
        return str(cur.fetchone()[0])


# A new project's specification lives in its planning documents. AGENTS.md is a
# later artefact -- it describes how agents work in a repo that already exists --
# so it is seeded on demand rather than at project creation.
DEFAULT_SPEC_GLOBS = ("planning/*.md",)
AGENTS_SPEC = "AGENTS.md"


def _default_specs(agents: bool = False) -> list[str]:
    return [*DEFAULT_SPEC_GLOBS, *([AGENTS_SPEC] if agents else [])]


def main(argv: list[str] | None = None) -> int:
    """`art evergreen` -- promote, seed, and look at the tier."""
    import argparse
    import json
    import pathlib

    from arteries import scope as scope_mod

    parser = argparse.ArgumentParser(prog="art evergreen", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    promote_p = sub.add_parser("promote", help="score persistent claims and promote")
    promote_p.add_argument("--project", default=None)
    promote_p.add_argument("--limit", type=int, default=20)
    promote_p.add_argument("--dry-run", action="store_true",
                           help="show scores without writing")

    seed_p = sub.add_parser(
        "seed", help="ingest a project's own specs into evergreen as core")
    seed_p.add_argument("paths", nargs="*",
                        help="files to seed; default is planning/*.md")
    seed_p.add_argument("--project", default=None)
    seed_p.add_argument("--kind", default="plan",
                        help="document kind, passed to `art ingest`")
    seed_p.add_argument("--agents", action="store_true",
                        help="also seed AGENTS.md, which describes how agents "
                             "work in a repo that already exists")

    stats_p = sub.add_parser("stats")
    stats_p.add_argument("--project", default=None)

    args = parser.parse_args(argv)
    project = args.project or PROJECT_ID
    scope_id = scope_mod.scope_for(project) or project

    if args.command == "seed":
        # Discovery plus `art ingest --core`, not a second extractor.
        #
        # The first version split markdown with `normalize.atoms` and wrote the
        # sentences straight in. That threw away everything `ingest.py` already
        # does: chunking, the compile call that assigns `kind` and extracts
        # entities, `derived_from` edges back to the chunk and document, and a
        # digest so re-seeding an unchanged file is a no-op. Two extractors for
        # one job, and the worse one was the default.
        import asyncio
        import pathlib

        from arteries import ingest

        patterns = args.paths or _default_specs(args.agents)
        paths = []
        for pattern in patterns:
            direct = pathlib.Path(pattern)
            if direct.is_file():
                paths.append(direct)
                continue
            paths.extend(sorted(m for m in pathlib.Path().glob(pattern)
                                if m.is_file()))
        paths = sorted(set(paths))
        if not paths:
            print("nothing to seed: no planning/*.md here, and no paths given")
            return 1

        seeded = 0
        for path in paths:
            result = asyncio.run(ingest.ingest_file(path, project, kind=args.kind,
                                                    core=True))
            print(f"  {result['status']:<10} {path}  "
                  f"({result.get('chunks', 0)} chunks, "
                  f"{result.get('core', 0)} core claims)")
            seeded += result.get("core", 0)
        print(f"seeded {seeded} core claims into {scope_id}")
        return 0

    if args.command == "stats":
        conn = psycopg2.connect(**DB_CONFIG)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT count(*), count(*) FILTER (WHERE core),
                           round(avg(incrementality)::numeric, 3)
                    FROM arteries.evergreen
                    WHERE scope_id = %s AND valid_until IS NULL
                    """,
                    (scope_id,),
                )
                total, core, mean = cur.fetchone()
        finally:
            conn.close()
        print(f"scope {scope_id}: {total} live, {core} core, "
              f"mean incrementality {mean if mean is not None else '-'}")
        return 0

    if args.dry_run:
        # The scores without the writes, because a threshold nobody can inspect
        # is a threshold nobody can argue with.
        conn = psycopg2.connect(**DB_CONFIG)
        try:
            rows = candidates(conn, project, args.limit)
            if not rows:
                print(f"nothing to score: every live claim is either already "
                      f"promoted or has not yet survived {MIN_MATURITY_DAYS} "
                      f"activity days")
            for row in rows:
                terms = measure(conn, row, scope_id)
                value = score(**terms)
                verdict = "promote" if eligible(
                    value, row.get("kind") or "fact", terms["reach"]) else "hold"
                print(f"{value:.3f}  {verdict:8} "
                      f"n={terms['novelty']:.2f} r={terms['reach']:.2f} "
                      f"d={terms['durability']:.0f} u={terms['use']:.2f}  "
                      f"{row['fact'][:64]}")
        finally:
            conn.close()
        return 0

    print(json.dumps(promote_once(project, limit=args.limit)))
    return 0


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(main())
