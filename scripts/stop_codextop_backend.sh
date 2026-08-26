#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CODEXTOP_CODEX_DIR="${CODEXTOP_CODEX_DIR:-${CODEX_HOME:-$HOME/.codex}}"
LOG_DIR="${CODEXTOP_LOG_DIR:-$CODEXTOP_CODEX_DIR/codextop/log}"
PID_FILE="$LOG_DIR/sampler.pid"

if [[ ! -f "$PID_FILE" ]]; then
  exit 0
fi

PID="$(<"$PID_FILE")"
if [[ "$PID" =~ ^[0-9]+$ ]] && kill -0 "$PID" >/dev/null 2>&1; then
  if [[ ! -r "/proc/$PID/cmdline" ]]; then
    PYTHONPATH="$REPO_DIR/src" "$PYTHON_BIN" - "$PID" <<'PY' >&2
import sys

from core.i18n import t

print(t("backend.stop_unverified", pid=sys.argv[1]))
PY
    exit 1
  fi
  CMDLINE="$(tr '\0' ' ' < "/proc/$PID/cmdline")"
  if [[ "$CMDLINE" != *"codex_quota_sampler.py"* ]]; then
    rm -f "$PID_FILE"
    exit 0
  fi
  kill "$PID"
  for _ in {1..20}; do
    if ! kill -0 "$PID" >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done
fi

if [[ -f "$PID_FILE" ]]; then
  rm -f "$PID_FILE"
fi
