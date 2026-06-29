#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
CAPILLARIES_SRC="${CAPILLARIES_ROOT:-../capillaries}/src"
export PYTHONPATH="src:${CAPILLARIES_SRC}:${PYTHONPATH:-}"

project="${1:-capillaries}"
interval="${2:-10}"

bold='\033[1m'
dim='\033[2m'
cyan='\033[36m'
green='\033[32m'
yellow='\033[33m'
nc='\033[0m'

header() { echo -e "\n${bold}${cyan}── $* ──${nc}"; }

while true; do
  clear
  echo -e "${bold}arteries watch${nc}  ${dim}project=${project}  $(date +%H:%M:%S)${nc}"

  header "Services"
  for pair in "Postgres:5432" "LLM:8001" "Embeddings:8003" "Capillaries:8000"; do
    name="${pair%%:*}"; port="${pair##*:}"
    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
      echo -e "  ${green}●${nc} ${name} :${port}"
    else
      echo -e "  ${yellow}○${nc} ${name} :${port}"
    fi
  done

  header "Memory"
  python3 -c "
import json
from arteries import storage
eph = storage.get_ephemeral('$project', '$project-hook', limit=50)
per = storage.get_persistent('$project', limit=50)
evg = storage.get_evergreen(limit=50)
print(f'  ephemeral: {len(eph)}  persistent: {len(per)}  evergreen: {len(evg)}')
if per:
    for m in per[-3:]:
        print(f'    • {m[\"fact\"][:80]}')
" 2>/dev/null || echo "  (db unavailable)"

  header "Recent Events (last 8)"
  python3 -c "
import json
from arteries import runlog
events = runlog.recent_events('$project', limit=8)
for e in events:
    ts = e.get('created_at','')
    if len(ts) > 19: ts = ts[5:19]
    et = e.get('event_type','?')
    src = e.get('source','?')
    payload = e.get('payload', {})
    detail = ''
    if et == 'prompt.retrieved':
        detail = f'conf={payload.get(\"confidence\",\"?\"):.2f}' if isinstance(payload.get('confidence'), (int,float)) else ''
    elif et == 'prompt.gate.decided':
        detail = payload.get('reason','')[:60]
    elif et == 'memory.ephemeral.extracted':
        detail = f'count={payload.get(\"count\",0)}'
    elif et == 'memory.compile.completed':
        detail = f'new={payload.get(\"new_persistent\",0)}'
    elif 'error' in payload:
        detail = payload['error'][:50]
    print(f'  {ts}  {et:<30} {src:<12} {detail}')
" 2>/dev/null || echo "  (db unavailable)"

  header "Capillaries Search"
  if curl -sf http://127.0.0.1:8000/health 2>/dev/null | python3 -c "
import sys, json
h = json.load(sys.stdin)
print(f'  prompts: {h.get(\"prompt_count\",\"?\")}  ready: {h.get(\"ready\",\"?\")}')
" 2>/dev/null; then true; else echo "  (server down)"; fi

  sleep "$interval"
done
