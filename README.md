# arteries

Always-on memory layer for capillaries prompt retrieval.


## CLI Shortcut

After installing the package, use `art` for arteries commands:

```bash
art setup --list
art setup --cwd /path/to/repo pi
art setup --cwd /path/to/repo claude
art setup --cwd /path/to/repo codex --check
art packet --message "manual compact"
art evergreen extract --project . --out evergreen_review.md
art setup-db
art eval "I prefer stdlib-first implementations."
art inspect --project default --events 10
art runs start --project default --cli codex
art runs recent --project default --limit 25
art runs summary --project default --limit 100
art doctor --project default
```

From this repo without installing, use:

```bash
bash scripts/art.sh setup --list
```

## Testing

Run the local unit tests without requiring Postgres or model services. Use the script so both arteries and capillaries are importable:

```bash
bash scripts/test.sh
```

For non-sibling repos, set `CAPILLARIES_ROOT` first:

```bash
CAPILLARIES_ROOT=/path/to/capillaries bash scripts/test.sh
```

Current tests cover heuristic ephemeral extraction and `MemoryFrame` assembly for ephemeral, persistent, and evergreen memory rows using mocked storage.

Run live Postgres smoke tests for the three memory tiers:

```bash
PYTHONPATH=src ARTERIES_LIVE_TESTS=1 python3 -m unittest tests.test_live_memory_tiers -v
```

These tests create isolated temporary records, verify each tier one by one, and clean up after themselves. The persistent test mocks the external LLM call so it only requires Postgres.



## Import Topology

Arteries is the runtime layer around capillaries. Keep imports one-way:

```text
agent CLI hook -> arteries -> capillaries
```

Capillaries owns prompt retrieval, gate decisions, reranking, and shared memory contract types such as `MemoryFrame`. Arteries owns memory extraction, storage, `MemoryFrame` assembly, tracing, and CLI hooks. Capillaries should not import arteries. If both projects need a shared type, keep it in capillaries and pass it into capillaries functions from arteries.

For local editable development, make both packages importable in the hook environment:

```bash
export PYTHONPATH=/path/to/arteries/src:/path/to/capillaries/src:$PYTHONPATH
```

Installed `.arteries` runtimes do this automatically with `ARTERIES_ROOT` and `CAPILLARIES_ROOT`.

## CLI Integration

The universal per-turn evaluator is:

```bash
bash scripts/eval.sh "I am working on RLVR memory promotion for capillaries."
```

Supported Tier 1-3 CLIs integrate through provider-specific setup for Pi, Codex, and Claude Code. The shared continuity packet generator is available as `art packet` and is wired into installed runtimes as `.arteries/hooks/compact-packet.sh`.

## Basic Activity Tracking

Arteries records minimal run events when `art eval` observes a turn, extracts ephemeral memory, builds a frame, gates prompt retrieval, retrieves a prompt, or completes compilation. Events use `ARTERIES_PROJECT` as the project id, defaulting to `default`. They write to Postgres when the `arteries.agent_runs` and `arteries.agent_events` tables exist; otherwise they fall back to repo-local JSONL under `.arteries/runs/`.

Inspect current memory plus recent events:

```bash
art inspect --project my-project --agent my-project-hook --events 10
```

Start or inspect runs directly:

```bash
art runs start --project my-project --agent my-project-hook --cli codex
art runs recent --project my-project --limit 25
art runs summary --project my-project --limit 100
art runs show <run-id>
```

Check wiring for a repo:

```bash
art doctor --project my-project --agent my-project-hook --cli codex --repo /path/to/repo
```

Stable script entry points:

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

For CLIs without a JSON hook protocol, use `scripts/generic-observe.sh`. It accepts the prompt as arguments or stdin and prints plain text context only when arteries retrieves a prompt.

For Claude/Codex-style hooks, use:

- `hooks/arteries-activate.js` for session start context
- `hooks/arteries-observe.js` for each user prompt
- `hooks/hooks.json` as the hook config

To reduce repeated Codex permission prompts, approve the specific script command prefixes you use often, such as `bash scripts/test.sh`, `bash scripts/live-test.sh`, and `bash scripts/smoke-cli.sh`. Avoid approving broad commands like plain `bash`.


## Native CLI Setup

Install arteries into another repo with provider-specific Tier 1-3 setup recipes:

```bash
art setup --cwd /path/to/repo pi
art setup --cwd /path/to/repo codex
art setup --cwd /path/to/repo claude
```

Supported providers:

```bash
art setup --list
```

For local editable imports, point the runtime at capillaries:

```bash
art setup --cwd /path/to/repo pi \
  --arteries-root /path/to/arteries \
  --capillaries-root /path/to/capillaries
```

Verify or remove an integration:

```bash
art setup --cwd /path/to/repo claude --check
art setup --cwd /path/to/repo claude --remove
```

Every provider installs a repo-local `.arteries/` runtime with stable scripts. The activate hook starts a fresh run, the observe hook stamps `ARTERIES_PROJECT`, `ARTERIES_AGENT_ID`, `ARTERIES_CLI`, and `ARTERIES_REPO`, and the compact hook builds a continuity packet from ephemeral, persistent, and evergreen memories:

```text
.arteries/config.json
.arteries/hooks/observe.sh
.arteries/hooks/generic-observe.sh
.arteries/hooks/activate.sh
.arteries/hooks/compact-packet.sh
.arteries/hooks/pi-compact-json.sh
.arteries/smoke.sh
```

Pi additionally installs `.pi/extensions/arteries.ts` as the Tier 1 native compaction replacement extension. Codex installs a compact prompt file and compact lifecycle hooks. Claude installs prompt/session hooks plus `PreCompact` and `PostCompact` packet hooks.

## Evergreen Review Import

Generate an editable Markdown review file from trusted project docs:

```bash
art evergreen extract \
  --project . \
  --out evergreen_review.md
```

Review `evergreen_review.md` in an editor. Delete memories you do not want, rewrite wording freely, or move items under `Rejected Memories`. The sidecar `evergreen_review.meta.json` keeps source spans and original text so edited memories can still be tied back to the file they came from.

Preview the import:

```bash
art evergreen import --review evergreen_review.md
```

Write accepted memories to `arteries.evergreen`:

```bash
art evergreen import --review evergreen_review.md --write
```
