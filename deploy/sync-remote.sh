#!/usr/bin/env bash
# Push the current branch to a local bare repository on a remote host, so that
# `ct self-update` there works without GitHub credentials.
#
#   bash deploy/sync-remote.sh [branch]
#
# Defaults: ssh alias `origin-host`, bare path ~/.calltable/upstream.git.
# Override with CALLTABLE_REMOTE / CALLTABLE_BARE.
set -euo pipefail

TARGET="${CALLTABLE_REMOTE:-origin-host}"
BARE="${CALLTABLE_BARE:-.calltable/upstream.git}"
BRANCH="${1:-main}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

if git status --porcelain | grep -q .; then
  echo "!! working tree is dirty; commit before syncing" >&2
  exit 1
fi

# Make sure the bare repository exists on the target.
ssh "$TARGET" "test -d '$BARE' || (git init --bare -q '$BARE' && git -C '$BARE' symbolic-ref HEAD refs/heads/main)"

if git remote | grep -qx calltable-upstream; then
  git remote set-url calltable-upstream "$TARGET:$BARE"
else
  git remote add calltable-upstream "$TARGET:$BARE"
fi
git push calltable-upstream "$BRANCH"

echo "pushed $BRANCH to $TARGET:$BARE"
echo "on $TARGET: git -C ~/.calltable/src pull --ff-only   # or ct self-update"
