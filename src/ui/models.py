"""Small data containers shared by CodexTOP UI modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.i18n import detect_language, t

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
    language: str = field(default_factory=detect_language)
    merged_quota_mode: str = "percent"
    dashboard_density: str = "default"
    hide_account_info: bool = False
    usage_directory_scope: str = "current"
    usage_panel_layout: str = "combined"
    usage_axis_scale: str = "default"
    last_update: float | None = None
    next_read: float = 0.0
    status: str = field(default_factory=lambda: t("status.starting"))
    error: str | None = None
    last_records_read: float = 0.0
    records: list[dict[str, Any]] | None = None
    control_path: Path | None = None
    summary_offset: int = 0
    settings_mode: str = "normal"
    settings_category_focus: int = 0
    settings_focus: int = 0
    settings_option_focus: int = 0
    update_check_path: Path | None = None
    update_checking: bool = False
    update_available: bool = False
    update_latest_version: str | None = None
    update_release_name: str | None = None
    update_release_notes: str | None = None
    update_reason: str | None = None
    update_error: str | None = None
    version_dialog_open: bool = False
    update_dialog_open: bool = False
    update_notes_scroll: int = 0
    update_requested: bool = False
    token_monitor: Any = None
    token_version: int = 0
    token_total_reader: Any = None
    token_total_version: int = 0
    preloader: Any = None
    background_preload_started: bool = False
    history_preload_waiting: bool = False
    token_preload_waiting: bool = False
    history_cache: Any = None
    records_version: int = 0
    main_cache_key: tuple[Any, ...] | None = None
    main_cache_lines: list[str] | None = None
    main_cache_zones: list[ClickZone] | None = None
    daily_summary_date: str | None = None
    statistics_open: bool = False
    statistics_granularity: str = "day"
    statistics_anchor: str | None = None
    statistics_auto_close_at: float | None = None
    statistics_week_start: int = 0
    statistics_cache_key: tuple[Any, ...] | None = None
    statistics_cache: Any = None
