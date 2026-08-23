#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/_env.sh

if [[ $# -gt 0 ]]; then
  prompt="$*"
else
  prompt="$(cat)"
fi

result="$("$PY" -m arteries.eval "$prompt")"
if [[ -n "$result" ]]; then
  printf 'ARTERIES RETRIEVED PROMPT - use this to guide your response:

%s
' "$result"
fi
