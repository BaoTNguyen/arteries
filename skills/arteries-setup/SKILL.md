---
name: arteries-setup
description: >
  Install arteries memory hooks into the current project for Claude Code, Codex,
  or Pi. Wires repo-local .arteries runtime scripts plus provider-specific hook
  config so arteries can observe turns, build memory, retrieve prompts through
  capillaries, and produce compaction packets. Use when the user says "arteries
  setup", "install arteries", "wire up arteries", "add arteries to this
  project", "arteries-setup", or "connect arteries".
argument-hint: "[claude|codex|pi]"
---

Install arteries into the current working directory.

## Steps

1. Determine the provider from the argument. Default to `claude` if none is given.
   Valid providers: `claude`, `codex`, `pi`.

2. Find the arteries root. Check in order:
   - `ARTERIES_ROOT` env var
   - `../arteries` relative to the current project
   - `~/Coding/Projects/arteries`

3. Run setup from the target project. The wrapper records the caller directory, so `--cwd` is not needed:

```bash
bash <arteries-root>/scripts/art.sh setup <provider>
```

4. Verify it worked:

```bash
bash <arteries-root>/scripts/art.sh setup <provider> --check
```

5. Run the smoke test:

```bash
bash .arteries/smoke.sh "arteries setup test"
```

6. Report what was installed and whether the smoke test passed.

If any step fails, use the concrete failure:
- Missing services: "Start Postgres, the generation endpoint on port 8001, and the embedding endpoint on port 8003."
- Missing arteries: "Set ARTERIES_ROOT or clone arteries as a sibling directory."
- Missing capillaries: "Pass --capillaries-root /path/to/capillaries."

## Removal

If the user says "remove arteries" or "uninstall arteries", run:

```bash
bash <arteries-root>/scripts/art.sh setup <provider> --remove
```
