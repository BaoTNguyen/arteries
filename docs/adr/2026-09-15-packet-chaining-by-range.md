---
status: accepted
date: 2026-09-15
approved-by: operator
drafted-by: operator
retire-evidence: a chaining failure the covered range can't explain
switch: none
---
# Packets chain by covered range, not by text overlap

## Context and Problem Statement

A compaction packet must say what is new since the previous one. That was
computed from word overlap against the host's previous summary.

## Decision Drivers

* "What is new" must be exact and cheap
* `arteries.packets` already records member ids

## Considered Options

* Each packet stores `previous_id`, `covers_from`, `covers_to` and `resume_from`; the next window starts at `covers_to`
* Word overlap against the host's previous summary (the old way): a guess that misses rewording

## Decision Outcome

Chosen option: "covered range", because chaining becomes arithmetic over a
column instead of string matching.

### Consequences

* Good, because each packet reads exactly the rows since the last one.
* Bad, because a missing previous packet means falling back to session start.

### Confirmation

`migrations/018_packet_chaining.sql`, `src/arteries/storage.py`. Proven by `tests/test_storage.py`.
