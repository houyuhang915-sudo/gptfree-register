#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PARENT="$(dirname "$ROOT")"
PROJECT="$(basename "$ROOT")"
VERSION="${FREE_CONSOLE_RELEASE_VERSION:-0.1.0}"
ARCHIVE="${1:-$PARENT/${PROJECT}-${VERSION}.tar.gz}"

rm -f "$ARCHIVE"
COPYFILE_DISABLE=1 tar -C "$PARENT" -czf "$ARCHIVE" \
  --exclude="$PROJECT/.env" \
  --exclude="$PROJECT/.git" \
  --exclude="$PROJECT/.venv" \
  --exclude="$PROJECT/.pytest_cache" \
  --exclude="$PROJECT/.playwright-cli" \
  --exclude="$PROJECT/data" \
  --exclude="$PROJECT/output" \
  --exclude="$PROJECT/deploy-data" \
  --exclude="$PROJECT/__pycache__" \
  --exclude="$PROJECT/**/__pycache__" \
  --exclude="$PROJECT/**/*.pyc" \
  --exclude="$PROJECT/core/outlook_accounts.txt" \
  --exclude="$PROJECT/core/icloud_accounts.txt" \
  --exclude="$PROJECT/core/output" \
  --exclude="$PROJECT/core/web_data" \
  --exclude="$PROJECT/core/runtime" \
  --exclude="$PROJECT/core/node_modules" \
  --exclude="$PROJECT/.DS_Store" \
  "$PROJECT"

if tar -tzf "$ARCHIVE" | grep -Eq "^${PROJECT}/(\\.env$|\\.git/|data/|output/|deploy-data/|\\.venv/|\\.pytest_cache/|\\.playwright-cli/|core/(outlook_accounts\\.txt|icloud_accounts\\.txt|output/|web_data/|runtime/|node_modules/)|.*__pycache__/|.*\\.pyc$)"; then
  echo "Archive contains runtime data or credential paths: $ARCHIVE" >&2
  exit 1
fi

printf 'Created clean release archive: %s\n' "$ARCHIVE"
