#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PLUGIN_DATA=1
printf '{"cwd":"%s"}' "$PWD" | node hooks/arteries-activate.js
printf '
'
