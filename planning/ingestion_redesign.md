# Ingestion redesign: session messages → knowledge graph

Status: design, pre-implementation. Written 2026-09-05 against `dev` @ 399933f.
Supersedes nothing; it is the plan `planning/audit_findings.md` was written to justify.

Every claim about current behaviour below carries a `file:line`. Anything not in the
repo is marked `[ASSUMPTION]`.

---

## 0. The shape, in one screen

```
 message (any session, any project)
   │  scope resolved from (repo_path → project_id → scope_id → session_id)
   │  split into atoms ─ dedupe ─ existing length filter
   ▼  (in-process; no table, no tier)
 EPHEMERAL  per session, per project. 48h. Verbatim turn kept in agent_events.
   │  strict filter: LLM compile + worth_keeping() + duplicate rejection
   │  screen only: nearest live persistent neighbours, empty table = no screen
   ▼  promote(1)
 PERSISTENT per project, read per scope. 60 active days, usage-exempt.
   │  incrementality score ≥ threshold, or core=true
   ▼  promote(1)
 EVERGREEN  the graph. One per scope group (e.g. harness). Permanent. Mutable.
   │
   ▼
 GEPHI  art graph export --seed X --hops N --out g.gexf
```

Retrieval closes the loop: every new message is scored against ephemeral +
persistent + evergreen before intake, and the retrieved neighbourhood is one of the
things atom dedupe compares against (§2.4, §6).

**Invariant P1 — one level per pass.** Two promotions, each reading exactly one
source table and naming exactly one destination in its `INSERT`. No function reads
ephemeral and writes evergreen. Asserted by `test_promotion_ladder.py` (§11).

**Atoms are not a tier.** An atom is one sentence-sized fact split out of a turn.
Splitting exists so dedupe has units small enough to repeat — whole turns are never
byte-identical, so deduping turns catches nothing. Atoms are computed in the intake
call and inserted straight into ephemeral. Rejected atoms are logged as
`memory.intake.rejected` events, not stored as rows.

---

## 1. Audit coverage

| # | Finding | Sev | Mechanism in the new design | Where |
|---|---|---|---|---|
| 1 | Stale sweep measures `source_ts`, not claim time | P0 | `ephemeral.claimed_at TIMESTAMPTZ`; sweep reads it; `STALE_CLAIM_MINUTES` 2 → 3 | `compile.py:_release_stale_claims` (`compile.py:172`), `schema.sql` |
| 2 | Assistant capture discarded 72% | P0 | **Already fixed** 2026-09-04 (`conversation.recent_user_turns`). New design keeps the corrected call and adds a regression test that self-as-reference returns non-empty | `assistant.py:capture_response` |
| 3 | No quality filter on the write path | P1 | Two-part gate in `promote.py`: (a) `COMPILE_SYSTEM` exclusions (see 5), (b) a `worth_keeping()` predicate run on every candidate memory before insert — rejects transient-intent phrasing, rejects claims whose subject is the conversation rather than the project, rejects claims with zero extracted entities *and* no numbers/paths. Rejections logged as `memory.compile.rejected_low_value` with the rule that fired | new `promote.py`, replaces `compile._write_results` gate-free path (`compile.py:527`) |
| 4 | Confidence carries no signal | P1 | Confidence is **removed from the promotion decision** and from `packet._score` (`packet.py:264`). It stays a stored annotation. Ranking uses `sim * TIER_WEIGHT * recency_decay` | `packet.py:258-264` |
| 5 | `COMPILE_SYSTEM` biases to inclusion on a false promise | P1 | Rule 4 rewritten. The false promise ("filtered mechanically after you answer") is deleted. Five explicit exclusions added: nothing derivable from the repo, nothing about the current conversation's own progress, nothing about what the user "wants" unless they said it, no restatement of a shown memory, no status | `compile.py:47` |
| 6 | Persistent store contaminated (11–17% transient, 14 cross-project) | P1 | Downstream of 3 + 5. Plus a hard scope assertion: `promote()` refuses to write a row whose `project_id` is not in the claiming worker's scope, raising rather than logging | `promote.py`, `scope.SCOPE_CTE` |
| 7 | No eviction | P1 | `art memory evict` + a decay term. Persistent rows with `access_count = 0` and `source_ts < now() - EVICT_UNUSED_DAYS` (default 90) get `valid_until = now()` and a `retired` edge, unless `kind IN ('decision','constraint')` or they have an evergreen child | new `evict.py`, `storage.py` |
| 8 | Promotion deletes the session's own working memory | P2 | `get_ephemeral` stops filtering on `status`. Visibility becomes `valid_until IS NULL` plus the retention rule in §3.5 (session end + 48h grace). Compilation sets `compiled_at`, never removes the row from the read path. A sweep tombstones expired rows | `storage.py:41-62`, `compile.py:702` |
| 9 | Sessions share ephemeral memory (518/612 in 3 buckets) | P2 | `ephemeral.session_id TEXT`, populated from `ARTERIES_SESSION_ID`, which `cli_normalize.py:117` **already exports**. No shell JSON parsing needed — the audit's Fix B is obsolete. Reads partition on `session_id`; claims stay on `agent_process_id` | `schema.sql`, `storage.insert_ephemeral` (`storage.py:65`), `cli_normalize.py:116` |
| 10 | Poison batches block the queue forever | P3 | `ephemeral.attempts INT DEFAULT 0`; `_claim_ephemeral` increments and excludes `attempts >= MAX_ATTEMPTS` (3); excluded rows get `status='quarantined'` and a `memory.compile.quarantined` event | `compile.py:_claim_ephemeral` (`compile.py:191`) |
| 11 | Timeout grazes the stale threshold | P3 | `STALE_CLAIM_MINUTES = 3`, applied with 1 | `compile.py:36` |
| 12 | Cold start wastes an embedding call | P3 | `_load_persistent_context` does `SELECT 1 FROM persistent … LIMIT 1` before embedding; empty → return `[]` with no embed call | `compile.py:249` |
| 13 | Hard availability dependency (44.4% pass failure) | P3 | Health probe before claim (see 25) + a deterministic degradation path: when the generator is unreachable, ephemeral rows stay readable in-session (fixed by 8), so the outage costs promotion, not recall | `promote.py` |
| 25 | Failed connections churn the queue | P3 | `httpx.get(<base>/health, timeout=1.0)` at the top of `compile_once`, before `_claim_ephemeral`; returns `{"status":"generator_unreachable","claimed":0}` | `compile.py:106` |
| 14 | Hook timeout mismatch (5000ms vs 10s) | P3 | `arteries-observe.js:70` raised to 9000ms, under the 10s in `hooks.json`. Intake work moves off the synchronous path anyway (§2) | `hooks/arteries-observe.js:70` |
| 15 | `contradicts` co-retrieves both sides unmarked | P4 | `MemoryItem` gains `via: str \| None`; `graph.expand` already computes it (`graph.py:163`) and `packet._rows` drops it. Contradicting pairs render as `A — contradicts → B` with both sides labelled | `packet.py`, `memory_types.py` |
| 16 | All 2457 edges `ontology_valid = false` | P4 | `add_edge` gains an `ontology_valid` argument; Layer-0 predicate bindings (`derived_from → prov:wasDerivedFrom`, `supersedes → prov:wasRevisionOf`) written at insert | `graph.py:90`, `ontology.py` |
| 17 | `derived_from` cartesian product | P4 | **Already fixed** 2026-09-04 via the model's `"from"` field. Kept; the `memory.compile.unattributed` counter becomes a doctor check that fails above 20% | `compile.py:558` |
| 18 | 55% of packet budget on what the host already has | P5 | `_allocations` "recent" 0.55 → 0.15; freed budget goes to evergreen + persistent | `packet.py:_allocations` |
| 19 | Nothing chains | P5 | `arteries.packets(id, session_id, turn_id, member_ids UUID[], summary, created_at)`. Next packet excludes ids already sent within the session | `packet.py`, `schema.sql` |
| 20 | Network call inside the compaction path | P5 | `_corpus_suggestion` moved behind a cache read; the fetch runs in the background compile process, never in packet assembly | `packet.py:_corpus_suggestion` |
| 21 | Dedupe is substring containment | P5 | Replaced with the same two-stage dedupe as intake: normalized-hash exact, then cosine ≥ `DUPLICATE_SIM` on already-computed vectors | `packet.py:_dedupe_memories` |
| 22 | No observations captured (evidence ladder has one class) | P6 | `PostToolUse` hook writing `tool.result` events (exit code, path, bytes), and `evidence TEXT` on persistent (`user \| observed \| stated \| inferred`) set at promotion. Contradiction resolution orders by evidence class, then recency | `hooks/hooks.json`, new `hooks/arteries-tool.js`, `schema.sql` |
| 23 | Stale docstring understates loss 2x | P6 | Docstring corrected to the measured 0.62 facts/row; `art doctor` recomputes and warns on drift | `compile.py:_reject_duplicates` (`compile.py:290`) |
| 24 | Codex compact prompt names v1 layout | P6 | Regenerated by `art setup` whenever the packet schema version changes; version stamped in the file | `.arteries/codex/compact_prompt.txt`, `setup_cli.py` |
| 26 | Graph expansion can never clear the packet floor; its output is always discarded | P4 | Rank the graph arm on its own scale and fuse tiers by rank instead of comparing a decayed hop score to a cosine floor (§20.4, §23.3). Ships with finding 15, whose harm is latent only because nothing from `expand` currently reaches the packet | `memory_select.py:136`, `graph.py:110`, `packet.py:258` |

**Open after this design:** none of the 24. Findings 2 and 17 are already fixed and
carried forward with tests.

**Not addressed, deliberately:** connection pooling (6/100 in use), worker pools,
`MAX_EPHEMERAL_BATCH` — the audit's "Not included" section is correct and this design
does not reopen it. The advisory lock is included as optional (§3.4).

---

## 2. Stage: intake → ephemeral

### 2.1 Scope, from sessions and projects

Unchanged mechanism, applied consistently. Derivation order:

1. `repo_path` = `ARTERIES_EVENT_CWD` or cwd (`scope.resolve`, `scope.py:96`).
2. `project_id` = longest-matching `scope_members.repo_path` (`scope.py:105`). Never
   `cwd.name` — that is the identity bug `setup_cli.py:79` caused.
3. `scope_id` = `scope_members.scope_id`. Unregistered project → scope of one, via the
   `UNION` arm of `SCOPE_CTE` (`scope.py:45`).
4. `session_id` = `ARTERIES_SESSION_ID` (`cli_normalize.py:117`), falling back to
   `"{project_id}-nosession"`.

Leak prevention, three places:
- Reads: every tier read goes through `SCOPE_CTE`. No raw `project_id =` on a read
  path. Enforced by a test that greps for `FROM arteries.persistent` without a
  preceding `SCOPE_CTE`.
- Writes: `promote()` asserts the destination `project_id` resolves into the claiming
  worker's scope; violation raises. This is what would have stopped the 14
  SmartCity/AgriTwin rows in finding 6.
- Entities: keyed by `scope_id` (`schema.sql:287`), already correct.
- T-Box: `ontology_bindings(scope_id, source)` + a scope filter in `ontology._lookup`.
  The one real leak the ontology design named; a prerequisite, not a follow-up.

### 2.2 Atoms

`normalize.py`, stdlib only:

```python
def atoms(text: str) -> list[str]:
    """Split a turn into one-sentence facts. No LLM on the hook path."""

def normalize_fact(text: str) -> str:
    """Hash key: lowercase, whitespace collapsed, terminal punctuation and a
    leading discourse marker stripped."""
```

Splitting rules: split on `[.!?]` + whitespace + capital, with abbreviation and
decimal guards; code fences and inline-code spans are atomic and never split; bullet
items are their own atom regardless of punctuation; an atom under
`MIN_EXTRACTABLE_WORDS` (5, `extract.py:37`) is dropped — the existing filter, reused
unchanged as asked. An atom over `MAX_ATOM_CHARS` (600) is kept whole with
`metadata.unsplit = true` rather than cut mid-clause.

**Atoms do not lose the turn.** `extract.py:1-17` records that verbatim per-turn
capture replaced pattern extraction *because* splitting lost signal, and the audit's
"Closed / not a defect" section calls verbatim ephemeral correct. That measurement
does not apply here: the whole turn is already stored in
`agent_events.payload.message_preview` (`schema.sql:170`) and `ephemeral.turn_id`
points at it. The compiler reads the turn as context; atoms are only what dedupe and
promotion operate on.

`observe.py` senders (heart, plexus, marrow) bypass splitting — they arrive terse and
deliberate, one observation per call, and are stored `unsplit`.

### 2.3 Dedupe

Three mechanisms. Two are free; the one that costs a model call runs in the
background, never on the hook path.

| | Mechanism | When | Catches |
|---|---|---|---|
| **A** | Partial unique index on `(project_id, session_id, fact_hash)` where `valid_until IS NULL` | intake, synchronous, in the database | Literal repeats after normalization |
| **D** | The compiler's judgement, having seen both texts | background, asynchronous | Paraphrase, and restatement vs. refinement |
| **E** | Screen against the context retrieved this turn (§6) | intake, synchronous, in memory | Facts you were just shown |

**A** is load-bearing because it is a constraint rather than code:

```sql
INSERT INTO arteries.ephemeral (...) VALUES (...)
ON CONFLICT (project_id, session_id, fact_hash) WHERE valid_until IS NULL
DO UPDATE SET seen_count = arteries.ephemeral.seen_count + 1,
              last_seen  = now()
RETURNING (xmax = 0) AS inserted;
```

Two sessions racing the same sentence resolve inside the index — no lock, no
read-then-write. The loser gets a counter bump, not a second row.

**D at intake, without an LLM call on the hook path.** A synchronous model call is out:
`UserPromptSubmit` has a 9s budget and the generator is unreachable 44% of the time
(finding 13), so every intake would gamble the turn. But the compile pass already runs
in the background after each turn and already loads ten ephemeral rows into a prompt.
Two places to get D nearly free:

1. **Intra-batch merge.** The batch is already in the prompt. `COMPILE_SYSTEM` gains a
   rule asking the model to name atoms *within the batch* that say the same thing;
   the loser is tombstoned and its `seen_count` added to the survivor. Zero extra
   calls.
2. **Session sweep.** A second batched pass over the session's live ephemeral, in the
   same background process, when `seen_count` or row count crosses a threshold. One
   call, off the hook path.

**Cosine at intake was considered and cut.** Its only job was paraphrase in the gap
between A and D — and with D running seconds later in a disposable session-scoped
tier, that gap costs nothing. The argument against it is already in this repo: the
comment above `DUPLICATE_SIM` (`compile.py:~275`) records that cosine cannot tell
restatement from subsumption, which is *why* the model names duplicates and the
threshold survives only as a backstop for near-identical strings. A second threshold
at intake reintroduces exactly that, on shorter text where cosine is noisier. The 0.97
check stays where it already is — the promotion backstop in `_reject_duplicates`.

Also rejected: a hash of sorted content words. It collapses "A supersedes B" into
"B supersedes A" — asserting two opposite sentences are one fact.

### 2.3.1 Grain: within session, then across

| Tier | Grain | Mechanism |
|---|---|---|
| **Ephemeral** | Within one session | A, keyed `(project_id, session_id, fact_hash)` |
| **Persistent** | Across sessions **and** projects in scope | D at promotion + the 0.97 backstop |
| **Evergreen** | Across the whole group | D at promotion + merge rule (§4.5) |

The same fact arriving independently in three sessions is **evidence the fact
matters**, so ephemeral keeps all three and counts them. Collapse happens at
promotion, *after* the signal is established, and the counts survive the merge:

```sql
ALTER TABLE arteries.persistent
    ADD COLUMN IF NOT EXISTS seen_sessions INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS seen_projects INT NOT NULL DEFAULT 1;
```

Both feed the `use` term of incrementality (§4.2). A fact three sessions reached
independently is more incremental, not less — which is the opposite of what a naive
intake-time collapse would have recorded.

### 2.4 Intake, end to end

One call, on the hook path, no LLM:

```
observe_turn(text, session_id, turn_id) -> {stored: n, duplicate: n, filtered: n}
```

1. Split into atoms.
2. Drop atoms under the length filter (`extract.extract_from_message`, unchanged).
3. Infer domains (`extract._infer_domains`, unchanged; capillaries taxonomy with the
   arteries fallback).
4. Screen against this turn's retrieved context (E).
5. Insert with `ON CONFLICT` (A).

No model call, no vector query beyond the one embedding the turn already needs. D
runs later, in the background compile process.

`EPHEMERAL_MODE='discard'` (`extract.py:74`) is honoured throughout — in that mode
atoms land in the in-process buffer and nothing touches Postgres.

---

## 3. Stage: ephemeral → persistent

### 3.1 What changes in ephemeral

```sql
ALTER TABLE arteries.ephemeral
    ADD COLUMN IF NOT EXISTS session_id   TEXT,
    ADD COLUMN IF NOT EXISTS claimed_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS compiled_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS attempts     INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS turn_id      TEXT,
    ADD COLUMN IF NOT EXISTS fact_hash    TEXT,
    ADD COLUMN IF NOT EXISTS seen_count   INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS last_seen    TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE UNIQUE INDEX idx_eph_dedupe
    ON arteries.ephemeral (project_id, session_id, fact_hash)
    WHERE valid_until IS NULL;
CREATE INDEX idx_eph_session
    ON arteries.ephemeral (project_id, session_id, source_ts DESC)
    WHERE valid_until IS NULL;
CREATE INDEX idx_eph_claimable
    ON arteries.ephemeral (project_id, source_ts) WHERE status = 'uncompiled';
```

`status='cleared'` is retired **as a visibility mechanism**, not as a column.
Compilation sets `compiled_at`; the read path (`storage.get_ephemeral`,
`storage.py:41`) filters on `valid_until IS NULL` and the retention rule in §3.5.
That is finding 8: the tier stops cannibalising itself, and in-session recall no
longer depends on the compiler being broken.

The column keeps being written for one release — see §8.

### 3.2 Concurrency, stated plainly

| Hazard | Mechanism | Why it holds |
|---|---|---|
| Double-compilation | `FOR UPDATE SKIP LOCKED` + `status='compiling'` + `claimed_at` | The claim commits before the HTTP call, and the sweep now measures the claim, so a live worker's rows are never released under it. Finding 1. |
| Stranded claims | Unscoped sweep on `claimed_at < now() - 3min` | Unscoped is deliberate and correct (audit, "Closed"): a claim is stranded exactly when its process is gone. |
| Poison batch | `attempts` counter, quarantine at 3 | Ordering stays `source_ts ASC`, so without the counter the oldest failing batch blocks everything behind it. Finding 10. |
| Duplicate facts from concurrent sessions | `fact_hash` unique index on `ephemeral` | The race resolves in the index, before either session reaches the LLM. This is the fix the audit could not have at the persistent level, where two differently-worded compilations both clear 0.97. |
| Entity upsert deadlock | `sorted(entities, key=name.lower())` before upsert | `ON CONFLICT DO UPDATE` takes row locks; consistent acquisition order across all transactions removes the cycle. Audit Fix D. |
| Generator outage churn | `/health` probe before claim | Two writes per row per turn for the length of an outage, otherwise. Finding 25. |
| Torn write | Existing `conn.rollback()` + `_release_claimed` in the write-error path (`compile.py:135`) | Already correct; kept, with a test. |

**Idempotency:** promotion writes carry `source_hash = sha256(sorted(parent_ids))`,
unique per project on live rows. A retried pass that re-derives the same fact from the
same parents hits the constraint and no-ops instead of writing a second row.

### 3.3 The strict filter

Order of checks, all in `promote.py`:

1. `validate_response` — structural, unchanged (`compile.py:346`).
2. `worth_keeping(mem)` — **new**, the gate finding 3 says does not exist. Rejects:
   - subject is the conversation, not the project (`^(the )?user (is|wants|asked)`,
     `this session`, `we are (now )?(verifying|testing|checking)`);
   - zero entities **and** no identifier/path/number (reuses `extract._NAMED`,
     `extract.py:128`);
   - restates a memory shown in the prompt (`duplicate_of` set);
   - `kind='fact'` with fewer than 6 words after normalization.
3. `_reject_duplicates` — unchanged, model call then 0.97 cosine backstop
   (`compile.py:290`).
4. Scope assertion (§2.1).

Confidence is not a gate. Finding 4 measured 84% of rows at ≥0.9; a floor on that
distribution is a no-op dressed as a filter.

### 3.4 The screen, and the empty table

`_load_persistent_context` (`compile.py:249`) selects the nearest live persistent rows
in scope and shows them to the compiler. Its job is comparison — spot contradictions,
spot refinements, name duplicates. It is **not** a gate:

- Empty persistent table → `[]` → the prompt says "None yet." → every candidate is
  judged by the strict filter alone. An empty table never blocks promotion.
- Non-empty → the neighbours are shown, and only an explicit `duplicate_of` or a 0.97
  cosine refuses a row. Similarity to an existing fact is never itself a rejection.

Cold-start ordering fix (finding 12): the existence check precedes the embed call, so
an empty store costs one index probe instead of a 4000-character Qwen3 round trip.

### 3.5 Retention, on an activity clock

Wall clock punishes you for stepping away. Three weeks off and the session you left
mid-task has expired along with everything in it, for no reason connected to whether
the memory was still good.

So retention is measured in **days the project saw work**, not calendar days. One
small table, one integer per row:

```sql
CREATE TABLE arteries.project_activity (
    project_id   TEXT NOT NULL,
    day          DATE NOT NULL,
    PRIMARY KEY (project_id, day)
);
-- INSERT ... ON CONFLICT DO NOTHING, once per turn. Row count = active days.
```

Each memory row is stamped with `activity_day INT` — the project's active-day count
at write. Age is `current_active_days - row.activity_day`. Two months away costs
nothing, because no rows were added to `project_activity` while you were gone.

| Tier | Rule | Rationale |
|---|---|---|
| **Ephemeral** | Expires at **session end + 48h grace**, with a 14-day wall-clock outer bound | This tier is about a session, not a project. Sessions get returned to across several days, so 48h rather than 24h — come back the day after tomorrow and your working set is intact. Return in a month and it is gone, correctly: that context is stale on any clock. |
| **Persistent** | **60 active days with `access_count = 0`.** Exempt: `kind IN ('decision','constraint')`, rows with an evergreen child, `core = true` | Age alone is the wrong signal — `storage.py:30` already records why decay was removed once, and finding 7 says the bound should be usefulness. A fact that keeps being retrieved never expires. |
| **Evergreen** | No expiry. Rows leave by supersede, merge, or explicit delete only | It is the permanent store. That is the whole point of the tier. |

Expiry is a tombstone (`valid_until = now()`), never a `DELETE`. Lineage survives, so
`art trace` can still answer where an evergreen row came from after its persistent
parent has been retired.

---

## 4. Stage: persistent → evergreen

### 4.1 Evergreen *is* the graph

The name moves. Today "evergreen" is three dead shell scripts and a docstring:
`scripts/evergreen-{extract,write,preview}.sh` all call `python -m arteries.evergreen`,
**a module that does not exist in `src/arteries/`**, and `memory_types.py:56` records
that the table they were written for "never held a row." `docs.py:4` says the review
flow they wrapped was replaced by `art docs`.

So the old evergreen is deleted outright — three scripts, and the README sections that
document them (`README.md:878-897`). The name is then reused for what actually earns
it: **the knowledge graph is the evergreen tier.** Permanent store, entity-structured,
scope-keyed, mutable in place.

Per `planning/ontology_design.md`: one database, one schema. Three-hop traversal
measures 2.1 ms against an 80k-claim projection, so a separate graph database does not
earn itself. Postgres is the graph store; Gephi is the viewer. Decided — no Neo4j.

```sql
CREATE TABLE arteries.evergreen (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id       TEXT NOT NULL,          -- scope, not project
    fact           TEXT NOT NULL,
    embedding      VECTOR(EMBED_DIM),
    kind           TEXT NOT NULL DEFAULT 'fact',
    core           BOOLEAN NOT NULL DEFAULT false,
    evidence       TEXT NOT NULL DEFAULT 'inferred',
    incrementality REAL,                   -- score at promotion, for audit
    episode_id     TEXT,                   -- inherited when parents agree (§4.5)
    task_id        TEXT,
    parent_ids     UUID[] NOT NULL DEFAULT '{}',
    source_meta    JSONB NOT NULL DEFAULT '{}',
    valid_from     TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until    TIMESTAMPTZ
);
CREATE INDEX idx_evergreen_scope ON arteries.evergreen (scope_id) WHERE valid_until IS NULL;
CREATE INDEX idx_evergreen_embedding ON arteries.evergreen USING hnsw (embedding vector_cosine_ops);
```

`memory_edges.src_kind` already lists `evergreen` (`schema.sql:350`) and
`persistent.child_ids` already documents "evergreen records compiled into"
(`schema.sql:80`). The schema was built for this; the rows never came.

**One graph per group.** Keying on `scope_id` is the decision: `harness` — arteries,
capillaries, heart, marrow, plexus — gets **one** graph, not five. That is the point
of grouping them, and it is what makes the cross-repo question answerable: a
constraint recorded in arteries is visible when planning heart. A standalone repo is a
scope of one and gets a graph to itself, which reads as a per-project store.

Entities are already scoped this way (`schema.sql:287`), so claims and the nodes they
mention finally share a namespace.

**The frame slot: renamed, done.** The last rename — `EvergreenMemory` →
`ScopeMemory` — happened while evergreen was being *removed*, and it broke capillaries
silently because no test exercised the memory path with a real frame. That is why
`capillaries/src/capillaries/agent/frame_compat.py` exists.

Evergreen is a real tier again, so the class is `EvergreenMemory` again. No alias: two
live names for one dataclass is how the shim got confused in the first place.

The rename tripped exactly one line, and the line was wrong anyway:

```python
# capillaries/src/capillaries/agent/frame_compat.py:76 — before
field = "sibling_insights" if cls.__name__ == "ScopeMemory" else "ground_truth_insights"
```

It used the class *name* as a proxy for which fields the class has. The name has now
moved twice; the fields moved once. So ask the fields:

```python
field = ("sibling_insights" if "sibling_insights" in cls.__dataclass_fields__
         else "ground_truth_insights")
```

`scope_memory_class()` already tried both names and needed only its order flipped.
Both repos are edited together, uncommitted, and the shim still resolves an old
arteries checkout correctly — verified by constructing the tier under both shapes.

The `MemoryFrame.scope` *field* is a separate question and stays as is: `frame_compat`
resolves it by `getattr` (`frame_compat.py:32`), so it already tolerates either name,
and there is no cost to leaving it.

### 4.2 Incrementality, operationally

"How incremental is this to the entire project" becomes four measurable terms:

```
novelty    = 1 - max_cosine(fact, live evergreen in scope)      # 0..1
reach      = min(1, distinct_entities_mentioned / 3)            # grounded breadth
durability = 1 if not superseded within DURABILITY_DAYS (14) else 0
use        = min(1, access_count / 3)                           # surfaced and kept

incrementality = 0.4*novelty + 0.2*reach + 0.2*durability + 0.2*use
```

Promote when `incrementality >= EVERGREEN_THRESHOLD` (default 0.6) **and**
`kind IN ('decision','constraint','preference')` OR `reach >= 2/3`. Facts about a
single file with no reuse stay persistent; that is the point.

`core = true` bypasses the score entirely. Core rows are the project's own
specification, and they seed the graph before any conversation does:

- **A new project seeds from `planning/*.md`** — the design documents, ingested with
  `art evergreen seed`, which runs `ingest.py`'s chunk-attributed claim path and
  writes the results straight to evergreen with `core = true`.
- **`AGENTS.md` joins once the project has one.** It is a later artefact — it
  describes how agents work in a repo that already exists — so it is seeded on demand
  rather than at project creation. `art evergreen seed --agents` adds it.
- `art remember --core` for anything typed by hand.

Core rows enter on the first pass, are exempt from eviction, and require `--force` to
delete. Re-seeding is idempotent: `documents.digest` (`schema.sql:319`) already skips
an unchanged file.

Thresholds are guesses calibrated against no data — same honesty as
`compile.py:DUPLICATE_SIM`. `art benchmark` re-derives them after a month.

### 4.3 Ontology breakdown and enrichment

On promotion, per claim:

1. Extract entities — the compile pass already emits `{"name","kind"}`. Entity yield
   is currently 0.15/claim against a 2–5 typical (ontology design, "Findings"); the
   Layer-1 vocabulary and the prompt change in §3.3 are what lift it.
2. `ontology.resolve()` canonicalizes against the T-Box, scope-filtered. Unmatched
   names are kept with `ontology_valid=false` — an ontology you are still growing must
   not eat facts it does not cover.
3. Write `mentions` edges, entities sorted (Fix D).
4. Write `ontology_ancestors` into entity metadata so `expand()` reaches a `pgvector`
   claim from a "vector store" query.
5. Layer-0 predicate binding: `derived_from → prov:wasDerivedFrom`,
   `supersedes → prov:wasRevisionOf`, with `memory_edges.ontology_valid = true`.
   That column has never been written (finding 16).

### 4.4 Episode inheritance, and why it does two jobs

An evergreen row inherits `episode_id` and `task_id` from its persistent parents
**only when they agree** — the same `CASE WHEN COUNT(DISTINCT …) = 1` subquery
`compile.py:583-592` already uses for persistent. A row promoted from several episodes
is by construction not about any one of them, so NULL is right.

It earns its place twice:

1. **RL exclusion.** `schema.sql:98-102` records the reason the column exists at all:
   without it, "compilation promotes an episode's own notes into persistent, where an
   untagged copy of the answer outlives the exclusion," and a retriever trains on its
   own previous solution. Evergreen is a third place that copy could outlive it, so it
   carries the tag too. Evergreen is **not** treated as always-safe general context.
2. **Fact resolution and supersede ordering.** When two evergreen rows conflict, the
   tiebreak is `evidence` class first (`user > observed > stated > inferred`, finding
   22), then **episode recency** — which episode last asserted it — and only then wall
   clock. Two claims made in the same episode are one revision, not two competing
   facts; two claims from different episodes are a real disagreement worth surfacing
   in `art conflicts`.

### 4.5 Mutation: edit, merge, supersede, delete

The graph updates itself; that was the ask. Rules:

| Operation | Trigger | Effect |
|---|---|---|
| **edit** | a promoted claim refines an evergreen row (compiler says `refines`, cosine ≥ 0.85) | new text written, old row `valid_until = now()`, `supersedes` edge with the reason on `metadata` |
| **merge** | two evergreen rows exceed 0.97 | survivor is the one with more parents; loser tombstoned, its edges repointed, `merged_into` edge written |
| **supersede** | explicit contradiction | tombstone + `supersedes` edge; the chain stays readable (17 winner-live/loser-dead measured, 0 inconsistent — the mechanism works) |
| **delete** | eviction, or `art graph delete <id>` | tombstone, never `DELETE`. Edges tombstoned in the same transaction. |

**Core protection:** `core = true` rows cannot be tombstoned by eviction or automatic
merge. A supersede against a core row does not tombstone it — it writes the
contradiction edge and raises a `graph.core_conflict` event for `art conflicts`.
Deleting a core node requires `art graph delete --force` and prints the affected
subgraph first.

**Dangling edges:** tombstoning a node tombstones its edges in the same transaction
(one `UPDATE … WHERE src_id = %s OR dst_id = %s`). Reads already filter
`valid_until IS NULL` (`schema.sql:363`), so a missed edge is invisible rather than
broken, but the transaction makes it consistent. `art doctor` counts live edges
pointing at dead nodes and reports non-zero as a defect.

---

## 5. Gephi export

```
art graph export --seed <entity-name|claim-id> --hops 2 --out graph.gexf
art graph export --scope harness --hops 3 --min-weight 0.4 --out harness.gexf
```

GEXF written with `xml.etree.ElementTree` — stdlib, no networkx, no gexf library. One
recursive CTE bounded by `--hops`, node attributes `kind`, `tier`, `ontology_class`,
`core`, `access_count`; edge attributes `rel`, `weight`. Tombstoned nodes excluded
unless `--include-retired`.

Hop bound is mandatory and defaults to 2. An unbounded export of a 20-repo scope is
how you get a hairball Gephi cannot lay out.

---

## 6. Loop closure

At `UserPromptSubmit`, before intake writes anything:

1. `packet._load_memories` retrieves from ephemeral + persistent + **evergreen** (the
   third arm is new; `TIER_WEIGHT` becomes `{ephemeral: 1.00, persistent: 0.95,
   evergreen: 0.90}` — evergreen is broader, so it is slightly discounted for a
   session-specific query, and `core` rows get a +0.1 floor bonus).
2. The retrieved set is handed to intake, but **not as the dedupe corpus** — corrected
   2026-09-07. The packet's 15 rows are ranked for relevance *to this message*, capped,
   and cut at `MEMORY_SIMILARITY_FLOOR = 0.55`. A stored fact that scored 0.54 is absent
   from the packet and would sail through a dedupe check built on it. Dedupe against the
   store stays where §2.2/§2.3 put it: the `fact_hash` unique index for exact matches,
   and a targeted similarity query over *live ephemeral for this session* — the whole
   tier, not a ranked slice of it.

   What the packet is good for is a narrower and real check: an atom that merely
   restates context the model was **just handed** should not be recorded as a new
   observation. `packet._dedupe_memories(items, previous_summary)` already does this
   shape of thing for packet assembly. This is the loop closing — what you were just
   told is not written down again as if you discovered it.
3. The packet's member ids are recorded in `arteries.packets`, so the next packet in
   the session excludes them (finding 19) and the compiler can see which claims were
   actually surfaced.

**Two retrieval systems share this packet; only one is memory.** `_load_memories` is the
memory RAG — ephemeral, persistent, and now evergreen, ranked into `MAX_PACKET_MEMORIES`.
`_corpus_suggestion` is a separate capillaries search over the prompt corpus, rendered as
"Suggested Approach". `GATE_COVERAGE_ABSTAIN = 0.92` gates **only the second one**: it
decides whether a corpus search is worth making, and has no effect on which memories are
retrieved. Worth keeping straight, because they read as one block in the rendered packet.

**`MAX_PACKET_MEMORIES = 15` needs re-deriving at commit 11.** Fifteen slots were sized
for two tiers. Three tiers competing for the same fifteen means evergreen wins slots from
ephemeral and persistent rather than adding to them, so the loop closes at the cost of
the working set. Either raise the cap or allocate per tier — decide with `art benchmark`,
not by guessing, since the 18% memory budget still truncates whatever the cap admits.

`EvergreenMemory` (`memory_types.py:48`) is where evergreen lands in the frame — it is
already the "wider than this project" slot, and its docstring says the evergreen table
that used to feed it never held a row. No `MemoryFrame` shape change, so capillaries
absorbs nothing.

---

## 7. Edge cases

| Case | Behaviour |
|---|---|
| Empty persistent table | Screen returns `[]`, no embed call, strict filter alone decides. Never a veto. |
| Two sessions write the same fact simultaneously | Resolved in `idx_eph_dedupe`. One row, one `seen_count` bump. No lock. |
| A turn's agent process dies before its compile pass runs | **Broken today, found 2026-09-07.** `_claim_ephemeral` (`compile.py:203`) filters `agent_process_id = %s OR parent_agent_id = %s`, where the parameter is the *current* process. No later process ever matches a dead PID, so those rows stay `uncompiled` forever: 28 of them are in the live database right now, one per dead PID, dated 2026-09-02/03. They are invisible to `doctor` too, which only collects `cleared`. Fix rides with commit 3 (`session_id`): claim on `session_id`, and let a claim older than `STALE_CLAIM_MINUTES` be claimable by any process in the project. |
| Rows stay live between compile and sweep | `storage.py:262` measures retrieval coverage over ephemeral rows that are not `cleared`. Under the new lifecycle a compiled row stays live until `SessionEnd`, so coverage reads high for longer and the retrieval gate suppresses capillaries calls it used to make. Bounded, not harmless: the query is keyed on `agent_process_id`, so the inflated coverage lasts one agent process, not the session. Watch it in `art benchmark` before widening the gate. |
| Fact contradicts an existing graph node | `contradicts` edge; both sides retrievable and **labelled** via `MemoryItem.via` (finding 15). Against a `core` node: no tombstone, `graph.core_conflict` event. |
| Project renamed | `scope_members.project_id` is the key and `repo_path` the resolver. `art scope move` updates one row; historical rows keep the old id and become unreachable — so the migration adds `art scope rename` that rewrites `project_id` across all seven tables in one transaction. |
| Session resumed | `ARTERIES_SESSION_ID` is stable across resume (`SessionStart` matcher includes `resume`, `hooks.json`). Same partition, and retention runs from session end rather than row age (§3.5), so a resumed session sees its own working set. |
| Multi-sentence fact that resists one-sentence normalization | `atoms()` keeps a code-fence or inline-code span whole and never splits inside one. An atom over `MAX_ATOM_CHARS` (600) is kept whole with `metadata.unsplit = true` rather than cut mid-clause. Cutting a finding in half is worse than a long atom. |
| Deleted node still referenced by an edge | Edges tombstoned in the node's transaction; `art doctor` counts live-edge-to-dead-node and reports non-zero as a defect. |
| Embedding model / dimension change | `migrate_embed_dim.py` exists. Extended: it must now cover `evergreen`. `EMBED_DIM` is one column width shared with capillaries and the two drifted silently once — so the migration writes an `embed_model` stamp into `arteries.scopes.metadata` and `art doctor` fails loudly on mismatch instead of returning garbage cosines. |
| Generator down | Health probe skips the cycle; ephemeral stays readable (finding 8), so recall degrades to "no promotion" rather than "no memory". |
| Ontology loaded for another domain | `ontology_bindings(scope_id, source)` + scope filter in `_lookup`. Without it a finance vocabulary grounds `position` inside a coding scope — the false-identity failure the whole design exists to avoid. |

---

## 8. Schema deltas

```sql
-- new tables
CREATE TABLE arteries.evergreen (...);           -- §4.1
CREATE TABLE arteries.project_activity (...);    -- §3.5
CREATE TABLE arteries.packets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL, session_id TEXT, turn_id TEXT,
    member_ids UUID[] NOT NULL DEFAULT '{}', summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE arteries.ontology_bindings (
    scope_id TEXT NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY (scope_id, source)
);

-- altered
ALTER TABLE arteries.ephemeral
    ADD COLUMN IF NOT EXISTS session_id TEXT,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS compiled_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS turn_id TEXT,
    ADD COLUMN IF NOT EXISTS fact_hash TEXT,
    ADD COLUMN IF NOT EXISTS seen_count INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS activity_day INT;
ALTER TABLE arteries.persistent
    ADD COLUMN IF NOT EXISTS evidence TEXT NOT NULL DEFAULT 'inferred',
    ADD COLUMN IF NOT EXISTS source_hash TEXT,
    ADD COLUMN IF NOT EXISTS core BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS activity_day INT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_persistent_source_hash
    ON arteries.persistent (project_id, source_hash) WHERE valid_until IS NULL;
```

### Why this branch may only add columns

Two checkouts of arteries — this branch and the one running on `main` — talk to **one**
Postgres. There is a single copy of the schema, shared between them. A schema change
made from here takes effect for main's code the moment it runs, without main changing
at all.

That makes the asymmetry:

- **Adding** a column is safe. Main's queries do not name it, so they never see it.
- **Dropping or renaming** one breaks main immediately: its `SELECT` still names the
  old column and the query errors.

This is not hypothetical. `schema.sql:49` keeps an `ephemeral.confidence` column this
branch no longer writes, precisely because "the main checkout still selects it, and
both run against the same database." `schema.sql:87` records that renaming
`persistent.scope` was attempted and reverted for the same reason.

**Rule for this branch: additive only, until main merges.**

The same logic covers `status='cleared'`. Main's `get_ephemeral` filters
`status = 'uncompiled'`. If the new code stops writing `'cleared'`, main sees every
compiled row as permanently uncompiled and re-serves it forever. So the new compiler
keeps writing the column even though the new read path ignores it, and it comes out
one release after main merges.

### Backfill

- `session_id` on existing ephemeral rows is unrecoverable — that *is* finding 9. Set
  it to `agent_process_id`, so old rows land in one bucket per hook and only new rows
  partition properly.
- `activity_day` backfills from `agent_events` by counting distinct
  `date(created_at)` per project up to each row's `source_ts`.
- `fact_hash` backfills by running `normalize_fact` over existing rows. The unique
  index is created `CONCURRENTLY` *after* the backfill dedupes collisions, so index
  creation cannot fail on legacy duplicates.
- The 270 junk `demo` scope rows pointing at deleted `/tmp` directories are deleted
  first (ontology design, "Findings").

---

## 9. File-by-file plan

| Path | Change | Why |
|---|---|---|
| `src/arteries/normalize.py` | **new** (~80 lines) | Sentence atoms + `normalize_fact` hash key. Stdlib, no LLM. §2.2 |
| `src/arteries/promote.py` | **new** (~200 lines) | `worth_keeping()` and the two promotion functions moved out of `compile._write_results`. Keeps the ladder legible and testable. §3.3 |
| `src/arteries/evergreen.py` | **new** (~200 lines) | The graph tier: incrementality scoring, promotion, seed, merge/edit/delete. §4 |
| `scripts/evergreen-*.sh` | **deleted** (3 files) | They call `arteries.evergreen` as it was — a review flow `art docs` replaced (`docs.py:4`). The name is reused, the old code is not. |
| `README.md` | rewrite `:878-897`, `:36`, `:590` | Documents the dead evergreen scripts and a three-tier story that no longer matches |
| `src/arteries/evict.py` | **new** (~70 lines) | Finding 7, on the activity clock. Read-side decay is what bounds write-side error. §3.5 |
| `src/arteries/activity.py` | **new** (~30 lines) | `project_activity` bump + `active_days()`. §3.5 |
| `src/arteries/migrate.py` | **new** (~120 lines) | Ordered migration runner: `schema_migrations`, checksums, per-file transactions, `no-transaction` headers. §14.3 |
| `migrations/*.sql` | **new** | One file per schema change, immutable once applied. §14.8 |
| `tests/conftest.py` | session fixture creating/dropping `arteries_test` | `conftest.py:41` — "the suite has no test database, it points at the live one" |
| `src/arteries/compile.py` | `claimed_at`, `attempts`, health probe, `STALE_CLAIM_MINUTES=3`, cold-start check before embed, `COMPILE_SYSTEM` exclusions, sorted entity upsert | Findings 1, 3, 5, 10, 11, 12, 13, 25, Fix D |
| `src/arteries/storage.py` | `get_ephemeral` visibility on retention not status; `session_id` + `fact_hash` on insert with `ON CONFLICT`; `activity_day` stamp; evergreen accessors | Findings 8, 9; §2.3, §3.5 |
| `src/arteries/graph.py` | `ontology_valid` on `add_edge`; `export_gexf`; `tombstone(node)`; evergreen arm in `expand` | Findings 15, 16; §4.4, §5 |
| `src/arteries/ontology.py` | scope filter in `_lookup` + cache key; Layer-1 vocabulary loader | The one real T-Box leak |
| `src/arteries/packet.py` | drop confidence from `_score`; `recent` 0.55→0.15; chaining via `arteries.packets`; cosine dedupe; `via` preserved; evergreen tier | Findings 4, 15, 18, 19, 20, 21 |
| `src/arteries/memory_types.py` | `MemoryItem.via`; `EvergreenMemory` fed from evergreen (renamed, done) | Finding 15, §4.1, §6 |
| `src/arteries/schema.sql` | §8 | — |
| `src/arteries/migrate_embed_dim.py` | cover `evergreen`; write the `embed_model` stamp | Edge case |
| `src/arteries/doctor.py` | checks: unattributed rate >20%, live-edge-to-dead-node, embed-model drift, `facts/row` drift | Findings 17, 23; §4.4 |
| `src/arteries/cli.py` | `evergreen`, `evict`, `conflicts`; `graph export` | — |
| `hooks/arteries-observe.js` | timeout 5000 → 9000 | Finding 14 |
| `hooks/arteries-tool.js` | **new** | `PostToolUse` → `tool.result` events. Finding 22, the empty evidence ladder. |
| `hooks/hooks.json` | register `PostToolUse` | Finding 22 |
| `scripts/evergreen-*.sh` | now resolve | They call a nonexistent module today |

---

## 10. Gaps found by diffing against current code

Things the current implementation does that a clean-sheet design would have dropped,
reclaimed here:

1. **Subagent claiming.** `_claim_ephemeral` (`compile.py:191`) claims both a parent's
   rows and any row naming it as `parent_agent_id`, ordered parent-first. Session
   partitioning must not break this — `session_id` governs *reads*, `agent_process_id`
   and `parent_agent_id` govern *claims*. Two different keys, deliberately.
2. **`[SUBAGENT]` / `[HEART]` / `[ASSISTANT]` source bars** in `COMPILE_SYSTEM`
   (`compile.py:56-72`). Different sources get different filters. Preserved verbatim
   in the rewritten prompt.
3. **Episode/task agreement subqueries** (`compile.py:583-592`). A fact compiled from
   several tasks carries NULL, so retrieval exclusion cannot hide general context.
   This is RL answer-sheet prevention and it must survive into evergreen: an evergreen
   row promoted from several persistent rows inherits `episode_id` only when they
   agree.
4. **`_decode_response` / `_close_brackets`** (`compile.py:446`). Recovers 3,600
   usable characters from a response missing one byte, and refuses to close a string
   mid-sentence. Keep exactly.
5. **`degrade.note`** (`degrade.py`). Every new handler classifies before swallowing.
   Seven defects shipped green under bare `except Exception`.
6. **`EPHEMERAL_MODE='discard'`** (`extract.py:74`). An in-process buffer path with no
   database. Candidates must honour it or the discard mode starts writing rows.
7. **Domain fallback** (`extract.py:32`). Capillaries owns the taxonomy; arteries
   falls back to its own only when capillaries is absent.
8. **`observe.py`** — the programmatic write path for heart/plexus/marrow, which do
   not type into a prompt. Candidates must accept these; they arrive as whole
   observations already terse and deliberate, so they bypass sentence-splitting
   (`unsplit = true`).
9. **Opt-in write** (`observe.py:305`). An unregistered project is not observed, and
   an explicit `--project` is checked on its own registration.
10. **`scope.resolve` component-wise path matching** (`scope.py:99`). `.../arteries` is
    a string prefix of `.../arteries-rework`. Never use string prefixes.

---

## 11. Test plan

Smallest set that fails if an invariant breaks. No fixtures beyond the existing
`conftest.py`.

| Test | Fails when |
|---|---|
| `test_promotion_ladder.py::test_no_two_level_writer` | Any module's AST shows a function reading tier N and writing tier N+2 |
| `test_normalize.py::test_atoms` | Sentence splitter breaks a code fence, a decimal, or an abbreviation |
| `test_normalize.py::test_dedupe_index` | Two inserts of one normalized fact in a session produce two rows instead of a `seen_count` bump |
| `test_normalize.py::test_word_order_not_collapsed` | "A supersedes B" and "B supersedes A" are treated as one fact |
| `test_promote.py::test_empty_persistent_does_not_block` | An empty persistent table suppresses a promotion |
| `test_retention.py::test_activity_clock_ignores_absence` | A 60-day gap with no activity expires a persistent row |
| `test_compile.py::test_stale_sweep_uses_claimed_at` | A freshly claimed old row is released by the sweep (finding 1) |
| `test_compile.py::test_poison_batch_quarantines` | A batch failing 3× is still claimed on the 4th pass |
| `test_compile.py::test_generator_unreachable_claims_nothing` | Rows flip to `compiling` while the health probe fails |
| `test_compile.py::test_worth_keeping_rejects_session_intent` | `"User is verifying that the fix works"` is promoted |
| `test_storage.py::test_compiled_row_still_visible` | A compiled ephemeral row vanishes from `get_ephemeral` (finding 8) |
| `test_storage.py::test_session_partition` | Session A reads session B's ephemeral |
| `test_graph.py::test_tombstone_kills_edges` | A live edge survives its node's tombstone |
| `test_graph.py::test_core_node_survives_supersede` | A core node is tombstoned by an automatic supersede |
| `test_graph.py::test_gexf_hop_bound` | Export returns nodes beyond `--hops` |
| `test_evergreen.py::test_incrementality_gate` | A single-file no-reuse fact promotes |
| `test_evergreen.py::test_episode_inherited_only_on_agreement` | A row promoted from two episodes carries either one's id |
| `test_evergreen.py::test_core_seed_idempotent` | Re-seeding an unchanged `planning/*.md` writes new rows |
| `test_packet.py::test_contradiction_labelled` | Both sides of a `contradicts` pair render identically (finding 15) |
| `test_assistant.py::test_self_reference_regression` | Passing the assistant's own text as `user_turn` returns non-empty (finding 2 guard) |

---

## 12. Branch and commits

```
git checkout dev && git pull
git checkout -b ingestion-redesign
```

Every commit leaves the tree working and the suite green. Merge to `dev`, soak, then
`dev` → `main`.

| # | Commit | Contents |
|---|---|---|
| 0 | Give the suite a database of its own | `migrate.py`, `migrations/`, `arteries_test` fixture, migrate-vs-schema drift test (§14) |
| 1 | Measure the claim, not the row's birth | `claimed_at`, `STALE_CLAIM_MINUTES=3`, migration, test (findings 1, 11) |
| 2 | Skip the cycle when the generator is down | Health probe, `attempts`/quarantine (findings 10, 13, 25) |
| 3 | Give every session its own working memory | `session_id` from `ARTERIES_SESSION_ID`, read partition, backfill (finding 9) |
| 4 | Stop compilation from eating the tier it reads | Retention-based visibility, `compiled_at` (finding 8) |
| 5 | Say what not to remember | `COMPILE_SYSTEM` exclusions, `worth_keeping`, `promote.py` (findings 3, 5, 6) |
| 6 | Confidence is an annotation, not a gate | `packet._score`, allocations 0.55→0.15 (findings 4, 18) |
| 7 | One sentence, once | `normalize.py`, atoms, `fact_hash` unique index, cosine + retrieval screens (§2) |
| 8 | Ground the vocabulary per scope | `ontology_bindings`, `_lookup` filter, Layer-0/1 binding (finding 16) |
| 9 | The graph is the evergreen tier | Delete the three dead scripts, `evergreen.py`, table, incrementality, seeding, mutation rules (§4) |
| 10 | Show the graph | `graph export` GEXF, hop bound (§5) |
| 11 | Close the loop | Evergreen retrieval arm, packet chaining, intake comparison (findings 19, 21; §6) |
| 12 | Decay what nothing reads | `activity.py`, `evict.py`, retention on the activity clock (finding 7, §3.5) |

**Shortest path to seeing dedupe run: 0 → 3 → 7.** Commit 7 is where dedupe lives, and
it depends on exactly one earlier commit — 3, for the `session_id` half of the dedupe
key. Commits 1, 2 and 4–6 are independent bug fixes that can land after. Taking that
path puts a working `art dedupe --dry-run` against real ephemeral rows three commits in
instead of eight, which is the right trade: dedupe is the piece most likely to be wrong
in a way the design cannot predict, so it should meet real data early. The remaining
commits keep their numbers; only the order of landing changes.
| 13 | Capture what happened, not just what was said | `PostToolUse` hook, `evidence` column (finding 22) |
| 14 | Housekeeping | Hook timeout, docstrings, doctor checks, compact prompt (findings 14, 23, 24) |

Commit 0 comes first and is a prerequisite for every one after it — there is no
rehearsing a migration without a database to rehearse it in.

Commits 0–4 are shippable on their own and fix the P0/P2 bugs. If you want a fast
merge, cut `dev` after 4.

Each commit follows §14.5: migration to `arteries_test`, suite green, migration
applied to live, *then* the code merges.

---

## 13. Decisions settled, and what is still open

### Settled 2026-09-05

| # | Question | Decision |
|---|---|---|
| 1 | Atom granularity | Split into sentences, no LLM rewrite. The verbatim turn stays in `agent_events`. |
| 2 | Is there a candidate tier? | **No.** Atoms are computed in-process and inserted straight into ephemeral; the unique index lives on `ephemeral` itself. Three tiers, two promotions. |
| 3 | Evergreen scope | Scope-wide (`scope_id`): one graph per project **group**. `harness` gets one graph across all five repos. |
| 4 | Graph database | Postgres + Gephi. No Neo4j. |
| 8 | Core seeding | A new project seeds from `planning/*.md`; `AGENTS.md` joins later, on demand. |
| 12 | Episode inheritance | Evergreen inherits `episode_id`/`task_id` when parents agree — for RL exclusion *and* for supersede ordering (§4.4). |
| — | Retention clock | Activity days, not wall clock. Ephemeral: session end + **48h**, 14-day outer bound. Persistent: 60 active days, usage-exempt. Evergreen: permanent. |
| — | Dedupe | **A** (unique index, sync) + **D** (compiler judgement, async: intra-batch merge + session sweep) + **E** (retrieval screen). Intake cosine cut — D covers paraphrase and the repo's own `DUPLICATE_SIM` comment argues against a second threshold. Word-bag hash rejected: it collapses "A supersedes B" into "B supersedes A". |
| — | Dedupe grain | Within session at ephemeral; across sessions and projects at promotion, after signal. `seen_sessions`/`seen_projects` survive the merge and feed incrementality. |
| — | Evergreen grain | One graph per scope **group** — `harness` gets one, not five. |
| Q2 | `ScopeMemory` rename | **Done, both repos.** The class is `EvergreenMemory`; no alias. `frame_compat.py:76` now detects by `__dataclass_fields__` instead of `cls.__name__`, which is what it should have done originally. |
| Q3 | Evergreen seeding trigger | **Manual** `art evergreen seed`, with `art doctor` reporting "has `planning/*.md`, empty graph" so forgetting is visible. Setup stays fast and unable to fail for unrelated reasons. |
| Q4 | Deleting the old evergreen scripts | **Safe, checked 2026-09-05.** No executable caller in any repo; the five external references are markdown handoff docs. Notes written into `capillaries/docs/rework_actions.md` (action 4.6) and `rework_design.md`. |
| — | Migrations | Versioned files + `schema_migrations`, a real test database, expand/migrate/contract, **schema to live before code to main**. §14 |

### Settled since — the five that blocked implementation

1. **Session-sweep trigger — `SessionEnd`.** The sweep fires when a session ends and
   is cleaned up after finishing a subtask for heart upstream. Row count is not a
   trigger; a long session simply carries a large working set, which is what the tier
   is for. Because a crashed CLI never fires the hook, the 48h expiry in §3.5 is the
   backstop: an unswept session ages out on its own rather than living forever.
2. **Ephemeral wall-clock outer bound — 14 days.** On top of session end + 48h. Note
   `doctor.py:44` already defaults `ARTERIES_EPHEMERAL_RETENTION_DAYS` to 14, so this
   is the existing number, not a new one.
3. **`status='cleared'` compatibility window — safe, checked.** Both readers filter on
   status explicitly: `storage.py:275` (`AND status <> 'cleared'`) and `doctor.py:52`
   (`AND e.status = 'cleared'`). The sweep must therefore keep writing `cleared`, and
   §2.3 is amended to set it — otherwise `doctor` collects nothing and ephemeral grows
   without bound. See §7 for the coverage-gate consequence.
4. **`PostToolUse` volume — not in v1.** Deferred to its own commit after dedupe is
   proven on turn-level capture. When it lands: tool name, exit code and target path
   only, never output, and no promotion past ephemeral without an explicit rule.
5. **The 2-slot advisory lock and heart's semaphore — both, cap 2.** §15.4 gives the
   gate, the circuit breaker and the three ways this shape is usually written wrong.

### Parked for next session

Raised, understood, not decided. Picking these up does not block commits 0–4.

- **`art migrate` runner scope.** §14.3/§14.9 propose ~120 lines: ordered files,
  checksums, `schema_migrations`, advisory lock, `no-transaction` headers,
  `baseline`, `render`. The alternative is Alembic — one dependency for what is a
  directory listing and a table. Recommend building it; revisit if the header
  grammar starts growing.
- **Shadow database.** §14.2 makes it optional. Worth standing up before the first
  backfill that touches a table larger than `ephemeral`'s current 612 rows.
- **Gephi export defaults.** `--hops 2`, tombstoned nodes absent rather than greyed.
- **Incrementality weights.** 0.6 composite at 0.4/0.2/0.2/0.2. Ship, then re-derive
  from `art benchmark`.
- **`preference` in the eviction exemptions.**
- **`llama-server` under systemd with `Restart=always`.** Outside all three repos and
  the actual fix for the 44% failure rate (§15.3).

---

## 14. Migration practice: test database → main → live

### 14.1 The hole this fills

`tests/conftest.py:41` says it plainly:

> The suite has no test database -- it points at the live one.

That is why `test_degrade` wrote 30 fabricated `internal.bug_swallowed` rows into the
one channel whose value depends on every entry being real. The autouse
`_no_live_events` fixture patches around the symptom; it does not give the suite a
database of its own. This branch adds one, because a migration you cannot rehearse is
a migration you are testing in production.

### 14.2 Three environments

| | What | Lifetime | Who writes to it |
|---|---|---|---|
| **test** | `arteries_test` database, same Postgres, same `arteries` schema name | Created and dropped per test session | The suite only |
| **shadow** *(optional)* | `arteries_shadow` database restored from a `pg_dump -n arteries` of live | Days, by hand | Migration rehearsals against real data volumes |
| **live** | `capillaries` database, `arteries` schema | Forever | Both checkouts |

The schema name has to stay `arteries` in all three — every query in the codebase
writes `arteries.ephemeral` literally, so isolation comes from a separate **database**,
not a separate schema. `config.DB_CONFIG` already reads `DB_NAME` from the environment
(`config.py:10`), so `DB_NAME=arteries_test` is the whole switch. No code change.

```python
# tests/conftest.py
@pytest.fixture(scope="session", autouse=True)
def _test_database():
    os.environ["DB_NAME"] = "arteries_test"
    subprocess.run(["dropdb", "--if-exists", "arteries_test"], check=True)
    subprocess.run(["createdb", "arteries_test"], check=True)
    setup_db.setup()          # schema.sql
    migrate.apply_all()       # every migration, in order
    yield
    subprocess.run(["dropdb", "arteries_test"], check=True)
```

Tests that need Postgres get a marker and skip cleanly when it is absent, the same way
`writes_events` works today. The suite must stay runnable on a laptop with no database.

### 14.3 Migrations as ordered files

Today's mechanism is `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` inline in `schema.sql`
(`schema.sql:48`, `:62`, `:91`, `:228`). It is idempotent, which is good, and
unversioned, which is not: nothing records what has been applied, nothing can be
replayed in order, and nothing can be rolled back.

```
migrations/
  001_claimed_at.sql
  002_session_id.sql
  003_ephemeral_dedupe.sql
  ...
```

```sql
CREATE TABLE IF NOT EXISTS arteries.schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum    TEXT NOT NULL
);
```

`art migrate status` lists applied and pending. `art migrate apply` runs pending in
order, each in its own transaction, recording the version and a checksum of the file.
**Applied migrations are immutable** — editing one after it has run makes the checksum
mismatch and `art migrate status` says so. Fix a mistake with a new migration, never by
editing an old one.

`schema.sql` remains the from-scratch definition. A `test_migrations.py` check applies
migrations to an empty database and asserts the result matches `setup_db.setup()` on
another empty database — so the two cannot drift, which is the failure mode
`schema.sql:220` already records happening once ("added them to the live database by
hand and left schema.sql for later").

### 14.4 Expand / migrate / contract

The pattern for changing a schema two codebases share. Never one deploy — always
three, and the middle one can take weeks.

**1. Expand.** Add the new thing, nullable or defaulted. Old code ignores it; new code
can use it. Safe to apply to live immediately.

**2. Migrate.** Backfill data. If a column is being replaced, new code writes *both*
old and new for a while (dual write), so a rollback loses nothing.

**3. Contract.** Once no reader depends on the old thing, drop it. This is the only
destructive step and it comes last, after main has merged and soaked.

For this branch: everything in §8 is step 1. `status='cleared'` is step 2's dual write.
Dropping `ephemeral.confidence` and `status` is step 3, a separate PR after main
merges.

### 14.5 Ordering: schema first, then code

The instinct is to merge code and migrate after. That is backwards here, and it breaks
`main`.

```
❌  merge code to main  →  main runs, SELECTs a column that does not exist  →  errors
✅  apply migration to live  →  main ignores the new columns  →  merge code  →  it works
```

Additive migrations are **invisible** to code that does not name the new columns, so
applying them early is free. Code that reads a column that is not there is not.

Order of operations for each commit in §12:

1. Write the migration, apply it to `arteries_test`, run the suite.
2. *(Optional, for anything touching a large table)* rehearse against `arteries_shadow`
   restored from a live dump, and time it.
3. Apply to live with `art migrate apply`. Live is now ahead of both checkouts, which
   is the safe direction.
4. Merge the code to `dev`. Soak.
5. Merge `dev` to `main`.
6. Only after that, and only in a separate PR, the contract migration.

### 14.6 Locking, and how not to freeze the live database

The database is shared with capillaries and with a live agent loop. A migration that
takes a heavy lock stalls turns.

- **`ADD COLUMN` with a constant default is instant** on PG 11+ — no table rewrite.
  `ADD COLUMN ... NOT NULL` *without* a default rewrites the table; never do that on
  `ephemeral` or `agent_events`. Add nullable, backfill in batches, then set NOT NULL.
- **`CREATE INDEX` takes a write lock.** Use `CREATE INDEX CONCURRENTLY`, which cannot
  run inside a transaction — so concurrent index migrations get their own file and are
  marked `-- no-transaction` in a header the runner reads.
- **Set a lock timeout before DDL** so a migration fails fast instead of queueing
  behind a long query and blocking every writer behind *it*:

  ```sql
  SET lock_timeout = '3s';
  SET statement_timeout = '5min';
  ```

- **Backfill in batches**, not one `UPDATE`. A single statement over `agent_events`
  holds row locks for its whole duration:

  ```sql
  UPDATE arteries.ephemeral SET fact_hash = ... WHERE fact_hash IS NULL
    AND id IN (SELECT id FROM arteries.ephemeral WHERE fact_hash IS NULL LIMIT 5000);
  -- repeat until 0 rows
  ```

- **Unique indexes go on last.** Build the index *after* the backfill has deduplicated
  existing rows, `CONCURRENTLY`, or creation fails on legacy duplicates and leaves an
  invalid index behind.

### 14.7 Before anything destructive

```bash
pg_dump -Fc -n arteries capillaries > ~/backups/arteries-$(date +%Y%m%dT%H%M%S).dump
```

Every contract step, every `DROP`, every backfill that overwrites. Restore is
`pg_restore -n arteries -d capillaries`. `art migrate apply --contract` refuses to run
without a dump newer than the migration file.

### 14.8 What each migration file must carry

A header the runner parses and a human can read:

```sql
-- 003_ephemeral_dedupe.sql
-- step: expand
-- reversible: yes
-- rewrites-table: no
-- estimated-rows: 612
-- no-transaction: false
```

Plus, in the file itself, a `-- DOWN` section for anything reversible. Not every
migration can be reversed — a backfill that overwrites cannot — and the header must
say so honestly rather than shipping a `DOWN` that silently loses data.

### 14.9 Bugs in §14.1–14.8, and their fixes

Re-read of the plan above. Nine defects; each would have bitten during execution.

**1 · `migrations/*.sql` would fail on the vector width.**
`setup_db.py:18` templates `VECTOR(EMBED_DIM)` into a real number before executing.
A migration runner reading raw SQL sends `VECTOR(EMBED_DIM)` to Postgres, which is a
syntax error. `arteries.evergreen` has a vector column, so migration 009 fails.
*Fix:* `migrate.py` applies the identical substitution, and a test asserts no
migration file contains the literal `EMBED_DIM` after templating.

**2 · The first run against live re-applies everything.**
Live already has every column `schema.sql` declares. `schema_migrations` is empty
there, so `art migrate apply` would run all of them. `IF NOT EXISTS` makes most
harmless, but not all — a backfill would re-run over real rows.
*Fix:* `art migrate baseline` stamps existing migrations as applied without executing
them. It runs **once**, against live, before any new migration. Without this step the
whole scheme is unsafe on the one database that matters.

**3 · The unique index on `ephemeral` fails, and NULLs make it useless.**
Two problems in one line. `session_id` is NULL on all 612 legacy rows, and NULLs are
distinct in a unique index, so legacy rows never dedupe and never conflict. Meanwhile
a straight `CREATE UNIQUE INDEX` aborts if the backfill left any genuine duplicate.
*Fix:* index on `COALESCE(session_id, '')`, and order the steps — backfill
`session_id`, backfill `fact_hash`, tombstone duplicates keeping the earliest row and
summing `seen_count`, *then* `CREATE UNIQUE INDEX CONCURRENTLY`. Four migrations, not
one.

**4 · `CREATE INDEX CONCURRENTLY` leaves an invalid index on failure.**
It fails without holding the lock, but the index name is now taken and marked invalid,
so the retry errors on a name collision and the planner ignores the index meanwhile.
*Fix:* every concurrent-index migration begins `DROP INDEX IF EXISTS`, and
`art migrate status` reports any `pg_index.indisvalid = false` as a defect rather than
staying silent.

**5 · Two checkouts can run `art migrate apply` at once.**
Nothing serializes the runner. Both read an empty `schema_migrations`, both apply.
*Fix:* `SELECT pg_advisory_lock(hashtext('arteries.migrate'))` for the duration of the
run. This is the one place a global lock is right — migrations are rare and must not
interleave.

**6 · `statement_timeout = '5min'` can kill a backfill mid-flight.**
Batched backfills are individually short, so the timeout is right for them — but it is
wrong for a single-statement migration on `agent_events`, which is the largest table.
*Fix:* the timeout is per-file, declared in the header (§14.8), and the runner refuses
a file that sets neither.

**7 · `RETURNING (xmax = 0)` is not portable enough to rely on.**
The trick reads a system column to distinguish insert from update. It works, and it is
the kind of cleverness someone decodes at 3am.
*Fix:* `RETURNING (xmax = 0) AS inserted` stays, but nothing branches on it —
`seen_count` tells the same story, durably, and is what the incrementality term reads
anyway.

**8 · `schema.sql` and `migrations/` will drift.**
A new column has to be added to both the `CREATE TABLE` body and a migration file.
Nothing enforces agreement, and `schema.sql:220` already records this exact drift
happening once — columns added to the live database by hand, `schema.sql` left for
later, and every fresh install broken by an INSERT naming columns that did not exist.
*Fix:* stop hand-editing `schema.sql`. It is **generated** —
`pg_dump --schema-only -n arteries` from a freshly migrated `arteries_test`, written
by `art migrate render`. The drift test then compares a generated file against a
committed one, which is a diff rather than a judgement call.

**9 · The test database needs `CREATE EXTENSION vector`.**
`schema.sql:5` requires it, and creating an extension needs privileges an ordinary
test role may not have. `createdb`/`dropdb` also fail while a connection is open —
including one a previous failed test left behind.
*Fix:* the fixture uses `template1` if `vector` is preinstalled there, otherwise
attempts the extension and **skips the whole Postgres-marked suite with a clear
message** if it cannot. It also runs `pg_terminate_backend` on stragglers before
`dropdb`. The suite must stay runnable on a laptop with no database — that is not a
nicety, it is the reason `conftest.py` grew its current shape.

### 14.10 Revised order of operations

```
once, ever:      art migrate baseline          # live only, stamps existing schema
per migration:   write file → apply to arteries_test → suite green
                 → (large table? rehearse on arteries_shadow, time it)
                 → pg_dump if destructive
                 → art migrate apply           # live, holds the advisory lock
                 → merge code to dev → soak → merge dev to main
after main:      contract migrations, separate PR
```

---

## 15. Concurrency, exactly

Every concurrency primitive this design uses, where it sits, and what it protects.
Nothing here is a thread pool.

### 15.1 Arteries

| Mechanism | Where | Protects against | Cost |
|---|---|---|---|
| **MVCC** (free, Postgres default) | every read | Readers blocking writers, and vice versa | none |
| **Unique index as arbiter** | intake dedupe, `idx_eph_dedupe` (§2.3) | Two sessions writing the same atom | none — optimistic, no lock taken |
| **`SELECT … FOR UPDATE SKIP LOCKED`** | `compile._claim_ephemeral` (`compile.py:191`) | Two compilers claiming one batch | one row lock, held briefly |
| **Lease + expiry** (`claimed_at` + stale sweep) | `compile._release_stale_claims` | A worker dying mid-batch and stranding rows | none; it is a timestamp comparison |
| **Attempt counter** (`attempts`, quarantine at 3) | `_claim_ephemeral` | A poison batch monopolising the queue head | one column |
| **Ordered `ON CONFLICT DO UPDATE`** | `graph.upsert_entity`, entities sorted by name | Deadlock between two compiles with overlapping entity sets | ordering only |
| **`pg_advisory_lock`, 2 slots** | `compile_once`, optional (§13) | More concurrent generation requests than `llama-server --parallel 2` can serve | one lock acquisition per turn |
| **`pg_advisory_lock`, 1 slot** | `art migrate apply` (§14.9 #5) | Two checkouts migrating simultaneously | migrations are rare |
| **asyncio + `httpx.AsyncClient`** | `_llm_compile` | Nothing — this is I/O waiting, single process, no shared state | none |
| **Process isolation** | one compiler subprocess per turn, fire-and-forget from the hook | A slow compile blocking the user's turn | a process spawn |

**Not used, deliberately:** thread pools, connection pooling (6 of 100 connections in
use), worker pools, drain loops. The audit is right that they optimise a resource idle
almost all the time.

**The one real serialization point is not the database.** A single compile slot drains
~1,260 rows/hour against a measured peak of 26. Postgres is not the bottleneck and
never will be at this scale. `llama-server` is: it runs `--parallel 2`, so worker
count is capped there regardless of what arteries does.

### 15.2 Capillaries

| Mechanism | Where | Notes |
|---|---|---|
| **`asyncio.Semaphore(4)`** | `db/embed.py:25,92,198`, `chunk.py:333` | Caps concurrent embedding requests. Batch-time only, not on the retrieval path |
| **`run_in_executor`** | `search/retriever.py:439-454` | Dense and sparse retrieval run in parallel against a blocking psycopg2 driver |
| **`asyncio.gather`** | `retriever.py:439`, `embed.py:60` | Fan-out within one query |
| **`fcntl` lock** | `daemon.py:20` | One uvicorn server per host, not a memory concern |
| **HNSW index scans** | `persistent`, `chunks`, `prompt_chunks` | Read-concurrent by construction |

Capillaries is read-mostly and its writes are batch jobs run by hand. It shares one
Postgres with arteries, so its concurrency story is MVCC plus whatever locks arteries
takes — which is why §14.6's lock discipline matters to both.

**One shared hazard worth naming:** capillaries and arteries both write vectors into
the same database against one `EMBED_DIM`. They already drifted silently once
(`config.py:30-45`) — capillaries moved to 1024 dims while arteries stayed at 768, and
every embedding write would have failed the moment compilation started working. That
is not a lock problem and no lock fixes it; the fix is the `embed_model` stamp and the
`art doctor` check in §7.

### 15.3 Upstream: heart and plexus

**Nothing new is needed at the database layer.** Concurrent `art observe` calls from N
agents are exactly what the unique index and `SKIP LOCKED` already handle, and
`observe.py:317` already files them under a stable per-source bucket rather than a
dead pid.

**Something is needed at the admission layer, and it belongs to heart.**

The failure is arithmetic, not correctness. Heart orchestrates N agents; each finished
turn spawns a compiler; each compiler wants a ~40s generation from one `llama-server`
running `--parallel 2`. At N=5 that is five requests for two slots, and the queue is
invisible — no error, just latency that looks like the model being slow. The audit
already measured what an unavailable generator does to this loop: 269 of 606 passes
failed, and every one of them claimed ten rows, flipped them to `compiling`, and
released them.

So the upstream requirement is **admission control, not locking**:

| Layer | Needs | Why not lower |
|---|---|---|
| **arteries** | The 2-slot advisory lock (§13, item 7) | Bounds concurrent compiles per database, which is the correct place for a per-database limit |
| **heart** | A concurrency cap on agent spawn, and a shared generation budget across its agents | Arteries sees one turn at a time and cannot know heart is about to spawn four more. Only the orchestrator has that number |
| **plexus** | Nothing. Goal decomposition is a planning step; its `art observe` writes are ordinary | Adding coordination here would be coordinating something that does not contend |

Two things heart should own, neither of which arteries can do for it:

1. **A semaphore on generation, not on agents.** Capping agents caps the wrong
   resource — agents spend most of their time not generating. Cap concurrent requests
   to `GENERATE_URL` at 2, matching `--parallel 2`, and let agents queue on that.
2. **A health gate before spawning.** Finding 25's `/health` probe belongs in heart
   too. Spawning five agents into a dead `llama-server` produces five failed compile
   passes and ten queue writes per turn, for the duration of the outage.

**The real fix for the 44% failure rate is outside all three repos** and the audit
says so plainly: a systemd unit with `Restart=always` on `llama-server`. Every lock
discussed above bounds the damage from that process being down. None of them keeps it
up.


### 15.4 The generation gate, concretely

Cap **2**. It is not tuned to how fast the model writes; it is the number of slots
`llama-server` was started with:

    --parallel 2 --ctx-size 98304        # 2 slots, 49152 tokens each

Qwen3.6-35B-A3B activates ~3B of 35B parameters per token, so each request finishes
sooner than a dense model of the same size would. That drains a queue faster. It does
not create a third slot. Setting the cap to 4 against `--parallel 2` moves the queue
from a place you can see and shed into llama-server's internal one, where a waiting
request looks identical to a slow one — which is the shape of the timeouts already in
the audit.

The cap is a mirror of the server flag. If `--parallel` changes, this changes with it,
so read it rather than hardcoding it twice.

```python
# heart/…/generation.py
import asyncio, os, time, httpx

_CAP = int(os.getenv("HEART_GENERATE_SLOTS", "2"))   # mirrors llama-server --parallel
_sem = asyncio.Semaphore(_CAP)                       # module level: one bowl per process
_down_until = 0.0                                    # circuit breaker

class Busy(Exception):
    """No slot within the wait budget. The caller defers; it does not fail."""

async def generate(client: httpx.AsyncClient, payload: dict, *, wait_s: float = 90.0):
    if time.monotonic() < _down_until:
        raise Busy("generation circuit open")
    try:
        await asyncio.wait_for(_sem.acquire(), timeout=wait_s)
    except asyncio.TimeoutError:
        raise Busy(f"no slot in {wait_s}s")
    try:
        r = await client.post(os.environ["GENERATE_URL"], json=payload, timeout=300.0)
        r.raise_for_status()
        return r.json()
    except (httpx.ConnectError, httpx.ReadTimeout):
        _open_circuit()
        raise Busy("generation unreachable")
    finally:
        _sem.release()

def _open_circuit(seconds: float = 30.0):
    global _down_until
    _down_until = time.monotonic() + seconds
```

Four things in there are load-bearing, and three of them are the bugs this shape
usually ships with:

1. **The semaphore is module level.** A `Semaphore` constructed inside the function is
   a fresh bowl with full tokens on every call, so it limits nothing. This is the most
   common way an admission gate silently does not exist.
2. **`release()` is in `finally`.** A raised exception that skips the release leaks a
   token permanently. Two leaks and the gate is closed forever — a deadlock that looks
   like the server hanging.
3. **The acquire is bounded.** Unbounded queueing is how one stuck request turns into
   every agent blocked behind it. After `wait_s` the caller gets `Busy`, not an error.
4. **`Busy` is not a failure.** The observation is already durable in `ephemeral`; the
   compile pass is what got deferred, and the next pass picks the same rows up. This is
   the safety mechanism: back-pressure never has to destroy work, because the work was
   written to Postgres before generation was attempted.

The circuit breaker covers the case the semaphore cannot: llama-server being *down*. A
gate sized to a dead server admits two requests, waits out both timeouts, admits two
more, forever. Thirty seconds of refusing after a connection error costs one slow turn
and saves the rest of the outage. It is the runtime half of Finding 25's `/health`
probe — the probe answers "should I spawn", the breaker answers "should I still be
sending".

**Per process, not per machine.** Two checkouts each hold their own bowl and sum to
four in-flight requests. That is what the 2-slot `pg_advisory_lock` in arteries is for
(§13); the semaphore is the cheap in-process half of the same limit.

---

## 16. When compilation runs: per turn, or once at session end

Raised 2026-09-07. The argument for batching: `seen_count` is an importance signal, and
per-turn compilation spends the promotion decision before that signal exists. A fact
stated on turn 3 and repeated on turns 9 and 20 is promoted at turn 3 with a count of 1.
The repeats still land — the intake `ON CONFLICT` bumps the row even after it is
`cleared` — but they arrive after the decision they should have informed.

### The trade

| | Per turn (today) | Once at `SessionEnd` |
|---|---|---|
| **Promotion sees `seen_count`** | No. Decides at first mention | Yes. The full session's count is in hand |
| **Model context per decision** | 10 rows, `MAX_EPHEMERAL_BATCH` | The whole session, so it can rank and cut across the set rather than judging rows in isolation |
| **Generation calls** | One per turn, most promoting nothing | One per session |
| **Load on `llama-server`** | Spread thin, but N agents × per-turn is the pile-up §15.4 exists for | One spike per session, easy to gate |
| **Persistent available mid-session** | Yes, one turn late | No. Nothing from this session is in persistent until it ends |
| **Crash safety** | Rows promoted before the crash survive | Nothing promoted. Rows stay `uncompiled` — recoverable only once §7's dead-PID claim bug is fixed |
| **Failure blast radius** | One turn's 10 rows | The whole session |
| **Cost of the 44% generator failure rate** | One turn lost, next turn retries | One session lost |

### What the live data says

681 ephemeral rows carry 658 distinct facts. Exact repeats are **3.4%** of rows, and
`persistent` has no exact duplicates at all (453 rows, 453 distinct facts). So on raw
text, `seen_count` is close to no signal — it would separate about twenty rows.

That number is not the one that matters. It measures raw turns, and the whole point of
normalizing to one-sentence atoms (§2.2) is to make restatements collide that do not
collide today. The post-normalization rate could be several times higher. Nobody knows
yet, and the argument for batching rests entirely on it.

`art dedupe --dry-run` in commit 7 measures it directly, against the same 681 rows.

### Decided 2026-09-07: promote once, at session end

Intake keeps running per turn in the background — extract, normalize, dedupe, insert,
embed. None of it calls the generator. Only **promotion** moves to `SessionEnd`, and
ephemeral rows clear only once that promotion succeeds.

Two objections in the table above do not survive contact with the actual design:

**"Persistent unavailable mid-session" only bites once per project.** Session 2 onward
reads everything session 1 promoted. The gap is the first session of a new project, and
in that session persistent is empty anyway — there is nothing to be unavailable. Within
any session, ephemeral already covers the working set, which is the tier's job.

**"Clear only on success" is already how it works.** `_write_results` opens one
transaction, writes persistent, and sets `status = 'cleared'` at `compile.py:699`, with
a single `conn.commit()` after both. A failed promotion rolls back the clear with it.
The property is a guarantee, not something to add.

Two costs are real and both have a fix already in the plan:

**Blast radius.** One session's rows in one pass is a bigger loss than one turn's ten,
against a generator that fails 44% of the time. Fix: chunk the promotion at
`MAX_EPHEMERAL_BATCH` rather than sending the session as one request. The count survives
chunking untouched — `seen_count` lives on the row and is computed by intake, not
derived from the batch — so chunking costs only cross-chunk ranking, not the signal that
motivated batching. It also keeps each call inside one 49152-token slot, which a long
session would otherwise overflow.

**A session that dies before `SessionEnd` promotes nothing.** Its rows stay
`uncompiled`, which is correct — but today they are stranded forever by the dead-PID
claim bug (§7). **Commit 3 is therefore a prerequisite, not an independent fix**: claim
on `session_id`, and let any claim older than `STALE_CLAIM_MINUTES` be taken by any
process in the project. With that, a crashed session's rows are promoted by the next
session in the project. Delayed, not lost.

`mentions` on persistent stays in the plan, with a narrower job: `seen_count` now gates
promotion within a session, and `mentions` accumulates the same fact recurring across
sessions and projects, where no single promotion pass can see it.

**The caution stands.** Repetition marks confusion as readily as importance — the same
error restated while stuck. `seen_count` is one input to ranking, not a promotion gate
on its own, and `access_count` remains the stronger signal because it counts times a
memory was *useful* rather than times something was *said*.

### Superseded: the per-turn recommendation


**Keep per-turn compilation. Add `mentions` to persistent instead.**

Batching buys one thing — `seen_count` at the moment of decision — and pays for it with
mid-session availability, crash safety and a whole-session blast radius against a
generator that fails 44% of the time. There is a cheaper way to get the same signal:
promote at first mention, and let later repeats update the row that already exists.

```sql
ALTER TABLE arteries.persistent ADD COLUMN mentions INT NOT NULL DEFAULT 1;
```

Intake already finds the ephemeral row by `fact_hash`; when that row has been promoted,
bump the persistent row it produced. Importance then accumulates rather than gating, and
it feeds the same places `access_count` does: eviction (§3.5) and incrementality (§4.2).

The distinction worth keeping in mind: **`mentions` is not quality.** Repetition in a
coding session often marks confusion — the same error restated while stuck — as readily
as it marks importance. It is one input to ranking, not a promotion gate. `access_count`
is the stronger signal, because it counts times a memory was *useful* rather than times
something was *said*, and it is already spread wide (356, 246, 235, 214, …).

**Revisit after commit 7.** If the dry run shows normalized repeats well above 3.4%,
batching becomes worth its costs and this decision flips.

---

## 17. What dev already has that this design never mentions

Surveyed 2026-09-07, after the pipeline design settled. Everything below is live code on
`dev` that the design either duplicates, contradicts, or silently invalidates.

### 17.1 Three side doors into persistent

The design says promotion goes one level at a time. Six modules write the tiers, and
three of them write **persistent directly, without ever touching ephemeral**:

| Path | Write | What it is |
|---|---|---|
| `art ingest` | `ingest.py:196` → `compile._write_results(conn, result, [], project)` | Documents → chunks → the same compile call the conversation path uses. The empty list in argument three is the claimed-ephemeral set: there is none |
| `art docs import` | `docs.py:242` → `storage.insert_persistent` | Markdown mined for candidate facts, a human approves a review file, accepted ones are inserted |
| `art remember` | `remember.py:81` → `storage.insert_persistent` | Explicit user or model statement, `scope='user'` |

None is wrong. Each exists because the ladder is the wrong shape for its input: a design
document has no session, and an explicit "remember this" has already cleared a higher bar
than any filter applies. But the design's one-level rule is written as though they do not
exist, and three of the four ways memory enters this system are outside it.

**They need a stated relationship to the ladder, not a rewrite.** Proposal: the rule is
*promotion* goes one level at a time. Direct insertion is an **authored** write, not a
promotion, and carries `evidence = 'user'` — the top of the ladder in finding 22 — so
contradiction resolution already prefers it over anything the extractor inferred. That
makes the exemption principled instead of accidental.

Two consequences follow and neither is in the plan:

- **§16 does not apply to them.** Session-end promotion is a session concept. `art ingest`
  and `art docs` run outside any session and promote at invocation. Fine, but the doc
  should say so rather than leaving a reader to assume `SessionEnd` gates everything.
- **Dedupe must cover them.** `seen_count` and `mentions` are computed by intake. A fact
  arriving through `art remember` has neither, so an identical fact later extracted from
  conversation cannot find it. `docs.py:235` already loads 1000 persistent rows and
  normalizes to compare — that is a third dedupe implementation, alongside intake's hash
  and compile's 0.97 cosine. Consolidate onto `fact_hash` at commit 7.

### 17.2 `docs.py` was the evergreen bootstrap, and evergreen is back

Its docstring, line 4:

> imports the ones a human accepts into persistent memory. **Was the evergreen bootstrap;
> the tier is gone**, the mechanism is worth keeping.

The tier is not gone. §4.5 plans manual core seeding from `planning/*.md` via a new
`art evergreen seed` — which is `art docs extract` + `art docs import` with a different
destination. 465 lines of working, human-reviewed extraction already exist for exactly
this job.

**Seeding should be `art docs import --tier evergreen --kind core`, not a new command.**
Human review is the right gate for core facts, and it is already built and already used.

### 17.3 `ingest.py` already builds part of §4's graph

`documents` (17 rows) and `chunks` (40 rows) are populated. Each claim carries a
`derived_from` edge back to its chunk and document, so provenance from a graph node to a
source span already works. §4 describes this as new. §9 does not list `ingest.py`.

The real question is entity extraction: `ingest.py` routes chunks through the same
compile call, so whatever ontology work lands in commit 8 changes document ingestion too,
untested. Commit 8 needs a document-path test or it will regress this quietly.

### 17.4 `setup_cli.py` and the migration runner

1640 lines, the largest module in the repo, and `setup_cli.py:114` calls
`setup_db.setup()`, which applies `schema.sql` wholesale. §14 introduces `migrations/`
and `art migrate baseline`. Two ways to create a schema, and the newer one has to know
the older one exists.

**`art setup` must call `migrate baseline` after applying `schema.sql`** — otherwise
every freshly set-up checkout has an empty `schema_migrations` and re-runs the entire
migration history on its first `art migrate apply`. That is migration bug #2 from §14.9,
arriving through a door §14 was not looking at.

### 17.5 Smaller, noted not planned

- **`scope` is almost entirely NULL** — 507 of 508 persistent rows. The one `user` row is
  `art remember`. So `get_persistent(..., scope='user')` works and nothing else uses the
  column. Evergreen is scope-group-keyed (§4.1); decide whether this column is that
  concept or a different one before commit 9 reuses the word.
- **`kind` is doing real work already**: 332 `fact`, 112 `decision`, 46 `preference`,
  18 `constraint`. The parked `preference` eviction exemption (§13) is not hypothetical —
  it protects 46 live rows.
- **`degrade.py`** classifies environment failure from code defect, written after seven
  defects shipped green behind `except Exception`. Every new module in this plan should
  use it rather than adding a bare handler.

### 17.6 Do the side doors reach evergreen?

Two of them, under one rule: **an authored write enters at the tier its evidence
justifies, and the operator names that tier. It never skips a level implicitly.**

| Path | Enters at | Why |
|---|---|---|
| `art remember` | persistent | A stated preference or constraint. Durable, but one person saying one thing once is not yet a fact about the project. It competes for evergreen on incrementality like anything else |
| `art ingest` (default) | persistent | A design document is a claim source, not a settled truth. Its claims age into the graph normally |
| `art ingest --kind core` / `art docs import --tier evergreen` | **evergreen, as `core`** | The initial specs (§4.5). Human-reviewed before insert, which is a stricter gate than incrementality, not a weaker one |

That last row is the only direct write to the graph, and it is the one §4.5 already
called for. It does not violate the one-level rule, because the rule governs
*promotion* — an automatic, unattended move. A human choosing a tier is an authored
write, and the human is the gate.

The two guards it needs:

- **`origin = 'authored'` on the node.** Mutation rules (§4.6) treat `core` as
  unsupersedable by inference; that protection only works if the graph can tell an
  authored core node from a promoted one.
- **`incrementality` stays NULL, not 0.** A seeded node never scored, and writing a
  zero would make it look like something that scored badly and got in anyway.

---

## 18. Tier weights with evergreen (commit 11)

```python
# packet.py:256
TIER_WEIGHT = {"ephemeral": 1.00, "persistent": 0.95, "evergreen": 0.90}
```

Evergreen sits **below** persistent, not above. A graph fact is more durable but less
situationally specific: it is true about the scope group, while a persistent claim is
true about this project and an ephemeral one is about the last hour. The existing
ordering already encodes that, and evergreen extends it in the same direction.

### On making the weights sum to 1

The two properties asked for are mutually exclusive, so this is worth stating plainly.

`_score` is a product, not a mixture:

    score = similarity × confidence × TIER_WEIGHT[tier]

Every input is in [0, 1], so **every score is already in [0, 1]**. Scores do not sum to
anything — each row carries its own, and they are used only to rank rows against each
other and to fill `MAX_PACKET_MEMORIES = 15` slots.

Normalizing the weights to sum to 1 divides all three by 2.85:

    ephemeral 0.35   persistent 0.33   evergreen 0.32      # sums to 1.00

That is a *uniform* rescale, so it changes no ranking whatsoever — the same 15 rows enter
the packet in the same order. What it does change is the ceiling: no row could then score
above 0.35, and a perfect match would read as a third of a point. `similarity` is still
compared to `MEMORY_SIMILARITY_FLOOR = 0.55` *before* the weight is applied, so the floor
is unaffected either way.

**Recommendation: keep the max weight at 1.00.** Weights summing to 1 would be right if
the three tiers split a fixed budget between them — they do not; every row is judged on
its own. Keeping 1.00 at the top is what makes a score directly readable as "how good is
this row, 0 to 1", which is the property actually wanted. Normalize at display time if a
share-of-total view is ever useful.

One cost either way: `score` is written into packet provenance (`packet.py:288`) and
marrow reads that log. Rescaling makes episodes recorded before and after the change
incomparable. A no-op for ranking is not a no-op for training data.

---

## 19. Two decisions, 2026-09-07

### 19.1 Eviction never touches `preference` or `constraint`

Decided. Age and disuse are not evidence that a preference stopped being true — a
preference read once a month is still true every day in between. These rows leave only
one way: **superseded by a newer statement of the same preference or constraint.**

```sql
-- evict.py: the exemption
AND kind NOT IN ('preference', 'constraint')
```

The supersede path needs one guard, or the exemption is worthless. A preference stated
by the user must not be overwritten by one the compiler *inferred* from watching a
session. Contradiction resolution already has the ladder for this (finding 22):

    user > observed > stated > inferred

So: a `preference` or `constraint` row may only be superseded by a row of **equal or
higher** evidence class. An inferred "prefers spaces" loses to a stated "I prefer tabs"
and is recorded as a `contradicts` edge instead of a supersede — visible, not silently
applied.

Protects 64 live rows today (46 `preference`, 18 `constraint`).

### 19.2 Drop `persistent.scope` rather than rename it

`scope` holds provenance — `NULL` compiled, `'user'` from `art remember`, `'reviewed'`
from `art docs` — and collides with scope *groups*, the concept evergreen is keyed on
(§4.1). The rename to `origin` was tried in `e5d9df1` and reverted in `d40ff8e`, because
a feature branch renamed a column the live main checkout was reading.

Renaming is now safe under §14's expand/migrate/contract. It is also unnecessary.

**One non-NULL row exists**, out of 508. The column's only consumer is
`storage.get_persistent(..., scope='user')`, which backs `art remember list`. Meanwhile
`source_meta JSONB NOT NULL DEFAULT '{}'` is already on the table and already carries
`art docs` provenance (path, line span, digest, review id).

So: write `source_meta->>'origin'` instead, and drop the column in a contract migration.
That ends the name collision permanently rather than moving it, and removes a column
instead of adding a migration to rename one. `art remember list` filters JSONB on a
listing query — unindexed, and irrelevant at this size.

**`evidence` is still a separate column, and still gets added.** It answers a different
question: `origin` is which door a row came through, `evidence` is how strongly it is
known. A `PostToolUse` observation enters through the conversation door with `observed`
evidence. Contradiction resolution orders on `evidence` and needs it indexed; nothing
orders on `origin`.

Order of operations, since this is the first real exercise of the migration runner:

    expand:    ALTER TABLE arteries.persistent ADD COLUMN evidence TEXT;   -- additive
    migrate:   backfill source_meta with origin, dual-write for one release
    contract:  ALTER TABLE arteries.persistent DROP COLUMN scope;          -- after main is on the new code

---

## 20. Better retrieval, in order of payoff

Arteries retrieves with a single dense cosine query. The measured result is in
`packet.py:238`: over 237 real plexus queries, the *top* hit per query runs p25 0.50,
p50 0.55, p90 0.66, max 0.78. A median query's best match sits exactly at the floor. That
is not a tuning problem, it is a single-strategy problem.

### 20.1 Hybrid dense + sparse, fused with RRF — port it, do not invent it

`capillaries/src/capillaries/search/retriever.py` already does this: pgvector dense
search and BM25 sparse search, merged with reciprocal rank fusion. Postgres-native both
sides — `tsvector` + GIN, which `schema.sql:170` already uses for `art search` over
`agent_events`. No new dependency, and the pattern is running in production next door.

Why it matters more here than in a general RAG system: memory queries in a coding session
are full of **exact identifiers** — `frame_compat.py:76`, `UndefinedColumn`,
`SELECT … FOR UPDATE SKIP LOCKED`, `EMBED_DIM`. An embedding compresses those into the
same 0.45–0.55 technical-prose band as everything else, which is precisely why the top
hits cluster at the floor. Lexical match retrieves them exactly, and RRF lets a row win
on either signal without needing the two scores to be commensurable.

Port `_to_or_tsquery` verbatim along with it. It quotes every token via `ts_parse` because
an unquoted `textkit/__init__.py` or a bare `&` is read as tsquery *syntax* and kills the
whole query. Real prompts are full of those, so it is the common case. That function is
the part that would otherwise be rediscovered the hard way.

**Correction: capillaries abandoned RRF on its serving path.** `retriever.py:40-48` says
so plainly — production goes through `union.union_candidates_broad`, which unions both
channels and lets a reranker order them, with no weighting anywhere. `search()` and
`_rrf_merge` survive *only* as a benchmark comparison arm and are marked for deletion.

So "port the pattern from capillaries" needs splitting. What is worth taking is the two
**channels** and `_to_or_tsquery`. The **fusion** is arteries' own decision, and capillaries'
measurement says RRF is the weaker of the two options it tried. Arteries cannot take the
stronger one: union-plus-rerank costs a model call per query against the two `llama-server`
slots that are already the bottleneck (§15). So the honest expectation is that arteries
gets the smaller of the two wins capillaries measured — still worth having, because the
gain over dense-only is where most of the value sits, but not the number capillaries sees.

**Cons.** RRF fuses by *rank*, so `MEMORY_SIMILARITY_FLOOR` stops meaning anything — a row
can be sparse-rank 2 with a cosine of 0.3, and the entry criterion has to be re-derived
from scratch rather than tuned. It needs a `tsvector` column and GIN index on `persistent`,
which is a migration against the shared live table. And the sparse arm returns noise for
short queries, exactly the turns §20.3 wants skipped anyway. Real cost: a migration, a
retrieval rewrite, and a full benchmark re-derivation.

### 20.2 Reinstated — the flat 0.5 is not neutral, it is high

Withdrawn once, then re-examined, and the withdrawal was wrong. It was withdrawn for
being about ordering *within* ephemeral. The damage is *across* tiers.

`_score` is multiplicative:

    score = similarity × confidence × TIER_WEIGHT[tier]

Ephemeral takes `NEUTRAL_SIMILARITY = 0.5` and `TIER_WEIGHT 1.00`, so its rows score
0.50. A persistent row must therefore reach:

    needed_sim = 0.50 / (0.95 × confidence)

| confidence | rows | similarity needed to outrank *any* ephemeral row |
|---|---|---|
| 1.00 | 110 | 0.526 |
| 0.95 | 69 | 0.554 |
| 0.90 | **268** | 0.585 |
| 0.85 | 7 | 0.619 |
| 0.80 | 62 | 0.658 |
| 0.70 | 11 | 0.752 |

Against the measured distribution of *top* hits — p25 0.50, p50 0.55, p90 0.66, max 0.78:

- **82% of persistent rows** (everything at confidence ≤ 0.95) need an above-median match
  just to tie an ephemeral row.
- The **modal** row, confidence 0.90 and 53% of the corpus, needs roughly a p70 match.
- A confidence-0.70 row needs 0.752 against an observed maximum of 0.78. It is
  effectively unreachable no matter how relevant it is.

And `_select_ephemeral` returns **up to 20 rows** for 15 slots. So on a median query,
ephemeral alone can fill the packet and persistent contributes nothing — not because
persistent had nothing good, but because 0.5 was picked as a stand-in for "unknown" and
happens to sit above most of the real score distribution.

The earlier dismissal — "binds only when more than 15 candidates compete" — was wrong on
its own terms. Twenty ephemeral candidates already exceed fifteen slots, before persistent
or evergreen contributes anything.

**Two defects, not one.** The constant is the visible one. The deeper one is that
`confidence` *multiplies* similarity, so it scales relevance instead of adjusting it, and
a low-confidence claim becomes unreachable rather than merely disfavoured. Confidence was
meant as a tie-breaker and is behaving as a gate. Finding 4 already argued confidence
should be an annotation rather than a gate; this is the same defect in the ranking
function.

**The right fix is §20.4, not similarity scoring.** Scoring ephemeral by cosine trades one
arbitrary comparison for another and still strips continuity when the topic shifts. Per-tier
slots remove the need to compare an ephemeral score against a persistent score at all,
which is the actual category error. See the verdict table below.

### 20.3 Do not retrieve on every turn

Retrieval runs unconditionally at `UserPromptSubmit`. Many turns are "yes", "continue",
"do that" — a centroid of nothing, which returns weak matches that still clear the floor
often enough to displace good rows.

`eval.py` already has `_triage_skip_reason`, which skips acknowledgements and clear
continuations *for the corpus arm*. Apply the same triage to memory retrieval. Saves an
embed call per skipped turn and, more importantly, stops injecting noise into turns where
the model already has its context in the live conversation.

### 20.4 Per-tier slots — promoted to first

Was listed last as a commit 11 detail. §20.2 shows it is the fix for the real defect, so
it moves to the front.

The category error: `_score` compares an ephemeral row's constant against a persistent
row's cosine as if they were the same quantity. They never were. Per-tier slots make the
comparison unnecessary — rank *within* each tier, on that tier's own policy, and give each
tier a guaranteed allocation:

    ephemeral   6 slots   by recency          (continuity: what just happened)
    persistent  6 slots   by cosine           (relevance: what we know)
    evergreen   3 slots   by graph proximity  (structure: what connects)

Unfilled slots spill to the tier with the most refused candidates, so an empty evergreen
costs nothing on a fresh project.

**Rejected 2026-09-07.** Preassigned slots decide in advance how much of each tier a turn
needs, and that varies per turn. A turn where one persistent claim is the only thing that
matters should be able to spend most of the packet on persistent.

**The replacement: fuse the tiers by rank, not by score.** The category error in §20.2 is
comparing an ephemeral constant against a persistent cosine. Ranks fix that without fixing
the allocation — each tier ranks its own candidates on its own policy (ephemeral by
recency, persistent by cosine, evergreen by graph proximity), and the tiers are merged by
reciprocal rank fusion:

    fused = Σ over tiers  weight_tier / (RRF_K + rank_within_tier)

No tier has a guaranteed share. A turn with three strong persistent hits gives persistent
three of the top slots; a turn with none gives it zero. `TIER_WEIGHT` keeps its current
meaning as a mild prior, and the incommensurable-scale problem disappears because a rank
is a rank in every tier.

It is also the same machinery as §20.1 — RRF fuses dense and sparse there, and tiers here.
One implementation, two uses.

Open question it inherits: `MEMORY_SIMILARITY_FLOOR` has to move from the fused score to
the per-tier ranking, or a tier with nothing relevant still contributes its rank-1 row.
Keep the 0.55 floor on the persistent arm before ranking, which is where it was measured.

**Cons of the rejected fixed-slot version, for the record.** Fixed allocation is its own
arbitrary choice — 6/6/3 is a guess, and a
turn genuinely dominated by one tier gets worse results than a global ranking would give.
It also cannot express "this persistent row is the single thing that matters this turn".
The counter is that the current global ranking cannot express that either, because it is
ranking on incommensurable scales; a defensible arbitrary split beats an indefensible
comparison. Re-derive the split from `art benchmark`, and make it config, not literals.

### Deliberately not doing

- **Cross-encoder reranking.** The standard next step, and capillaries has `rerank_score`.
  It costs a model call per query against the two `llama-server` slots that are already
  the system's bottleneck (§15). Revisit only if the retrieval loop stops being the
  cheapest part of a turn.
- **A vector database.** 508 persistent rows. pgvector with HNSW (`schema.sql:115`) is
  not the limiting factor and will not be for years.
- **LLM query rewriting.** Same cost objection as reranking, and hybrid retrieval solves
  most of what rewriting would: the reason a raw message retrieves badly is usually a
  literal token the embedding lost, not a phrasing the model could improve.

### 20.5 Verdict

| # | Change | Fixes | Real cost | Verdict |
|---|---|---|---|---|
| **4** | Per-tier slots | The category error: comparing a constant to a cosine. On a median query persistent currently gets zero slots | ~30 lines in `_load_memories`, no migration, no new query | **Do first.** Highest ratio in the list, and it is the fix §20.2 was reaching for |
| **3** | Retrieval triage | Retrieval running on "yes"/"continue" turns, injecting weak matches | Reuses `_triage_skip_reason`; mostly wiring | **Do second.** Small, and it cleans the benchmark before anything downstream is measured against it |
| **1** | Hybrid dense + sparse | Exact identifiers being compressed into the 0.45–0.55 prose band | Migration + retrieval rewrite + benchmark re-derivation. Fusion is the arm capillaries deprecated | **Do third, own commit.** Biggest single win, and the only one that needs its own before/after measurement |
| **2** | Score ephemeral by cosine | Nothing 4 does not fix better | Loses continuity on topic shift; 27% of rows have no embedding | **Diagnosis kept, remedy rejected.** The finding was right and the proposed fix was not |

Sequencing matters more than usual: 4 changes what enters the packet, 3 changes which
turns retrieve at all, and 1 changes what retrieval returns. Landing 1 first would measure
a new retriever through a ranking function that discards most of its output.

---

## 21. What hybrid retrieval actually bought capillaries

Asked 2026-09-07, before committing to §20.1. Numbers from
`capillaries/docs/rework_actions.md`.

### 21.1 The measured gains

Fusion versus the best single channel, on the two query populations:

| population | best single channel | best hybrid | delta |
|---|---|---|---|
| naming ("13-week cash flow model") | sparse-only 93.7% | hybrid 0.3/0.7 → **95.0%** | +1.3 pp |
| describing (conversational) | dense-only 17.2% | hybrid 50/50 → **22.0%** | +4.8 pp |

On the golden set (n=20): dense 14/20, sparse 17/20, hybrid+rerank 17/20 — hybrid ties
sparse. That set is lexically contaminated, sharing literal words with the titles it
expects, which is the one shape where BM25 wins.

**Fusion is worth a few points, not a transformation.** The doc's own conclusion is that
RRF beats query routing on *both* populations, which is a real result — but the win is
+1.3 and +4.8 points.

### 21.2 The gain arteries cannot inherit

The largest retrieval win in capillaries had nothing to do with fusion. Its dense channel
was dead:

| probe | `snowflake-arctic-embed-m-v2.0` | `qwen3-0.6b` | random |
|---|---|---|---|
| verbatim self-retrieval, rank 1 | **1 / 40** | — | ~0 |
| `notes`-as-query recall@10 | **0.8%** | **24.9%** | ~1.1% |
| `notes`-as-query recall@1 | **0.4%** | **8.8%** | ~0.1% |

Arctic scored at the random baseline. Swapping the embedding model was a 30× gain — and
BM25 had been silently carrying the whole system, which is *why* sparse looked so strong
in the golden-set table above.

**Arteries already runs Qwen3-Embedding-0.6B** (`packet.py:238`). It does not get this
win, because it never had this bug. Any claim that "hybrid transformed capillaries"
conflates the fusion change with the embedding-model fix that happened alongside it.

### 21.3 Contradiction in the capillaries sources — unresolved

`retriever.py:40-48` says the serving path uses `union.union_candidates_broad` and that
RRF survives only as a benchmark arm. `rework_actions.md:360` says union-then-rerank was
built, measured, and **reverted** for losing 15 points of R@10, leaving RRF as the
incumbent. Both cannot be current. §20.1's earlier "capillaries abandoned RRF" was based
on the code comment alone and should not be relied on until this is checked against the
actual serving path. Flagged for capillaries, not resolved here.

### 21.4 How arteries differs, and what to expect

| | capillaries | arteries |
|---|---|---|
| document | a prompt chunk, paragraphs | one normalized sentence |
| corpus | ~970 chunks / 2072 embedded | **492 live persistent rows**, scope-group-wide |
| slice returned | top 10 ≈ 1% | top 15 ≈ 3% |
| what is actually *searched* | every chunk | persistent only — ephemeral is fetched, not searched |
| vocabulary | curated prompts, human-authored | facts the system wrote from the same sessions it later queries |

Three of these cut **against** a large sparse gain:

1. **BM25 is weak on one-sentence documents.** Term frequency is 0 or 1 everywhere and
   length normalization has nothing to work with. Capillaries' chunks are paragraphs.
2. **Taking the top 3% of 508 rows is an easier problem** than the top 1% of 2072.
3. **Query and document share an author.** Arteries writes the facts it later retrieves,
   from the same sessions — vocabulary mismatch, the thing dense retrieval exists to
   bridge and sparse cannot, is rarer here than in a curated corpus.

One cuts **for** it, and it is the reason to still do this: arteries' queries are full of
exact identifiers — `frame_compat.py:76`, `UndefinedColumn`, `EMBED_DIM` — which is
precisely capillaries' "naming" population, where sparse alone scored 93.7%.

**Honest expectation: a few points on identifier-bearing queries, near zero elsewhere.**
Roughly capillaries' +1.3/+4.8, not more.

### 21.5 The measurement trap, which matters more than the change

Capillaries' hardest-won lesson is that two of its three benchmarks were contaminated:
the `title` benchmark leaks the query into `search_tsv` at weight A, and the golden set
shares literal words with expected titles. Both flatter sparse. Its own summary of the
episode: *"Sequencing error — measure first."*

`art benchmark` has the right shape — recall@k and MRR against LLM-written paraphrase
queries with the source claim as label, the same design as capillaries' clean `notes`
benchmark. **But it has capillaries' contamination risk built in**: queries are generated
*from the claim*, so unless the generator is pushed to avoid the claim's own vocabulary,
they will share tokens with it — and a shared-token benchmark systematically over-reports
a sparse channel.

So before §20.1 is measured at all:

1. Save a query set (`art benchmark --save`) on the current dense-only retriever. That is
   the before number, and it must exist before the change, not after.
2. Verify held-out-ness the way capillaries did: check what fraction of each generated
   query's tokens appear literally in its target claim. If it is high, the benchmark will
   report a sparse win that real traffic will not reproduce.
3. Only then port the channels, and compare on the identical saved set.

Step 2 is the one capillaries skipped, and it cost them a rebuild.

### 21.6 What the retrieval corpus actually is

Clarified 2026-09-07, because "the corpus" is easy to over-count.

Only **persistent** is searched. `get_persistent_by_relevance` runs the cosine query
across the caller's whole scope group, over live rows:

| project | live persistent | embedded |
|---|---|---|
| heart | 229 | 229 |
| arteries | 214 | 214 |
| capillaries | 46 | 46 |
| marrow | 3 | 3 |
| **total searched** | **492** | **492** |

`scope_members` puts all five harness repos in one scope, so any project's query searches
all 492. Every live row is embedded, and `schema.sql:115` gives them an HNSW index.

**Ephemeral is not a search corpus.** `_select_ephemeral` calls
`storage.get_ephemeral(project_id, agent_id, limit=20)` — this project, this agent, most
recent 20. No query vector is involved, there is no vector index on `arteries.ephemeral`,
and the tier holds 28 live rows right now (699 total, the rest `cleared`). The only
similarity query that ever touches it is `max_ephemeral_similarity` for the coverage gate,
which sequentially scans this agent's rows.

This is the missing premise under §20.2. Ephemeral gets `NEUTRAL_SIMILARITY` not because
scoring it was overlooked, but because it is **selected rather than retrieved** — there is
no similarity to carry forward. Scoring it on cosine is therefore not a scoring change; it
means adding a vector query and an index to a tier designed as a recency buffer. That
raises the cost of the rejected §20.2 remedy well beyond what was implied, and strengthens
the rank-fusion answer in §20.4: rank ephemeral on the policy it already has.

Two smaller consequences:

- **`chunks` (40 rows, HNSW-indexed) is a third embedded corpus** and is not in the memory
  retrieval path at all. `art ingest` writes it; only `documents`/`chunks` provenance reads
  it. Evergreen (§4) is where it should connect.
- **The scope group makes the corpus cross-project by default.** A query from heart already
  searches arteries' 214 rows. This is the behaviour §4.1 wants for evergreen, and it is
  already true for persistent — worth knowing before treating cross-project reach as new.

### 21.7 Retrieval method per tier

Four paths, not one. Each tier answers a different question, so each is read differently.

| tier | method | keyed on | filter | ordering | index |
|---|---|---|---|---|---|
| **ephemeral** | direct fetch, no query | `project_id` + `agent_process_id` | `status='uncompiled'`, `valid_until IS NULL` | `source_ts DESC`, limit 20 | none — sequential scan |
| **persistent** | cosine vector search | scope group (`SCOPE_CTE`) | `valid_until IS NULL`, `embedding IS NOT NULL`, `similarity ≥ 0.3` | cosine distance, limit 20 | HNSW (`schema.sql:115`) |
| **graph** | traversal from persistent seeds | top 5 seeds | scope on the *claim* side, `valid_until IS NULL` | `edge_weight × decay × seed_similarity`, limit 8 | b-tree on `memory_edges` |
| **fallback** | recency | project scope | live rows | `source_ts DESC`, limit 20 | — |

`storage.py:53` — ephemeral. `storage.py:141` — persistent. `graph.py:110` — expand.
`memory_select.py:214` — the fallback, taken when the embedder is down or nothing is
embedded, and logged rather than silently degraded.

**The graph is not a fourth store.** `graph.expand` starts from persistent seeds and
returns persistent claims; `memory_edges` and `entities` are a layer *over* the tier, not
beside it. Two traversals run at once: direct claim-to-claim edges (`refines`, `supports`,
`contradicts`, `supersedes`), and claim → `mentions` → entity ← `mentions` ← claim. The
second is the one that pays — 52 mentions edges produce 210 co-mentioning pairs against 33
direct edges, and an earlier version walking only direct edges returned nothing useful.

**Expansion is gated, not automatic.** `route.choose(seeds)` picks `cosine` or
`cosine+expansion` per query and logs the decision to the action ledger.

**Two different thresholds, easy to confuse.** `RELEVANCE_THRESHOLD = 0.3` cuts the SQL
query — what comes back from the database at all. `MEMORY_SIMILARITY_FLOOR = 0.55` cuts
packet entry — what is worth showing. A row can pass the first and fail the second.

**Why the asymmetry is right.** Ephemeral answers "what just happened in this session",
which does not depend on the current message; searching it by similarity would answer a
question nobody asked. Persistent answers "what do we know that bears on this message",
which is a similarity question. The graph answers "what connects to what we just found",
which is a traversal question and needs the other two to have run first.

`doctor.unreached` records that `graph.expand`, its edge traversal, the expansion gate,
the benchmark context, `MemoryFrame.scope` and `max_ephemeral_similarity` were each
written, tested, and never called. Confirm anything here is reachable before building on
it.

**Where §4's evergreen lands.** It becomes the first tier with its own *store* and its own
traversal, rather than a layer over persistent. That is the real structural change in this
rework, and it is why the graph question and the tier question are the same question here
but were not before.

---

## 22. The three retrieval changes, consolidated

Per-tier slots rejected (§20.4). Three changes remain, and **none of them touches a
persistent row**. Two change the query, one changes the ranking; the only schema effect is
an added `tsvector` column and GIN index.

### 22.1 Target path

```
UserPromptSubmit
  │
  ├─ TRIAGE (§20.3) ─────────── "yes" / "continue" / acknowledgement → skip retrieval
  │
  ├─ ephemeral    fetch by project + agent, live, source_ts DESC, 20     [unchanged]
  │                 └─ rank within tier: recency
  │
  ├─ persistent   dense cosine (HNSW, scope group, sim ≥ 0.3)            [unchanged]
  │             + sparse tsvector/BM25 (new)                             [§20.1]
  │                 └─ RRF within tier → one persistent ranking
  │
  ├─ evergreen    graph traversal from persistent seeds, hop-bounded
  │                 └─ rank within tier: decayed edge score
  │
  └─ RRF ACROSS TIERS (§20.4) ── weight_tier / (RRF_K + rank_in_tier) → top 15
```

### 22.2 What each change is worth, and what it costs

| | changes | cost | expected gain |
|---|---|---|---|
| **triage** | which turns retrieve at all | reuses `eval.py:_triage_skip_reason`; wiring | no recall gain. Removes noise turns from the benchmark, which is what makes the other two measurable |
| **hybrid** | what the persistent query returns | migration (tsvector + GIN), retrieval rewrite, benchmark re-derivation | capillaries measured +1.3 pp on naming queries, +4.8 pp on describing. Expect the low end (§21.4) |
| **rank fusion** | how tiers are combined | ~40 lines in `_load_memories` | fixes persistent contributing **zero rows on a median query** (§20.2). Largest of the three, and the cheapest |

### 22.3 RRF appears twice, and that is worth watching

Once inside persistent (dense + sparse), once across tiers. The two are independent uses of
the same function, but composing them loses information: a claim that was rank 1 on *both*
channels and a claim that was rank 1 dense and rank 40 sparse arrive at the tier fusion as
the same "persistent rank 1". Confidence in a row does not survive into the second stage.

Not fatal, and not worth pre-solving. But if hybrid measures worse than expected, this is
the first place to look — carry the intra-tier fused score through as a tie-breaker before
concluding the sparse channel does not help.

### 22.4 Order, and why

1. **Rank fusion.** Biggest effect, no migration, and until it lands the packet is mostly
   ephemeral on a median query — so any retrieval improvement underneath it is invisible.
2. **Triage**, then save a benchmark query set. This is the *before* number, and it has to
   be recorded on the improved ranking, not the old one.
3. **Hybrid**, own commit, measured against that identical saved set.

Landing hybrid first would measure a better retriever through a ranking function that
discards most of its output.

---

## 23. Retrieval as it actually runs, one turn, with line numbers

Traced 2026-09-07 against `dev`. Constants are the live values, not the function defaults.

### 23.1 The call chain

    hooks/arteries-observe.js
      └─ eval.evaluate(message)                                    eval.py:132
          └─ packet.build_packet(message, event, provenance)       packet.py:189
              └─ packet._load_memories(...)                        packet.py:267
                  ├─ embed_text_sync(message, is_query=True)       one HTTP call
                  └─ memory_select.select_for_frame(...)           memory_select.py:77
                      ├─ _select_ephemeral(context)                memory_select.py:102
                      ├─ _select_persistent(message, ctx, emb)     memory_select.py:178
                      │   ├─ storage.get_persistent_by_relevance   storage.py:128
                      │   ├─ route.choose(seeds)                   route.py:51
                      │   └─ _expand(seeds[:5], ctx, limit=8)      memory_select.py:136
                      │       └─ graph.expand(...)                 graph.py:110
                      └─ _prior_attempt_at_this_task filter        memory_select.py:88

### 23.2 Step by step

**Ephemeral — no query involved.** `EPHEMERAL_MODE = "compile"`, so
`storage.get_ephemeral(project_id, agent_process_id, limit=20)`:

```sql
WHERE project_id = %s AND agent_process_id = %s
  AND status = 'uncompiled' AND valid_until IS NULL
ORDER BY source_ts DESC LIMIT 20
```

This agent's own unpromoted rows, newest first. A subagent also pulls up to 10 of its
parent's, deduped to 20. Right now `heart` has **0 live ephemeral rows** — all 302 are
`cleared` — so in this project the tier contributes only what the current process just
wrote.

**Persistent — cosine, and the threshold is off.** `PERSISTENT_READ = "relevance"`, so
`get_persistent_by_relevance(project_id, query_emb, limit=20, threshold=RELEVANCE_THRESHOLD)`.

`RELEVANCE_THRESHOLD` is **0.0** (`config.py:93`), not the `0.3` in the function signature.
Nothing is cut in SQL; the 20 nearest rows come back whatever their similarity. The
`SCOPE_CTE` expands to all five harness projects, so 20 rows out of 492 live candidates.

**Routing.** `route.choose(seeds)` counts seeds at or above `STRONG_SIMILARITY = 0.65`.
Five or more (`ENOUGH_STRONG`) and the strategy is `cosine` — return the seeds, no graph
walk. Fewer, and it is `cosine+expansion`. The decision is logged to the action ledger.

**Graph expansion.** `_expand(seeds[:5], limit=8)` → `graph.expand(hops=1, decay=0.6)`.
Two traversals in one query: direct `persistent→persistent` edges, and
claim → `mentions` → entity ← `mentions` ← claim. Each neighbour is scored
`edge_weight × decay`, then multiplied by the similarity of the seed it was reached from,
and written back onto the row as `similarity` so it is judged on the same axis as a direct
hit.

**Scoring and cut.** `_score` (`packet.py:258`) refuses anything carrying a similarity
below `MEMORY_SIMILARITY_FLOOR = 0.55`, then scores
`similarity × confidence × TIER_WEIGHT`. Ephemeral has no similarity, takes
`NEUTRAL_SIMILARITY = 0.5`, and skips the floor. Sort, take 15, dedupe against the previous
summary, render into sections under a byte budget.

### 23.3 Finding: graph expansion can never reach the packet

Arithmetic, not a guess. A neighbour's final similarity is:

    direct edge:    weight(1.0) × decay(0.6) × seed_similarity  =  0.60 × seed_sim
    shared entity:  decay(0.6) × decay(0.6) × seed_similarity   =  0.36 × seed_sim

To clear `MEMORY_SIMILARITY_FLOOR = 0.55`:

| path | seed similarity required |
|---|---|
| direct claim-to-claim edge | **0.917** |
| shared entity (the common one) | **1.528** — impossible, cosine is capped at 1.0 |

The measured maximum top-hit similarity is **0.78** (`packet.py:238`, 237 plexus queries).
So no expanded neighbour has ever entered a packet, and none can.

It is worse than that, because the two gates fight each other. Expansion runs *only* when
fewer than five seeds reach 0.65 — that is, only when seed similarity is low, which is
exactly when the product is smallest. The condition that triggers the walk guarantees the
walk's output is discarded.

`doctor.unreached` (`doctor.py:156`) lists `graph.expand` and "the expansion gate" among
six things written, tested, and never invoked. They are invoked now. The output is thrown
away one function later, which the reachability check cannot see.

**Fix, and it is not a new constant.** The floor was measured on *query-to-claim cosine*
(`packet.py:238`). A decayed hop score is a different quantity on a different scale, and
comparing them was the same category error as §20.2's constant-versus-cosine. Under §20.4's
rank fusion this dissolves: expansion results are ranked within the evergreen/graph arm on
their own scale and fused by rank, never compared to a cosine floor. Until then, the honest
stopgap is to exempt `via_graph` rows from the floor and cap their count.

This is the strongest argument in the document for doing §20.4 first: an entire retrieval
mechanism is running, costing a database round trip per weak query, and contributing
nothing.

---

## 24. Retrieval readiness, per path

Status of every path that compares an incoming message against a store. Checked against
`dev` and live data, 2026-09-07.

| # | path | code | verdict |
|---|---|---|---|
| 1 | **Ephemeral fetch** — recency, agent-scoped, no query | `storage.py:53` | **Good, one data caveat.** Query is correct and cheap. But `agent_process_id` falls back to `str(os.getpid())` when `ARTERIES_AGENT_ID` is unset (`config.py:58`), and 84 of 717 rows carry a bare PID across 83 distinct ids — one row each, unretrievable and unclaimable forever. Hook paths export a stable name (`heart-hook`, 314 rows) and are fine. Commit 3's `session_id` key fixes retrieval and claiming together |
| 2 | **Ephemeral → packet scoring** — `NEUTRAL_SIMILARITY = 0.5` | `packet.py:258` | **Rework.** Not a retrieval defect, a comparison defect. 82% of persistent rows need an above-median match just to tie a flat ephemeral row, so on a median query persistent contributes nothing (§20.2) |
| 3 | **Persistent cosine** — HNSW, scope-group-wide | `storage.py:128` | **Good to go.** Correct, indexed, and the only path with a measured quality baseline (237 queries, p50 0.55). Note `RELEVANCE_THRESHOLD = 0.0` (`config.py:93`) so nothing is cut in SQL — the packet floor does all the cutting. Harmless, but the `0.3` in the function signature is misleading and should be dropped |
| 4 | **Persistent sparse / BM25** | — | **Does not exist.** Optional addition (§20.1). Expect capillaries' +1.3/+4.8 points, not more (§21) |
| 5 | **Expansion gate** — `cosine` vs `cosine+expansion` | `route.py:51` | **Sound, untestable today.** The rule (fewer than 5 seeds at 0.65 → walk) is reasonable and logged to the action ledger. It cannot be evaluated while its consumer discards everything |
| 6 | **Graph expansion** | `graph.py:110` | **Broken. Finding 26.** Output can never clear `MEMORY_SIMILARITY_FLOOR`; needs 0.917 seed similarity for a direct edge against a measured ceiling of 0.78, and 1.528 for the shared-entity path. Ships with finding 15 |
| 7 | **Recency fallback** — embedder down or nothing embedded | `memory_select.py:214` | **Untested.** Degradation is announced rather than silent, which is right. Nothing exercises it; one test that stubs the embedder to fail and asserts a non-empty frame |
| 8 | **Coverage gate** — `max_ephemeral_similarity` | `storage.py:262` | **Test before trusting.** Sequential scan over this agent's live ephemeral, gating the capillaries corpus call only. `doctor.unreached` lists it among the six written-but-never-called. It is called now; whether 0.92 is the right abstain point has never been measured |
| 9 | **Evergreen / graph store** | — | **Does not exist.** §4 builds it |

### Summary

**Good to go: 1 and 3.** The two paths that actually run on every turn are sound. The
ephemeral fetch needs `session_id` to stop losing PID-keyed rows; the persistent cosine
query needs nothing.

**Rework before measuring anything: 2 and 6.** Both are the same defect — a score on one
scale compared to a score on another. Rank fusion (§20.4) fixes both at once, and until it
lands every number from `art benchmark` describes a ranking function that discards graph
results entirely and buries persistent behind a constant.

**Test, do not change: 5, 7, 8.** Each is plausible code with no evidence behind it. Two of
the three are on `doctor.unreached`.

**Build: 4 and 9.** Optional and required respectively.

### Order for accurate numbers from the start

1. Fix 2 and 6 — rank fusion. Nothing measured before this is meaningful.
2. Add 1's `session_id` key. Otherwise the ephemeral arm is silently missing rows.
3. Add tests for 5, 7, 8, then `art benchmark --save`. **This is the baseline.**
4. Only then build 4 and 9, each measured against that saved set.

---

## 25. Retrieval findings vs. the planned commits

The plan in §12 is an *ingestion* rework: intake, dedupe, promotion, evergreen, decay. It
touches retrieval once, at commit 11. Everything in §20–24 was found afterwards. Mapping
the two:

| retrieval finding | planned commit | status |
|---|---|---|
| Confidence multiplies similarity, gating instead of annotating (§20.2) | **6 — "Confidence is an annotation, not a gate"** (findings 4, 18) | **Already planned.** The plan named this defect before it was measured in the ranking function. Commit 6 edits `packet._score`, which is the exact site |
| Ephemeral keyed on `agent_process_id`; 84 rows under bare PIDs unreachable (§24.1) | **3 — "Give every session its own working memory"** | **Already planned**, but framed as a *claiming* fix. It is equally a *retrieval* fix, and the commit message should say so |
| Graph expansion can never clear the floor (finding 26) | **11 — "Close the loop"** | **Adjacent, not covered.** Commit 11 adds the evergreen retrieval arm; it does not fix the arm that already exists |
| Rank fusion across tiers (§20.4) | — | **New.** Naturally belongs with 11 |
| Hybrid dense + sparse (§20.1) | — | **New.** No commit anywhere near it |
| Retrieval triage (§20.3) | — | **New** |
| Benchmark baseline before corpus changes (§24) | — | **New, and it conflicts with the plan's order** |

### The conflict worth acting on

Commit 11 is late — after 5 through 9 have changed the filter, the dedupe, the ontology,
and added a whole tier. Those commits change **what is in the store**. Retrieval fixes
change **how the store is read**. Measure after both and neither is attributable.

Worse, the baseline itself decays: `art benchmark` builds ground truth *out of the store*,
so a query set saved before commit 5 and re-run after commit 9 is scored against a
different corpus. It is not the same measurement.

And the retrieval defects are not dev-only. `main` carries identical constants,
`_score`, `route.choose` and `_expand` — `graph.py` and `route.py` do not differ between
the branches at all. The checkout running the hooks has been paying for a traversal whose
output the floor rejects.

### Recommended split

**Branch A — `retrieval-floor-fix`, off `main`, merged to `main` first.** Small, no schema
change, fixes a live defect:

1. Rank fusion in `_load_memories`; graph rows ranked on their own scale (findings 26, 15)
2. Finding 15's `contradicts` labelling, which must ship in the same commit
3. `session_id` on the ephemeral read path — the retrieval half of planned commit 3
4. Tests for the three untested paths (§24: fallback, coverage gate, expansion gate)
5. `art benchmark --save baseline.json` — **the before number, on a corpus nothing has
   touched yet**

**Branch B — `ingestion-redesign`, off `dev`, as planned in §12.** Commits 0–12 unchanged,
except that commit 6 inherits the ranking work already done in A, and commit 11 has only
the evergreen arm left to add.

**Then hybrid and triage**, measured against `baseline.json` from A. They are retrieval
changes and do not belong in an ingestion branch at all.

The cost of not splitting is one number: after a single branch containing both, there is no
way to say whether retrieval improved, the corpus improved, or one masked a regression in
the other. That is the mistake capillaries made and spent an audit undoing (§21.5).

---

## 26. Branch B as ingestion **and** retrieval rework

Asked directly: does §12 already cover hybrid RRF, three-tier retrieval in the steady
state, and every audit finding? **No, no, and no.** Three gaps, all fixable by adding
commits rather than restructuring.

### 26.1 Hybrid RRF — absent

No commit in §12 touches the persistent query. §20.1 was written after the plan. It needs
its own commit: a `tsvector` column and GIN index on `persistent`, the sparse channel,
`_to_or_tsquery` ported from capillaries, and RRF within the tier.

### 26.2 Steady-state retrieval — half covered, never described

Commit 11 adds "the evergreen retrieval arm". It does not say what retrieval *is* once a
project is twenty sessions in and all three tiers are populated. That state is different
in kind, not just in size:

| tier | at session 1 | at session 20 |
|---|---|---|
| ephemeral | this session's rows, ~20 | unchanged — the tier is session-scoped by design |
| persistent | near-empty; the screen is a no-op | 500+ rows; cosine has real competition and the floor starts binding |
| evergreen | empty; contributes nothing | the graph carries the project's structure, and is the only tier that grows without bound |

Three consequences the plan does not state:

1. **Ephemeral's `TIER_WEIGHT = 1.00` was set when it was competing against a near-empty
   persistent tier.** At session 20 the same constant means the last twenty turns outrank
   a project's accumulated knowledge. Under rank fusion this is a weight to re-derive from
   `art benchmark`, not a constant to keep.
2. **Evergreen is the only unbounded tier**, so it is the only one where the hop bound and
   the entry criterion do real work. Persistent at 500 rows and 15 slots is an easy
   retrieval problem; evergreen at 5,000 nodes is not.
3. **The screen from persistent back to ephemeral (§3.4) inverts.** At session 1 it never
   fires. At session 20 it fires constantly, and becomes the main reason a true-but-known
   fact is not re-recorded — which is where `mentions` gets its value.

### 26.3 Audit coverage — four findings have a mechanism but no commit

Cross-checked mechanically against §12:

| finding | state |
|---|---|
| 2, 17 | Fixed 2026-09-04. Correctly unassigned |
| **12** — cold start wastes an embedding call | Mechanism in §1, **no commit** |
| **15** — `contradicts` co-retrieves both sides unmarked | Mechanism in §1, **no commit** |
| **20** — network call inside the compaction path | Mechanism in §1, **no commit** |
| **26** — graph expansion cannot clear the floor | **New, no commit** |

15 and 26 must ship together: 15's harm is latent only because 26 discards everything
`expand` produces.

### 26.4 Revised commit list

Additions in **bold**. Existing numbers keep their contents.

| # | commit | why here |
|---|---|---|
| 0 | Test database | unchanged |
| **A** | **Rank fusion; graph on its own scale; `contradicts` labelled** (findings 15, 26) | **First.** A live defect in `main`, and until it lands no benchmark number means anything (§24) |
| 1 | `claimed_at` | unchanged |
| 2 | Health probe, quarantine — **plus finding 12's cold-start guard** | same file, same pass |
| 3 | `session_id` — **read path as well as claim path** | it is a retrieval fix too (§24.1) |
| **B** | **Retrieval triage** (§20.3) | cheap, and it removes noise turns before the baseline |
| **C** | **`art benchmark --save baseline.json`** | **the before number, on a corpus nothing has touched yet** |
| 4–9 | retention, filter, confidence, dedupe, ontology, evergreen tier | unchanged. Commit 6 inherits A's ranking work |
| **D** | **Hybrid dense + sparse RRF** (§20.1) | measured against C |
| 10–12 | Gephi, close the loop, decay | 11 now only has the evergreen arm left to add |
| **E** | **Finding 20 — corpus fetch out of the compaction path** | packet-side, groups with 11 |
| 13–14 | observations, housekeeping | unchanged |

C is the load-bearing addition. Every commit from 4 onward changes what is *in* the store,
and `art benchmark` derives its ground truth from the store — so a query set saved after
commit 5 is not comparable to one saved before it. The baseline has to exist before the
corpus moves.

A is the other one that cannot slide: it fixes a defect running in `main` today, and it
gates the meaning of every measurement after it.
