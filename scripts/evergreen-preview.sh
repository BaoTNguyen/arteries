#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-src}"
review="${1:-evergreen_review.md}"
python3 -m arteries.evergreen import --review "$review"
