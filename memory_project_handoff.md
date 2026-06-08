# Memory Project — Handoff from prompt-system

Everything the memory project needs to know about how prompt-system consumes and depends on memory.

## MemoryFrame contract

The gate (`agent/gate.py`) defines a `MemoryFrame` dataclass that the memory project must populate and pass via the `/agent/route` API. The gate reads the frame but never mutates it — the memory project owns all write, eviction, and scoring logic.

### Schema

```python
@dataclass
class Insight:
    text: str           # the insight content
    source: str         # where it came from (e.g. "user_feedback", "retrieval_result")
    domain: str | None  # domain tag if applicable
    confidence: float   # 0-1, default 1.0

@dataclass
class CachedRetrieval:
    prompt_id: str      # which prompt was retrieved
    situation: str      # the situation/query that triggered retrieval
    score: float        # rerank score at retrieval time
    relevance: float    # post-hoc relevance (user feedback or decay), default 1.0

@dataclass
class EphemeralMemory:
    recent_messages: list[str]  # last N user messages in the conversation
    topic_drift: float          # 0-1, how far current topic has drifted from session start
    turn_count: int             # turns since last retrieval

@dataclass
class PersistentMemory:
    session_insights: list[Insight]          # insights accumulated this session
    prior_retrievals: list[CachedRetrieval]  # prompts retrieved earlier in this session
    active_domains: list[str]                # domains the user is working in right now

@dataclass
class EvergreenMemory:
    user_intent: list[str]                    # long-term user goals/patterns
    recurring_domains: list[str]              # domains the user works in across sessions
    ground_truth_insights: list[Insight]      # validated insights that persist across sessions
    last_retrieval_ts: float | None           # unix timestamp of last retrieval
    retrieval_confidence: float | None        # confidence of last retrieval result

@dataclass
class MemoryFrame:
    ephemeral: EphemeralMemory
    persistent: PersistentMemory
    evergreen: EvergreenMemory
```

### JSON wire format

The API endpoint (`POST /agent/route`) accepts the frame as a `memory` field in the request body. Deserialization is in `agent/api.py:_build_memory_frame()`.

```json
{
  "situation": "...",
  "memory": {
    "ephemeral": {
      "recent_messages": ["msg1", "msg2"],
      "topic_drift": 0.25,
      "turn_count": 3
    },
    "persistent": {
      "session_insights": [
        {"text": "...", "source": "user_feedback", "domain": "sales", "confidence": 0.9}
      ],
      "prior_retrievals": [
        {"prompt_id": "GTM & Launch Plan Review", "situation": "GTM strategy for B2B SaaS", "score": 0.988, "relevance": 1.0}
      ],
      "active_domains": ["sales", "product"]
    },
    "evergreen": {
      "user_intent": ["autonomous prompt selection", "SaaS product work"],
      "recurring_domains": ["product", "finance", "sales"],
      "ground_truth_insights": [],
      "last_retrieval_ts": 1748736000.0,
      "retrieval_confidence": 0.95
    }
  }
}
```

## How the gate uses memory today

The gate runs a three-stage decision pipeline. Memory sits between heuristics and embedding proximity:

1. **Heuristic check** — kills greetings, followups, and messages under 5 words without a workflow verb. No memory involvement.
2. **Memory check** (`_check_memory_frame`) — if a MemoryFrame is provided:
   - **Topic drift > 0.3** → force search (conversation shifted, new retrieval needed)
   - **Cached retrieval match** → skip search if a prior retrieval covers this situation (relevance > 0.75, score > 0.7, lexical overlap > 50%)
   - **Stale context** → force search if turn_count > 5 and drift > 0.15 (too long without retrieval)
3. **Embedding proximity** — embeds the message, finds nearest neighbor in the corpus. If similarity < threshold (currently 0.50), skips search.

Additionally, `active_domains` in persistent memory lowers the embedding threshold by 0.05 — if the user is actively working in a domain, the gate is more permissive.

## Domain filtering — the unsolved problem for memory

### The problem

The embedding gate cannot distinguish in-domain from out-of-domain queries. snowflake-arctic-embed-m-v2.0 compresses all similarity scores into a narrow band:

| Query | Similarity | Verdict |
|---|---|---|
| Debug memory leak (no prompts in corpus) | 0.926 | Should block |
| Cold email (good prompts exist) | 0.917 | Should pass |
| GTM strategy (good prompts exist) | 0.908 | Should pass |
| Chicken tikka masala recipe | 0.873 | Should block |
| Weather in Tokyo | 0.868 | Should block |
| Board presentation (good prompts exist) | 0.884 | Should pass |
| PRD (good prompts exist) | 0.882 | Should pass |
| Birthday party for 5 year old | 0.856 | Should block |
| DCF model (good prompts exist) | 0.847 | Should pass |

There is no static threshold that cleanly separates these. "Birthday party" (0.856) sits between "DCF model" (0.847) and "Board presentation" (0.884).

### Why memory should own this

The MemoryFrame already has the right fields:
- `recurring_domains` — knows the user works in SaaS/product/finance
- `active_domains` — knows what the user is doing right now
- `user_intent` — knows the user's goals

A memory-aware domain filter can reject "birthday party" trivially because it doesn't match any known domain, without needing the embedding to do it. This is fundamentally a user-context problem, not a corpus-similarity problem.

### Suggested approach

The memory project should populate `active_domains` and `recurring_domains`, then the gate can add a domain-alignment check:

- If memory has domain context and the query doesn't align with any known domain → raise the similarity threshold or skip search
- If memory has no domain context (cold start) → fall through to embedding proximity as today

This keeps the gate stateless (it just reads the frame) while the memory project handles the intelligence of domain tracking.

## Other memory-dependent behaviors the gate should eventually support

### Confidence-aware retrieval

`retrieval_confidence` and `last_retrieval_ts` in evergreen memory are defined but not yet consumed. Intended use: if the last retrieval was low-confidence, the gate should be more eager to search again on related queries rather than relying on cached retrievals.

### User intent routing

`user_intent` in evergreen memory is defined but not consumed. Intended use: if the user's long-term intent is "autonomous prompt selection", the gate should be more aggressive about searching (lower thresholds) since the goal is reliable automated retrieval without human approval.

### Session insight accumulation

`session_insights` in persistent memory is defined but the memory project needs to decide:
- What constitutes an insight (user corrections, domain signals, failed retrievals)?
- When to promote session insights to `ground_truth_insights` in evergreen?
- Eviction policy for stale insights?

## Retrieval pipeline context

For the memory project to make good decisions about caching and confidence, it helps to understand what the retrieval pipeline returns:

- **Rerank scores**: 0-1 from the cross-encoder (mixedbread-ai/mxbai-rerank-base-v2). Scores above 0.95 are strong matches. Scores cluster tightly — top-5 spread is often < 0.05.
- **RRF scores**: Reciprocal Rank Fusion of dense (embedding) + sparse (BM25) retrieval, weighted 0.5/0.5 with k=50.
- **Source filtering**: Default is `source='private'`. Public prompts are excluded unless explicitly requested.
- **Corpus**: 849 private prompts, median length 2,203 chars. 114 are Image Gen prompts (12.4%) which tend to surface as false positives for business queries due to broad keyword overlap.

## Files in prompt-system that touch memory

| File | What it does |
|---|---|
| `agent/gate.py` | Defines MemoryFrame schema, consumes it in `_check_memory_frame()` |
| `agent/api.py` | Deserializes JSON → MemoryFrame in `_build_memory_frame()`, passes to gate |
| `agent/route.py` | Receives `source` parameter (routed from API), does not interact with memory directly |
