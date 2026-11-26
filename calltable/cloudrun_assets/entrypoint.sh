#!/bin/sh
# calltable Cloud Run executor entrypoint.
#
# Environment (set by the Cloud Run job definition / the hub at execute time):
#   CALLTABLE_JOB_ID     job reserved for this execution (per execution)
#   CALLTABLE_HUB_URL    hub URL, normally a tailnet address
#   CALLTABLE_TOKEN      hub bearer token (Secret Manager)
#   CALLTABLE_EXEC_HOST  calltable host name (default: cloudrun)
#   TS_AUTHKEY           optional Tailscale auth key (Secret Manager); when set,
#                        a userspace tailscaled is started in this container and
#                        only the executor's hub traffic goes through it.
#   CALLTABLE_WORK_DIR   optional work root (an ephemeral-disk mount); default /tmp
#   CALLTABLE_MIRROR_ROOT  directory with git mirrors baked into the image
#                        (default /opt/calltable/mirrors); used as the executor's
#                        mirror_root when it exists, else $WORK/mirrors
#   CALLTABLE_GIT_URL, CALLTABLE_GIT_REFS  set by the hub when it pushed the job's
#                        commit to the project's git remote (profile cloudrun.git);
#                        CALLTABLE_GIT_TOKEN optional read token (Secret Manager)
set -eu

: "${CALLTABLE_JOB_ID:?CALLTABLE_JOB_ID is required}"
: "${CALLTABLE_HUB_URL:?CALLTABLE_HUB_URL is required}"
: "${CALLTABLE_TOKEN:?CALLTABLE_TOKEN is required}"
HOST="${CALLTABLE_EXEC_HOST:-cloudrun}"
WORK="${CALLTABLE_WORK_DIR:-/tmp}/calltable"
STATE="/tmp/calltable-state"
mkdir -p "$WORK" "$STATE"
chmod 0700 "$WORK" "$STATE"
MIRRORS="$WORK/mirrors"
BAKED="${CALLTABLE_MIRROR_ROOT:-/opt/calltable/mirrors}"
if [ -d "$BAKED" ]; then
  MIRRORS="$BAKED"
fi

PROXY=""
if [ -n "${TS_AUTHKEY:-}" ]; then
  SOCK="$STATE/tailscaled.sock"
  # Userspace networking: no TUN device or NET_ADMIN needed on Cloud Run.
  # --state=mem: makes this an ephemeral node that disappears after exit.
  tailscaled --tun=userspace-networking --state=mem: --socket="$SOCK" \
    --outbound-http-proxy-listen=localhost:1055 \
    --socks5-server=localhost:1056 >"$STATE/tailscaled.log" 2>&1 &
  TS_NAME="ct-$(printf '%s' "$HOST-$CALLTABLE_JOB_ID" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9-' '-' | cut -c1-50)"
  i=0
  until tailscale --socket="$SOCK" up --auth-key="$TS_AUTHKEY" \
      --hostname="$TS_NAME" --accept-dns=false >"$STATE/tailscale-up.log" 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 10 ]; then
      echo "calltable: tailscale up failed" >&2
      cat "$STATE/tailscale-up.log" "$STATE/tailscaled.log" >&2 || true
      exit 4
    fi
    sleep 2
  done
  PROXY="http://localhost:1055"
  echo "calltable: joined tailnet as $TS_NAME"
fi
unset TS_AUTHKEY

cat >"$STATE/config.json" <<JSON
{
  "token": "",
  "hub_url": "$CALLTABLE_HUB_URL",
  "hub_proxy": "$PROXY",
  "exec_host": "$HOST",
  "data_dir": "$WORK/data",
  "runs_root": "$WORK/runs",
  "mirror_root": "$MIRRORS",
  "max_jobs": 1,
  "poll_interval": 3,
  "use_systemd": false,
  "shell": "/bin/bash",
  "min_free_gb": 0,
  "runs_max_gb": 0
}
JSON
[ -x /bin/bash ] || sed -i 's#"/bin/bash"#"/bin/sh"#' "$STATE/config.json"

# The executor becomes PID 1 here. It notices that and forks a minimal init
# (calltable/proc_init.py) that reaps the processes a job orphans, so the image
# needs no tini / --init.
exec python3 -m calltable exec --config "$STATE/config.json" \
  --once --job-id "$CALLTABLE_JOB_ID"
