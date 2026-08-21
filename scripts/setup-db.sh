#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/_env.sh
"$PY" -m arteries.setup_db
