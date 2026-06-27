#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
CAPILLARIES_SRC="${CAPILLARIES_ROOT:-../capillaries}/src"
export PYTHONPATH="src:${CAPILLARIES_SRC}:${PYTHONPATH:-}"

if [[ $# -gt 0 ]]; then
  prompt="$*"
else
  prompt="$(cat)"
fi

result="$(python3 -m arteries.eval "$prompt")"
if [[ -n "$result" ]]; then
  printf 'ARTERIES RETRIEVED PROMPT - use this to guide your response:

%s
' "$result"
fi
