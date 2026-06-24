#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PLUGIN_DATA=1
node hooks/arteries-activate.js
printf '
'
