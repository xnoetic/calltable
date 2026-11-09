#!/usr/bin/env bash
# Executor entry point for a WSL distribution started from a Windows scheduled
# task (WSL usually has no systemd, so the executor falls back to direct mode).
#
#   bash deploy/wsl/exec.sh
#
# Overridables: CALLTABLE_SRC (checkout inside WSL), CALLTABLE_CONFIG.
set -euo pipefail

SRC="${CALLTABLE_SRC:-$HOME/.calltable/src}"
CONFIG="${CALLTABLE_CONFIG:-$HOME/.config/calltable/exec.json}"
export PYTHONPATH="$SRC${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m calltable exec --config "$CONFIG"
