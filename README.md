# arteries

Arteries is the memory and tracing layer around capillaries.

Capillaries decides whether a prompt from your private corpus should be used. Arteries gives that decision a memory frame, records what happened, and leaves enough trace data that you can later answer practical questions like:

- What did the agent observe in this repo?
- Which memories did it extract?
- Which prompt did capillaries retrieve?
- What gate match caused retrieval to open?
- What user message triggered it?
- Did Codex, Pi, or Claude produce the event?

The goal is not to make another agent. The goal is to make the agents you already use carry project memory and leave an audit trail.

## Where it sits in the stack

Arteries is the memory and tracing layer in a five-repo agent stack:

```text
capillaries  prompt/skill retrieval
arteries     memory + trace substrate               <- this repo
heart        orchestration + environment + reward
plexus       goal decomposition + acceptance loop
marrow       RL training on heart's episodes
```

Capillaries decides which prompt fits. Arteries gives that decision a memory frame and records what happened. Heart runs coding agents that ride arteries' hooks; marrow later trains on the decision ledger arteries leaves behind. Dependencies point one way: arteries imports capillaries, never the reverse.

## What sets it apart

Every coding CLI is bolting on its own memory feature, each incompatible with the next. Arteries takes the opposite bet — one memory substrate, many front-ends:

- **One memory across six CLIs.** Codex, Claude Code, Pi, OpenCode, Cursor, and Hermes all read and write the same project memory through explicit per-CLI adapters. Switch tools mid-project and the context follows you instead of resetting.
- **Memory that surfaces by relevance, not recency.** Persistent memories are embedded at compile time and matched against the current message by vector similarity. A test-writing subagent surfaces test memories, a code agent surfaces code memories — task isolation falls out of the embeddings, with no scope labels to maintain.
- **Three tiers with real half-lives.** Ephemeral (per process, high churn), persistent (per project, compiled facts), evergreen (global, explicitly promoted). Observations get compiled up the tiers in the background; they don't all live forever at the same weight.
- **An audit trail that keeps gate and retrieval honest.** A gate opening on a nearest-match title and the prompt actually retrieved are logged as distinct events, with bounded previews and SHA-256 hashes. You can reconstruct why a prompt surfaced months later.
- **It never fabricates.** If a CLI only exposes user turns, the continuity packet marks the assistant side as not captured rather than inventing a plausible reply.
- **Degrades instead of failing.** Postgres down? Telemetry falls back to repo-local JSONL. Embedding server down? Retrieval falls back to recency. The hooks keep working.

## What problem it solves

Coding CLIs forget too much between sessions, and each CLI has its own hook or extension model. If you use Codex in one project, Pi in another, and Claude Code somewhere else, the useful context gets scattered.

Arteries gives those tools one shared local memory path:

```text
CLI hook or extension -> arteries -> capillaries -> private prompt corpus
                         |
                         +-> Postgres memory and run logs
                         +-> repo-local fallback logs
```

It handles four jobs:

1. Observe each user turn that reaches the hook.
2. Extract short-lived project memories from that turn.
3. Build the `MemoryFrame` that capillaries uses for gate and retrieval decisions.
4. Log runs, gate decisions, retrieved prompts, compaction packets, and memory compilation.

Capillaries remains the retrieval engine. Arteries owns the runtime layer around it.

## How the pieces fit

Keep the dependency direction one-way:

```text
agent CLI hook -> arteries -> capillaries
```

Capillaries owns:

- prompt gating
- prompt search and reranking
- private prompt corpus access
- shared memory contract types such as `MemoryFrame`

Arteries owns:

- explicit setup adapters for Codex, Claude Code, Pi, OpenCode, Hermes, and Cursor
- `.arteries/` runtime scripts inside target repos
- ephemeral, persistent, and evergreen memory storage
- `MemoryFrame` assembly
- compaction packets
- central trace output
- run/event telemetry

The two repos should usually sit next to each other:

```text
/home/bao-tn/Coding/Projects/arteries
/home/bao-tn/Coding/Projects/capillaries
```

The scripts assume that sibling layout unless you pass `--capillaries-root`.

## Services it expects

For memory and retrieval to work, keep these available locally:

- PostgreSQL with `pgvector`
- the `capillaries` database
- an OpenAI-compatible embedding endpoint, usually `http://127.0.0.1:8003/v1/embeddings`
- an OpenAI-compatible chat/completions endpoint, usually `http://127.0.0.1:8001/v1/chat/completions`
- capillaries importable on `PYTHONPATH`

Defaults live in `src/arteries/config.py` and environment variables can override them:

```bash
DB_HOST=/var/run/postgresql
DB_PORT=5432
DB_NAME=capillaries
DB_USER=$USER
DB_PASSWORD=
EMBED_URL=http://127.0.0.1:8003/v1/embeddings
GENERATE_URL=http://127.0.0.1:8001/v1/chat/completions
```

Initialize the arteries schema once:

```bash
cd /home/bao-tn/Coding/Projects/arteries
bash scripts/setup-db.sh
```

## Command entry points

If the package is installed, use `art`:

```bash
art setup --list
art trace --repo /path/to/project
art runs summary --project project-name
art inspect --project project-name --agent project-name-hook
```

From this repo without installing, use the wrapper:

```bash
cd /home/bao-tn/Coding/Projects/arteries
bash scripts/art.sh setup --list
```

The wrapper sets `PYTHONPATH` for arteries and capillaries. It also remembers the directory you called it from, so setup commands can target the current project without `--cwd`.

## Set up a project

Go to the project you want the agent to work in, then run the setup command for each CLI you use there. Setup is additive: installing Cursor later does not remove an existing Codex, Claude, Pi, OpenCode, or Hermes adapter.

List supported adapters:

```bash
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup --list
```

Install one adapter:

```bash
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup add codex
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup add claude
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup add pi
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup add opencode
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup add cursor
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup add hermes
```

The older shorthand still works:

```bash
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup codex
```

Verify or remove a single adapter at any time:

```bash
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup check cursor
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup remove cursor

# Equivalent legacy flag style:
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup cursor --check
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup cursor --remove
```

If capillaries is not a sibling of arteries, pass it explicitly:

```bash
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup add codex   --capillaries-root /path/to/capillaries
```

Use a stable project name when activity from multiple CLIs should land in the same trace:

```bash
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup add codex --project career-ops
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup add cursor --project career-ops
```

Smoke test the shared runtime:

```bash
bash .arteries/smoke.sh "arteries setup test"
```

## What setup installs

Every provider gets a repo-local runtime:

```text
.arteries/config.json
.arteries/hooks/observe.sh
.arteries/hooks/generic-observe.sh
.arteries/hooks/activate.sh
.arteries/hooks/compact-packet.sh
.arteries/hooks/pi-compact-json.sh
.arteries/smoke.sh
```

The runtime sets:

```text
ARTERIES_ROOT
CAPILLARIES_ROOT
ARTERIES_PROJECT
ARTERIES_AGENT_ID
ARTERIES_CLI
ARTERIES_REPO
PYTHONPATH
```

Provider-specific files:

- Codex: `.codex/config.toml`, `.arteries/codex/compact_prompt.txt`, and an `AGENTS.md` block. Codex gets a PreCompact packet hook where hooks are available, plus the compact prompt override as a fallback. Assistant response memory uses transcript capture on prompt submit when available, or the generated assistant observe hook when the host exposes a response event.
- Claude Code: `.claude/settings.local.json` hooks for session start, prompt submit, pre-compact, post-compact, and subagent metadata. Assistant response memory is captured from the transcript during prompt submit, so no separate Stop hook is needed.
- Pi: `.pi/extensions/arteries.ts` for extension-based compaction replacement and assistant response observation.
- OpenCode: `.opencode/plugins/arteries.ts` for plugin events, assistant response observation, and compaction context injection.
- Cursor: `.cursor/rules/arteries.mdc` and `.cursor/mcp.json` for explicit rule/MCP use, manual assistant response observation, and manual compact packet fallback.
- Hermes: `HERMES.md` and `.hermes/mcp.json` as a conservative context-file/MCP adapter until a native hook schema is verified. It includes manual assistant response observation and compact packet commands.

Compaction support levels:

| CLI | Support | Behavior |
|-----|---------|----------|
| Pi | Native replacement | Returns an Arteries packet as the compaction summary and observes assistant response events exposed by Pi. |
| OpenCode | Native context injection | Adds the Arteries packet during `experimental.session.compacting` and records assistant response events from the plugin stream. |
| Claude Code | Native prompt and compact hooks | Emits the packet on PreCompact/PostCompact hooks and records the previous assistant reply from the transcript during UserPromptSubmit. |
| Codex | Hook plus compact prompt fallback | Runs a PreCompact packet hook where available, installs a compact prompt, and captures assistant replies from transcript-aware prompt hooks or explicit assistant observe events. |
| Cursor | Manual/rule fallback | Rules tell the agent to run `assistant-observe.sh` for useful assistant replies and `compact-packet.sh` before summarizing long sessions. |
| Hermes | Manual/context fallback | `HERMES.md` tells Hermes to run `assistant-observe.sh` for useful assistant replies and `compact-packet.sh` when context pressure appears. |

Codex config detail: `experimental_compact_prompt_file` is top-level and points to `../.arteries/codex/compact_prompt.txt`, because Codex resolves relative project config paths from the `.codex/` folder.

## What happens during use

After setup, start your CLI in the target project and work as usual.

On session start, arteries starts a run and writes `.arteries/current-run.json`.

On each observed user prompt, `arteries.eval`:

1. logs `turn.observed`
2. extracts ephemeral memories
3. builds a `MemoryFrame`
4. asks capillaries whether prompt retrieval should open
5. retrieves a prompt if the gate opens
6. prints the retrieved prompt text back to the CLI hook
7. compiles ephemeral memories into persistent project memory in the background

On compaction, arteries builds a continuity packet with:

- current project, agent, CLI, trigger, and capability context
- the most recent 10 user/assistant exchanges when the CLI provides transcript data
- recent user turns from Arteries run logs when assistant text is unavailable
- ephemeral memory for short-lived project observations
- relevance-filtered persistent project memory
- evergreen global memory
- use rules that keep the packet below current user, developer, system, and repo instructions

Arteries never fabricates missing assistant answers. If a CLI only exposes user prompts, the packet marks assistant text as not captured.

## Memory tiers

Arteries stores memory in the `arteries` schema inside the shared `capillaries` database.

Ephemeral memory is per project and per agent process. It is high churn. It captures recent observations before compilation.

Persistent memory is per project. It stores compiled project facts, preferences, and decisions. Each persistent memory is embedded at compile time (via the embedding server at port 8003) and retrieved by vector similarity against the current message, so only relevant memories surface in the MemoryFrame.

Evergreen memory is global. It stores cross-project facts you have explicitly imported or promoted.

Recent retrievals are also stored, so the frame can tell capillaries which prompts have already surfaced.

## Relevance-filtered retrieval

Persistent memories are embedded at compile time using the same embedding server that capillaries uses (snowflake-arctic-embed-m-v2.0, 768-dim). At retrieval time, the current user message is embedded and matched against persistent memories via pgvector HNSW cosine search. Only memories above a similarity threshold are included in the MemoryFrame.

This replaces the previous recency-based retrieval and naturally isolates subagents by task — a test-writing agent surfaces test-relevant memories, a code agent surfaces code-relevant memories, without needing scope labels.

The threshold defaults to 0.3 and is configurable:

```bash
ARTERIES_RELEVANCE_THRESHOLD=0.5
```

If the embedding server is down or no persistent memories have embeddings yet, retrieval falls back to recency-based ordering.

Backfill existing persistent memories:

```bash
art backfill-embeddings --project career-ops
```

## Subagent memory isolation

Subagents can run with restricted memory modes via the `ARTERIES_MEMORY` env var:

| Preset | Persistent read | Ephemeral mode | Use case |
|--------|----------------|----------------|----------|
| (unset) | relevance-filtered | compile | Normal agent |
| `readonly` | relevance-filtered | discard | Reads memory, leaves no trace |
| `clean` | none | discard | No memory in or out |

```bash
ARTERIES_MEMORY=readonly bash scripts/eval.sh "temporary analysis"
ARTERIES_MEMORY=clean bash scripts/eval.sh "scratch work"
```

In discard mode, ephemeral extractions are kept in-process memory only — no DB writes. They're visible in the MemoryFrame during the agent's lifetime and vanish when the process exits.

Individual env vars override presets:

```bash
ARTERIES_EPHEMERAL=discard    # compile (default) or discard
ARTERIES_PERSISTENT_READ=none # relevance (default) or none
```

## Trace data

Arteries writes telemetry to Postgres when these tables exist:

```text
arteries.agent_runs
arteries.agent_events
```

If Postgres is unavailable, it falls back to repo-local JSONL:

```text
.arteries/runs/*.jsonl
```

Common event types:

```text
run.started
turn.observed
memory.ephemeral.extracted
memory.frame.built
prompt.gate.decided
prompt.retrieved
memory.compile.completed
*.failed
```

A gate decision and a retrieved prompt are not the same thing. The gate may log a nearest corpus title because that title justified opening search. The retriever can still choose a different prompt.

Trace labels this explicitly:

```json
{
  "kind": "gate_decision",
  "gate_nearest_match_title": "Full Adversarial Check Protocol Breakpoint Reality Check",
  "search_opened": true
}
```

```json
{
  "kind": "prompt_retrieved",
  "retrieved_prompt_id": "60006f86-3983-4351-8a51-d64f5dd08a85"
}
```

## Central tracing from this repo

You can inspect another project from the arteries repo:

```bash
cd /home/bao-tn/Coding/Projects/arteries
bash scripts/art.sh trace \
  --repo /home/bao-tn/Coding/Projects/career-ops \
  --events 100 \
  --memories 20 \
  --prompt-preview 1000 \
  --message-preview 1000
```

The trace output includes:

- current run
- run summary
- recent events
- memory tiers
- prompt timeline
- gate nearest-match labels
- retrieved prompt references: title, file path, content hash, status, source, prompt length, and preview
- previous, current, and next observed user-turn context around retrieved prompts when that data was logged

Older `turn.observed` events may only have `message_chars`. Newer events include a bounded preview and a SHA-256 hash:

```json
{
  "message_chars": 2039,
  "message_preview": "Update my job preferences...",
  "message_preview_truncated": true,
  "message_sha256": "..."
}
```

Retrieved turns can also show `retrieval_situation_preview`, which comes from the retrieval log and often preserves the triggering user message even for older runs.

## Lower-level inspection

Inspect memory plus recent events:

```bash
bash scripts/art.sh inspect \
  --project career-ops \
  --agent career-ops-hook \
  --events 20
```

Summarize recent activity:

```bash
bash scripts/art.sh runs summary \
  --project career-ops \
  --repo /home/bao-tn/Coding/Projects/career-ops \
  --limit 100
```

Show one run:

```bash
bash scripts/art.sh runs show <run-id> \
  --repo /home/bao-tn/Coding/Projects/career-ops
```

Watch a project in a terminal:

```bash
bash scripts/watch.sh career-ops 10
```

## Hot reload expectations

Tracing changes are immediate because `bash scripts/art.sh trace` runs the current local source.

Python behavior inside hooks usually updates on the next hook invocation because the hook scripts call `python3 -m arteries...` each time. Still, restart the active CLI session when you change hook config, provider setup, or anything loaded at CLI startup.

Use this rule:

- Trace output changes: no restart.
- Future `turn.observed` payload changes: restart the CLI session to be certain.
- Codex `.codex/config.toml` changes: restart Codex.
- Pi extension changes: restart or reload Pi.
- Claude hook settings: restart Claude Code or start a new session.
- Prompt database changes: no CLI restart, unless a separate capillaries server has cached prompt data.

## Evergreen import

Evergreen memory is for durable cross-project facts. Generate a review file from trusted docs:

```bash
bash scripts/art.sh evergreen extract \
  --project . \
  --out evergreen_review.md
```

Edit the Markdown file by hand. Delete memories you do not want, rewrite wording, or move items under rejected memories. Preview import:

```bash
bash scripts/art.sh evergreen import --review evergreen_review.md
```

Write accepted memories:

```bash
bash scripts/art.sh evergreen import --review evergreen_review.md --write
```

## Evergreen CRUD

Manage evergreen memories directly:

```bash
art evergreen list
art evergreen list --json
art evergreen add "User prefers pytest" --domains "technical,testing" --confidence 0.9
art evergreen edit <id-prefix> --fact "Updated fact" --domains "technical"
art evergreen rm <id-prefix>
```

ID prefixes are matched uniquely — pass enough characters to be unambiguous.

## Testing

Run local tests without Postgres or model services:

```bash
bash scripts/test.sh
```

Run live memory-tier tests against Postgres:

```bash
PYTHONPATH=src ARTERIES_LIVE_TESTS=1 python3 -m unittest tests.test_live_memory_tiers -v
```

The live tests create temporary records and clean up after themselves. The persistent compile path mocks the external LLM call, so it only needs Postgres.

## Windows notes

This repo is shell-script first. The least painful Windows path is WSL2 or Git Bash with a Python virtual environment.

Use sibling checkouts such as:

```text
C:/src/arteries
C:/src/capillaries
```

Install both packages in editable mode, start Postgres with `pgvector`, then set the service URLs:

```powershell
$env:CAPILLARIES_ROOT = "C:/src/capillaries"
$env:DB_HOST = "127.0.0.1"
$env:DB_PORT = "5432"
$env:DB_NAME = "capillaries"
$env:EMBED_URL = "http://127.0.0.1:8003/v1/embeddings"
$env:GENERATE_URL = "http://127.0.0.1:8001/v1/chat/completions"
```

Then initialize the schema:

```powershell
python -m arteries.setup_db
```

## Troubleshooting

If setup cannot find capillaries, pass `--capillaries-root`.

If Codex complains about `.codex/config.toml`, check that `experimental_compact_prompt_file` is top-level and points to `../.arteries/codex/compact_prompt.txt`.

If no prompts surface, run trace first. Look for `prompt.gate.decided`. If `search_opened` is false, capillaries decided the prompt corpus was not relevant enough. If search opened but no `prompt.retrieved` appears, inspect capillaries retrieval and model services.

If trace shows only character counts for older turns, that is expected. Message previews were added later. New turns record bounded previews and hashes.

If Postgres is down, arteries still writes fallback JSONL under `.arteries/runs/`, but memory tiers and prompt corpus lookup need the database.

## Stable scripts

These scripts are safe entry points to approve in Codex instead of approving broad `bash` access:

```bash
bash scripts/setup-db.sh
bash scripts/test.sh
bash scripts/live-test.sh
bash scripts/eval.sh "thanks"
bash scripts/generic-observe.sh "thanks"
bash scripts/hook-observe-smoke.sh "thanks"
bash scripts/hook-activate-smoke.sh
bash scripts/smoke-cli.sh "thanks"
```
