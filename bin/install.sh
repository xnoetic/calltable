#!/usr/bin/env bash
# Install/update the calltable client: clone or pull into ~/.calltable/src and
# put the `ct` shim on PATH.
#
#   bash bin/install.sh
#
# Overridables: CALLTABLE_REPO_URL, CALLTABLE_SRC, CALLTABLE_BIN, PYTHON.
set -euo pipefail

REPO_URL="${CALLTABLE_REPO_URL:-https://github.com/eanzhao-os/calltable.git}"
SRC="${CALLTABLE_SRC:-$HOME/.calltable/src}"
BIN_DIR="${CALLTABLE_BIN:-$HOME/.local/bin}"
PYTHON_BIN="${PYTHON:-python3}"

if [ -d "$SRC/.git" ]; then
  echo "==> updating $SRC"
  git -C "$SRC" pull --ff-only
else
  echo "==> cloning $REPO_URL -> $SRC"
  mkdir -p "$(dirname "$SRC")"
  git clone "$REPO_URL" "$SRC"
fi

chmod +x "$SRC/bin/ct"
mkdir -p "$BIN_DIR"
ln -sfn "$SRC/bin/ct" "$BIN_DIR/ct"
echo "==> linked $BIN_DIR/ct -> $SRC/bin/ct"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "!! add this to your shell profile: export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  "$BIN_DIR/ct" --version || true
else
  echo "!! python3 not found; install Python 3.12+ first" >&2
fi
echo "next: ct setup --hub http://hub.example.internal:8091 && ct doctor"
