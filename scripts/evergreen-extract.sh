#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
CAPILLARIES_SRC="${CAPILLARIES_ROOT:-../capillaries}/src"
export PYTHONPATH="src:${CAPILLARIES_SRC}:${PYTHONPATH:-}"
project="${1:-.}"
out="${2:-evergreen_review.md}"
python3 -m arteries.evergreen extract --project "$project" --out "$out"
