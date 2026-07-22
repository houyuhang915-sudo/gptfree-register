#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -r requirements.txt

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

export TZ="${TZ:-${FREE_CONSOLE_TIMEZONE:-Asia/Shanghai}}"
export FREE_CONSOLE_TIMEZONE="${FREE_CONSOLE_TIMEZONE:-$TZ}"

exec gunicorn --workers 1 --threads "${FREE_CONSOLE_THREADS:-8}" --timeout 120 \
  --bind "${FREE_CONSOLE_HOST:-0.0.0.0}:${FREE_CONSOLE_PORT:-8866}" app:app
