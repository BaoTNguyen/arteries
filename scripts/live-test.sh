#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/_env.sh
export ARTERIES_LIVE_TESTS=1
"$PY" -m unittest tests.test_live_memory_tiers -v
