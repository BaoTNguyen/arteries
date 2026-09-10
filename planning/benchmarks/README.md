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
