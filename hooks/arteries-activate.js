#!/usr/bin/env node
// arteries — SessionStart hook
// Injects arteries context into every session so the agent knows:
// 1. A memory-aware prompt retrieval system is active
// 2. Prompts may appear as injected context (agent doesn't invoke them)

const fs = require('fs');
const path = require('path');

const isCopilot = Boolean(process.env.COPILOT_PLUGIN_DATA);
const isCodex = !isCopilot && Boolean(process.env.PLUGIN_DATA);

const context = `ARTERIES MEMORY SYSTEM ACTIVE.

You have an always-on memory layer (arteries) observing this conversation.
It silently:
- Extracts memories from each turn (ephemeral → persistent → evergreen tiers)
- Tracks domain context, topic drift, and conversation patterns
- Triggers prompt retrieval from a private corpus when a situation warrants it

When arteries retrieves a prompt, it appears as injected context in the
conversation. Use retrieved prompts as-is — fill template slots from
conversation context. Trust the retrieval: arteries already ran gate checks
(heuristic, memory, embedding proximity) before deciding to surface it.

You do NOT need to call any tool or CLI for this — arteries handles it.
If a retrieved prompt appears, it is because the memory + gate system decided
it was relevant to the current situation.`;

function writeOutput(output) {
  if (isCopilot) {
    process.stdout.write(JSON.stringify(
      output ? { additionalContext: output } : {}));
    return;
  }
  if (isCodex) {
    process.stdout.write(JSON.stringify({
      systemMessage: 'ARTERIES:ACTIVE',
      hookSpecificOutput: {
        hookEventName: 'SessionStart',
        additionalContext: output,
      },
    }));
    return;
  }
  process.stdout.write(output);
}

try {
  writeOutput(context);
} catch (e) {
  // Silent fail — don't block session start
}
