#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/_env.sh

if [[ $# -gt 0 ]]; then
  prompt="$*"
else
  prompt="$(cat)"
fi

"$PY" -m arteries.eval "$prompt"
