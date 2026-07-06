"""Small data containers shared by CodexTOP UI modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass
class ClickZone:
    x1: int
    x2: int
    y: int
    kind: str
    value: Any


@dataclass
class MonitorState:
    period: str
    interval: int
    tz: str
    log_path: Path
    restore_interval: int
    state_path: Path
    curve_mode: str
    display_scope: str
    window_scope: str
    color_scheme: str
    last_update: float | None = None
    next_read: float = 0.0
    status: str = "启动中"
    error: str | None = None
    last_records_read: float = 0.0
    records: list[dict[str, Any]] | None = None
    control_path: Path | None = None
    summary_offset: int = 0
    settings_mode: str = "normal"
    settings_focus: int = 0
    settings_option_focus: int = 0
    update_check_path: Path | None = None
    update_checking: bool = False
    update_available: bool = False
    update_latest_version: str | None = None
    update_reason: str | None = None
    update_error: str | None = None
    update_confirming: bool = False
    update_requested: bool = False
