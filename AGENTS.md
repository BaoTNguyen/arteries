# Arteries

Always-on memory layer. You do not invoke it — it runs automatically.

Arteries observes every turn, builds memory across sessions, and surfaces
curated prompts from a private corpus when the current situation warrants it.
When a prompt is retrieved, it appears as injected context. Use it as-is —
fill any `[SLOTS]` or `{{mustache}}` placeholders from conversation context.

No action required from you. If no prompt appears, none was relevant.

<!-- arteries:start -->
## Arteries Memory

At session start, run `PLUGIN_DATA=1 node /home/bao-tn/Coding/Projects/arteries/.arteries/hooks/arteries-activate.cjs` and include the output as session context.
On each user prompt, pipe the prompt JSON to `PLUGIN_DATA=1 node /home/bao-tn/Coding/Projects/arteries/.arteries/hooks/arteries-observe.cjs` and use any returned `additionalContext` to guide your response.
When compacting, run `ARTERIES_CLI=codex bash /home/bao-tn/Coding/Projects/arteries/.arteries/hooks/hook-compact-packet.sh codex-compact` and preserve the returned continuity packet.
When an assistant response is available from a hook or transcript event, pipe that event to `ARTERIES_CLI=codex bash /home/bao-tn/Coding/Projects/arteries/.arteries/hooks/hook-assistant-observe.sh codex-assistant` so Arteries can extract assistant-discovered project memory.

Arteries observes turns and assistant responses, builds memory, may surface retrieved prompts, and produces compact continuity packets as additional context.
<!-- arteries:end -->
