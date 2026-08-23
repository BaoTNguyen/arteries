#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/_env.sh
# Kept as a thin alias: `art setup` applies the schema as part of bootstrapping
# a repo, and this exists so older hook configs and muscle memory keep working.
"$PY" -m arteries.setup_db
