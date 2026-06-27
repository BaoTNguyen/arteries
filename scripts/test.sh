#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
CAPILLARIES_SRC="${CAPILLARIES_ROOT:-../capillaries}/src"
export PYTHONPATH="src:${CAPILLARIES_SRC}:${PYTHONPATH:-}"
python3 -m unittest discover -s tests -v
