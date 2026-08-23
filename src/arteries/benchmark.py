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
    """Cosine alone against the routed path that actually runs in production.

    The second arm calls `_select_persistent`, not `_expand` -- measuring a code
    path the system does not take is how a benchmark quietly stops describing
    anything.
    """
    from arteries import memory_select, storage
    from arteries.embed import embed_text_sync

    ctx = memory_select.context_from_env()
    cosine_ranks, routed_ranks, recovered = [], [], []
    strategies: dict[str, int] = {}

    for case in cases:
        vec = embed_text_sync(case["query"], is_query=True)
        seeds = storage.get_persistent_by_relevance(project, vec, limit=window, threshold=0.0)

        from arteries import route as router
        plan = router.choose(case["query"], seeds)
        strategies[plan.strategy] = strategies.get(plan.strategy, 0) + 1

        if plan.strategy == "cosine":
            routed = seeds
        elif plan.strategy == "cosine+entity":
            routed = seeds + memory_select._by_entity(plan.entities, ctx)
        else:
            routed = seeds + memory_select._expand(seeds[:5], ctx, limit=8)

        r_cos = _rank(case["id"], seeds)
        r_routed = _rank(case["id"], routed)
        cosine_ranks.append(r_cos)
        routed_ranks.append(r_routed)
        if r_cos is None and r_routed is not None:
            recovered.append({"query": case["query"], "via": plan.strategy,
                              "rank": r_routed})

    def score(ranks: list[int | None]) -> dict:
        n = len(ranks) or 1
        return {"found": sum(1 for r in ranks if r),
                "mrr": round(sum(1 / r for r in ranks if r) / n, 3)}

    return {"window": window, "n": len(cases), "cosine": score(cosine_ranks),
            "routed": score(routed_ranks), "recovered": recovered,
            "strategies": strategies}


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
    print(f"  {'window':>6}  {'cosine':>12}  {'routed':>12}   recovered")
    for r in results:
        c, e = r["cosine"], r["routed"]
        print(f"  {r['window']:>6}  {c['found']:>3}/{r['n']} ({c['mrr']:.2f})"
              f"  {e['found']:>3}/{r['n']} ({e['mrr']:.2f})   "
              f"{len(r['recovered'])}")
    for rec in results[0]["recovered"][:4]:
        print(f"          + via {rec['via']:<18} rank {rec['rank']}  {rec['query'][:44]}")
    print(f"\n  routes chosen: {results[-1]['strategies']}")
    widest = max(results, key=lambda r: r["window"])
    if widest["routed"]["found"] == widest["cosine"]["found"]:
        print("  At the widest window routing changes nothing -- cosine is not "
              "missing\n  anything yet. Re-run as the corpus grows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
