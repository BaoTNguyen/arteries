# arteries

Always-on memory layer for capillaries prompt retrieval.


## CLI Shortcut

After installing the package, use `art` for arteries commands:

```bash
art setup --list
art setup --cwd /path/to/repo claude
art setup --cwd /path/to/repo codex --check
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

Run the local unit tests without requiring Postgres or model services:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Current tests cover heuristic ephemeral extraction and `MemoryFrame` assembly for ephemeral, persistent, and evergreen memory rows using mocked storage.

Run live Postgres smoke tests for the three memory tiers:

```bash
PYTHONPATH=src ARTERIES_LIVE_TESTS=1 python3 -m unittest tests.test_live_memory_tiers -v
```

These tests create isolated temporary records, verify each tier one by one, and clean up after themselves. The persistent test mocks the external LLM call so it only requires Postgres.



## CLI Integration

The universal per-turn evaluator is:

```bash
bash scripts/eval.sh "I am working on RLVR memory promotion for capillaries."
```

Any CLI can integrate arteries by running that command on each user prompt and injecting stdout when it is non-empty.

## Basic Activity Tracking

Arteries records minimal run events when `art eval` observes a turn, extracts ephemeral memory, builds a frame, gates prompt retrieval, retrieves a prompt, or completes compilation. Events use `ARTERIES_PROJECT` as the project id, defaulting to `default`. They write to Postgres when the `arteries.agent_runs` and `arteries.agent_events` tables exist; otherwise they fall back to repo-local JSONL under `.arteries/runs/`.

Inspect current memory plus recent events:

```bash
art inspect --project prompt-system --agent prompt-system-hook --events 10
```

Start or inspect runs directly:

```bash
art runs start --project prompt-system --agent prompt-system-hook --cli codex
art runs recent --project prompt-system --limit 25
art runs summary --project prompt-system --limit 100
art runs show <run-id>
```

Check wiring for a repo:

```bash
art doctor --project prompt-system --agent prompt-system-hook --cli codex --repo /path/to/repo
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

Install arteries into another repo with provider-specific setup recipes:

```bash
art setup --cwd /path/to/repo generic
art setup --cwd /path/to/repo claude
art setup --cwd /path/to/repo codex
```

Supported providers:

```bash
art setup --list
```

Verify or remove an integration:

```bash
art setup --cwd /path/to/repo claude --check
art setup --cwd /path/to/repo claude --remove
```

Every provider installs a repo-local `.arteries/` runtime with stable scripts. The activate hook starts a fresh run and the observe hook stamps `ARTERIES_PROJECT`, `ARTERIES_AGENT_ID`, `ARTERIES_CLI`, and `ARTERIES_REPO`:

```text
.arteries/config.json
.arteries/hooks/observe.sh
.arteries/hooks/generic-observe.sh
.arteries/hooks/activate.sh
.arteries/smoke.sh
```

Generic CLIs can call `bash .arteries/hooks/generic-observe.sh "user prompt"` and inject stdout when non-empty.

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
