"""Load CodexTOP runtime defaults from the repository's pyproject.toml."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


PROJECT_CONFIG_ENV = "CODEXTOP_PROJECT_CONFIG"
DEFAULT_PROJECT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"
DEFAULT_RESET_CREDITS_INTERVAL_SECONDS = 60
DEFAULT_HTTP_429_FALLBACK_SECONDS = 10 * 60
DEFAULT_WEEK_START = 0
WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass(frozen=True, slots=True)
class CodexTopProjectConfig:
    reset_credits_interval_seconds: int = DEFAULT_RESET_CREDITS_INTERVAL_SECONDS
    http_429_fallback_seconds: int = DEFAULT_HTTP_429_FALLBACK_SECONDS
    week_start: int = DEFAULT_WEEK_START


def _positive_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 1:
        return fallback
    return int(value)


def _week_start(value: Any) -> int:
    if isinstance(value, str):
        return WEEKDAY_NAMES.get(value.strip().lower(), DEFAULT_WEEK_START)
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6:
        return value
    return DEFAULT_WEEK_START


def project_config_path() -> Path:
    configured = os.environ.get(PROJECT_CONFIG_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_PROJECT_CONFIG_PATH


def load_project_config(path: Path | None = None) -> CodexTopProjectConfig:
    config_path = (path or project_config_path()).expanduser()
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return CodexTopProjectConfig()

    tool = payload.get("tool") if isinstance(payload, dict) else None
    codextop = tool.get("codextop") if isinstance(tool, dict) else None
    codextop = codextop if isinstance(codextop, dict) else {}
    return CodexTopProjectConfig(
        reset_credits_interval_seconds=_positive_int(
            codextop.get("reset_credits_interval_seconds"),
            DEFAULT_RESET_CREDITS_INTERVAL_SECONDS,
        ),
        http_429_fallback_seconds=_positive_int(
            codextop.get("http_429_fallback_seconds"),
            DEFAULT_HTTP_429_FALLBACK_SECONDS,
        ),
        week_start=_week_start(codextop.get("week_start")),
    )


PROJECT_CONFIG = load_project_config()
