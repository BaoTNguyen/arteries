# Continuity packet v2: state fields, watermarks, and retraction

Status: spec, not built. Supersedes the section layout in `packet.py:build_packet`
and the section names in `.arteries/codex/compact_prompt.txt`.

## 0. Why v1 needs replacing

The survey of Claude Code 2.1.260, Codex 0.153.2, OpenCode 1.18.27 and Cursor
2026.07.09 is input, not a finding. It shows up here only where a design decision
depends on it. What follows is the criticism of what we currently ship.

| # | Defect in v1 | Where | Consequence |
|---|---|---|---|
| 1 | 55% of the budget goes to `Recent Conversation` | `_allocations` | We spend most of a 20k packet re-sending the ten turns the host CLI still holds verbatim. Claude's third prompt and Cursor's two watermarks both exist to *avoid* exactly this. |
| 2 | Dedupe against the host summary is substring containment on tokenised text | `_dedupe_memories` | A paraphrase in the host's summary walks straight through it. Dedupe by string is a guess; dedupe by watermark is arithmetic. |
| 3 | No packet identity and no range | `build_packet` | `previousSummary` is read and never written back. Nothing records which rows entered the last packet, so "what is new since" has no query — only a heuristic. Every packet rebuilds from zero. |
| 4 | Contradiction resolution runs in `compile.py` and never reaches the packet | `compile.py:620` | Supersession is async, batched, LLM-driven, and terminal: the loser gets `valid_until = now()` and disappears behind every `valid_until IS NULL` filter. The agent that believed the wrong thing is never told it was wrong, so it re-derives it. |
| 5 | Within-session contradictions are not resolved at all | `_load_memories` | Two ephemeral rows saying A and ¬A both clear the floor, both rank by cosine, and both enter the same packet. Recency is not even a tiebreak in `_score`. |
| 6 | The similarity floor gates retractions too | `MEMORY_SIMILARITY_FLOOR` | A correction is relevant because it is a correction, not because it resembles the trigger message. At 0.55 against the wrong trigger, the one line that prevents a re-derivation is the line that gets dropped. |
| 7 | `_score` multiplies by `ephemeral.confidence`, which schema.sql documents as unused | `_score` | The ephemeral half of the ranking is cosine alone, wearing a confidence term that is always 1.0. |
| 8 | Sections are provenance tiers | `build_packet` | Current Context / Recent / Ephemeral / Persistent / Use Rules says where a fact came from. A reader resuming work needs to know what the fact *is*. |
| 9 | A 60-second HTTP call sits in the compaction path | `_corpus_suggestion` | `urlopen(..., timeout=60)` runs on every packet build, compaction included. Hooks have host-side timeouts. A slow capillaries turns "compaction is enriched" into "compaction produced nothing", which is the one failure mode a continuity packet cannot have. Skip the corpus when the trigger is compaction. |

Items 1–3 are why the packet cannot chain. Items 4–6 are why it cannot say a thing
was wrong. Item 9 is why it may not arrive at all.

## 1. Packet schema

Every field is always rendered. Empty renders as `(none)`. A missing `Blocked`
section is ambiguity, and ambiguity is what makes a reader re-derive.

| Field | Type | Card. | Source | Empty form |
|---|---|---|---|---|
| `packet_id` | uuid | 1 | minted at build | — |
| `previous_packet_id` | uuid \| null | 0–1 | last row in `packet_log` for (project, session) | null = cold start |
| `covers_from` | timestamptz | 1 | previous packet's `covers_to`, else session start | — |
| `covers_to` | timestamptz | 1 | `now()` at build | — |
| `resume_from` | turn id | 1 | last turn folded into this packet | — |
| `objective` | text | 1 | latest `decisions` row of type `task.objective`, else first user turn in range | `(unknown)` |
| `constraints` | text[] | 0–n | `persistent` where `kind IN ('preference','constraint')` | `(none)` |
| `decisions` | {chosen, rejected, why}[] | 0–n | `arteries.decisions` in range | `(none)` |
| `state.done` | text[] | 0–n | `ephemeral`/`persistent` `kind='fact'`, evidence `observed` | `(none)` |
| `state.in_progress` | text[] | 0–n | open `episodes` (`status='running'`) | `(none)` |
| `state.blocked` | text[] | 0–n | facts tagged domain `blocker`, failing commands from the journal | `(none)` |
| `retracted` | {believed, refuted_by, evidence, resolved_at}[] | 0–n | §3 | `(none)` |
| `unresolved` | {a, b, why_tied}[] | 0–n | §3, tie case | `(none)` |
| `open_question` | text | 0–1 | last assistant turn ending in a question with no following user turn | `(none)` |
| `files` | {path, why}[] | 0–n | journal `file.read` / `file.edit` in range | `(none)` |
| `next` | text | 1 | one concrete action | `(unknown)` |
| `dropped` | text[] | 0–n | §5 budget overflow record | `(none)` |

`retracted` is the field none of the four surveyed CLIs has. `previous_packet_id`
+ `covers_from`/`covers_to`/`resume_from` are Cursor's chaining expressed against
our own ledger.

### Storage

One new table and one new column. Nothing else.

```sql
CREATE TABLE IF NOT EXISTS arteries.packet_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    cli             TEXT NOT NULL,
    previous_id     UUID REFERENCES arteries.packet_log(id),
    covers_from     TIMESTAMPTZ NOT NULL,
    covers_to       TIMESTAMPTZ NOT NULL,
    resume_from     TEXT,
    row_ids         JSONB NOT NULL DEFAULT '[]',  -- what was compiled in
    body            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Evidence class. `source` (user|assistant) says who spoke; this says how it
-- was known, which is what §3's precedence ladder ranks on.
ALTER TABLE arteries.ephemeral
    ADD COLUMN IF NOT EXISTS evidence TEXT NOT NULL DEFAULT 'stated';
    -- observed | stated | inferred
```

Judgment call: `evidence` is a schema change rather than a heuristic over
`source`, because the precedence ladder is the whole method and deriving its key
input by guesswork at render time would make every adjudication unauditable.
`extract.py` already knows whether a fact came from a tool result or from prose.

Judgment call: `row_ids` is JSONB rather than a join table. It is written once,
read once, and never queried by element.

## 2. Assembly

No model call on the hot path. Every emitted line carries a row id, so it cannot
hallucinate — and it works under Cursor, which exposes no compaction hook at all.

```
build(session, cli, budget):
    prev      = packet_log.latest(project, session)
    window    = (prev.covers_to if prev else session_start, now())
    rows      = ledger rows in window          # ephemeral, decisions, journal, episodes
    conflicts = detect(rows + prev.row_ids)    # §3
    packet    = render(fields, conflicts)
    fit(packet, budget)                        # §5
    packet_log.insert(...)
    return packet
```

Cost: two indexed queries and a render. Milliseconds, against a summarisation
call for all four surveyed CLIs.

Corpus retrieval (`_corpus_suggestion`) is removed from this path. It is a
retrieval feature that rode in on a shared function; on a compaction trigger it
is a 60-second liability with no continuity value.

## 3. Contradiction resolution

### 3.1 Detection

Cheap and deterministic, in this order. None of these requires a model.

1. **Existing supersede edge.** `memory_edges WHERE rel='supersedes'`. Already
   written by `compile.py`. Free — the work is done, we just never rendered it.
2. **Value overwrite.** Two facts with cosine ≥ 0.85 whose extracted literals
   differ. A literal is a number, a path, a flag, a version, or a quoted
   identifier. High similarity plus differing literals is the signature.
3. **User correction.** A user turn opening with a negation marker (`no,`,
   `actually`, `that's wrong`, `I meant`, `not `) pairs against the assistant
   claim in the immediately preceding turn.

Model-assisted, and strictly optional:

4. **Semantic negation** — same subject, opposite predicate, no differing
   literal ("the hook fires before compaction" / "the hook does not fire before
   compaction"). Only runs on pairs above the cosine threshold that (2) and (3)
   did not already classify. If the model is unreachable, these pairs are simply
   not detected. Nothing else degrades.

Deliberately out of v1: **refutation by tool result** — assistant says "tests
pass", journal holds a non-zero exit. Highest-value detector here, and the only
one needing claim→command pairing logic we do not have. Build it when the
journal carries a `command.result` event with an exit code and the extractor
tags the claim it settles.

### 3.2 Precedence

```
user     > observed > stated > inferred
```

- **Across classes, the stronger wins regardless of order.** A later inference
  never overturns an earlier observation. This is the rule that separates the
  method from "hope recency wins", which is what all four surveyed CLIs do.
- **Within a class, later wins.** Two observations of the same file are a
  change, not a dispute.
- **A user statement is overturned only by a later user statement.** The user is
  the authority on intent, preference, and what they asked for.
- **Genuine tie** — same class, same subject, no ordering — resolves to nothing.
  Both go to `unresolved` with a note. Never silently pick. A wrongly retracted
  true fact is worse than a carried-forward stale one, because the retraction is
  believed.

### 3.3 The near-miss guard

Two statements are *not* a contradiction when any holds:

- different subject: differing path, entity, or identifier token
- different scope: one about a file, one about a config default
- different time and both marked as such ("was X", "now Y") — that is a value
  change, rendered under `decisions`, not `retracted`
- one is a subset of the other rather than its negation

The guard runs before adjudication and its output is a drop, never a retraction.

### 3.4 Rendering

One line each. The retracted claim appears nowhere else in the packet as live.

```
- Believed: EMBED_DIM is 1024. Refuted by: config.py:EMBED_DIM = 768 (read, turn 19).
```

### 3.5 Persistence

A resolution settled in session N is not re-litigated in session N+1:

- loser gets `valid_until = now()` (existing tombstone convention)
- a `memory_edges` row `rel='supersedes'` with `metadata.reason` records why
- the retraction line renders for the remainder of the session and in the *next*
  packet, then stops. It has done its job once the wrong fact is out of context
  and nothing has re-asserted it.

Judgment call: two packets, not forever. A permanent retraction list grows
without bound and eventually costs more budget than the re-derivation it prevents.

## 4. Worked example

A real-shaped session on this repo. Two genuine contradictions, one near-miss
that must survive.

```markdown
## Continuity packet
covers 2026-09-03T14:02Z → 2026-09-03T17:41Z · chains a41c…9f · resume from turn 63

## Objective
Cut the compaction packet over to state fields and get retraction rendering, so
a wrong fact stops surviving compaction.

## Constraints
- stdlib only in heart; arteries may use psycopg2 and the existing venv
- no new dependency for anything a few lines cover
- packet build must never fail a turn

## Decisions
- Chose: watermark chaining over string dedupe. Rejected: keeping
  `_dedupe_memories`. Why: a paraphrase in the host summary defeats containment.
- Chose: `evidence` as a column. Rejected: deriving it from `source`. Why: the
  precedence ladder is unauditable if its key input is guessed at render time.

## Work state
### Done
- schema.sql takes the `packet_log` table and the `evidence` column cleanly
- `_corpus_suggestion` confirmed as the 60s call in the compaction path
### In progress
- rewriting `_allocations`; recent-conversation share not yet chosen
### Blocked
- (none)

## Retracted
- Believed: EMBED_DIM is 1024. Refuted by: config.py reads 768 (observed,
  turn 19). Inference lost to observation.
- Believed: the packet budget defaults to 6000. Refuted by: user statement,
  "no, it's 20000, I changed it last week" (turn 41). Assistant claim lost to user.

## Unresolved
- (none)

## Open question
- (none)

## Relevant files
- src/arteries/packet.py: the rewrite target; `_allocations` and `build_packet`
- src/arteries/schema.sql: packet_log + evidence column
- .arteries/codex/compact_prompt.txt: names the v1 sections, must be regenerated

## Next
Rewrite `build_packet` to render the v2 field list against the existing loaders.
```

The near-miss: the session also established that `compact-packet.sh` deliberately
leaves `RERANKER_DEVICE` unset, and that `.arteries/env` sets it to `cuda:1`.
Cosine 0.91, differing literals — detector (2) fires. The subject guard drops it:
different files, both true. It never reaches `retracted`.

## 5. Budget and overflow

Never drop the oldest item silently. That is Codex's failure mode and it is
information loss disguised as success.

Drop order, first to go:
1. `files` entries beyond the five most recently touched
2. `decisions` rationale, keeping chosen/rejected
3. recent-conversation excerpts (the host still has these; see §0.1)

Never dropped: `retracted`, `unresolved`, `open_question`, `state.blocked`,
`next`. If those alone exceed the budget, the packet emits them and lists the
rest under `dropped` by name.

Suggested split, replacing v1's 55% to recent conversation: state 30, decisions
15, retracted 15, files 10, constraints 10, objective/next 10, headroom 10.
Judgment call, not measured. `art benchmark` should re-derive it.

## 6. Delivery matrix

The body is byte-identical across all five. Only delivery varies.

| CLI | Mode | Mechanism | On failure | Verify |
|---|---|---|---|---|
| pi | replace | `packet --format pi-compaction-json` (exists) | fall back to augment | canary in next turn |
| opencode | replace | hand over the compaction output | override | canary |
| codex | override | PreCompact hook rewrites `.arteries/codex/compact_prompt.txt` with the packet inlined, then the remote call carries it | stale prompt file → packet from previous build, log it | canary |
| claude | augment | `SessionStart(matcher: compact)` stdout — the only stdout the model sees. PreCompact stdout is discarded. | write file + AGENTS.md pointer | canary |
| cursor | none | write `.arteries/packet.md`, point AGENTS.md at it | — | canary |

`cli_caps.py` already carries the right axes and the right values; codex is
correctly `can_override_compact_prompt=True, can_replace_compaction=False` — a
local prompt file, remote execution. No changes needed there.

Codex is the interesting delivery: `compact_prompt` is static text, so the only
way a fresh packet crosses the wire is for the PreCompact hook to rewrite the
file before compaction fires. Cheap, and it means the file on disk is always the
last packet — which doubles as Cursor's delivery too.

**Canary:** each packet embeds one short random token. Grep the next turn's
transcript for it. A delivery mode that silently does nothing is otherwise
indistinguishable from one that worked, and three of the five above can fail
that way.

## 7. Edge cases

| Case | Behaviour |
|---|---|
| Cold start, no prior packet | `covers_from` = session start, `previous_packet_id` = null. Full build. |
| Refuting evidence itself retracted | Retraction is voided; both claims go to `unresolved` with the chain named. |
| Ledger and transcript disagree | Ledger wins for state, transcript for verbatim quotes (`open_question`). The transcript is what the model saw; the ledger is what was observed. |
| Packet over budget | §5. Explicit `dropped` list, never silent. |
| Host compacts without firing a hook | Cursor's normal case. The file on disk is the packet; staleness is bounded by build frequency. Build on every N turns, not only on compaction. |
| Conflict spans a session boundary | Tombstone + supersedes edge carry it. §3.5. |
| Contradiction where both rows are `inferred` | Later wins, but the retraction line says `(inference over inference)` so the reader knows the ground is soft. |
| Compaction fires during compaction | `packet_log` unique on (session, covers_to); second build returns the first. |

## 8. Not building

- **Refutation by tool result** (§3.1). Build when the journal carries exit codes
  and the extractor tags claims to commands.
- **A retraction ledger UI.** `art trace` already reads `memory_edges`.
- **Per-CLI packet bodies.** One body, five deliveries. If a CLI ever needs a
  different body, that is a delivery bug first.
- **Tuning the §5 split before measuring it.** The numbers are a starting point.
