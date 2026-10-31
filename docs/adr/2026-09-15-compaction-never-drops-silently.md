---
status: accepted
date: 2026-09-15
approved-by: operator
drafted-by: operator
retire-evidence: canary presence below 100% or re-derivation rising after a budget change
switch: none
---
# A compaction packet never drops its critical fields silently

## Context and Problem Statement

A packet has a budget. Cutting the oldest items to fit is how other CLIs lose
state without saying so: information loss disguised as success.

## Decision Drivers

* What is open, blocked or next must survive any budget
* Anything dropped must be named

## Considered Options

* Fields never dropped (`retracted`, `unresolved`, `open_question`, `state.blocked`, `next`, `canary`); a fixed drop order for the rest; dropped items listed by name
* Trim the oldest items to fit (the failure mode observed in Codex)

## Decision Outcome

Chosen option: "protected fields and a named drop list". Drop order: `files`
beyond the five most recent, then decision rationale (keeping chosen and
rejected), then the `state.done` tail, then conversation excerpts.

### Consequences

* Good, because a resuming agent always learns what was cut.
* Bad, because protected fields alone can exceed a small budget.

### Confirmation

`src/arteries/packet.py` (budget fit). Proven by `tests/test_packet_state.py`.
