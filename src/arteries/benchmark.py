"""Does retrieval actually find the right memory?

Every threshold in the retrieval path was a guess -- the relevance floor, the
expansion gate, the hop decay. This makes them measurable without waiting for a
reward signal, by building ground truth out of the store itself: take a claim,
have the model write the question a developer would ask months later whose
answer is that claim, deliberately in different words, then check whether
retrieval brings the claim back.

    art benchmark --save qs.json   # generate a query set and measure against it
    art benchmark --load qs.json   # re-measure the same set after a change
    art benchmark --window 1 3 10  # sweep the cosine window

**Save the query set.** Generation runs at temperature 0.4, so a fresh set each
run means run-to-run variance swamps the effect of whatever you changed. Compare
threshold A against threshold B on identical queries or the numbers mean
nothing.

Reading it: `recall@k` is how often the target appeared in the first k results,
`MRR` is the mean of 1/rank. A window sweep is the informative run -- a tight
window stands in for a large corpus, where the target falls outside the slice
cosine returns.
"""

from __future__ import annotations

import argparse
import json

import httpx
import psycopg2
import psycopg2.extras

from arteries.config import COMPILE_MODEL, DB_CONFIG, GENERATE_URL

QUERY_PROMPT = """For each numbered fact, write the question a developer would ask months later whose answer is that fact.

Rules:
- Use DIFFERENT vocabulary than the fact. Do not reuse its distinctive nouns verbatim.
- Ask it the way someone who half-remembers would: vague, oblique, or by consequence.
- One question per fact, under 12 words.

Respond with JSON only: {"questions": {"0": "...", "1": "..."}}

"""


def sample_claims(project: str, n: int, db_config: dict | None = None) -> list[dict]:
    """Claims that carry graph edges -- the population expansion can act on."""
    conn = psycopg2.connect(**(db_config or DB_CONFIG))
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT p.id, p.fact
                FROM arteries.persistent p
                JOIN arteries.memory_edges e
                  ON e.src_id = p.id::text AND e.valid_until IS NULL
                 AND e.dst_kind IN ('entity', 'persistent')
                WHERE p.valid_until IS NULL AND p.embedding IS NOT NULL
                ORDER BY p.id LIMIT %s
                """,
                (n,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def build_queries(claims: list[dict]) -> list[dict]:
    """One paraphrased query per claim, in a single model call."""
    listing = "\n".join(f"[{i}] {c['fact']}" for i, c in enumerate(claims))
    resp = httpx.post(GENERATE_URL, timeout=300.0, json={
        "model": COMPILE_MODEL, "temperature": 0.4,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": QUERY_PROMPT + listing}],
    })
    resp.raise_for_status()
    questions = json.loads(resp.json()["choices"][0]["message"]["content"])["questions"]
    return [
        {"id": str(claims[int(i)]["id"]), "fact": claims[int(i)]["fact"], "query": q}
        for i, q in questions.items() if int(i) < len(claims)
    ]


def _rank(target: str, rows: list[dict]) -> int | None:
    for i, r in enumerate(rows, 1):
        if str(r["id"]) == target:
            return i
    return None


def run(cases: list[dict], project: str, window: int) -> dict:
    """Three arms on identical queries.

    `cosine` is persistent retrieval alone. `expansion` forces the graph walk on
    for every query regardless of the gate -- that is the A/B of the mechanism
    itself, and the gate would otherwise hide it by rarely firing. `routed` is
    what production does, gated.

    Also reports what expansion costs: claims added per query, and how many of
    those were the target versus filler occupying context budget.
    """
    from arteries import memory_select, route as router, storage
    from arteries.embed import embed_text_sync

    # context_from_env() reads ARTERIES_PROJECT, which a human running `art
    # benchmark` has not set -- so it resolved to "default" while the queries ran
    # against the cwd-resolved project. The scope CTE then found no members and
    # every expansion returned empty. Build the context from the same project the
    # rest of the run uses.
    env_ctx = memory_select.context_from_env()
    ctx = memory_select.AgentContext(
        cli=env_ctx.cli, project_id=project, agent_id=env_ctx.agent_id,
        parent_agent_id=env_ctx.parent_agent_id, agent_role=env_ctx.agent_role,
        event=env_ctx.event, capabilities=env_ctx.capabilities,
    )
    arms: dict[str, list] = {"cosine": [], "expansion": [], "routed": []}
    recovered, lost, added_counts = [], [], []
    strategies: dict[str, int] = {}

    for case in cases:
        vec = embed_text_sync(case["query"], is_query=True)
        seeds = storage.get_persistent_by_relevance(project, vec, limit=window, threshold=0.0)

        forced = memory_select._expand(seeds[:5], ctx, limit=8)
        added_counts.append(len(forced))

        plan = router.choose(case["query"], seeds)
        strategies[plan.strategy] = strategies.get(plan.strategy, 0) + 1
        if plan.strategy == "cosine":
            routed = seeds
        elif plan.strategy == "cosine+entity":
            routed = seeds + memory_select._by_entity(plan.entities, ctx)
        else:
            routed = seeds + forced

        r = {"cosine": _rank(case["id"], seeds),
             "expansion": _rank(case["id"], seeds + forced),
             "routed": _rank(case["id"], routed)}
        for k, v in r.items():
            arms[k].append(v)
        if r["cosine"] is None and r["expansion"] is not None:
            recovered.append({"query": case["query"], "rank": r["expansion"]})
        # Expansion appends below the seeds, so it cannot displace a hit. If this
        # ever fires, the ordering contract broke.
        if r["cosine"] is not None and r["expansion"] is not None \
                and r["expansion"] > r["cosine"]:
            lost.append(case["query"])

    def score(ranks: list[int | None]) -> dict:
        n = len(ranks) or 1
        return {"found": sum(1 for r in ranks if r),
                "mrr": round(sum(1 / r for r in ranks if r) / n, 3)}

    total_added = sum(added_counts)
    return {
        "window": window, "n": len(cases),
        "cosine": score(arms["cosine"]),
        "expansion": score(arms["expansion"]),
        "routed": score(arms["routed"]),
        "recovered": recovered, "displaced": lost,
        "claims_added": total_added,
        "useful_added": len(recovered),
        "noise_ratio": round(1 - (len(recovered) / total_added), 3) if total_added else None,
        "strategies": strategies,
    }


def main(argv: list[str] | None = None) -> int:
    from arteries import scope

    parser = argparse.ArgumentParser(prog="art benchmark", description=__doc__)
    parser.add_argument("--n", type=int, default=20, help="claims to sample")
    parser.add_argument("--window", type=int, nargs="+", default=[1, 3, 10],
                        help="cosine result windows to sweep")
    parser.add_argument("--project", default=None)
    parser.add_argument("--save", help="write the generated query set here")
    parser.add_argument("--load", help="reuse a saved query set instead of generating")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    import pathlib

    project = args.project or scope.current_project()

    if args.load:
        cases = json.loads(pathlib.Path(args.load).read_text())
        print(f"reusing {len(cases)} saved queries from {args.load}")
    else:
        claims = sample_claims(project, args.n)
        if len(claims) < 5:
            print(f"only {len(claims)} claims with graph edges -- run `art compile` first")
            return 1
        print(f"building {len(claims)} paraphrased queries...")
        cases = build_queries(claims)
        if args.save:
            pathlib.Path(args.save).write_text(json.dumps(cases, indent=1))
            print(f"saved to {args.save} -- reuse it with --load to compare runs")
        else:
            print("  (no --save: these queries are one-off, so this run is not"
                  " comparable to another)")
    results = [run(cases, project, w) for w in args.window]

    if args.as_json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"\n{len(cases)} queries, target claim known\n")
    print(f"  {'window':>6} {'cosine':>13} {'+expansion':>13} {'routed':>13}"
          f" {'added':>7} {'useful':>7}")
    for r in results:
        c, e, t = r["cosine"], r["expansion"], r["routed"]
        print(f"  {r['window']:>6} {c['found']:>4}/{r['n']} ({c['mrr']:.2f})"
              f" {e['found']:>4}/{r['n']} ({e['mrr']:.2f})"
              f" {t['found']:>4}/{r['n']} ({t['mrr']:.2f})"
              f" {r['claims_added']:>7} {r['useful_added']:>7}")

    widest = max(results, key=lambda r: r["window"])
    print()
    if widest["displaced"]:
        print(f"  WARNING: expansion pushed {len(widest['displaced'])} targets down. "
              "It appends below\n  seeds and must never displace a hit.")
    for rec in widest["recovered"][:4]:
        print(f"  recovered at rank {rec['rank']}: {rec['query'][:56]}")
    if widest["noise_ratio"] is not None:
        print(f"\n  expansion added {widest['claims_added']} claims across "
              f"{widest['n']} queries; {widest['useful_added']} were the target.")
        print(f"  noise ratio {widest['noise_ratio']:.0%} -- the rest occupy context "
              "budget without\n  being what was asked for. That is not automatically "
              "waste (a neighbour can\n  be useful without being the target) but it is "
              "the cost side of the trade.")
    if widest["expansion"]["found"] == widest["cosine"]["found"]:
        print("\n  Expansion recovers nothing cosine missed at this corpus size.")
    print(f"\n  routes production would choose: {widest['strategies']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
