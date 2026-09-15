# Compaction v3: state fields, watermarks, retraction — rebuilt on the ingestion rework

Status: design, pre-implementation. Written 2026-09-11 against `main` @ 2bf8495
(heart) / arteries `main` post-§27.

Supersedes `planning/compaction_packet_v2.md`. That document was written before the
ingestion rework landed, and four of its assumptions are no longer true. Every claim
about current behaviour below carries a `file:line`. Anything not verifiable in the
repos is marked `[ASSUMPTION]`.

---

## 0. Verdict

1. **Split the builder.** `build_packet` serves two jobs — per-turn retrieval
   injection (`heart/src/heart/episode.py:359`, `art packet --format
   provenance-json`) and compaction continuity (the five CLI hooks). They want
   different layouts from the same loaders. One entry point, two renderers.
2. **The compaction renderer drops provenance-tier headings for state fields.**
   "Ephemeral Memory / Persistent Memory / Scope Memory" (`packet.py:270-273`) tells
   a resuming agent where a fact was stored. It needs to know what the fact *is*:
   objective, constraints, decisions, done / in-progress / blocked, retracted, next.
3. **Chaining becomes arithmetic instead of string overlap.** `arteries.packets`
   already records member ids (`storage.py:444`); add a covered range and read
   "what is new since the last packet" from the range, not from word overlap against
   the host's previous summary (`packet.py:544`).
4. **Retraction is the differentiator, and the ingestion rework moved its
   foundation.** Promotion now runs once at `SessionEnd`, so the compiler's
   `supersedes` / `contradicts` edges (`compile.py:805-893`) do not exist yet for the
   session being compacted. Mid-session retraction has to be detected in the packet
   builder from ephemeral rows plus `tool.result` observations. v2 assumed the edges
   were free; they are free only for *previous* sessions.
5. **Nothing in this needs a model call, a new service, or a destructive migration.**
   One table gains three columns, one gains a range. Everything else is a render
   change over loaders that already exist.

---

## 1. What moved since v2 was written

| v2 assumption | What actually landed | Consequence for compaction |
|---|---|---|
| Defect 1: 55% of budget on `Recent Conversation` | Fixed, and better than v2 proposed: the share is capability-aware — 0.55 when the packet *replaces* the host's summary, 0.15 when it augments (`packet.py:794-830`) | Keep it. v3 inherits `_allocations` and adds a compaction profile rather than a flat number |
| Defect 2: dedupe is substring containment | Replaced with content-word overlap at `SUMMARY_OVERLAP = 0.8` (`packet.py:539-556`) | Still a guess about the host's prose. Watermarks make it unnecessary for the ledger half; keep the overlap check only for the host's own summary text |
| Defect 3: no packet identity, no range | Half landed. `arteries.packets` stores `member_ids` and `recent_packet_members` reads the last two (`storage.py:444-491`) | Ids are enough to *demote* a repeat (`packet.py:_rank_within`). They are not enough to answer "what is new since", because a row created after the last packet has no id in it to be absent from. Needs `covers_from` / `covers_to` |
| Defect 4: contradiction resolution never reaches the packet | Unchanged, and now harder: promotion moved to `SessionEnd` (`ingestion_redesign.md` §16, decided 2026-09-07) | The edges arrive after the session they describe. See §4 |
| Defect 5: within-session contradictions unresolved | Unchanged | Still the gap. Now the *only* place mid-session retraction can come from |
| Defect 6: similarity floor gates retractions | Unchanged in effect, but the floor is now known to be the binding constraint (§27, finding 2 of "what measuring changed") | Retractions must bypass ranking entirely — they are not candidates competing for slots |
| Defect 7: `_score` multiplies by unused confidence | Fixed. Similarity alone; confidence annotates (`packet.py:360-382`) | Nothing to do |
| Defect 8: sections are provenance tiers | Unchanged | The core of v3 |
| Defect 9: 60-second HTTP call in the compaction path | Fixed. Hook path reads a cache; the background compile pass fills it (`packet.py:109-131`) | Verify the compaction path never sets `ARTERIES_CORPUS_INLINE=on`. Note `art packet --format provenance-json` is documented as wanting inline (`packet.py:96-99`) and heart calls exactly that form with a 60s timeout (`episode.py:357-361`) — a latency path worth confirming, not a correctness bug |
| v2 §3.2 precedence ladder ranks on `ephemeral.evidence` | `evidence` landed on **persistent** and **evergreen** only (`migrations/014_evidence.sql`, `schema.sql:404-405`). Ephemeral has no such column | The ladder's key input is missing exactly where compaction needs it. §4.2 resolves this without a schema change |
| v2 §5 suggests a fixed budget split (state 30, decisions 15, …) | The rework rejected fixed per-tier slots in favour of reciprocal rank fusion (`ingestion_redesign.md` §20.4; `packet.py:429-435`) | Fixed byte shares per *section* are still right — sections are not competing for relevance, they are different questions. Fixed slots per *tier* stay rejected |
| v2 assumes hybrid dense+sparse retrieval is coming | Built, measured, shipped off: mrr 0.67 → 0.54 (§27, commit D) | Do not reintroduce it here. Identifier-heavy compaction queries are the one case that would justify it, and the benchmark query set that would prove it does not exist yet |

Two v2 items landed unchanged and remain correct: the delivery matrix (§6) and the
canary token. `cli_caps.py` still carries the right axes and values.

---

## 2. One builder, two jobs

Today one function renders both paths (`packet.py:251`). The jobs differ in every
respect that matters:

| | Retrieval injection | Compaction continuity |
|---|---|---|
| Trigger | every non-triaged user prompt | context exhaustion, or `/compact` |
| Question | "what do we know that bears on *this message*?" | "what must survive so work continues?" |
| Ranking | RRF across arms, floor on the persistent arm | none — the fields are enumerated, not ranked |
| Recency of conversation | the host still has it | the host is about to lose it |
| Budget | 6000 (heart) / hook default | 20000 |
| Failure mode | a weak match wastes 200 bytes | a lost decision costs an hour of re-derivation |

So: keep `_load_memories`, `_arms`, `_fuse`, `_allocations`, the triage skip, the
corpus cache. Add `render_state(...)` beside `render_tiers(...)` and pick by trigger.
The loaders are shared; only the layout forks.

`PACKET_SCHEMA_VERSION` (`packet.py:320`) becomes two versions, or the compaction
renderer gets its own. `art setup` already regenerates the Codex compact prompt when
that number changes — which is the mechanism keeping finding 24 closed, and it must
not be bypassed.

**ponytail:** one module, one new render function, no new class hierarchy. If a third
consumer ever appears, that is when a renderer registry earns itself.

---

## 3. The packet schema

Every field renders always. Empty renders as `(none)` — a missing `Blocked` section is
ambiguity, and ambiguity is what makes a reader re-derive.

| Field | Source (what fills it) | Empty form | Droppable |
|---|---|---|---|
| `packet_id`, `previous_packet_id` | `arteries.packets` (`storage.py:444`); add `previous_id` | `null` = cold start | no |
| `covers_from` / `covers_to` | previous packet's `covers_to`, else session start; `now()` | — | no |
| `resume_from` | last `turn_id` folded in — `ephemeral.turn_id` exists (`ingestion_redesign.md` §3.1) | `(unknown)` | no |
| `objective` | `plexus` `goal.started` payload when `PLEXUS_GOAL_ID` is set (`SPINE.md`); else first user turn in range. **Not** `arteries.decisions` — see below | `(unknown)` | no |
| `constraints` | `persistent` where `kind IN ('preference','constraint')` — the exact set eviction never touches (`evict.py:14`), so these are the stable half of the store | `(none)` | no |
| `decisions` | `persistent` where `kind='decision'`, plus `chose` / `over` edges written at promotion (`compile.py:826-830`). Mid-session: ephemeral atoms the compiler has not seen yet | `(none)` | rationale only |
| `state.done` | ephemeral atoms whose backing `tool.result` event shows a successful mutation (`hooks/arteries-tool.js`) | `(none)` | tail |
| `state.in_progress` | open `episodes` (`status='running'`), plus the highest-`seen_count` ephemeral atoms not yet in `done` | `(none)` | no |
| `state.blocked` | `tool.result` events with non-zero exit in range, and `sandbox.denied` / `guardrail.hit` from the journal (`SPINE.md`) | `(none)` | **never** |
| `retracted` | §4 | `(none)` | **never** |
| `unresolved` | §4, tie case | `(none)` | **never** |
| `open_question` | last assistant turn ending in `?` with no following user turn (`conversation.py` already isolates assistant text) | `(none)` | **never** |
| `files` | journal `file.read` / `file.edit`; failing that, paths parsed out of `tool.result` events | `(none)` | beyond 5 |
| `next` | most recent `in_progress` item, phrased as an action | `(unknown)` | **never** |
| `dropped` | budget overflow record, by name | `(none)` | no |
| `canary` | one short random token | — | no |

**`arteries.decisions` is the wrong table, and v2 named it twice.** Every writer is a
*retrieval-gate* decision — `memory_select.py:296`, `packet.py:170`, `packet.py:177`,
and `eval.py` — so the table holds "did we search the corpus this turn", not "we chose
watermarks over string dedupe". Project decisions live in `persistent` at
`kind='decision'` and in the `chose` / `over` edges promotion writes. Reading
`arteries.decisions` for the `objective` or `decisions` fields would fill a continuity
packet with the memory system's own telemetry.

`file.read` / `file.edit` are not in the `SPINE.md` catalog and are emitted nowhere in
`src/` or `hooks/` — verified. `files` comes from `tool.result` paths only, until
something emits them.

`next` is derived, never stored, and it is the one line most worth getting right.

### What the ingestion rework contributes that no surveyed CLI has

| Mechanism | Where it landed | What it buys compaction |
|---|---|---|
| Atoms + `fact_hash` + `seen_count` | §27 commit 7 (three turns → one row, count 3) | Repetition as a ranking signal. A fact the session kept restating is either what matters or where it is stuck — both belong in a continuity packet. §16's caution ("repetition marks confusion as readily as importance") is a *feature* here: "we have been round this three times" is exactly what a resumed session needs |
| Evidence ladder | `evidence.py`, `migrations/014` | Retraction that is ordered rather than recency-guessed. This is the field none of Claude Code, Codex, OpenCode or Cursor has |
| `PostToolUse` observations | `hooks/arteries-tool.js` — records only failures and mutations, never output | `state.done`, `state.blocked`, and the only `observed` rung the ladder has |
| `access_count` | pre-existing, spread wide (356, 246, 235, 214…) | Carry-forward priority. A claim that keeps being retrieved is worth budget; one never read is not |
| Activity-day clock | `activity.py`, `migrations/013` | A session resumed after three weeks away still has its constraints. Wall-clock retention would have expired them for no reason connected to truth |
| Evergreen graph + scope grouping | `evergreen.py`, `migrations/009` | Cross-repo constraints survive compaction in a *different* repo of the same scope. A harness-wide rule recorded in arteries is present when the compacted session is in heart |
| RRF by arm | `packet.py:429` | The `files` and `state` fields can be filled from incommensurable sources without comparing a hop score to a cosine |
| `arteries.packets` | `migrations/012` | Chaining. Extend, do not rebuild |

---

## 4. Contradiction and retraction

### 4.1 What is knowable when

This is the hole v2 could not have seen, because promotion ran per turn when it was
written.

| Signal | Available mid-session? | Why |
|---|---|---|
| `supersedes` / `contradicts` edges | **No** — only for previous sessions | Written in `_write_results` at promotion, which now runs once at `SessionEnd` (§16) |
| `persistent.evidence` | Previous sessions only | Set at promotion (`compile.py:730-793`) |
| Ephemeral atoms | Yes | Written per turn by intake, no model call |
| `tool.result` events | Yes | `PostToolUse`, synchronous |
| User turn text | Yes | `agent_events` |

So compaction retraction runs on the three rows that *are* available, and reads the
edges only for cross-session contradictions. That split is not a compromise: the
edges are the record of resolutions already made, and the detectors are for the ones
this session made and nobody has adjudicated yet.

### 4.2 Detectors — cheap, deterministic, no model call

1. **Existing supersede edge** (`memory_edges WHERE rel IN ('supersedes','contradicts')`).
   Free; already written; never rendered. `contradicts` is already labelled through
   `MemoryItem.via` (§27 commit A), so both sides are identifiable.
2. **Refutation by tool result.** v2 deferred this as "highest-value, needs claim→command
   pairing we do not have". The `PostToolUse` hook now records exit codes, so the pairing
   is: an ephemeral atom asserting success (`tests pass`, `it works`, `fixed`) in the same
   turn window as a non-zero exit on a matching tool or path. Promote this to a v3
   detector — it is the one that catches the most expensive class of wrong belief.
3. **Value overwrite.** Two atoms with cosine ≥ 0.85 whose extracted literals differ
   (a number, path, flag, version, or quoted identifier). Reuse `extract._NAMED`.
4. **User correction.** A user turn opening with a negation marker (`no,`, `actually`,
   `that's wrong`, `I meant`, `not `) pairs against the assistant claim in the
   immediately preceding turn.

Optional and last: **semantic negation** via the compiler, only on pairs above the
cosine threshold that 2–4 did not classify, and only in the background pass. If the
generator is unreachable — 44% historically, now gated by a health probe
(`compile.py:106`) — these pairs go undetected and nothing else degrades.

### 4.3 Precedence

```
user > observed > stated > inferred
```

`evidence.rank()` already implements the ordering (`evidence.py:34`). Mid-session,
the class is derived rather than read: a user turn is `user`, a `tool.result` is
`observed`, an assistant atom is `stated`, a compiler-derived claim is `inferred`.
`evidence.for_source()` already does this mapping for the batch case
(`compile.py:740`) — reuse it, do not add `ephemeral.evidence`. One less column, and
the derivation is identical to the one promotion will apply later.

- Across classes the stronger wins regardless of order. A later inference never
  overturns an earlier observation.
- Within a class, later wins. Two observations of the same file are a change, not a
  dispute.
- A user statement is overturned only by a later user statement.
- Genuine tie → `unresolved`, both sides rendered with a note. Never silently pick.
  A wrongly retracted true fact is worse than a carried-forward stale one, because
  the retraction is believed.

### 4.4 Near-miss guard

Runs before adjudication; its output is a drop, never a retraction. Not a
contradiction when: different subject (differing path, entity, identifier token);
different scope (one about a file, one about a config default); different time and
both marked as such — that is a value change, rendered under `decisions`; or one is a
subset of the other rather than its negation.

The worked example from v2 §4 stays valid and is worth keeping as the regression
case: `compact-packet.sh` leaves `RERANKER_DEVICE` unset, `.arteries/env` sets it to
`cuda:1` — cosine 0.91, differing literals, detector 3 fires, subject guard drops it.
Both are true.

### 4.5 Lifetime

Two packets, then it stops. Loser gets `valid_until = now()` at promotion; a
`supersedes` edge with `metadata.reason` records why; the retraction line renders for
the rest of the session and in the next packet. A permanent retraction list grows
without bound and eventually costs more budget than the re-derivation it prevents.

---

## 5. Assembly and budget

```
build_compaction_packet(session, cli, budget):
    prev      = packets.latest(project, session)          # + covers_to
    window    = (prev.covers_to if prev else session_start, now())
    rows      = ledger rows in window                     # ephemeral, decisions, events, episodes
    conflicts = detect(rows) + edges(prev sessions)       # §4
    fields    = render(rows, conflicts)
    fit(fields, budget)
    packets.insert(id, previous_id, covers_from, covers_to, resume_from, member_ids, body)
```

Two indexed queries and a render. Measured comparator: the current packet builds in
141 ms (§27 commit E) against a summarisation call for all four surveyed CLIs.

**Never dropped:** `retracted`, `unresolved`, `open_question`, `state.blocked`,
`next`, `canary`. If those alone exceed the budget, emit them and list the rest under
`dropped` by name — never drop the oldest item silently, which is Codex's failure mode
and is information loss disguised as success.

**Drop order:** `files` beyond the five most recently touched → `decisions` rationale
(keep chosen/rejected) → `state.done` tail → recent-conversation excerpts, last and
only when the host keeps them (`can_replace_compaction=False`).

Section shares extend `_allocations` with a compaction profile. The existing function
already branches on `can_replace_compaction` and already documents why shares sum to
0.96 rather than 1.0 (`packet.py:816-819`) — keep both properties. The numbers are a
guess; `art benchmark` re-derives them.

---

## 6. Delivery

The body is byte-identical across all six. Only delivery varies. `cli_caps.py` already
carries the axes.

| CLI | Mode | Mechanism | On failure | Verify |
|---|---|---|---|---|
| pi | replace | `packet --format pi-compaction-json` (`packet.py:69`) | fall back to augment | canary |
| opencode | replace | plugin `experimental.session.compacting` (`README.md:559`) | override the compact prompt | canary |
| codex | override | PreCompact hook rewrites `.arteries/codex/compact_prompt.txt` with the packet inlined | stale prompt file → previous packet, logged | canary |
| claude | augment | `SessionStart(matcher: compact)` stdout — the only stdout the model sees; PreCompact stdout is discarded | write file + AGENTS.md pointer | canary |
| cursor | none | write `.arteries/packet.md`, point the rule file at it | — | canary |
| hermes | none | `HERMES.md` pointer, conservative until its hook model is verified | — | canary |

**Canary:** each packet embeds one short random token; grep the next turn's transcript
for it. Three of the six can fail silently, and a delivery mode that does nothing is
otherwise indistinguishable from one that worked.

The Claude Code sample in `.compaction-corpus/claude-compact-6a0324cb.md` is worth
keeping as the shape reference: eight numbered sections, requests in order,
file-by-file with code excerpts, and an explicit "current, unanswered" item. What to
take: the ordered request list and the unanswered-item marker. What not to take: the
code excerpts. They are the bulk of that summary's bytes and the host CLI can re-read
the file; a packet that re-sends source is spending continuity budget on something
`cat` recovers.

---

## 7. Cross-stack impact

### arteries

Owns all of it. Schema changes are additive only while two checkouts share one
Postgres (`ingestion_redesign.md` §8):

```
arteries.packets: + previous_id, covers_from, covers_to, resume_from, body
```

All nullable or defaulted. Nothing is dropped, nothing renamed. §28's lesson applies
to the ordering: expand → migrate → **stop naming the old thing** → contract, and the
precondition is `grep -rn` showing no source names the old column, not "main still
works".

`PACKET_SCHEMA_VERSION` bumps, which makes `compact_prompt_stale` true until `art
setup` runs — already the case on live (§27, "Still open"), and the check firing is
the mechanism working.

### capillaries

Untouched, and deliberately. The corpus suggestion stays behind the cache
(`packet.py:109`), the gate stays in arteries (`GATE_COVERAGE_ABSTAIN`), and
`MemoryFrame` keeps its shape — so `frame_compat.py` needs no third rename. The one
thing to confirm: the compaction path must not set `ARTERIES_CORPUS_INLINE=on`. A
60-second corpus call is survivable on a retrieval turn and fatal on a compaction
hook, which is the one build that has no second chance.

### heart

Two contacts, both real.

1. `_context_packet` (`episode.py:325`) shells out to `art packet --budget 6000
   --format provenance-json`. That is the **retrieval** path, and §2's split must keep
   it on the tier renderer — a role's context packet is not a resumption document.
   Role memory policy (`clean` for the test role) is honoured there and must stay so.
2. **Compaction inside a sandbox cannot build a packet.** The context mount is
   read-only, network is always none, and the container holds no credential
   (`sandbox.py:504`, `sandbox.py:615`). An agent CLI that compacts mid-episode
   therefore cannot call `art`. Two consequences: the pre-built packet in `/context`
   must be written in a form the host CLI's own compaction is instructed to preserve
   (the AGENTS.md pointer, same as Cursor's delivery), and verifiers still get no
   `/context` mount — a verifier that can read the continuity packet can be steered
   by it, and that is a reward-integrity property, not a security one.

Two live defects found while answering §12, both in this path and both cheap:
heart's episode packet has never carried a corpus suggestion (§12.2), and packet
chaining is inert inside episodes because no session id is stamped (§12.3). Neither is
caused by v3; both are fixed by three environment variables at `episode.py:355`.

heart stays stdlib-only and keeps shelling out. No new event kinds are strictly
needed; `packet.built` with `covers_from`/`covers_to` would be additive and would let
`pulse` see chaining gaps. Additive-only and tolerant readers make that safe
(`SPINE.md`).

### plexus

`objective` is the field where plexus pays off. When `PLEXUS_GOAL_ID` /
`PLEXUS_FEATURE_ID` are in the environment, heart already stamps them onto every event
(`SPINE.md`), so a compaction packet inside a feature attempt can name the goal rather
than guessing from the first user turn. Task ids are conventionally
`<goal_id>-<feature_id>-a<attempt>`, which also means a packet chain can be assembled
across the attempts of one feature — worth having, not worth building first.

### marrow

One hazard and one requirement.

- **Comparability.** Packet provenance records the fused *rank*, not a score, precisely
  because ranks survive changes in arm sizes (`packet.py:476-480`). A layout change
  does not break that, but the provenance rows must carry the renderer version, or
  episodes recorded before and after v3 look identical and are not. §18's warning
  about rescaling applies verbatim: a no-op for ranking is not a no-op for training
  data.
- **Episode exclusion.** Evergreen and persistent rows carry `episode_id` so a
  retriever cannot train on its own previous solution (`schema.sql:98-102`,
  `ingestion_redesign.md` §4.4). A compaction packet is a *third* place that copy
  could outlive the exclusion — it is written to disk, mounted into a container, and
  fed back as context. The `retracted` and `state.done` fields are the risky ones.
  Tag packet bodies with the episode that produced them and exclude them the same way.

---

## 8. Measurement

Four checks, in this order. The first three are free.

1. **Canary presence.** Percentage of compactions where the token appears in the next
   turn. Any delivery mode below 100% is broken, not degraded.
2. **Re-derivation count.** After compaction, how many of the next five turns re-read
   a file already in `files`, or re-run a command whose result is already in
   `state.done`. This is the metric the whole feature exists to move, and it is
   computable from `tool.result` events with no labelling.
3. **Retraction precision, by hand.** Every `retracted` line over the first week, read
   and classified. A false retraction is the worst failure this design can produce —
   the agent is told a true thing is wrong — so this is a gate, not a dashboard.
4. **`art benchmark`** re-derives the section shares and `MAX_PACKET_MEMORIES`.

**The trap.** §21.5 and §27 both record it: hybrid retrieval's first measurement was
37/40 against 21/40, which was a list of 21 compared against a list of 1. Before/after
sets here are incomparable for the same reason — v3 renders fields v1 has no analogue
for, so "more lines survived" is not a result. Fix the comparison to re-derivation
count on matched sessions, and save a baseline before the first commit lands, the way
commit C did.

Also: two `worth_keeping` rules were discarded after running them against the store
(§27), one of which refused 150 of 527 good rows. Run every detector in §4.2 in
dry-run over the existing ephemeral corpus before it can retract anything.

---

## 9. Edge cases

| Case | Behaviour |
|---|---|
| Cold start, no prior packet | `covers_from` = session start, `previous_packet_id` = null. Full build |
| **Compaction before any promotion has run** | The normal case now, and v2 did not consider it. Edges and `persistent.evidence` are empty for this session; detectors 2–4 carry retraction; `constraints` comes from previous sessions and may be empty on a project's first session, when there is nothing to be missing |
| **Compaction inside a network-less sandbox** | No `art` call is possible. The packet in `/context` is what exists; staleness is bounded by how recently heart built it. The host CLI is instructed to preserve it. Never a build attempt that fails |
| Session resumed days later | `ARTERIES_SESSION_ID` is stable across resume; ephemeral retention is session end + 48h grace on a 14-day wall-clock bound (§3.5), so the working set is intact tomorrow and correctly gone in a month |
| Retraction itself retracted | Retraction is voided; both claims go to `unresolved` with the chain named |
| Host compacts with no hook | Cursor's normal case. The file on disk is the packet. Build every N turns, not only on compaction |
| Ledger and transcript disagree | Ledger wins for state, transcript for verbatim quotes (`open_question`). The transcript is what the model saw; the ledger is what was observed |
| Compaction fires during compaction | Unique on `(session_id, covers_to)`; the second build returns the first |
| Both sides `inferred` | Later wins, and the line says `(inference over inference)` so the reader knows the ground is soft |
| Generator unreachable | Detectors 1–4 need no model. Only semantic negation is lost, and only for pairs the others did not classify |
| Packet over budget | §5. Explicit `dropped` list, never silent |
| `seen_count` high because the session was stuck | Rendered under `state.blocked` or `in_progress`, never as a `done` fact. Repetition is evidence of attention, not of truth |

---

## 10. Commit sequence

| # | Commit | Why here |
|---|---|---|
| 0 | Baseline: re-derivation count on the last N compactions, saved | Every commit after this changes what a packet contains. The before number has to exist first — commit C's lesson |
| 1 | `packets` gains `previous_id`, `covers_from`, `covers_to`, `resume_from`, `body`; `record_packet` writes them | Additive migration, no reader change |
| 2 | Split the renderer; compaction path renders state fields from existing loaders; retraction fields present but always `(none)` | Ships a better layout with no new detection risk. Verifiable by canary alone |
| 3 | Detectors 1 and 2 (existing edges; refutation by tool result), dry-run flag first | Highest value, and 2 is the one v2 deferred and the `PostToolUse` hook unblocked |
| 4 | Detectors 3 and 4, precedence via `evidence.for_source`, near-miss guard | The adjudication half. Gated on §8 check 3 |
| 5 | Delivery: Codex prompt regeneration, Claude `SessionStart(compact)`, Cursor/Hermes file pointer, canary everywhere | Delivery last, because a canary is only meaningful once there is something worth delivering |
| 6 | ~~heart: `ARTERIES_CORPUS_INLINE=on`, `ARTERIES_CORPUS_TIMEOUT=20`, `ARTERIES_SESSION_ID=task_id` at `episode.py:355`~~ **landed 2026-09-13**, `TestContextPacketEnv`; AGENTS.md pointer in `/context` so in-sandbox compaction preserves the packet | §12.2 and §12.3 were independent of the rest and went first |
| 7 | Re-derive shares and caps from `art benchmark`; correct the guesses | After the corpus has moved |

## 11. Not building

- **`ephemeral.evidence`.** Derived from source with `evidence.for_source`, which
  promotion already uses. A column whose value is computable from data in the same row
  is a second place to keep in sync.
- **Hybrid dense + sparse retrieval.** Measured and shipped off. Revisit only with a
  benchmark query set containing identifier queries — the gap §27 names as still open.
- **Per-CLI packet bodies.** One body, six deliveries. A CLI needing a different body
  is a delivery bug first.
- **A model call anywhere on the compaction path.** Semantic negation runs in the
  background pass or not at all.
- **A retraction ledger UI.** `art trace` already reads `memory_edges`.
- **Code excerpts in the packet.** The host can re-read the file.
- **Tuning §5's shares before measuring them.**

---

## 12. The three questions, decided

### 12.1 Mid-session decisions: no new column, no intake classifier

Verified: `kind` exists on `persistent` only (`schema.sql:133`); `ephemeral` has no
such column and `extract.py` does no classification at all. A marker at intake means a
classifier on the hook path, which is the one thing this design refuses everywhere
else.

So `decisions` is filled from three signals already present, in order:

1. `persistent.kind='decision'` plus the `chose` / `over` edges promotion writes
   (`compile.py:826-830`) — everything settled in *previous* sessions.
2. Ephemeral atoms with `source='user'` matching a choice marker (`use X`, `don't`,
   `instead of`, `let's`, `switch to`, `rejected`, `go with`). Same detector family as
   §4.2's user-correction rule, so one shared marker list, not two.
3. `seen_count` orders them. Never gates them.

**Ceiling accepted:** mid-session decisions render without the *why*, because the
rationale is prose the compiler has not read yet. The next packet — built after
`SessionEnd` promotion — carries the structured version with its reason. Mark it with
a `ponytail:` comment naming the upgrade path: if §8's re-derivation measurement shows
decisions specifically being lost, add `kind` to ephemeral and pay for the classifier
then.

This hurts least where it is most likely: under `augment` delivery (claude, codex,
cursor) the host keeps the conversation, so the rationale survives in its own summary.
Under `replace` (pi, opencode) it does not — so the reclaimed budget from §5 should go
to `decisions` in the replace profile specifically.

### 12.2 heart's episode packet has no corpus suggestion, and never has — **fixed 2026-09-13**

Confirmed, and it is a live defect rather than a question. The chain:

- `build_packet` calls `_corpus_suggestion(..., inline=False)` (`packet.py:266`).
- With `CORPUS_INLINE` off it reads cache only, returning
  `{"status": "not_cached"}` on a miss (`packet.py:165-168`).
- The cache's only writer is `warm_suggestion` (`packet.py:119`), called from the
  detached compile pass when `ARTERIES_WARM_MESSAGE` is set (`compile.py:952-957`).
- The only setter of that variable is `eval.py:342` — the hook path wrapper that
  `.arteries/hooks/arteries-observe.cjs:71` invokes.
- heart does not go through `arteries.eval`. It shells out to `art packet` directly
  with `_situation(task)` (`episode.py:354-361`), a string no hook ever saw, so its
  cache key is never warmed.
- Result: `corpus.status` is `not_cached`, `episode.py:372-378` only emits on `ok`, and
  `decision.retrieval.corpus` has never fired from an episode.

Nothing in any of the five repos sets `ARTERIES_CORPUS_INLINE` — verified by grep.

**Fix: set `ARTERIES_CORPUS_INLINE=on` in the env heart already builds at
`episode.py:355`.** The flag exists to keep a 60-second call off a 9-second hook.
heart is not a hook: it is a background orchestrator that already allows
`timeout=60` on this subprocess, and nobody is typing while it runs.

**And set `ARTERIES_CORPUS_TIMEOUT` below heart's subprocess timeout.** Both default
to 60 (`packet.py:96`, `episode.py:361`), so a slow corpus does not degrade the corpus
half — it times out the whole `art packet` call, `episode.py:363` returns
`status: failed`, and the episode runs with **no memory at all**. 20 seconds leaves
room for the rest of the build. This cliff exists today; turning inline on is what
makes it reachable, so the two changes ship together.

Measured on live before and after, same message, `heart` project:
`{'status': 'not_cached', 'coverage': 0.0}` in 0.27s → `{'status': 'no_match',
'confidence': 0.0}` in 1.96s. The gate now actually runs. A corpus-shaped query
returns `{'status': 'ok', 'mode': 'needs_context', 'title': 'Multi-Agent Scope
Design', 'confidence': 0.999, 'trace_id': 'pf_tr_1c5d47115761'}` — and the trace id
is what the feedback half of the loop attaches an episode outcome to, so it was not
only the prompt that was missing.

Rejected: having heart warm the cache itself. That makes heart know about arteries'
background compile pass — a layering violation and a second scheduler, to avoid one
environment variable.

### 12.3 Plexus attempts: `ARTERIES_SESSION_ID = task_id`, so chaining starts clean per attempt — **fixed 2026-09-13**

The question was malformed, and finding out why answers it. heart stamps
`ARTERIES_EPISODE_ID` and `ARTERIES_TASK_ID` (`episode.py:355`, `episode.py:633`) but
never `ARTERIES_SESSION_ID`. So `record_packet` falls back to `_env_session_id()`,
gets `None` (`storage.py:33-41`), and writes a row with a NULL session. Then
`recent_packet_members` returns an empty set the moment session is None
(`storage.py:479-481`). **Chaining is inert inside heart today** — every episode
packet is a cold start, and the demotion of already-shown claims never fires.

So the decision is what identity heart stamps. Stamp the task id, which by plexus
convention is `<goal_id>-<feature_id>-a<attempt>` (`SPINE.md`). That gives:

- **Chaining within an attempt.** The implementer, test and review roles of one
  attempt share a chain, so role 3's packet does not re-send what role 1's already
  did. That is what continuity means at this scale.
- **A clean start at each attempt.** `a2` inherits nothing from `a1`.

Clean per attempt is the right default for two reasons. An attempt is a retry after
failure, so the content most likely to carry forward is the belief that failed. And
attempts are the unit RL compares — leaking `a1`'s packet into `a2` makes their
contexts differ in a way reward cannot see, which is the contamination class
`episode_id` exclusion already exists to prevent (`schema.sql:98-102`).

What is genuinely worth carrying between attempts — "a1 died on a migration lock
timeout" — travels by the two paths built for it: promotion to persistent at session
end, `episode_id`-tagged and excluded from RL retrieval, and plexus's own
`escalation.raised/resolved` events. The packet chain does not need to duplicate
either.

Verified on live: two packets under one stamped session make
`recent_packet_members` return 27 ids for the next build to demote, against a set
that was empty for every episode heart has ever run.

One consequence to accept: role memory policy still wins. The test role runs
`clean` (`episode.py:341-344`), so it gets no packet and therefore no chain, which is
correct and must survive this change.
