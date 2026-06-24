#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-src}"
project="${1:-.}"
out="${2:-evergreen_review.md}"
python3 -m arteries.evergreen extract --project "$project" --out "$out"
