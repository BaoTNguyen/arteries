#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-src}"
export PLUGIN_DATA=1
export CLAUDE_PLUGIN_ROOT="$PWD"

prompt="${1:-thanks}"
printf '{"prompt":%s}
' "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$prompt")"   | node hooks/arteries-observe.js
printf '
'
