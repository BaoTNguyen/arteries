#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-src}"

prompt="${1:-thanks}"

echo '== unit tests =='
bash scripts/test.sh

echo '== eval =='
if output="$(bash scripts/eval.sh "$prompt")"; then
  if [[ -n "$output" ]]; then
    printf '%s
' "$output"
  else
    echo '(no retrieved prompt)'
  fi
else
  echo 'eval failed'
  exit 1
fi

echo '== generic observe =='
generic_output="$(bash scripts/generic-observe.sh "$prompt")"
if [[ -n "$generic_output" ]]; then
  printf '%s
' "$generic_output"
else
  echo '(no generic context)'
fi

echo '== codex/claude observe hook =='
bash scripts/hook-observe-smoke.sh "$prompt"

echo '== session start hook =='
bash scripts/hook-activate-smoke.sh

echo '== db check =='
if command -v psql >/dev/null 2>&1; then
  psql "${DB_NAME:-capillaries}" -c "select count(*) as ephemeral_rows from arteries.ephemeral;" || true
else
  echo 'psql not found; skipped'
fi
