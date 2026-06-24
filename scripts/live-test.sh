#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-src}"
export ARTERIES_LIVE_TESTS=1
python3 -m unittest tests.test_live_memory_tiers -v
