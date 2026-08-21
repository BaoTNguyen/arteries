#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/_env.sh
project="${1:-.}"
out="${2:-evergreen_review.md}"
"$PY" -m arteries.evergreen extract --project "$project" --out "$out"
