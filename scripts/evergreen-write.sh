#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
CAPILLARIES_SRC="${CAPILLARIES_ROOT:-../capillaries}/src"
export PYTHONPATH="src:${CAPILLARIES_SRC}:${PYTHONPATH:-}"
review="${1:-evergreen_review.md}"
python3 -m arteries.evergreen import --review "$review" --write
