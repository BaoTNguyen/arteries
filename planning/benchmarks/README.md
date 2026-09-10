# Retrieval baselines

The number a change has to beat. Saved query sets, not just results: `art
benchmark` generates queries at temperature 0.4, so a fresh set each run means
run-to-run variance swamps whatever you changed.

    art benchmark --load planning/benchmarks/baseline-20260909.json

## baseline-20260909.json

40 claims sampled from `arteries`, taken **before** any commit that changes what
gets written to the store. Everything from the filter work onward changes the
corpus, and `art benchmark` builds its ground truth out of the corpus -- so a
set saved after those commits is not measuring the same thing.

Captured on: `main` + rank fusion (findings 15, 26), the claim lease, the health
probe and quarantine, session keying, time-based ephemeral visibility, and
retrieval triage.

```
  window        cosine    +expansion        routed   added  useful
       1   21/40 (0.53)   24/40 (0.56)   24/40 (0.56)     173       3
       3   32/40 (0.65)   36/40 (0.66)   36/40 (0.66)     275       4
      10   37/40 (0.67)   38/40 (0.67)   38/40 (0.67)     293       1

  held out (14 queries, overlap <= 0.34)
       1    8/14 (0.57)    8/14 (0.57)    8/14 (0.57)
       3   11/14 (0.67)   12/14 (0.68)   12/14 (0.68)
      10   12/14 (0.68)   13/14 (0.68)   13/14 (0.68)
```

## Read the held-out split, not the headline

Mean query/claim token overlap is **0.51**, and 14 of 40 queries share more than
half their words with the claim they are meant to find. `QUERY_PROMPT` asks the
model for different vocabulary; it complies about half the time.

capillaries learned what that costs. Two of its three benchmarks shared
vocabulary with their targets, both flattered BM25, and the conclusions drawn
from them had to be discarded -- including a dense channel scoring at the random
baseline that nobody noticed, because sparse was carrying the system.

The useful result here: the held-out subset scores **the same or better** than
the full set (0.57/0.67/0.68 MRR against 0.53/0.65/0.67). Token reuse is not
inflating these numbers, because a dense retriever is largely indifferent to
literal overlap.

That changes the moment a lexical channel is added. When hybrid retrieval lands,
compare the **held-out** rows, or the sparse arm will look better than it is.

## What this baseline does not measure

- Packet composition. Recall of a target claim is not the same question as which
  15 rows reach the model.
- Ephemeral, which is recency-ranked and has no target to find.
- The evergreen tier, which does not exist yet.

## Hybrid retrieval: built, measured, off

Measured 2026-09-10 against this baseline, with the hybrid arm truncated to the
same window as the cosine arm so both lists are the same length:

```
  window        cosine        hybrid
      10   37/40 (0.67)   36/40 (0.54)
  held out  12/14 (0.68)   12/14 (0.53)
```

No recall gain, a clear ranking loss. Weighting does not rescue it -- at
0.9 dense / 0.1 lexical the hybrid arm still falls to mrr 0.58, because RRF adds
a term per channel and a row at dense rank 5 that is also lexical rank 1
outscores dense rank 1 at any weighting. That is RRF behaving correctly and being
wrong for this corpus.

**Why**, which is the part worth keeping: BM25 needs term frequency and these
documents are one sentence each, where capillaries' chunks are paragraphs. And
every query in this set is a paraphrase written to *avoid* the claim's
vocabulary -- exactly the case a lexical channel cannot serve.

**What this did not measure** is the case the channel was built for: a query
naming an identifier. `get_persistent_by_text("why does UndefinedColumn happen
with claimed_at")` returns five relevant rows today, and nothing in these 40
queries looks like that.

So `ARTERIES_HYBRID=off` is the default, the code and the GIN index stay, and the
evidence that would flip it is a query set containing identifier queries. Adding
one is the next benchmark job, not a tuning pass on this one.

An honest first measurement of the truncation bug is worth recording too: before
the hybrid arm was cut to `window`, it read 37/40 at window 1 against cosine's
21/40. That was a list of 21 being compared against a list of 1 -- the lexical
channel contributes 20 candidates whatever the window is. It measured the length
of the list, not the quality of retrieval.
