---
name: arteries-watch
description: >
  Show arteries status and trace output: service health, memory counts, recent
  events, prompt timeline entries, retrieved prompt references, and capillaries
  search status for a project. Use when the user says "arteries status",
  "arteries watch", "what is arteries doing", "show memory", "show trace",
  "arteries dashboard", or "arteries-watch".
argument-hint: "[project-name]"
---

Show a concise arteries status report.

## Steps

1. Determine the project name from the argument, or fall back to the current directory name.

2. Find arteries root using the same logic as `arteries-setup`.

3. Check service health:

```bash
pg_isready 2>/dev/null
curl -sf http://127.0.0.1:8001/health
curl -sf http://127.0.0.1:8003/health
curl -sf http://127.0.0.1:8000/health
```

4. If the target repo is known, prefer central trace:

```bash
bash <arteries-root>/scripts/art.sh trace --repo /path/to/repo --events 50 --memories 10
```

5. Otherwise use project-level inspect and summary:

```bash
bash <arteries-root>/scripts/art.sh inspect --project <project> --agent <project>-hook --events 10
bash <arteries-root>/scripts/art.sh runs summary --project <project> --limit 50
```

6. Report services, memory counts, recent failures, recent gate decisions, retrieved prompt ids/titles, and the current run id.

For a live terminal dashboard, suggest:

```bash
bash <arteries-root>/scripts/watch.sh <project>
```
