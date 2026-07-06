"""Persist CodexTOP state and coordinate the background sampler."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from ui import color_schemes
from .constants import *
from ui.models import MonitorState

def send_sampler_interval(
    state: MonitorState,
    interval: int,
    *,
    sample_now: bool = True,
    all_auth: bool = True,
) -> None:
    control_path = state.control_path or (state.log_path.parent / DEFAULT_CONTROL_FILE)
    control_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": int(time.time()),
        "updated_at_ns": time.time_ns(),
        "interval": max(1, int(interval)),
        "sample_now": bool(sample_now),
        "all_auth": bool(all_auth),
    }
    tmp_path = control_path.with_name(f".{control_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, control_path)


def read_sampler_interval(control_path: Path, fallback: int) -> int:
    try:
        payload = json.loads(control_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback
    interval = payload.get("interval") if isinstance(payload, dict) else None
    if isinstance(interval, (int, float)) and interval > 0:
        return max(1, int(interval))
    return fallback


def read_codextop_state(state_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def saved_period(payload: dict[str, Any]) -> str | None:
    period = payload.get("period")
    return period if isinstance(period, str) and period in PERIOD_CHOICES else None


def saved_interval(payload: dict[str, Any]) -> int | None:
    interval = payload.get("interval")
    if isinstance(interval, (int, float)) and interval > 0:
        return max(1, int(interval))
    return None


def saved_curve_mode(payload: dict[str, Any]) -> str | None:
    mode = payload.get("curve_mode")
    valid_modes = {value for _label, value in CURVE_MODE_CHOICES}
    return mode if isinstance(mode, str) and mode in valid_modes else None


def saved_display_scope(payload: dict[str, Any]) -> str | None:
    scope = payload.get("display_scope")
    valid_scopes = {value for _label, value in DISPLAY_SCOPE_CHOICES}
    return scope if isinstance(scope, str) and scope in valid_scopes else None


def saved_window_scope(payload: dict[str, Any]) -> str | None:
    scope = payload.get("window_scope")
    valid_scopes = {value for _label, value in WINDOW_SCOPE_CHOICES}
    return scope if isinstance(scope, str) and scope in valid_scopes else None


def saved_color_scheme(payload: dict[str, Any]) -> str | None:
    scheme = payload.get("color_scheme")
    return scheme if isinstance(scheme, str) and color_schemes.color_scheme_exists(scheme) else None


def save_codextop_state(state: MonitorState) -> None:
    state.state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": int(time.time()),
        "period": state.period,
        "interval": max(1, int(state.interval)),
        "curve_mode": state.curve_mode,
        "display_scope": state.display_scope,
        "window_scope": state.window_scope,
        "color_scheme": state.color_scheme,
    }
    tmp_path = state.state_path.with_name(f".{state.state_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, state.state_path)


def sampler_status(log_dir: Path) -> str:
    pid_path = log_dir / "sampler.pid"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return "后台 未运行"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "后台 未运行"
    except PermissionError:
        return "后台 运行中"
    return f"后台 PID {pid}"


def parse_interval(value: str) -> int:
    raw = value.strip().lower()
    for label, seconds in INTERVAL_CHOICES:
        if raw == label:
            return seconds
    if raw.endswith("s") and raw[:-1].isdigit():
        seconds = int(raw[:-1])
    elif raw.endswith("m") and raw[:-1].isdigit():
        seconds = int(raw[:-1]) * 60
    elif raw.isdigit():
        seconds = int(raw)
    else:
        raise argparse.ArgumentTypeError("更新间隔格式应为 15s、2m 或秒数")
    if seconds <= 0:
        raise argparse.ArgumentTypeError("更新间隔必须大于 0")
    return seconds
