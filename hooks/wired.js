// The approval check every plugin hook runs before it touches anything.
//
// The plugin is enabled per user, so its hooks fire in every folder Claude
// Code, Cursor (which loads Claude plugins) or any other host opens. Only
// `arteries setup` writes .vascular/arteries/config.json, so that file is the
// user's approval. Without it a hook spawns nothing, writes nothing and
// injects nothing.
//
// Walks up the way git finds .git, so a session in a wired repo's subfolder
// still counts. The root it returns is the repo, which the hook passes on as
// ARTERIES_REPO; otherwise arteries would treat the subfolder as the repo.
const fs = require('fs');
const path = require('path');

function wiredRoot(cwd) {
  if (!cwd) return null;
  for (let dir = path.resolve(cwd); ; dir = path.dirname(dir)) {
    if (fs.existsSync(path.join(dir, '.vascular', 'arteries', 'config.json'))) return dir;
    if (path.dirname(dir) === dir) return null;
  }
}

// The identity `arteries setup` recorded for the repo, and the transcript the
// host names, set on the child's env the way the generated .cjs hooks do.
// Without them a plugin turn is filed under the cwd's name, logged by a pid,
// and priced at $0 for want of a transcript (ARTERIES-B14).
function identity(env, repo, event) {
  let config = {};
  try {
    config = JSON.parse(fs.readFileSync(path.join(repo, '.vascular', 'arteries', 'config.json'), 'utf8'));
  } catch {}
  if (config.project && !env.ARTERIES_PROJECT) env.ARTERIES_PROJECT = String(config.project);
  if (config.agent_id && !env.ARTERIES_AGENT_ID) env.ARTERIES_AGENT_ID = String(config.agent_id);
  const transcript = event.transcript_path || event.transcriptPath;
  if (typeof transcript === 'string' && transcript) env.ARTERIES_TRANSCRIPT = transcript;
}

module.exports = wiredRoot;
module.exports.identity = identity;
