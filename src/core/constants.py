"""Shared constants for the CodexTOP terminal application."""

from __future__ import annotations

import re

from ui import color_schemes
from .i18n import t
from .paths import default_paths
from .project_config import PROJECT_CONFIG
from .version import APP_VERSION, PACKAGE_VERSION

DEFAULT_PATHS = default_paths()
DEFAULT_LOG_DIR = DEFAULT_PATHS.log_dir
DEFAULT_LOG_FILE = "quota_snapshots.jsonl"
DEFAULT_CONTROL_FILE = "sampler_control.json"
DEFAULT_STATE_FILE = "codextop_state.json"
DEFAULT_SAMPLER_INTERVAL_SECONDS = 60
DEFAULT_PERIOD = "5h"
DEFAULT_CURVE_MODE = "connected"
DEFAULT_DISPLAY_SCOPE = "list"
DEFAULT_DASHBOARD_DENSITY = "default"
DEFAULT_HIDE_ACCOUNT_INFO = False
DEFAULT_WINDOW_SCOPE = "both"
DEFAULT_MERGED_QUOTA_MODE = "percent"
DEFAULT_USAGE_DIRECTORY_SCOPE = "current"
DEFAULT_USAGE_PANEL_LAYOUT = "combined"
DEFAULT_USAGE_AXIS_SCALE = "default"
DEFAULT_COLOR_SCHEME = color_schemes.default_color_scheme_key()
SIDEBAR_WIDTH = 23
INTERVAL_CHOICES = [
    ("5s", 5),
    ("10s", 10),
    ("15s", 15),
    ("30s", 30),
    ("60s", 60),
    ("2m", 120),
    ("5m", 300),
]
PERIOD_CHOICES = ["5m", "15m", "30m", "1h", "5h", "12h", "1d", "3d", "7d", "30d", "all"]
CURVE_MODE_CHOICES = [
    (t("choice.curve.connected"), "connected"),
    (t("choice.curve.points"), "points"),
    (t("choice.curve.braille"), "braille"),
    (t("choice.curve.box"), "box"),
    (t("choice.curve.bar"), "bar"),
    (t("choice.curve.fine_bar"), "fine_bar"),
]
DISPLAY_SCOPE_CHOICES = [
    (t("choice.display.list"), "list"),
    (t("choice.display.focus"), "focus"),
    (t("choice.display.merged"), "merged"),
    (t("choice.display.usage"), "usage"),
]
DASHBOARD_DENSITY_CHOICES = [
    (t("choice.dashboard_density.default"), "default"),
    (t("choice.dashboard_density.compact"), "compact"),
    (t("choice.dashboard_density.relaxed"), "relaxed"),
    (t("choice.dashboard_density.fill"), "fill"),
]
USAGE_DIRECTORY_SCOPE_CHOICES = [
    (t("choice.directory.current"), "current"),
    (t("choice.directory.all"), "all"),
]
USAGE_PANEL_LAYOUT_CHOICES = [
    (t("choice.layout.combined"), "combined"),
    (t("choice.layout.split"), "split"),
]
USAGE_AXIS_SCALE_CHOICES = [
    (t("choice.axis.default"), "default"),
    (t("choice.axis.log"), "log"),
]
WINDOW_SCOPE_CHOICES = [
    (t("choice.window.auto"), "auto"),
    (t("choice.window.both"), "both"),
    (t("choice.window.5h"), "5h"),
    (t("choice.window.7d"), "7d"),
]
MERGED_QUOTA_MODE_CHOICES = [
    (t("choice.merged.percent"), "percent"),
    (t("choice.merged.actual"), "actual"),
]
PERIOD_SECONDS = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 3600,
    "5h": 5 * 3600,
    "12h": 12 * 3600,
    "1d": 86400,
    "3d": 3 * 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "all": None,
}
WINDOW_MARKERS = {
    "5h": "●",
    "7d": "◆",
}
WINDOW_KEYS = tuple(WINDOW_MARKERS.keys())
BRAILLE_LEGEND_MARKER = "⣿"
BOX_LEGEND_MARKER = "━"
BAR_LEGEND_MARKER = "█"
WINDOW_PRIORITIES = {
    "5h": 2,
    "7d": 1,
}
TOKEN_RATE_HOURLY_THRESHOLD_SECONDS = 15 * 60
TOKEN_RATE_SMOOTHING_ALPHA = 0.35
RESET_CREDIT_TITLE_WIDTH = 6
RESET_CREDIT_MIN_BAR_WIDTH = 6
GAP_SECONDS = 3 * 60
RESET_CREDITS_INTERVAL_SECONDS = PROJECT_CONFIG.reset_credits_interval_seconds
HTTP_429_FALLBACK_SECONDS = PROJECT_CONFIG.http_429_fallback_seconds
SUMMARY_WEEK_START = PROJECT_CONFIG.week_start
HTTP_401_CREDENTIAL_HINT = t("credential.http_401_hint")
ANSI_RE = re.compile(r"\x1b\[[0-9;?<>]*[A-Za-z~]")
BRAILLE_DOT_BITS = {
    (0, 0): 0x01,
    (0, 1): 0x02,
    (0, 2): 0x04,
    (0, 3): 0x40,
    (1, 0): 0x08,
    (1, 1): 0x10,
    (1, 2): 0x20,
    (1, 3): 0x80,
}
BAR_QUADRANT_BITS = {
    (0, 0): 0x01,
    (1, 0): 0x02,
    (0, 1): 0x04,
    (1, 1): 0x08,
}
BAR_QUADRANT_CHARS = [
    " ",
    "▘",
    "▝",
    "▀",
    "▖",
    "▌",
    "▞",
    "▛",
    "▗",
    "▚",
    "▐",
    "▜",
    "▄",
    "▙",
    "▟",
    "█",
]
