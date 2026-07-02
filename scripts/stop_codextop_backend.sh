#!/usr/bin/env bash
set -euo pipefail

CODEXTOP_CODEX_DIR="${CODEXTOP_CODEX_DIR:-${CODEX_HOME:-$HOME/.codex}}"
LOG_DIR="${CODEXTOP_LOG_DIR:-$CODEXTOP_CODEX_DIR/codextop/log}"
PID_FILE="$LOG_DIR/sampler.pid"

if [[ ! -f "$PID_FILE" ]]; then
  exit 0
fi

PID="$(<"$PID_FILE")"
if [[ "$PID" =~ ^[0-9]+$ ]] && kill -0 "$PID" >/dev/null 2>&1; then
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
