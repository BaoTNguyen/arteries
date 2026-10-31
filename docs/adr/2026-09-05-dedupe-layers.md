---
status: accepted
date: 2026-09-05
approved-by: operator
drafted-by: operator
retire-evidence: duplicate rates measured by art benchmark
switch: none
---
# Dedupe in three layers: unique index, compiler judgement, retrieval screen

## Context and Problem Statement

The same fact arrives many times, worded differently, across turns, sessions
and projects. Each duplicate costs retrieval slots.

## Decision Drivers

* Exact repeats must never land twice
* Paraphrases need judgement, which can't run on the hook path
* Signal (how often a fact recurs) must survive the merge

## Considered Options

* A: a unique index (sync) + D: compiler judgement (async: in-batch merge, session sweep) + E: a retrieval-time screen
* An intake cosine cut: D covers paraphrase without a threshold on the hook path

## Decision Outcome

Chosen option: "A + D + E". Within a session at ephemeral; across sessions and
projects at promotion, after signal. `seen_sessions` and `seen_projects` survive
the merge and feed incrementality.

### Consequences

* Good, because intake stays a single insert, and judgement runs where a model call is allowed.
* Bad, because paraphrases coexist in ephemeral until the sweep.

### Confirmation

`src/arteries/compile.py` (merge, sweep), the ephemeral unique index in `src/arteries/schema.sql`.
