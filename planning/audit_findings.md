# Arteries audit — open findings

Compiled 2026-09-04 from a live audit against the `capillaries` Postgres and
the installed CLIs. Every item marked **measured** has a query or code path
behind it; **read** means found by reading code but not exercised against data.

Nothing here has been fixed. Severity is about damage to memory correctness,
not effort.

---

## P0 — data corruption

### 1. Stale-claim sweep measures the wrong clock
`compile.py:_release_stale_claims` — **measured**

```sql
WHERE status = 'compiling'
  AND source_ts < now() - (%s || ' minutes')::interval
```

`source_ts` is when the ephemeral row was *written*, not when it was claimed.
There is no `claimed_at` column (confirmed against the live schema). Any row
that queued for more than `STALE_CLAIM_MINUTES` is born stale, so the next
pass's sweep releases it while the holding worker is still mid-generation.
`FOR UPDATE SKIP LOCKED` does not help — the claim commits before the HTTP call.

Result: two passes compile the same batch and both write.

Evidence: 70 ephemeral rows contributed to persistent facts written in more
than one pass. One batch, 2026-09-01:

```
eid 740e78eb… | 8 facts | 08:41:23.607 → 08:41:37.437 | gap 13.8s
```

Five rows, two passes, 13.8s apart — far under the 2-minute threshold.
Eight persistent facts where there should be four. `_reject_duplicates` does
not catch it: two independent compilations of identical input produce
differently-worded facts that clear 0.97 cosine.

Fix: add `claimed_at TIMESTAMPTZ`, set it in `_claim_ephemeral`, sweep on it.

### 2. Assistant capture discards 72% of content — FIXED 2026-09-04
`assistant.py:capture_response` — **measured**

```python
prior = recent_turns(limit=1)
user_turn = prior[-1] if prior else ""
stored = store_assistant_response(text, user_turn)
```

`conversation.recent_turns` filters roles `{"user","assistant"}` and returns
`turns[-limit:]`. At Stop-hook time the last turn is the assistant's own
response, so the restatement stripper is handed the assistant text as its own
"user question" and the overlap filter deletes everything.

Evidence: 432 `memory.assistant.stored` events, 312 stored nothing. 441 lines
dropped by the overlap filter vs 2 by narration. `ref_chars == input_chars` in
20/20 instrumented events, all with `stored: 0`. Proven by direct execution:
with a correct `user_turn` the stripper keeps the text; with self as
`user_turn` it returns `''`.

**Fixed.** `conversation.recent_user_turns()` added (mirroring the existing
`recent_assistant_turns`), `capture_response` now calls it, and the old
`recent_turns` was deleted — the repo's own `doctor` unreached-check flagged
it the moment `capture_response` stopped calling it, and it had no other
callers. Its test now covers `recent_user_turns`. 298 passed, 7 skipped.

Recovery, replaying the corrected stripper over all 465 captured responses:

```
BUGGY   kept     1,148 chars    non-empty  76/465  (16%)
FIXED   kept   615,035 chars    non-empty 463/465 (100%)
```

535x the content, 387 more responses captured — and that is measured against
the 2000-char previews, so the true figure is larger.

---

## P1 — memory quality

### 3. No quality filter exists on the write path
`compile.py` — **read**

Between the model's JSON and a permanent row there are exactly two checks:

- `validate_response` — purely structural (non-empty `fact`, `kind` in enum,
  `duplicate_of` parses as UUID, `rel` known). Nothing about worth.
- `_reject_duplicates` — a *redundancy* check. Asks "do we have this?", never
  "should we have this?"

`confidence` is stored (`mem.get("confidence", 0.8)`, line 576) and never
gated on anywhere. Novel noise passes at any store size; on a cold start
`_reject_duplicates` fails open and *everything* the model names is written.

### 4. Confidence carries no signal
**measured**

```
0.6 |   3      0.9 | 198
0.7 |   9      1.0 | 118
0.8 |  48
```

84% of the 376 active facts sit at ≥0.9. `packet.py:264` multiplies retrieval
score by it (`sim * confidence * TIER_WEIGHT`), which is a no-op at this
distribution. A confidence floor cannot be used as the fix in (3).

### 5. `COMPILE_SYSTEM` biases toward inclusion on a false promise
`compile.py:47` — **read**

Rule 4: *"Do not skip a new fact merely because something similar exists.
Exact restatements are filtered mechanically after you answer."*

Five inclusion instructions, zero exclusions. The permissiveness is justified
by a downstream filter that is only a 0.97 cosine check, and on a cold start
does not exist at all.

Fix: add exclusions. Nothing derivable from the repo; nothing that only
matters to the current conversation.

### 6. Persistent store is contaminated
**measured**

~11–17% of live rows are transient session intent ("User is verifying
that…"). 14 rows are cross-project (SmartCity/AgriTwin filed under
arteries/heart), including a one-session instruction stored as a permanent
`constraint`. Direct consequence of (3) and (5).

### 7. No eviction
**read**

`access_count` is tracked and never acted on. Nothing decays. Write-side
filtering will never be perfect, so read-side decay is what bounds the error.
Codex uses `max_unused_days` for exactly this.

---

## P2 — in-session retrieval

### 8. Promotion deletes the session's own working memory
`storage.py:get_ephemeral` — **measured**

```sql
WHERE ... AND status = 'uncompiled' AND valid_until IS NULL
```

Compilation marks rows `cleared`, so a promoted row disappears from the tier
that serves in-session recall. Current live state: **599 cleared, 28
uncompiled** — the entire working set across the whole project is 28 rows.

Promotion lag over 2030 rows:

```
min 0.16s | median 3d 13h | p90 5d 10h | max 13d
```

Bimodal. When llama-server is up, promotion is effectively instant and the
row is visible to its own session for a fraction of a second. The multi-day
median is backlog from the 44% failure rate (see 13). **In-session recall
currently works only while the compiler is broken.**

Fix: gate visibility on an expiry/session state, not on compile status.
Compile stays background and stops cannibalizing the tier it reads from.

### 9. Sessions share ephemeral memory
**measured**

518 of 612 ephemeral rows are pooled under three fixed `*-hook`
`agent_process_id` values, so separate sessions read each other's working
memory. There is no `session_id` column.

Root cause is a literal default repeated across every hook script:
`export ARTERIES_AGENT_ID="${ARTERIES_AGENT_ID:-arteries-hook}"`.

**The host's session id is already available.** `assistant.response` event
payloads carry it directly:

```json
{"prior_turn": true, "session_id": "2d02562d-1fdb-425d-b70d-5e2a87702947", ...}
```

So Fix B does not need to parse the hook payload — the identifier is already
flowing through the event stream.

---

## P3 — compile loop robustness

### 10. Poison batches block the queue forever
`compile.py` — **read**

`_release_claimed` fires immediately on failure and `_claim_ephemeral` orders
`source_ts ASC`, so a batch that fails deterministically is reclaimed on the
very next pass, always as the oldest ten. No attempt counter exists, so
nothing behind it ever compiles.

Codex's `jobs` table solves this with `retry_remaining` alongside
`lease_until` and `input_watermark`/`last_success_watermark`.

### 11. Timeout grazes the stale threshold
**read**

`COMPILE_TIMEOUT` is 60s and the invalid-response path calls `_llm_compile`
twice — up to 120s, exactly `STALE_CLAIM_MINUTES = 2`. Even after (1) is
fixed, that path can trip the sweep. 3 minutes gives it room.

### 12. Cold start wastes an embedding call
`compile.py:_load_persistent_context` — **read**

```python
vec = embed_text_sync(" ".join(r["fact"] for r in batch)[:4000])
```

Embeds up to 4000 characters through Qwen3 *before* discovering the
persistent table is empty. Not a correctness bug; a wasted round trip on
every batch until the store fills.

### 13. Compilation has a hard availability dependency
**measured**

269 of 606 passes fail (44.4%): 158 "All connection attempts failed" (local
llama-server down), 83 empty error, a few JSON truncations at ~2500 chars.
Batches are released and retried so nothing is lost permanently, but
promotion stops entirely while the 27B is off.

### 25. Failed connections churn the queue instead of backing off
`compile.py:compile_once` — **measured**

Nothing checks whether the generation server is reachable before work is
claimed. While llama-server is down, every turn spawns a compiler that claims
10 rows, flips them to `compiling`, fails to connect, and releases them back
to `uncompiled`. No progress, two writes per row per turn, for as long as the
outage lasts.

The failures arrive in hour-long bursts — 16, 15 and 14 in a single hour —
which is a process being down for stretches, not per-request contention.
Confirming this: the error is `All connection attempts failed`, a TCP connect
failure, and there are **zero** read timeouts in the distribution.

Cheapest fix in the file, and the only item here that addresses the 44%
failure rate in (13) directly:

```python
try:
    httpx.get(GENERATE_URL.rsplit("/v1/", 1)[0] + "/health", timeout=1.0)
except Exception:
    return {"status": "generator_unreachable", "claimed": 0}
```

Before `_claim_ephemeral`. One second of budget to skip the whole cycle.

Note this is availability, not concurrency. Serializing compilation does
nothing for it, and the real repair is outside arteries: a systemd unit with
`Restart=always` on llama-server, or a deterministic degradation path when the
27B is unreachable.

### 14. Hook timeout mismatch
`hooks/arteries-observe.js` vs `hooks/hooks.json` — **read**

The JS gives Python 5000ms; `hooks.json` allows 10s. Embedding call, DB write
and corpus retrieval all have to finish inside the shorter budget or the
write is killed mid-flight.

---

## P4 — graph

### 15. `contradicts` co-retrieves both sides of a conflict
`graph.py` — **measured**

`contradicts` is in `RELATIONS`, so `graph.expand` pulls both statements into
the packet as identical-looking bullets, and `via_graph` is dropped at the
`MemoryItem` boundary. The packet presents A and ¬A with no marking.

### 16. All edges are unvalidated
**measured**

`ontology_valid=false` on all 2457 edges.

### 17. `derived_from` is a cartesian product — FIXED 2026-09-04
`compile.py:_write_results` — **measured**

`for eph_id in claimed_ids` nested inside `for mem, vec in zip(...)`, so every
new fact linked to every claimed row regardless of actual provenance. The same
`claimed_ids` was also written to `parent_ids` and used for the episode/task
agreement subqueries, so the fiction reached three places.

**Fixed.** The compile prompt now asks for `"from"`: the 1-based numbers of the
records each memory was distilled from. `validate_response` checks they are
integers within the batch, and `_write_results` uses that subset for the edges,
for `parent_ids`, and for the episode/task subqueries.

Verified against the MoE on a live 8-record batch:

```
memories: 6   with usable "from": 6/6
avg sources per fact: 1.33   (cartesian would be 8)
```

Attribution is visibly correct -- `from=[1,2]` grouped the two subtask-logic
questions, `from=[7,8]` the two end-to-end questions.

When the model omits the field the old behaviour is kept, so a silent model
never costs lineage that previously existed, and a
`memory.compile.unattributed` event records how often that happens. Watch that
counter: if compliance drops, the fallback is quietly restoring the fiction.

The cross-encoder was considered and rejected for this. It separates well
(top match median +8.50 against +8.75 max for non-top links), but scoring
inside `_write_results` would put network I/O back inside the write
transaction -- the exact problem the batched-embedding comment above it
records fixing.

Structurally, supersedes chains are sound: 17 winner-live/loser-dead, 3
both-dead, 0 inconsistent.

---

## P5 — packet builder

### 18. 55% of the budget goes to what the host already has
`packet.py:_allocations` — **read**

```python
"recent": int(budget * 0.55),
```

`Recent Conversation` is the one section every host CLI retains verbatim.
Claude's third compaction prompt and Cursor's two watermarks both exist
specifically to avoid re-sending it.

### 19. Nothing chains
`packet.py` — **read**

`previousSummary` is read for dedupe and never written back. No record of
which rows entered which packet, so packets cannot be incremental.

### 20. Network call inside the compaction path
`packet.py:_corpus_suggestion` — **read**

`urlopen(req, timeout=60)` runs during packet assembly.

### 21. Dedupe is substring containment
`packet.py:_dedupe_memories` — **read**

Containment on `_norm()` text. Misses paraphrase, over-merges on prefixes.

---

## P6 — foundations

### 22. No observations are captured anywhere
**measured**

All 25 `agent_events` types are memory/prompt/turn bookkeeping. Zero tool
results, file edits, or command exits. The `user > observed > stated >
inferred` evidence ladder therefore has **one populated class**, so
contradiction resolution degrades to recency — the exact failure the design
exists to beat.

### 23. Stale docstring understates loss by 2x
`compile.py:_reject_duplicates` — **measured**

Docstring claims the LLM pass is a decomposer at 53 ephemeral → 76 facts
(1.43/row). Live: 763 claimed → 475 written = **0.62 facts/row**.

### 24. Codex compact prompt names the v1 layout
`.arteries/codex/compact_prompt.txt` — **read**

Must be regenerated if the packet schema changes.

---

## Closed / not a defect

- **94 extractions writing zero rows, 2026-08-17 to 08-23.** Historical, not
  live. Zero occurrences from 08-24 onward, matching commit `b75b5e7`
  (2026-08-22) "Store turns whole; compress assistant replies by selection".
- **Ephemeral per-message, verbatim, synchronous capture.** Correct for a
  disposable in-session tier. The CLIs surveyed have no equivalent tier; the
  granularity and immediacy critiques apply to persistent only.
- **Cold-start duplicate rejection failing open.** Correct behaviour on its
  own — there is nothing to duplicate. Only a problem combined with (3).
- **Unscoped stale sweep.** Deliberate and right: a claim is stranded exactly
  when its process is gone, so scoping the sweep to the caller means nobody
  releases it.

---

## Retracted analysis: the "barren message" study

An earlier pass reconstructed ephemeral-to-persistent attribution with the
cross-encoder (working around the cartesian `parent_ids` of finding 17) and
labelled each user message by whether it ever produced a persistent fact that
was read. It found 269 of 494 cleared user messages (54%) produced nothing,
and that word count separated the two groups threefold.

**Those results are invalid.** They measured finding (2), not message quality.

Re-running the real `COMPILE_SYSTEM` prompt over 30 of the "barren" messages,
once alone and once with the answer recovered from `assistant.response`
events:

```
facts from question alone     :  6   (0.20/msg)
facts from question + answer  : 92   (3.07/msg)
lift: 15.3x
```

The messages were never useless. They produced nothing because the half of
the exchange carrying the content was deleted before compilation. Word count
appeared to separate only because long *questions* sometimes carry their own
content while short ones depend entirely on the answer -- and the answer was
gone.

Two consequences:

- **Do not build a stricter message-to-ephemeral gate on these labels**, and
  do not train a classifier on them. Re-derive after new data accumulates
  under the fix.
- The feature that looked most promising by intuition -- "contains a code
  identifier or path" -- had **exactly zero** discriminating power (0.09 in
  both groups) even before the labels were known to be wrong.

---

## Design claims contradicted by measurement

`planning/session_001_design_exploration.md` anticipated multi-agent
concurrency — it is the stated reason Postgres was chosen over SQLite
(line 280). The architecture is right. Six specific claims in that document
no longer match the live system, and they are recorded here so the two files
stop disagreeing silently.

| Claim | Where | Status |
|---|---|---|
| "the **narrow race window** where both compile simultaneously is **self-correcting** on the next pass" | L143 | **False, both halves** — see (1) |
| "Records marked `status = compiling` when async call starts — **prevents double-compilation**" | L148 | **Mechanism defeated** — see (1) |
| "sync and async tracks from multiple agents **never interfere**" | L290 | **False** — see (8) |
| "Ephemeral is **per-agent-process** — each agent only sees its own" | L239, L216 | **False in practice** — see (9) |
| "**Connection pooling** for variable number of agent processes" | L289 | **Not implemented** |
| "compiles immediately, no batching delay (**freshness first**)" | L114, L147 | **Still true; under review** |

### The race is not narrow and does not self-correct
L143 assumes two things. Neither holds.

*Not narrow*: `_release_stale_claims` sweeps on `source_ts` — the row's birth
time, not its claim time — so any ephemeral row older than
`STALE_CLAIM_MINUTES` is released the instant it is claimed. The race is the
common case, not a corner.

*Not self-correcting*: the stated corrective is that each compiler "reads
existing persistent records". That read is `_reject_duplicates`, which
refuses only at ≥0.97 cosine. Two independent compilations of identical input
produce differently-worded facts that both survive. Measured: 70 ephemeral
rows compiled by more than one pass.

L148 names the correct mechanism — `status = 'compiling'` does prevent
double-compilation. The sweep undoes it. Fixing the sweep restores the
document's stated behaviour rather than replacing it.

### Sync and async tracks do interfere
One direction only. Compilation marks rows `cleared`; `get_ephemeral` reads
`status = 'uncompiled'`. So the async track removes rows from the sync
track's own retrieval. L290's claim holds for *writes* — MVCC does what it
says — but not for visibility.

### Per-agent isolation exists in the schema, not in the data
The `agent_process_id` column and its filters are implemented as described.
518 of 612 rows nonetheless sit under three fixed `*-hook` agent ids, so the
partition has one bucket per hook rather than one per session.

### Connection pooling was planned, not built
L289 lists it among "Postgres-specific advantages used". `storage._conn()`
opens a fresh `psycopg2.connect` per call. Currently harmless — 6 of 100
connections in use — but the document overstates what is in place.

### Freshness-first is a live design decision, not a defect
L114 and L147 state the per-turn, no-batching trigger deliberately. The
advisory-lock proposal (P3 §10 discussion) preserves "fires immediately" —
every turn still spawns, all but one exits in milliseconds — but queueing is
batching by another name.

**This is a revision of a stated principle, not a gap.** Decide it
consciously. The case for revising: with many concurrent sessions, N
simultaneous 40s generation requests to one llama-server do not run in
parallel, so freshness is lost anyway, and lost less predictably.

---

## Ready-to-apply fixes

Four changes, ordered. Each is independent — none depends on another, and none
requires the parallelism work discussed elsewhere in this file. Together they
are roughly 25 lines plus one migration.

Parallelism is deliberately absent: a single compile slot drains ~1,260
rows/hour against a measured peak of 26, and `llama-server` runs
`--parallel 2`, so worker count is capped by the server regardless. These four
are coordination-correctness fixes, not throughput fixes.

### Fix A — `claimed_at`, so the lease measures claim time  → finding (1)

```sql
ALTER TABLE arteries.ephemeral ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
```

```python
# _claim_ephemeral
SET status = 'compiling', claimed_at = now()

# _release_stale_claims
WHERE project_id = %s
  AND status = 'compiling'
  AND claimed_at < now() - (%s || ' minutes')::interval
```

Raise `STALE_CLAIM_MINUTES` from 2 to 3 at the same time: the
invalid-response path calls `_llm_compile` twice at a 60s timeout, so a
legitimate pass can run 120s and graze the old threshold (finding 11).

Keep the sweep unscoped by agent — that part is correct and deliberate.

### Fix B — one ephemeral partition per session  → finding (9)

Root cause is a literal default, repeated across every hook script:

```sh
export ARTERIES_AGENT_ID="${ARTERIES_AGENT_ID:-arteries-hook}"
```

Every session in a project therefore shares one `agent_process_id`, which is
why 518 of 612 rows sit in three buckets. The host CLI already supplies a
session identifier in the hook payload; thread it through instead of
defaulting to a constant:

```sh
sid=$(printf '%s' "$event_json" | python3 -c \
  'import json,sys; print(json.load(sys.stdin).get("session_id","") or "")')
export ARTERIES_AGENT_ID="${ARTERIES_AGENT_ID:-${ARTERIES_PROJECT}-${sid:-hook}}"
```

Do this before Fix D's visibility change, or sessions will read each other's
working memory more, not less.

### Fix C — check reachability before claiming  → findings (13), (25)

```python
try:
    httpx.get(GENERATE_URL.rsplit("/v1/", 1)[0] + "/health", timeout=1.0)
except Exception:
    return {"status": "generator_unreachable", "claimed": 0}
```

At the top of `compile_once`, before `_claim_ephemeral`. Stops the
claim/fail/release churn that runs for the whole duration of a llama-server
outage. One second of budget.

This bounds the damage; it does not fix the cause. That belongs outside
arteries — a systemd unit with `Restart=always`.

### Fix D — sort entities before upsert  → deadlock prevention

```python
for ent in sorted(mem.get("entities") or [],
                  key=lambda e: (e.get("name") or "").lower()):
```

In `_write_results`. `graph.upsert_entity` uses `ON CONFLICT ... DO UPDATE`,
which takes a row lock, and entities are currently iterated in whatever order
the model returned them. Two concurrent compiles with overlapping entity sets
can acquire the same rows in opposite orders and deadlock. Consistent
acquisition order across all transactions removes the hazard.

Not yet observed in the logs — this is the one preventive item in the set.

### Not included, deliberately

- **Batch size.** Leave `MAX_EPHEMERAL_BATCH` at 10. With ~48x headroom a
  larger batch buys nothing measurable, and `compile.py:44` already says the
  answer to timeouts is a smaller model, not a bigger number.
- **Connection pooling.** 6 of 100 connections in use.
- **Drain loops and worker pools.** They optimize a resource that is idle
  almost all the time.
- **The advisory lock.** Worth doing, but it is insurance against (1)
  recurring rather than a fix for anything currently broken — and if added, key
  it globally with two slots to match `--parallel 2`, not per project.
