# Arteries

Always-on memory layer. You do not invoke it — it runs automatically.

Arteries observes every turn, builds memory across sessions, and surfaces
curated prompts from a private corpus when the current situation warrants it.
When a prompt is retrieved, it appears as injected context. Use it as-is —
fill any `[SLOTS]` or `{{mustache}}` placeholders from conversation context.

No action required from you. If no prompt appears, none was relevant.

<!-- arteries:start -->
## Arteries Memory

At session start, run `ARTERIES_CLI=codex bash .arteries/hooks/activate.sh` to start a new arteries run and load context.
On each user prompt, run `ARTERIES_CLI=codex bash .arteries/hooks/generic-observe.sh "<prompt>"` and use any returned text as additional context.
When context pressure or compaction happens, run `ARTERIES_CLI=codex bash .arteries/hooks/compact-packet.sh codex-compact` and preserve the packet as continuity context.

Arteries observes turns, builds memory, may surface retrieved prompts, and produces compact continuity packets.
<!-- arteries:end -->
