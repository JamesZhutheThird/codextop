#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
CODEXTOP_CODEX_DIR="${CODEXTOP_CODEX_DIR:-${CODEX_HOME:-$HOME/.codex}}"
CODEXTOP_RUNTIME_DIR="$CODEXTOP_CODEX_DIR/codextop"
AUTH_FILE="${CODEXTOP_AUTH:-$CODEXTOP_CODEX_DIR/auth.json}"
AUTH_LIST="${CODEXTOP_AUTH_LIST:-$CODEXTOP_RUNTIME_DIR/auth_list}"
LOG_DIR="${CODEXTOP_LOG_DIR:-$CODEXTOP_RUNTIME_DIR/log}"
SETTINGS_DIR="$CODEXTOP_RUNTIME_DIR/settings"
INTERVAL="${CODEXTOP_INTERVAL:-60}"
STDOUT_LOG="$LOG_DIR/sampler.stdout.log"
STDERR_LOG="$LOG_DIR/sampler.stderr.log"
PID_FILE="$LOG_DIR/sampler.pid"

mkdir -p "$AUTH_LIST/backup" "$LOG_DIR" "$SETTINGS_DIR"

if [[ -f "$PID_FILE" ]]; then
  PID="$(<"$PID_FILE")"
  if [[ "$PID" =~ ^[0-9]+$ ]] && kill -0 "$PID" >/dev/null 2>&1; then
    exit 0
  fi
fi

ARGS=(
  "$REPO_DIR/src/codextop/codex_quota_sampler.py"
  --auth "$AUTH_FILE"
  --auth-list "$AUTH_LIST"
  --interval "$INTERVAL"
  --log-dir "$LOG_DIR"
)

if [[ "${CODEXTOP_ALL_AUTH:-1}" == "1" ]]; then
  ARGS+=(--all-auth)
fi

cd "$REPO_DIR"
export PYTHONDONTWRITEBYTECODE
if command -v setsid >/dev/null 2>&1; then
  setsid -f "$PYTHON_BIN" "${ARGS[@]}" >>"$STDOUT_LOG" 2>>"$STDERR_LOG" < /dev/null
else
  nohup "$PYTHON_BIN" "${ARGS[@]}" >>"$STDOUT_LOG" 2>>"$STDERR_LOG" < /dev/null &
fi
