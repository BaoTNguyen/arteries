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
import pathlib
import re

import httpx
import psycopg2
import psycopg2.extras

from arteries import memory_select, route as router, scope, storage
from arteries.config import COMPILE_MODEL, DB_CONFIG, GENERATE_URL
from arteries.embed import embed_text_sync

# Two populations, because they ask different things of retrieval and the answer
# differs. capillaries names them "describing" and "naming" and measured a
# lexical channel winning on one and losing on the other.
QUERY_PROMPT = """For each numbered fact, write the question a developer would ask months later whose answer is that fact.

Rules:
- Use DIFFERENT vocabulary than the fact. Do not reuse its distinctive nouns verbatim.
- Ask it the way someone who half-remembers would: vague, oblique, or by consequence.
- One question per fact, under 12 words.

Respond with JSON only: {"questions": {"0": "...", "1": "..."}}

"""


IDENTIFIER_PROMPT = """For each numbered fact, write the question a developer would ask whose answer is that fact, the way someone asks when they are staring at the thing.

Rules:
- REUSE the fact's identifiers verbatim: file paths, function names, column names, error types, constants. Those are what the person has in front of them.
- Use ordinary words for everything else. Do not restate the fact.
- Under 12 words. Phrase it as a question about the identifier.

Example fact: "The stale-claim sweep measures claimed_at rather than source_ts."
Example question: "why does claimed_at matter for the sweep?"

Respond with JSON only: {"questions": {"0": "...", "1": "..."}}

"""

# A claim with no identifier cannot produce an identifier query, and asking for
# one anyway gets a paraphrase wearing the wrong label -- which would quietly
# turn this set back into the first one.
_HAS_IDENTIFIER = re.compile(r"[/.]\w|\b\w+_\w+|`|\b[A-Z]{2,}\b|[a-z][A-Z]")


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


def build_queries(claims: list[dict], style: str = "paraphrase") -> list[dict]:
    """One query per claim, in a single model call.

    `paraphrase` avoids the claim's vocabulary and measures whether retrieval
    can bridge wording. `identifier` reuses it deliberately and measures the
    other half of real traffic: someone staring at `UndefinedColumn` and asking
    about `UndefinedColumn`. A lexical channel is invisible on the first and is
    the entire point of the second.
    """
    if style == "identifier":
        claims = [c for c in claims if _HAS_IDENTIFIER.search(c["fact"])]
        if not claims:
            return []
    prompt = IDENTIFIER_PROMPT if style == "identifier" else QUERY_PROMPT
    listing = "\n".join(f"[{i}] {c['fact']}" for i, c in enumerate(claims))
    resp = httpx.post(GENERATE_URL, timeout=300.0, json={
        "model": COMPILE_MODEL, "temperature": 0.4,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt + listing}],
    })
    resp.raise_for_status()
    questions = json.loads(resp.json()["choices"][0]["message"]["content"])["questions"]
    return [
        {"id": str(claims[int(i)]["id"]), "fact": claims[int(i)]["fact"], "query": q,
         "overlap": round(token_overlap(q, claims[int(i)]["fact"]), 3)}
        for i, q in questions.items() if int(i) < len(claims)
    ]


# A query that reuses the claim's own words is not a test of retrieval, it is a
# test of string matching. capillaries learned this the expensive way: two of its
# three benchmarks shared vocabulary with their targets, both flattered BM25, and
# the conclusions drawn from them had to be thrown out. QUERY_PROMPT asks the
# model for different vocabulary and the model complies about half the time --
# measured over 40 queries, mean overlap 0.505, and 14 of them shared more than
# half their tokens with the target.
#
# So the overlap is recorded per query and the report splits on it. The low
# overlap subset is the honest number, and it is the one to watch when a lexical
# channel is added, because the high-overlap half will flatter it.
HELD_OUT_OVERLAP = 0.34

_WORD = re.compile(r"[a-z0-9_./]+")


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 3}


def token_overlap(query: str, fact: str) -> float:
    """Fraction of the query's content words that appear in the claim."""
    words = _content_words(query)
    return len(words & _content_words(fact)) / len(words) if words else 0.0


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
    arms: dict[str, list] = {"cosine": [], "hybrid": [], "expansion": [], "routed": []}
    recovered, lost, added_counts = [], [], []
    strategies: dict[str, int] = {}

    for case in cases:
        vec = embed_text_sync(case["query"], is_query=True)
        seeds = storage.get_persistent_by_relevance(project, vec, limit=window, threshold=0.0)

        forced = memory_select._expand(seeds[:5], ctx, limit=8)
        added_counts.append(len(forced))

        plan = router.choose(seeds)
        strategies[plan.strategy] = strategies.get(plan.strategy, 0) + 1
        routed = seeds if plan.strategy == "cosine" else seeds + forced

        # The lexical channel measured against the same queries as the cosine
        # one. Fused, not routed: capillaries measured routing and fusion beat
        # the best routed configuration on both query populations.
        # Truncated to the same window as the cosine arm. Without this the
        # lexical channel contributes up to 20 candidates whatever `window` is,
        # so "37/40 at window 1" compares a list of 21 against a list of 1 --
        # which measures the length of the list, not the quality of retrieval.
        hybrid = memory_select._hybrid(project, case["query"], seeds)[:window]

        r = {"cosine": _rank(case["id"], seeds),
             "hybrid": _rank(case["id"], hybrid),
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
        "hybrid": score(arms["hybrid"]),
        "expansion": score(arms["expansion"]),
        "routed": score(arms["routed"]),
        "recovered": recovered, "displaced": lost,
        "claims_added": total_added,
        "useful_added": len(recovered),
        "noise_ratio": round(1 - (len(recovered) / total_added), 3) if total_added else None,
        "strategies": strategies,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="art benchmark", description=__doc__)
    parser.add_argument("--n", type=int, default=20, help="claims to sample")
    parser.add_argument("--window", type=int, nargs="+", default=[1, 3, 10],
                        help="cosine result windows to sweep")
    parser.add_argument("--project", default=None)
    parser.add_argument("--style", choices=("paraphrase", "identifier"),
                        default="paraphrase",
                        help="paraphrase avoids the claim's words; identifier reuses them")
    parser.add_argument("--save", help="write the generated query set here")
    parser.add_argument("--load", help="reuse a saved query set instead of generating")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

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
        cases = build_queries(claims, style=args.style)
        if not cases:
            print(f"no claims suitable for a {args.style} query set")
            return 1
        if args.save:
            pathlib.Path(args.save).write_text(json.dumps(cases, indent=1))
            print(f"saved to {args.save} -- reuse it with --load to compare runs")
        else:
            print("  (no --save: these queries are one-off, so this run is not"
                  " comparable to another)")
    # Backfill overlap for query sets saved before it was recorded, so an old
    # baseline is still comparable to a new run rather than silently missing the
    # split.
    for case in cases:
        case.setdefault("overlap", round(token_overlap(case["query"], case["fact"]), 3))

    held_out = [c for c in cases if c["overlap"] <= HELD_OUT_OVERLAP]
    results = [run(cases, project, w) for w in args.window]
    held_out_results = ([run(held_out, project, w) for w in args.window]
                        if len(held_out) >= 5 else [])

    if args.as_json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"\n{len(cases)} queries, target claim known\n")
    print(f"  {'window':>6} {'cosine':>13} {'hybrid':>13} {'+expansion':>13}"
          f" {'routed':>13}")
    for r in results:
        c, h, e, t = r["cosine"], r["hybrid"], r["expansion"], r["routed"]
        print(f"  {r['window']:>6} {c['found']:>4}/{r['n']} ({c['mrr']:.2f})"
              f" {h['found']:>4}/{r['n']} ({h['mrr']:.2f})"
              f" {e['found']:>4}/{r['n']} ({e['mrr']:.2f})"
              f" {t['found']:>4}/{r['n']} ({t['mrr']:.2f})")

    mean_overlap = sum(c["overlap"] for c in cases) / (len(cases) or 1)
    print(f"\n  query/claim token overlap: mean {mean_overlap:.2f}, "
          f"{sum(1 for c in cases if c['overlap'] > 0.5)}/{len(cases)} above 0.5")
    if held_out_results:
        print(f"\n  held out -- the {len(held_out)} queries that share at most "
              f"{HELD_OUT_OVERLAP:.0%} of their words with the claim.\n"
              "  This is the honest number. The rest reuse the claim's own\n"
              "  vocabulary, which tests string matching rather than retrieval\n"
              "  and will flatter any lexical channel added later.\n")
        print(f"  {'window':>6} {'cosine':>13} {'hybrid':>13} {'+expansion':>13}"
              f" {'routed':>13}")
        for r in held_out_results:
            c, h, e, t = r["cosine"], r["hybrid"], r["expansion"], r["routed"]
            print(f"  {r['window']:>6} {c['found']:>4}/{r['n']} ({c['mrr']:.2f})"
                  f" {h['found']:>4}/{r['n']} ({h['mrr']:.2f})"
                  f" {e['found']:>4}/{r['n']} ({e['mrr']:.2f})"
                  f" {t['found']:>4}/{r['n']} ({t['mrr']:.2f})")
    else:
        print(f"  too few low-overlap queries ({len(held_out)}) for a held-out"
              " split; treat the numbers above as an upper bound")

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
