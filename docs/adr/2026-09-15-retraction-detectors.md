---
status: accepted
date: 2026-09-15
approved-by: operator
drafted-by: operator
retire-evidence: hand-read retraction precision over a week; replay is not valid evidence here
switch: ARTERIES_RETRACTION_DRY_RUN (operator env)
---
# Retraction uses deterministic detectors, a fixed precedence, and dry-run first

## Context and Problem Statement

A compaction packet should retract beliefs that turned out wrong. Promotion runs
at `SessionEnd`, so the compiler's `supersedes` edges don't exist yet for the
session being compacted. A false retraction is the worst failure: the agent is
told a true thing is wrong, and believes it.

## Decision Drivers

* No model call on the compaction path
* Never silently pick a side; a wrong retraction is worse than a stale fact

## Considered Options

* Four detectors (existing edges, refutation by tool result, value overwrite at cosine ≥ 0.85, user correction), precedence user > observed > stated > inferred, a near-miss guard, ties rendered as unresolved
* A model call on the compaction path: semantic negation runs only in the background pass, or not at all
* An `ephemeral.evidence` column: the class derives from source (`evidence.for_source`)

## Decision Outcome

Chosen option: "deterministic detectors with fixed precedence", running in
dry-run over the existing corpus before they may retract anything.

### Consequences

* Good, because detection costs two indexed queries and no model.
* Bad, because paraphrased contradictions go unseen when the generator is down.

### Confirmation

`src/arteries/packet.py` (detectors, near-miss guard), `src/arteries/evidence.py`. Proven by `tests/test_packet_state.py`.
