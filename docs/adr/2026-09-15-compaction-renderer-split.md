---
status: accepted
date: 2026-09-15
approved-by: operator
drafted-by: operator
retire-evidence: re-derivation count after compaction (the metric the packet exists to move)
switch: none
---
# One packet builder, two renderers: retrieval tiers and compaction state

## Context and Problem Statement

`build_packet` served per-turn retrieval injection and compaction continuity.
They differ in trigger, question, ranking, budget and failure cost: a weak
retrieval match wastes 200 bytes; a lost decision costs an hour.

## Decision Drivers

* A resuming agent needs what a fact *is* (objective, decisions, done, blocked,
  retracted, next), not which tier stored it
* Loaders, fusion and the corpus cache are shared and already right

## Considered Options

* One entry point, shared loaders, `render_state` beside `render_tiers`, chosen by trigger
* A renderer class hierarchy: no third consumer exists to justify one
* Per-CLI packet bodies: one body, six deliveries; a CLI needing another body is a delivery bug

## Decision Outcome

Chosen option: "one builder, two renderers". The compaction renderer has its own
schema version, and `art setup` regenerates the Codex compact prompt when that
version changes.

### Consequences

* Good, because compaction gets state fields without touching retrieval ranking.
* Bad, because a schema-version bump must keep reaching `art setup`, or stale prompts persist.

### Confirmation

`src/arteries/packet.py` (`render_state`, `render_tiers`). Proven by `tests/test_packet_state.py`.
