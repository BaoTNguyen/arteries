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
const fs = require('fs');
const path = require('path');

function read(stream) {
  try { return JSON.parse(fs.readFileSync(0, 'utf8') || '{}'); }
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
// A web fetch is recorded whatever its outcome: it is how arteries knows the
// session read the open web, and the assistant's later words in it are marked
// untrusted (trust.py). The host only -- never the query or the full URL.
const web = /^(webfetch|websearch|web_fetch|web_search)$/i.test(tool);

if (!failed && !mutating && !web) process.exit(0);

// The session's folder, never ours: CLAUDE_PLUGIN_ROOT or process.cwd() would
// attribute the observation to the plugin checkout.
const cwd = (typeof event.cwd === 'string' && event.cwd) ||
  (Array.isArray(event.workspace_roots) &&
   event.workspace_roots.find(r => typeof r === 'string' && r)) || '';
if (!cwd) process.exit(0);

const sessionId = event.session_id || event.conversation_id || undefined;
const pluginRoot = process.env.CLAUDE_PLUGIN_ROOT || path.resolve(__dirname, '..');
const env = { ...process.env };
env.ARTERIES_CLI = env.ARTERIES_CLI || (event.cursor_version ? 'cursor' : 'claude');
env.ARTERIES_EVENT_CWD = cwd;
if (sessionId) env.ARTERIES_SESSION_ID = String(sessionId);
else delete env.ARTERIES_SESSION_ID;
const srcPath = path.join(pluginRoot, 'src');
const capSrc = process.env.CAPILLARIES_ROOT
  ? path.join(process.env.CAPILLARIES_ROOT, 'src')
  : path.join(pluginRoot, '..', 'capillaries', 'src');
// ponytail: capillaries path is best-effort; arteries works without it
const extra = fs.existsSync(capSrc) ? `${srcPath}:${capSrc}` : srcPath;
env.PYTHONPATH = env.PYTHONPATH ? `${extra}:${env.PYTHONPATH}` : extra;

let host = '';
if (web && typeof input.url === 'string') {
  try { host = new URL(input.url).host; } catch { host = ''; }
}
const target = web ? (host || 'search') :
  (input.file_path || input.path || input.notebook_path ||
   (typeof input.command === 'string' ? input.command.slice(0, 200) : '') || '');

try {
  execFileSync('python3', ['-m', 'arteries.observe_tool'], {
    input: JSON.stringify({ tool, exit_code: exitCode, failed, target,
                            session_id: sessionId }),
    timeout: 3000,
    stdio: ['pipe', 'ignore', 'ignore'],
    env,
    cwd,
  });
} catch {
  // Memory must never fail a turn, and an observation least of all.
}
process.exit(0);
