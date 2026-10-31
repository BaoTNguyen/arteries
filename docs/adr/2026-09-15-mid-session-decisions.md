---
status: accepted
date: 2026-09-15
approved-by: operator
drafted-by: operator
retire-evidence: re-derivation measurement showing decisions specifically being lost
switch: none
---
# Mid-session decisions come from existing signals, not an intake classifier

## Context and Problem Statement

The packet's `decisions` field needs this session's decisions too, before
promotion has read their rationale. `ephemeral` has no `kind` column.

## Decision Drivers

* No classifier on the hook path

## Considered Options

* Fill from `persistent.kind='decision'` with its `chose`/`over` edges, plus user atoms matching choice markers, ordered by `seen_count`
* Add `kind` to ephemeral with an intake classifier: the one thing this design refuses everywhere else

## Decision Outcome

Chosen option: "existing signals". Ceiling accepted: mid-session decisions
render without their why until the next packet after promotion.

### Consequences

* Good, because intake stays classifier-free.
* Bad, because under `replace` delivery (pi, opencode) the rationale is lost
  until promotion; the reclaimed budget goes to `decisions` there.

### Confirmation

`src/arteries/packet.py` (decisions loader).
