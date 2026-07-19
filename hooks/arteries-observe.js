#!/usr/bin/env node
// arteries — UserPromptSubmit hook
// Fires on every user message. Runs the arteries pipeline:
// 1. Reads the user prompt
// 2. Checks the gate (heuristic → memory → embedding proximity)
// 3. If gate says search: calls cap find with current MemoryFrame
// 4. Injects retrieved prompt as additional context for the agent
//
// The heavy lifting (memory extraction, MemoryFrame population) runs
// in the arteries Python process. This hook is the bridge.

const fs = require('fs');
const { execFileSync } = require('child_process');
const path = require('path');

const isCopilot = Boolean(process.env.COPILOT_PLUGIN_DATA);
const isCodex = !isCopilot && Boolean(process.env.PLUGIN_DATA);

function writeOutput(output) {
  if (isCopilot) {
    process.stdout.write(JSON.stringify(
      output ? { additionalContext: output } : {}));
    return;
  }
  if (isCodex) {
    process.stdout.write(JSON.stringify({
      systemMessage: 'ARTERIES:RETRIEVAL',
      hookSpecificOutput: {
        hookEventName: 'UserPromptSubmit',
        additionalContext: output,
      },
    }));
    return;
  }
  process.stdout.write(output || '');
}

let input = '';
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  try {
    const data = JSON.parse(input.replace(/^﻿/, ''));
    const prompt = (data.prompt || '').trim();

    if (!prompt) {
      writeOutput('');
      return;
    }

    // Call arteries Python process to evaluate the turn.
    // arteries-eval reads the prompt, checks the gate with current
    // MemoryFrame, and if retrieval is warranted, calls cap find
    // and returns the prompt text to inject. Returns empty if no
    // retrieval needed.
    const pluginRoot = process.env.CLAUDE_PLUGIN_ROOT || process.cwd();
    const env = { ...process.env };
    env.ARTERIES_CLI = env.ARTERIES_CLI || 'codex';
    env.ARTERIES_EVENT = env.ARTERIES_EVENT || 'UserPromptSubmit';
    const srcPath = path.join(pluginRoot, 'src');
    const capSrc = process.env.CAPILLARIES_ROOT
      ? path.join(process.env.CAPILLARIES_ROOT, 'src')
      : path.join(pluginRoot, '..', 'capillaries', 'src');
    // ponytail: capillaries path is best-effort; arteries works without it
    const extra = fs.existsSync(capSrc) ? `${srcPath}:${capSrc}` : srcPath;
    env.PYTHONPATH = env.PYTHONPATH ? `${extra}:${env.PYTHONPATH}` : extra;

    const result = execFileSync(
      'python3',
      ['-m', 'arteries.eval', prompt],
      { timeout: 5000, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'], env }
    ).trim();

    if (result) {
      writeOutput(
        'ARTERIES RETRIEVED PROMPT — use this to guide your response:\n\n' +
        result
      );
    } else {
      writeOutput('');
    }
  } catch (e) {
    // Silent fail — never block the user's turn
    writeOutput('');
  }
});
