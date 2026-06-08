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

## Open Questions (Unanswered)

### RLVR Design Surface

1. **What's the verifiable reward?** RLVR needs a signal computable without human judgment. Options:
   - Memory gets referenced in a future retrieval the user doesn't correct (positive)
   - Memory aligns with a user action (positive)
   - Memory reduces retrieval errors (positive)
   - Memory is retrieved but irrelevant/contradicted (negative)
   
   Which of these can be measured, and which *should* be measured?

2. **Training data bootstrap**: Before enough promotion history exists, what's the initial promotion policy?
   - Simple heuristic (e.g., insight appears in 3+ sessions → promote)?
   - LLM-as-judge making promotion calls, replaced by RLVR once data exists?

3. **Scope of the RLVR model**: 
   - Small classifier trained from scratch (cheaper, needs feature engineering)?
   - Fine-tuned LLM with RL (more flexible, needs infrastructure)?
   - What's the appetite for training infrastructure?

### Other Design Surfaces Not Yet Explored

- **Storage & persistence**: Where does each tier live? SQLite? Files? In-memory with flush?
- **Computation**: What computes topic_drift, domain classification, insight extraction? Local models? LLM calls? Heuristics?
- **Cold start**: What happens with zero memory? How does domain knowledge bootstrap?
- **Integration surface**: How does arteries get conversation data? Sidecar? Middleware?
- **Performance budget**: What latency is acceptable per turn? LLM calls in the hot path?
- **Ground truth validation**: How are insights validated beyond RLVR? User feedback? Implicit signals?
- **Scope boundaries**: What's v1 vs. future? Minimum viable memory that delivers value over no-memory baseline?
- **Domain filtering**: How does arteries populate active_domains and recurring_domains to solve the narrow-band embedding problem?
- **Confidence-aware retrieval**: How should retrieval_confidence and last_retrieval_ts be consumed?
- **User intent routing**: How should user_intent affect gate aggressiveness?
- **Eviction policy**: When and how do stale memories get removed at each tier?
