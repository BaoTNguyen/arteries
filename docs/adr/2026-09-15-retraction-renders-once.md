---
status: accepted
date: 2026-09-15
approved-by: operator
drafted-by: operator
retire-evidence: corrections observed disappearing before they take hold
switch: none
---
# A retraction renders in one packet; persistent memory carries it after

## Context and Problem Statement

Once a detector fires, how long should the packet keep showing the correction?

## Decision Drivers

* A packet asks persistent memory what is currently true
* No second, parallel memory of what a packet said before

## Considered Options

* Render once; the loser gets `valid_until = now()` at promotion and a `supersedes` edge records why
* Render for two packets, mirroring the demotion window: demotion is about slot staleness, a different question, and needs a new counter

## Decision Outcome

Chosen option: "render once", because the promotion pipeline already makes the
correction a persistent fact.

### Consequences

* Good, because there is no packet-level tracking to keep in sync.
* Bad, because a correction depends on promotion running at `SessionEnd`.

### Confirmation

`src/arteries/packet.py`, `src/arteries/compile.py` (supersede at promotion).
