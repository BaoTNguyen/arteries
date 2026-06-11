# Design Exploration Session 001 — 2026-06-08

## System Context Established

The arteries project is a memory layer that populates and manages a `MemoryFrame` consumed by an existing prompt-retrieval gate in the `prompt-system` repo. The gate is stateless — it reads the frame but never mutates it. Arteries owns all write, eviction, scoring, and lifecycle logic.

The gate runs a 3-stage decision pipeline:
1. Heuristic check (kills greetings, short messages)
2. Memory check — reads the MemoryFrame via `POST /agent/route`
3. Embedding proximity — snowflake-arctic-embed-m-v2.0, reranked by mxbai-rerank-base-v2

Corpus: ~849 private prompts, median 2,203 chars. 114 are Image Gen prompts (12.4%) that surface as false positives.

---

## Q&A — Decisions Made

### Session Boundaries

**Q: What is the "session" boundary? Is a "session" a single chat conversation, a working block, or something else?**

A: Sessions start when the user launches Claude or any other agent in a terminal. A conversation can be held in this terminal over time where the user may have different work sessions (starting and ending different sessions within the same conversation).

### Caller Architecture

**Q: Who is the caller today? Chat UI, automated agent, CLI tool?**

A: Agents call the route API. Arteries sits between the agents and the gate, or alongside agents as something they query before making the route call.

### User Model

**Q: Single-user or multi-user?**

A: Single user, but different memory stores (partitioned by something other than user identity — likely project, domain, or agent context).

### Memory Store Triggers

**Q: What triggers "different memory stores"?**

A: Triggers depend on the memory tier level:
- **Evergreen** always applies within a project scope
- **Persistent** applies across different sessions within that project
- **Ephemeral** applies to the current session only

### Memory Survival & Escalation

**Q: When a session ends, what should survive into the next session?**

A: Only persistent and evergreen memory survive. Memories follow an **escalation ladder** — one step at a time:

```
Ephemeral → compiles into → Persistent → compiles into → Evergreen
```

Promotion is always one step (never ephemeral straight to evergreen). Each tier is a distillation of the one below it. This ensures memories are accurate for any situation the user is in.

### Compilation Logic

**Q: What does "compile" mean concretely?**

A: Compile means extracting insights through relevant filters and summarizing if what's extracted is too long.

### Memory Formation

**Q: What triggers ephemeral → persistent compilation?**

A: Memory formation at the ephemeral level works similarly to how current chatbots compile memories across conversations — pull out notable signals from the conversation as it happens. Promotion from persistent → evergreen is the novel part where RLVR comes in.

### RLVR for Promotion

The user is considering using Reinforcement Learning with Verifiable Rewards for the promotion decision (persistent → evergreen). The idea: "should this persistent memory be promoted to evergreen?" becomes a decision a model makes, verified after the fact by checking if the promoted memory proved useful in future sessions.

---

## Decisions Made — Session 2 (Integration, Ingestion, Architecture)

### Integration Surface

**Q: How does arteries receive conversation data?**

A: Arteries runs as a background process observing the current ongoing conversation directly. It sees all memory tiers and the last 10 message/response pairs (including autonomous agent turns). It is NOT in the prompt-system gate's request/response path as middleware — it is an independent observer/enhancer.

### Enhancement Strategy

**Q: How does arteries enhance responses?**

A: Arteries augments agent replies (post-response enhancement), NOT input injection. Rationale: input injection risks silent, incorrect guidance steering the agent in wrong directions. Reply augmentation keeps the agent's reasoning uncontaminated and lets arteries add context visibly.

### Gate Relationship

**Q: How does arteries connect to the prompt-system gate?**

A: Arteries is in the hot path (Option A — synchronous). It evaluates every turn, decides whether to trigger a gate call, populates the MemoryFrame, and forwards to the gate. The gate fires when the recent conversation context (last 10 pairs + relevant memory) matches a prompt/skill in the corpus. Arteries' core responsibilities:
1. Decide what conversation details become memories
2. Maintain domain understanding (solves the narrow-band embedding problem)
3. Act as a topic filter for memory database searches
4. Trigger gate calls and populate MemoryFrame when retrieval is warranted

### Ingestion Architecture — Dual-Track

**Q: What's the latency budget and ingestion approach?**

A: Two parallel ingestion tracks:

**Sync track — Small local model (~30-80ms per turn)**
- A fine-tuned 1-3B parameter model running locally
- Reads the last 10 message/response pairs each turn
- Extracts candidate memories with domain tags and confidence scores
- Handles gate trigger decisions and surfaces top-5 most relevant memories
- Feeds **ephemeral memory**
- RLVR fits naturally here — this is the model that gets trained with verifiable rewards

**Async track — Background LLM extraction (~1-3s, non-blocking)**
- LLM call fires in the background after each turn — compiles immediately, no batching delay (freshness first)
- Does deep extraction: nuance, implied context, multi-turn reasoning, complex contradictions
- Available by next turn (one-turn-behind is a non-issue since sync track covers the current turn from the 10-pair window)
- Reads existing persistent records as context during compilation for contradiction resolution
- Feeds **persistent memory**

**Evergreen compilation — Heuristic-based.** Same insight appearing in 3+ projects' persistent memory triggers promotion to global evergreen. Cross-project deduplication handled by async LLM during compilation to central persistent table.

### Memory Retrieval

**Q: How are memories surfaced at query time?**

A: In-memory vector search (same approach already used in prompt-system for the prompt database). Arteries surfaces up to 5 most relevant memories for injection. RLVR helps determine relevance based on previous work conversation samples.

### Compilation Flow & Contradiction Handling

**Q: How does memory flow through the tiers, and how are contradictions resolved?**

**Full compilation pipeline:**

```
Subagent ephemeral → (compile-back) → Parent ephemeral → (async LLM) → Persistent → (3+ project heuristic) → Evergreen
```

**Key rules:**
- Subagents **never write to persistent directly** — all subagent memories funnel through the parent's ephemeral space first
- Only the parent agent's async LLM compiles into persistent — single compilation path per agent tree
- The async LLM reads existing persistent records during compilation, resolving contradictions on the way in
- Contradictions from child compile-back coexist in parent ephemeral without resolution — the async LLM resolves them during ephemeral → persistent compilation
- Independent top-level agents on the same project compile to persistent independently — the narrow race window where both compile simultaneously is self-correcting on the next pass since each reads existing persistent records

**Compilation lifecycle with backpressure handling:**

1. Each turn, async compilation fires immediately for newest uncompiled ephemeral records (freshness first, no batching)
2. Records marked `status = compiling` when async call starts — prevents double-compilation and protects from compile-back interference
3. On successful compilation: source ephemeral records are cleared, persistent records written
4. Always keep full context (not extracted memories) for the last 10 message/response pairs
5. If turns arrive faster than compilation can process: uncompiled records accumulate naturally
6. **Safety cap**: if uncompiled ephemeral records hit the context limit, oldest uncompiled records are evicted to a durable overflow queue (same Postgres instance), not deleted
7. Async track drains overflow queue FIFO when current compilation finishes and no new urgent records are waiting
8. Hard cap on overflow queue — if exceeded under sustained backpressure, oldest records are truly evicted (genuine system overload, not a design flaw)

**Ephemeral record statuses:** `uncompiled` → `compiling` → `cleared`

**Contradiction resolution mechanism:**
- Ephemeral → persistent: async LLM resolves contradictions during compilation, reading existing persistent records as context. Old records get `valid_until = now`, new records get `valid_from = now`, lineage links them.
- Persistent → evergreen: handled during cross-project promotion (same insight in 3+ projects). Async LLM detects semantic equivalence.
- Evergreen contradictions: rare, simple overwrite-with-lineage. User manually reviews a few times per year.

### Industry Research Insights

Research was conducted on production memory systems (ChatGPT, Mem0, Zep/Graphiti, Letta/MemGPT, LangChain) to inform design decisions:

- **ChatGPT** uses no per-query retrieval — all memories are pre-synthesized offline ("Dreaming") and dumped into the system prompt every turn. Simple but doesn't scale.
- **Zep/Graphiti** uses a temporal knowledge graph with three parallel searches (cosine, BM25, BFS) fused via Reciprocal Rank Fusion. Retrieval is LLM-free (150ms P95), but ingestion is expensive (multiple LLM calls per message, 600K+ tokens per conversation reported).
- **Letta/MemGPT** uses OS-inspired virtual context management — the model controls its own memory paging via function calls. "Sleep-time agents" handle async consolidation.
- **Mem0** uses vector search + optional reranking + optional graph memory. Async by default. ~6,900 tokens per query vs ~26,000 for full-context baselines.

Key latency reference points:
- In-memory vector search (FAISS/HNSW, <10K memories): **<1ms**
- Vector + cross-encoder rerank (top 20): **40-80ms**
- LLM-based relevance filtering: **200ms-2s+**
- Zep full pipeline (vector + BM25 + graph + rerank): **200ms P95**

The dual-track architecture chosen for arteries avoids Zep's ingestion cost problem while preserving LLM-quality extraction via the async path.

---

### Data Model

**Q: What does a memory record look like across tiers?**

A: All tiers share a common base schema with tier-specific additions. Retrieval uses natural language fact + domain/entity tags (tags pre-filter search space, vector similarity ranks within the subset). Injection surfaces the natural language fact + domain context only (no confidence scores or lineage exposed to the agent). Top-5 most relevant memories are surfaced per turn.

**Shared fields across all tiers:**
- Record ID
- Fact text (natural language)
- Fact embedding (for vector search, pgvector `vector` type)
- Domain/entity tags (for pre-filtering, JSONB with GIN index)
- Confidence score
- Source timestamp
- Access count (how often surfaced in top-5)
- Project ID

**Ephemeral-only fields:**
- `agent_process_id` (isolates each agent's ephemeral space — agents only see their own)
- `parent_agent_id` (nullable — references the agent that spawned this one, null for top-level agents)

**Ephemeral + Persistent shared additions:**
- `valid_from` / `valid_until` (validity windows for temporal reasoning and rollback)

**Persistent-only fields:**
- Parent record IDs (lineage — references to ephemeral records it was compiled from)
- Child record IDs (lineage — references to evergreen records it compiled into)
- Source project ID (on the central persistent table, since per-project tables merge into one)

**Evergreen-only fields:**
- Parent record IDs (lineage — references to persistent records it was compiled from)
- No validity windows (stable tier, rarely contradicted — simple overwrite-with-lineage on the rare contradiction)
- No `project_id` (evergreen is global, not project-scoped)

**Scoping:**
- Ephemeral: per-project, per-agent-process (partitioned by `agent_process_id`)
- Persistent: per-project tables that compile into a central persistent table, shared across all agents
- Evergreen: **global** — available to all agents across all projects. Not project-scoped. Reserved for truths that transcend any single project (user preferences, organization-wide patterns). Project-specific long-term truths live in persistent memory instead.

**Temporal tracking approach:**
- Ephemeral and persistent get full validity windows (`valid_from`, `valid_until`) since facts change quickly
- When contradiction is resolved during compilation, old record gets `valid_until = now`, new record gets `valid_from = now`, lineage links them
- Rollback = restore old record by clearing `valid_until`
- RLVR versioning = know exactly what was surfaced when
- Evergreen skips validity windows — stable facts, rarely contradicted, overwrite-with-lineage is sufficient

**Ephemeral → persistent compilation:**
- Both additive and replacement
- Additive: new insights the async LLM extracts that the sync model missed
- Replacement: contradiction resolution — new facts supersede old ones, old records get `valid_until` set

### Multi-Agent Architecture

**Q: Who uses arteries?**

A: The user plus multiple autonomous agent instances. Typically 3-5 agents per project, potentially more. Agents can be separate processes or threads within one process.

**Memory isolation model:**
- **Ephemeral is per-agent-process** — each agent only sees its own ephemeral memories, isolated by `agent_process_id`
- **Persistent and evergreen are shared** — all agents on the same project read and write to the same store concurrently

### Memory Inheritance (Parent-Child Agents)

**Q: When a parent agent spawns subagents, how does memory flow?**

A: Memory inheritance follows an OOP-like pattern:

**At spawn time:**
- Parent's ephemeral memories are **snapshotted** (frozen copy at spawn time, not a live reference)
- Each child gets a copy of the snapshot with its own `agent_process_id` and `parent_agent_id` set to the parent
- Children do NOT see new memories the parent extracts after spawning

**During child execution:**
- Each child extracts its own ephemeral memories independently
- Children can read shared persistent and evergreen memory (global access)

**At child completion (compile-back):**
- Child's NEW ephemeral memories (not the inherited snapshot) are merged back into the parent's ephemeral space
- No contradiction resolution during compile-back — both parent and child memories coexist
- The async LLM handles contradiction resolution later during ephemeral → persistent compilation
- Child's ephemeral records are deleted after merge

**Lifecycle operations added to architecture:**
- **SPAWN**: copy parent ephemeral records → new child `agent_process_id`, set `parent_agent_id`
- **COMPILE-BACK**: merge child's new ephemeral records into parent's ephemeral space, delete child records

### Cold Start

**Q: What happens with zero memory on a brand new project?**

A: No special bootstrap mode. Arteries starts empty, observes the conversation as it progresses, and begins extracting ephemeral memories as context builds naturally. Domain understanding emerges from accumulated memories rather than being pre-configured.

### Storage

**Q: Where does the data live?**

A: **Single PostgreSQL instance with pgvector** for all three tiers.

**Why Postgres over SQLite:**
- Multi-agent concurrency is the deciding factor. Multiple agent processes writing persistent/evergreen memories simultaneously requires row-level MVCC locking — SQLite's single-writer model would create contention.
- A hybrid approach (SQLite for ephemeral, Postgres for shared tiers) was considered and rejected — the coordination overhead between two database systems (two query syntaxes, cross-storage compilation boundary, two failure surfaces) isn't worth the marginal performance gain on ephemeral writes.
- Ephemeral isolation is achieved via `agent_process_id` column + filtering, not separate databases.

**Postgres-specific advantages used:**
- Vectors in the same table as structured data (no JOIN needed for retrieval)
- JSONB with GIN indexes for domain/entity tag pre-filtering
- Iterative HNSW scan (`hnsw.iterative_scan = 'relaxed_order'`) for filtered vector search at scale
- Partial indexes per project for smaller, faster index builds
- Connection pooling for variable number of agent processes
- True concurrent writes — sync and async tracks from multiple agents never interfere

**Alternatives evaluated:**
- **SQLite + sqlite-vec**: Best for single-user/single-process. WAL mode handles basic concurrency but single-writer model becomes a bottleneck with many agent processes writing shared memory.
- **DuckDB + vss**: Rejected — experimental persistence with data loss risk, OLAP engine for OLTP workload mismatch, poor delete handling for ephemeral churn, no pre-filtered vector search.
- **LanceDB / ChromaDB**: Rejected — non-relational, cannot express lineage tracking, per-project compilation, or cross-tier foreign keys without pushing integrity logic into application code.

---

## Open Questions (Unanswered)

### RLVR Scope (Decided)

**Q: Where does RLVR meaningfully improve the architecture?**

A: RLVR targets the **sync model's per-turn decisions** — high volume, fast feedback loops, clean reward signals. The sync model (1-3B fine-tuned, running locally) is the single RLVR-trained model handling all three per-turn decisions:

**RLVR-trained decisions (sync model):**

| Decision | Reward signal | How verified |
|---|---|---|
| **Top-5 retrieval ranking** — which memories to surface | Was the memory reflected in the agent's response? User didn't correct → positive. Surfaced but unused → negative. | Semantic overlap comparison: memory fact text vs agent output. Every turn, immediate feedback. **Strongest candidate.** |
| **Gate trigger** — should the prompt-system be called? | Did the retrieved prompt get used by the agent? | Prompt retrieved → used in response = positive. Gate skipped → agent struggled = negative (noisier). Every turn, immediate. |
| **Ephemeral extraction** — what's worth remembering from this turn? | Did the async LLM also extract it (or equivalent)? | Async LLM acts as automatic grader. Sync extracted + async confirmed = positive. Sync missed what async found = negative. Every turn, seconds delay. |

**NOT RLVR-trained (too rare, too slow for learning):**

| Decision | Why not RLVR | Method instead |
|---|---|---|
| **Persistent → evergreen promotion** | Feedback loop is weeks/months (need future project usage to verify). Low volume. | Heuristic: same insight in 3+ projects' persistent memory → promote |
| **Contradiction resolution** | Hard to verify correctness without human judgment | Async LLM judgment |
| **Cross-project deduplication** | Noisy signal, rare events | Embedding similarity + async LLM |

### Persistent → Evergreen Promotion

**Q: What triggers promotion to evergreen?**

A: The same insight must appear in persistent memory across **3+ different projects**. Cross-project recurrence signals a global truth, not a project-specific choice. The central persistent table (where per-project tables compile into) is the surface where cross-project pattern detection runs.

**"Same insight" detection:** Handled by the async LLM during compilation to the central persistent table — it identifies and tags semantically equivalent insights across projects. Not RLVR-trained (too rare, noisy signal).

---

## Open Questions (Unanswered)

### RLVR Infrastructure

1. **Training data bootstrap**: Before enough per-turn signal exists, what's the initial policy for the sync model? LLM-as-judge making per-turn calls, replaced by RLVR once enough data exists?
2. **Scope of the RLVR model**: Fine-tuned 1-3B LLM with RL, or small classifier with feature engineering? What's the appetite for training infrastructure?

### Other Open Design Surfaces

- **Scope boundaries**: What's v1 vs. future? Minimum viable memory that delivers value over no-memory baseline?
- **Confidence-aware retrieval**: How should retrieval_confidence and last_retrieval_ts be consumed?
- **User intent routing**: How should user_intent affect gate aggressiveness?
