#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/_env.sh
review="${1:-evergreen_review.md}"
"$PY" -m arteries.evergreen import --review "$review" --write
