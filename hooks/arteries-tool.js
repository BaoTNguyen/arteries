#!/usr/bin/env node
// arteries — PostToolUse hook
//
// Finding 22: the evidence ladder had one rung. Everything stored was `stated`,
// something said in a session. Nothing was `observed` -- the test actually
// passed, the file actually existed, the command actually exited 0. With one
// rung, two contradicting claims can only be ordered by recency.
//
// This is the only place observations exist. It is also the highest-volume hook
// there could be, firing on every tool call, so what it records is deliberately
// narrow:
//
//   * tool name, exit status, and target path. Never output.
//   * only calls that observed something. A successful Read confirms nothing
//     that was in doubt; a non-zero exit, a write, or a delete does.
//
// Writes an event, never an ephemeral row. An observation is evidence the
// compiler can weigh, not a memory competing for packet slots -- promoting one
// needs an explicit rule that does not exist yet.

const { execFileSync } = require('child_process');

function read(stream) {
  try { return JSON.parse(require('fs').readFileSync(0, 'utf8') || '{}'); }
  catch { return {}; }
}

const event = read();
const tool = event.tool_name || event.toolName || '';
const input = event.tool_input || event.toolInput || {};
const response = event.tool_response || event.toolResponse || {};

// A successful read observes nothing. A refactoring turn is fifty of those, and
// fifty rows saying "the file I asked for existed" is not evidence, it is noise
// competing with the one row that mattered.
const exitCode = Number(response.exit_code ?? response.exitCode ?? 0);
const failed = exitCode !== 0 || Boolean(response.error) || response.is_error === true;
const mutating = /write|edit|create|delete|remove|move|rename|notebook/i.test(tool);

if (!failed && !mutating) process.exit(0);

const target = input.file_path || input.path || input.notebook_path ||
  (typeof input.command === 'string' ? input.command.slice(0, 200) : '') || '';

try {
  execFileSync('python3', ['-m', 'arteries.observe_tool'], {
    input: JSON.stringify({ tool, exit_code: exitCode, failed, target }),
    timeout: 3000,
    stdio: ['pipe', 'ignore', 'ignore'],
  });
} catch {
  // Memory must never fail a turn, and an observation least of all.
}
process.exit(0);
