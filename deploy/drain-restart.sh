#!/usr/bin/env bash
# Restart one Linux host's executor: drain -> wait until idle -> restart -> undrain.
#
#   bash deploy/drain-restart.sh runner-a
#   bash deploy/drain-restart.sh runner-a --dry-run      # drain and wait only
#   CALLTABLE_DRAIN_TIMEOUT=21600 bash deploy/drain-restart.sh runner-a
#
# Needs the `ct` client on PATH (or CALLTABLE_CT pointing at the executable) and
# an ssh login on the target host. Never restarts while jobs are still running:
# if the wait budget is exhausted it leaves the host drained and exits 1.
set -euo pipefail

HOST="${1:?usage: drain-restart.sh <host> [--dry-run]}"
MODE="${2:-}"
CT="${CALLTABLE_CT:-ct}"
TIMEOUT="${CALLTABLE_DRAIN_TIMEOUT:-21600}"
INTERVAL="${CALLTABLE_DRAIN_INTERVAL:-60}"
UNIT="${CALLTABLE_EXEC_UNIT:-calltable-exec.service}"

echo "== drain $HOST"
"$CT" drain "$HOST"

deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  running=$("$CT" hosts --json 2>/dev/null | python3 -c '
import json,sys
host=sys.argv[1]
state=json.load(sys.stdin)
for item in state["hosts"]:
    if item["name"] == host:
        print(len(item.get("running") or []))
        break
else:
    print("0")
' "$HOST")
  echo "   running=$running"
  if [ "$running" = "0" ]; then
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "!! $HOST still has $running running job(s) after ${TIMEOUT}s" >&2
    echo "!! left drained; restart manually once it is idle, then: $CT undrain $HOST" >&2
    exit 1
  fi
  sleep "$INTERVAL"
done

if [ "$MODE" = "--dry-run" ]; then
  echo "== idle; not restarting (--dry-run). Run: $CT undrain $HOST"
  exit 0
fi

echo "== restart executor on $HOST"
ssh "$HOST" "systemctl --user restart $UNIT"
sleep 5
ssh "$HOST" "systemctl --user is-active $UNIT"

echo "== undrain $HOST"
"$CT" undrain "$HOST"
