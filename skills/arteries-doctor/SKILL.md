---
name: arteries-doctor
description: >
  Diagnose arteries integration health: database connectivity, hook wiring,
  schema state, service reachability, and trace availability. Use when the user
  says "arteries doctor", "is arteries working", "arteries health", "debug
  arteries", "arteries-doctor", or when arteries hooks are failing.
argument-hint: "[project-name]"
---

Run arteries doctor for the current project and diagnose issues.

## Steps

1. Determine project name from the argument or current directory name.

2. Find arteries root using the same logic as `arteries-setup`.

3. Run doctor from the target project:

```bash
bash <arteries-root>/scripts/art.sh doctor --project <project> --cli claude --repo .
```

Use the actual CLI name when known: `codex`, `pi`, or `claude`.

4. Check services:

```bash
pg_isready
curl -sf http://127.0.0.1:8001/health
curl -sf http://127.0.0.1:8003/health
curl -sf http://127.0.0.1:8000/health
```

5. Check provider wiring:

```bash
bash <arteries-root>/scripts/art.sh setup <provider> --check
```

6. If doctor passes but behavior is confusing, run central trace:

```bash
bash <arteries-root>/scripts/art.sh trace --repo . --events 50 --memories 10
```

7. Report specific fixes:
   - `db_ok: false`: start Postgres or run `bash <arteries-root>/scripts/setup-db.sh`.
   - `schema_ok: false`: run `bash <arteries-root>/scripts/setup-db.sh`.
   - Service down: name the missing service and port.
   - Hooks missing: run `bash <arteries-root>/scripts/art.sh setup <provider>`.
   - Codex config error: check that `experimental_compact_prompt_file` is top-level and points to `../.arteries/codex/compact_prompt.txt`.
