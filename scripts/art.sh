#!/usr/bin/env bash
set -euo pipefail

export ARTERIES_CALLER_CWD="${ARTERIES_CALLER_CWD:-$PWD}"
cd "$(dirname "$0")/.."
CAPILLARIES_SRC="${CAPILLARIES_ROOT:-../capillaries}/src"
export PYTHONPATH="src:${CAPILLARIES_SRC}:${PYTHONPATH:-}"
python3 -m arteries "$@"
