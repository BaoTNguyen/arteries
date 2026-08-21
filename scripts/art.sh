#!/usr/bin/env bash
set -euo pipefail

export ARTERIES_CALLER_CWD="${ARTERIES_CALLER_CWD:-$PWD}"
cd "$(dirname "$0")/.."
source scripts/_env.sh
"$PY" -m arteries "$@"
