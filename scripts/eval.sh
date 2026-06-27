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

python3 -m arteries.eval "$prompt"
