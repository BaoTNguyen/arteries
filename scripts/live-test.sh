#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
CAPILLARIES_SRC="${CAPILLARIES_ROOT:-../capillaries}/src"
export PYTHONPATH="src:${CAPILLARIES_SRC}:${PYTHONPATH:-}"
export ARTERIES_LIVE_TESTS=1
python3 -m unittest tests.test_live_memory_tiers -v
