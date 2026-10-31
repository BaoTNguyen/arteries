---
status: accepted
date: 2026-09-05
approved-by: operator
drafted-by: operator
retire-evidence: re-derivation or benchmark numbers showing the tiers lose or keep the wrong facts
switch: none
---
# Three memory tiers, sentence atoms, and activity-day retention

## Context and Problem Statement

Session turns become memory. Deciding the unit, the tiers it passes through and
how long each tier keeps it decides what a later agent can recall.

## Decision Drivers

* Intake runs on the hook path, so it must stay cheap
* The verbatim turn must stay recoverable
* Idle weeks must not expire a project's memory

## Considered Options

* Sentence atoms, no LLM rewrite; three tiers (ephemeral, persistent, evergreen); retention counted in activity days
* A candidate tier before ephemeral: atoms go straight into ephemeral, whose unique index does the job
* Wall-clock retention: a project idle for a month would lose everything

## Decision Outcome

Chosen option: "sentence atoms, three tiers, activity days". Ephemeral: session
end + 48h, 14-day outer bound. Persistent: 60 active days, usage-exempt.
Evergreen: permanent, one graph per scope group (`harness` gets one, not five).
The verbatim turn stays in `agent_events`.

### Consequences

* Good, because intake needs no model call and nothing expires during idle time.
* Bad, because a crashed CLI never fires `SessionEnd`, so the 48h expiry is the only backstop.

### Confirmation

`src/arteries/storage.py`, `src/arteries/compile.py`; retention defaults in `src/arteries/doctor.py`.
