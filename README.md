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

- setup for Codex, Pi, and Claude Code
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

Go to the project you want the agent to work in, then run the setup command for the CLI you use there.

Codex:

```bash
cd /path/to/project
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup codex
```

Pi:

```bash
cd /path/to/project
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup pi
```

Claude Code:

```bash
cd /path/to/project
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup claude
```

If capillaries is not a sibling of arteries, pass it explicitly:

```bash
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup codex \
  --capillaries-root /path/to/capillaries
```

Use a stable project name when you want Codex, Pi, and Claude activity to land in the same trace:

```bash
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup codex --project career-ops
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup pi --project career-ops
```

Verify setup:

```bash
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup codex --check
bash .arteries/smoke.sh "arteries setup test"
```

Remove it:

```bash
bash /home/bao-tn/Coding/Projects/arteries/scripts/art.sh setup codex --remove
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

- Codex: `.codex/config.toml`, `.arteries/codex/compact_prompt.txt`, and an `AGENTS.md` block.
- Pi: `.pi/extensions/arteries.ts` for compaction replacement.
- Claude Code: `.claude/settings.local.json` hooks for session start, prompt submit, pre-compact, and post-compact.

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

On compaction, arteries builds a continuity packet from ephemeral, persistent, and evergreen memory.

## Memory tiers

Arteries stores memory in the `arteries` schema inside the shared `capillaries` database.

Ephemeral memory is per project and per agent process. It is high churn. It captures recent observations before compilation.

Persistent memory is per project. It stores compiled project facts, preferences, and decisions.

Evergreen memory is global. It stores cross-project facts you have explicitly imported or promoted.

Recent retrievals are also stored, so the frame can tell capillaries which prompts have already surfaced.

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
