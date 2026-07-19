"""Shared constants for the CodexTOP terminal application."""

from __future__ import annotations

import re

from ui import color_schemes
from .paths import default_paths
from .version import APP_VERSION, PACKAGE_VERSION

DEFAULT_PATHS = default_paths()
DEFAULT_LOG_DIR = DEFAULT_PATHS.log_dir
DEFAULT_LOG_FILE = "quota_snapshots.jsonl"
DEFAULT_CONTROL_FILE = "sampler_control.json"
DEFAULT_STATE_FILE = "codextop_state.json"
DEFAULT_SAMPLER_INTERVAL_SECONDS = 60
DEFAULT_PERIOD = "5h"
DEFAULT_CURVE_MODE = "connected"
DEFAULT_DISPLAY_SCOPE = "all"
DEFAULT_WINDOW_SCOPE = "both"
DEFAULT_USAGE_DIRECTORY_SCOPE = "current"
DEFAULT_USAGE_PANEL_LAYOUT = "combined"
DEFAULT_COLOR_SCHEME = color_schemes.default_color_scheme_key()
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
    ("连续", "connected"),
    ("间断", "points"),
    ("精细", "braille"),
    ("线条", "box"),
    ("柱状", "bar"),
    ("精细柱状", "fine_bar"),
]
DISPLAY_SCOPE_CHOICES = [
    ("看板模式", "all"),
    ("专注模式", "current"),
    ("合并模式", "merged"),
    ("用量模式", "usage"),
]
USAGE_DIRECTORY_SCOPE_CHOICES = [
    ("当前目录", "current"),
    ("全部目录", "all"),
]
USAGE_PANEL_LAYOUT_CHOICES = [
    ("合并", "combined"),
    ("拆分", "split"),
]
WINDOW_SCOPE_CHOICES = [
    ("同时", "both"),
    ("仅 5h", "5h"),
    ("仅 7d", "7d"),
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
