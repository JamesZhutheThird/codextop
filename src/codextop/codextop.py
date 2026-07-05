#!/usr/bin/env python3
"""Fullscreen Codex quota monitor with clickable terminal controls."""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import select
import shutil
import sys
import termios
import time
import tty
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

TOOLKIT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLKIT_DIR))
import check_codex_quota as quota
try:
    from .paths import default_paths, ensure_runtime_layout, iter_snapshot_log_paths, recent_month_keys
except ImportError:
    from paths import default_paths, ensure_runtime_layout, iter_snapshot_log_paths, recent_month_keys


DEFAULT_PATHS = default_paths()
DEFAULT_LOG_DIR = DEFAULT_PATHS.log_dir
DEFAULT_LOG_FILE = "quota_snapshots.jsonl"
DEFAULT_CONTROL_FILE = "sampler_control.json"
DEFAULT_STATE_FILE = "codextop_state.json"
DEFAULT_SAMPLER_INTERVAL_SECONDS = 60
APP_VERSION = "v1.1.1"
DEFAULT_PERIOD = "5h"
DEFAULT_CURVE_MODE = "connected"
DEFAULT_DISPLAY_SCOPE = "all"
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
]
DISPLAY_SCOPE_CHOICES = [
    ("看板模式", "all"),
    ("专注模式", "current"),
    ("合并模式", "merged"),
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
WINDOW_PRIORITIES = {
    "5h": 2,
    "7d": 1,
}
RESET_CREDIT_TITLE_WIDTH = 6
RESET_CREDIT_MIN_BAR_WIDTH = 6
GAP_SECONDS = 3 * 60
ANSI_RE = re.compile(r"\x1b\[[0-9;?<>]*[A-Za-z~]")


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
    last_update: float | None = None
    next_read: float = 0.0
    status: str = "启动中"
    error: str | None = None
    last_records_read: float = 0.0
    records: list[dict[str, Any]] | None = None
    control_path: Path | None = None
    summary_offset: int = 0


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1


def visible_width(text: str) -> int:
    plain = ANSI_RE.sub("", text)
    return sum(char_width(char) for char in plain)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def fit_ansi(text: str, width: int) -> str:
    if width <= 0:
        return ""
    output: list[str] = []
    used = 0
    pos = 0
    had_style = False
    for match in ANSI_RE.finditer(text):
        chunk = text[pos:match.start()]
        for char in chunk:
            char_w = char_width(char)
            if used + char_w > width:
                if had_style:
                    output.append("\x1b[0m")
                return "".join(output)
            output.append(char)
            used += char_w
        output.append(match.group(0))
        had_style = True
        pos = match.end()
    for char in text[pos:]:
        char_w = char_width(char)
        if used + char_w > width:
            if had_style:
                output.append("\x1b[0m")
            return "".join(output)
        output.append(char)
        used += char_w
    return "".join(output) + (" " * max(0, width - used))


def pad_ansi(text: str, width: int) -> str:
    fitted = fit_ansi(text, width)
    return fitted + (" " * max(0, width - visible_width(fitted)))


def compact_ansi(text: str, width: int) -> str:
    return fit_ansi(text, width).rstrip()


def right_ansi(text: str, width: int) -> str:
    fitted = compact_ansi(text, width)
    return (" " * max(0, width - visible_width(fitted))) + fitted


def center_ansi(text: str, width: int) -> str:
    fitted = compact_ansi(text, width)
    pad = max(0, width - visible_width(fitted))
    return (" " * (pad // 2)) + fitted + (" " * (pad - pad // 2))


def plain_fit(text: Any, width: int) -> str:
    return strip_ansi(fit_ansi(str(text or "-"), width)).rstrip()


def ansi_ellipsis(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if visible_width(text) <= width:
        return fit_ansi(text, width)
    suffix = "..."
    suffix_width = visible_width(suffix)
    if width <= suffix_width:
        return suffix[:width]
    return fit_ansi(fit_ansi(text, width - suffix_width).rstrip() + suffix, width)


def fg(color: str | None) -> str:
    if not color:
        return ""
    color = color.strip()
    if color.startswith("#") and len(color) == 7:
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        return f"\x1b[38;2;{r};{g};{b}m"
    named = {
        "dim": "\x1b[2m",
        "red": "\x1b[31m",
        "green": "\x1b[32m",
        "yellow": "\x1b[33m",
        "magenta": "\x1b[35m",
        "blue": "\x1b[34m",
        "cyan": "\x1b[36m",
        "white": "\x1b[37m",
        "bright_cyan": "\x1b[96m",
    }
    return named.get(color, "")


def paint(text: str, color: str | None = None, *, bold: bool = False, dim: bool = False, reverse: bool = False) -> str:
    codes = []
    if bold:
        codes.append("\x1b[1m")
    if dim:
        codes.append("\x1b[2m")
    if reverse:
        codes.append("\x1b[7m")
    codes.append(fg(color))
    prefix = "".join(code for code in codes if code)
    return f"{prefix}{text}\x1b[0m" if prefix else text


def paint_style(text: str, style: str | None) -> str:
    if not style:
        return text
    tokens = style.split()
    color = next((token for token in tokens if token != "bold"), None)
    return paint(text, color, bold="bold" in tokens)


def percent_color(value: Any) -> str:
    return quota.percent_gradient_style(value)


def percent_text(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "--%"
    return f"{value:g}%"


def countdown(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "-"
    return quota.countdown_text(seconds).strip()


def reset_credit_countdown(seconds: Any, day_width: int) -> str:
    day_width = max(1, day_width)
    if not isinstance(seconds, (int, float)):
        return f"{'-' * day_width}d --h"
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours = rem // 3600
    return f"{days:{day_width}d}d {hours:2d}h"


def reset_credit_day_width(seconds_values: list[Any]) -> int:
    max_days = 0
    found_number = False
    for seconds in seconds_values:
        if not isinstance(seconds, (int, float)):
            continue
        found_number = True
        max_days = max(max_days, max(0, int(seconds)) // 86400)
    if not found_number:
        return 1
    return max(1, len(str(max_days)))


def reset_credit_right_width(day_width: int) -> int:
    digits = "9" * max(1, day_width)
    return visible_width(f"于 {digits}d 23h 后过期")


def format_time(ts: int | float | None) -> str:
    if not isinstance(ts, (int, float)):
        return "-"
    dt = datetime.fromtimestamp(ts)
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%m-%d %H:%M")


def axis_time_label(ts: int, start_ts: int, end_ts: int) -> str:
    start_dt = datetime.fromtimestamp(start_ts)
    end_dt = datetime.fromtimestamp(end_ts)
    current_dt = datetime.fromtimestamp(ts)
    if start_dt.date() != end_dt.date() or end_ts - start_ts >= 86400:
        return current_dt.strftime("%m-%d %H:%M")
    return current_dt.strftime("%H:%M")


def time_axis_line(start_ts: int, end_ts: int, width: int, prefix_width: int = 4) -> str:
    if width <= 0:
        return ""
    for count in (6, 3):
        labels = [
            axis_time_label(
                int(start_ts + (end_ts - start_ts) * i / max(1, count - 1)),
                start_ts,
                end_ts,
            )
            for i in range(count)
        ]
        required = sum(len(label) for label in labels) + 2 * (len(labels) - 1)
        if count == 3 or width >= required:
            break

    canvas = [" "] * width
    last_end = -1
    for index, label in enumerate(labels):
        if len(label) > width:
            label = label[:width]
        position = round(index * (width - 1) / max(1, len(labels) - 1))
        start = max(0, min(width - len(label), position - len(label) // 2))
        if start <= last_end:
            start = min(width - len(label), last_end + 1)
        for offset, char in enumerate(label[: max(0, width - start)]):
            canvas[start + offset] = char
        last_end = start + len(label) - 1
    return paint((" " * prefix_width) + "".join(canvas), "dim")


def compact_reset_at(value: Any, reset_epoch: int | None = None) -> str:
    if reset_epoch is not None:
        return format_time(reset_epoch)
    if not isinstance(value, str) or not value:
        return "-"
    if len(value) >= 16 and value[:5] == datetime.now().strftime("%m-%d"):
        return value[6:11]
    return value[-11:] if len(value) > 11 else value


def progress_bar(value: Any, width: int, max_value: float = 100.0) -> str:
    width = max(4, width)
    if not isinstance(value, (int, float)) or max_value <= 0:
        return paint("░" * width, "dim")
    raw_value = max(0.0, float(value))
    ratio = max(0.0, min(100.0, raw_value / max_value * 100))
    filled = max(0, min(width, round(width * ratio / 100)))
    if raw_value <= 0:
        filled = 1
    color = "#b95f5f" if raw_value <= 0 else percent_color(ratio)
    return paint("█" * filled, color) + paint("░" * (width - filled), "dim")


def section_rule(title: str, width: int) -> str:
    label = f" {title} "
    pad = max(0, width - visible_width(label))
    left = pad // 2
    right = pad - left
    return paint("─" * left + label + "─" * right, "dim")


def account_index(account: dict[str, Any]) -> int | str | None:
    index = account.get("index", account.get("i"))
    return index if isinstance(index, (int, str)) else None


def is_current(account: dict[str, Any]) -> bool:
    return bool(account.get("current", account.get("cur")))


def account_error(account: dict[str, Any]) -> str | None:
    error = account.get("error", account.get("err"))
    return str(error) if error else None


def account_email(account: dict[str, Any]) -> str:
    return str(account.get("email") or "-")


def account_plan(account: dict[str, Any]) -> str:
    return str(account.get("plan_type") or account.get("plan") or "-")


def window_info(account: dict[str, Any], key: str) -> dict[str, Any]:
    if "quota" in account:
        window = account.get("quota", {}).get(key, {})
        reset_after = window.get("reset_after_seconds")
        reset_epoch = int(time.time() + reset_after) if isinstance(reset_after, (int, float)) else None
        return {
            "left": window.get("remaining_percent"),
            "reset_after": reset_after,
            "reset_at": compact_reset_at(window.get("reset_at")),
            "reset_epoch": reset_epoch,
        }
    raw = account.get("q", {}).get(key)
    if isinstance(raw, list) and len(raw) >= 4:
        left, reset_epoch, reset_after, _limit = raw[:4]
        return {
            "left": left,
            "reset_after": reset_after,
            "reset_at": compact_reset_at(None, reset_epoch if isinstance(reset_epoch, int) else None),
            "reset_epoch": reset_epoch if isinstance(reset_epoch, int) else None,
        }
    return {"left": None, "reset_after": None, "reset_at": "-", "reset_epoch": None}


def reset_rows(account: dict[str, Any], width: int) -> list[str]:
    rows: list[str] = []
    if "reset_credits" in account:
        reset = account.get("reset_credits", {})
        available = reset.get("available_count")
        credits = [
            credit for credit in reset.get("credits", [])
            if credit.get("status") == "available"
        ]
        rows.append(center_ansi(f"剩余 {available if isinstance(available, int) else '-'} 次可用重置次数", width))
        if not credits:
            rows.append(center_ansi(paint("无可用重置次数", "dim"), width))
            return rows
        reset_items: list[tuple[str, Any, Any]] = []
        for credit in credits[:4]:
            title = quota.compact_reset_title(credit.get("title"))
            seconds = credit.get("expires_after_seconds")
            remaining = credit.get("expires_remaining_percent")
            reset_items.append((title, remaining, seconds))
        day_width = reset_credit_day_width([item[2] for item in reset_items])
        for title, remaining, seconds in reset_items:
            rows.append(reset_credit_row(title, remaining, seconds, width, day_width))
        return rows

    available = account.get("rc")
    credits = account.get("r", [])
    rows.append(center_ansi(f"剩余 {available if isinstance(available, int) else '-'} 次可用重置次数", width))
    if not credits:
        rows.append(center_ansi(paint("无可用重置次数", "dim"), width))
        return rows
    now = int(time.time())
    reset_items: list[tuple[str, Any, Any]] = []
    for credit in credits[:4]:
        if not isinstance(credit, list) or len(credit) < 2:
            continue
        title, expires_epoch = credit[:2]
        remaining_percent = credit[2] if len(credit) > 2 else None
        remaining = expires_epoch - now if isinstance(expires_epoch, int) else None
        reset_items.append((str(title), remaining_percent, remaining))
    day_width = reset_credit_day_width([item[2] for item in reset_items])
    for title, remaining_percent, remaining in reset_items:
        rows.append(reset_credit_row(title, remaining_percent, remaining, width, day_width))
    return rows


def reset_credit_row(title: str, remaining_percent: Any, seconds: Any, width: int, day_width: int = 1) -> str:
    left_width = RESET_CREDIT_TITLE_WIDTH
    right_text = f"于 {reset_credit_countdown(seconds, day_width)} 后过期"
    right_width = min(
        reset_credit_right_width(day_width),
        max(0, width - left_width - RESET_CREDIT_MIN_BAR_WIDTH - 2),
    )
    bar_width = max(RESET_CREDIT_MIN_BAR_WIDTH, width - left_width - right_width - 2)
    style = quota.reset_after_style(seconds)
    left = f"{plain_fit(title, left_width):<{left_width}} {progress_bar(remaining_percent, bar_width)}"
    right = fit_ansi(paint_style(right_text, style), right_width) if right_width else ""
    row = f"{left} {right}" if right else left
    return fit_ansi(row, width)


def quota_rows(account: dict[str, Any], width: int, *, compact: bool = False) -> list[str]:
    rows: list[str] = []
    for key in ("5h", "7d"):
        info = window_info(account, key)
        left = info["left"]
        label_text = f"{key}({WINDOW_MARKERS[key]})"
        label = paint(label_text, bold=True)
        pct = paint(percent_text(left).rjust(4), percent_color(left))
        bar_width = max(8, width - visible_width(label_text) - 1 - 1 - 4)
        rows.append(f"{label} {progress_bar(left, bar_width)} {pct}")
        reset_line = f"于 {countdown(info['reset_after'])} 后在 {info['reset_at']} 重置"
        rows.append(right_ansi(paint_style(reset_line, quota.reset_after_style(info["reset_after"])), width))
        if key == "5h" and not compact:
            rows.append("")
    return rows


def percent_sum_text(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "--%"
    if float(value).is_integer():
        return f"{int(value)}%"
    return f"{value:g}%"


def merged_ratio_percent(value: Any, max_value: Any) -> float | None:
    if not isinstance(value, (int, float)) or not isinstance(max_value, (int, float)) or max_value <= 0:
        return None
    return max(0.0, min(100.0, float(value) / float(max_value) * 100))


def valid_quota_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        account for account in accounts
        if isinstance(account, dict) and not account_error(account)
    ]


def merged_plan_text(accounts: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for account in valid_quota_accounts(accounts):
        plan = account_plan(account)
        if not plan or plan == "-":
            continue
        counts[plan] = counts.get(plan, 0) + 1
    if not counts:
        return "-"
    return " / ".join(f"{plan} x{count}" if count > 1 else plan for plan, count in counts.items())


def merged_available_resets(accounts: list[dict[str, Any]]) -> int | None:
    total = 0
    found = False
    for account in valid_quota_accounts(accounts):
        if "reset_credits" in account:
            available = account.get("reset_credits", {}).get("available_count")
        else:
            available = account.get("rc")
        if isinstance(available, int):
            total += available
            found = True
    return total if found else None


def current_account_id(accounts: list[dict[str, Any]], current: int | str | None) -> str:
    if current is not None:
        return str(current)
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if is_current(account):
            index = account_index(account)
            return str(index) if index is not None else "-"
    return "-"


def current_account_obj(accounts: list[dict[str, Any]], current: int | str | None) -> dict[str, Any] | None:
    if current is not None:
        for account in accounts:
            if not isinstance(account, dict):
                continue
            if account_index(account) == current:
                return account
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if is_current(account):
            return account
    return None


def current_account_quota_summary(accounts: list[dict[str, Any]], current: int | str | None) -> str:
    account = current_account_obj(accounts, current)
    account_id = current_account_id(accounts, current)
    if not account or account_error(account):
        return account_id
    left_5h = window_info(account, "5h").get("left")
    left_7d = window_info(account, "7d").get("left")
    return (
        f"{account_id} "
        f"(5h {paint(percent_text(left_5h), percent_color(left_5h))} "
        f"7d {paint(percent_text(left_7d), percent_color(left_7d))})"
    )


def credit_expire_epoch(credit: Any, observed_ts: int) -> int | None:
    if isinstance(credit, list) and len(credit) >= 2 and isinstance(credit[1], int):
        return credit[1]
    if not isinstance(credit, dict):
        return None
    seconds = credit.get("expires_after_seconds")
    if isinstance(seconds, (int, float)):
        return observed_ts + int(seconds)
    expires_dt = quota.parse_datetime_utc(credit.get("expires_at"))
    return int(expires_dt.timestamp()) if expires_dt is not None else None


def merged_reset_expiration_rows(accounts: list[dict[str, Any]], limit: int = 3) -> list[tuple[int, str, str]]:
    now = int(time.time())
    rows: list[tuple[int, str, str]] = []
    for account in valid_quota_accounts(accounts):
        index = account_index(account)
        account_id = str(index) if index is not None else "-"
        if "reset_credits" in account:
            credits = [
                credit for credit in account.get("reset_credits", {}).get("credits", [])
                if isinstance(credit, dict) and credit.get("status") == "available"
            ]
        else:
            credits = account.get("r", [])
        if not isinstance(credits, list):
            continue
        for credit in credits:
            expire_epoch = credit_expire_epoch(credit, now)
            if expire_epoch is None:
                continue
            title = quota.compact_reset_title(credit.get("title")) if isinstance(credit, dict) else str(credit[0] if credit else "-")
            rows.append((expire_epoch, account_id, title))
    return sorted(rows, key=lambda item: item[0])[:limit]


def merged_window_info(accounts: list[dict[str, Any]], key: str) -> dict[str, Any]:
    total = 0.0
    contributors = 0
    reset_candidates: list[int] = []
    for account in valid_quota_accounts(accounts):
        info = window_info(account, key)
        left = info.get("left")
        if not isinstance(left, (int, float)):
            continue
        total += float(left)
        contributors += 1
        reset_epoch = info.get("reset_epoch")
        if isinstance(reset_epoch, int):
            reset_candidates.append(reset_epoch)

    if contributors == 0:
        return {
            "left": None,
            "max_left": None,
            "contributors": 0,
            "reset_after": None,
            "reset_at": "-",
            "reset_epoch": None,
        }

    nearest_reset = min(reset_candidates) if reset_candidates else None
    reset_after = max(0, nearest_reset - int(time.time())) if nearest_reset is not None else None
    left: int | float = int(total) if total.is_integer() else round(total, 2)
    return {
        "left": left,
        "max_left": contributors * 100,
        "contributors": contributors,
        "reset_after": reset_after,
        "reset_at": compact_reset_at(None, nearest_reset),
        "reset_epoch": nearest_reset,
    }


def account_quota_detail_line(accounts: list[dict[str, Any]], current: int | str | None, key: str, width: int) -> str:
    sortable: list[tuple[float, str]] = []
    for account in valid_quota_accounts(accounts):
        index = account_index(account)
        account_id = str(index) if index is not None else "-"
        left = window_info(account, key).get("left")
        if not isinstance(left, (int, float)):
            continue
        text = f"{account_id}({percent_text(left)})"
        sortable.append((float(left), paint(text, percent_color(left))))
    entries = [text for _left, text in sorted(sortable, key=lambda item: item[0], reverse=True)]
    return ansi_ellipsis(", ".join(entries), width)


def merged_quota_detail_reset_line(
    accounts: list[dict[str, Any]],
    current: int | str | None,
    key: str,
    info: dict[str, Any],
    width: int,
) -> str:
    reset_text = f"于 {countdown(info['reset_after'])} 后在 {info['reset_at']} 重置"
    reset = paint_style(reset_text, quota.reset_after_style(info["reset_after"]))
    reset_width = min(visible_width(reset), width)
    detail_width = max(0, width - reset_width - 1)
    if detail_width <= 0:
        return right_ansi(reset, width)
    detail = account_quota_detail_line(accounts, current, key, detail_width)
    return fit_ansi(detail, detail_width) + " " + right_ansi(reset, reset_width)


def merged_quota_rows(accounts: list[dict[str, Any]], current: int | str | None, width: int, *, compact: bool = False) -> list[str]:
    rows: list[str] = []
    for key in ("5h", "7d"):
        info = merged_window_info(accounts, key)
        left = info["left"]
        max_left = info["max_left"] if isinstance(info["max_left"], (int, float)) else 100
        label_text = f"{key}({WINDOW_MARKERS[key]})"
        label = paint(label_text, bold=True)
        pct_raw = percent_sum_text(left)
        pct_width = max(4, visible_width(pct_raw))
        ratio = merged_ratio_percent(left, max_left)
        pct = paint(pct_raw.rjust(pct_width), percent_color(ratio))
        bar_width = max(8, width - visible_width(label_text) - 1 - 1 - pct_width)
        rows.append(f"{label} {progress_bar(left, bar_width, float(max_left))} {pct}")
        rows.append(merged_quota_detail_reset_line(accounts, current, key, info, width))
        if key == "5h" and not compact:
            rows.append("")
    return rows


def read_snapshots(log_path: Path, period: str, tz_name: str) -> list[dict[str, Any]]:
    records_by_time: dict[int, dict[str, Any]] = {}
    months = None if period == "all" else recent_month_keys(tz_name, 2)
    for path in iter_snapshot_log_paths(log_path, months):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                timestamp = record.get("t")
                if isinstance(timestamp, int) and isinstance(record.get("a"), list):
                    records_by_time[timestamp] = record
    return [records_by_time[timestamp] for timestamp in sorted(records_by_time)]


def account_at(record: dict[str, Any], index: int | str) -> dict[str, Any] | None:
    for account in record.get("a", []):
        if account.get("i") == index:
            return account
    return None


def window_points(records: list[dict[str, Any]], index: int | str, window: str) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for record in records:
        account = account_at(record, index)
        if not account or account.get("err"):
            continue
        raw = account.get("q", {}).get(window)
        if not isinstance(raw, list) or len(raw) < 4:
            continue
        left, reset_ts, reset_after, limit_window = raw[:4]
        if not isinstance(left, (int, float)):
            continue
        points.append(
            {
                "t": record["t"],
                "left": float(left),
                "reset": reset_ts if isinstance(reset_ts, int) else None,
                "reset_after": reset_after,
                "limit": limit_window,
            }
        )
    return points


def reset_in_gap(prev: dict[str, Any], next_point: dict[str, Any] | None, start_ts: int, end_ts: int) -> int | None:
    candidates = []
    for point in (prev, next_point):
        if not point:
            continue
        reset_ts = point.get("reset")
        if isinstance(reset_ts, int) and start_ts < reset_ts < end_ts:
            candidates.append(reset_ts)
    if not candidates:
        return None
    midpoint = (start_ts + end_ts) / 2
    return min(candidates, key=lambda item: abs(item - midpoint))


def value_at(points: list[dict[str, Any]], ts: int, reset_value: float = 100.0) -> tuple[float, bool]:
    times = [point["t"] for point in points]
    pos = bisect.bisect_right(times, ts) - 1
    if pos < 0:
        return points[0]["left"], True

    prev = points[pos]
    next_point = points[pos + 1] if pos + 1 < len(points) else None
    next_ts = next_point["t"] if next_point else ts
    gap_end = next_ts if next_point else ts
    predicted = False

    if next_point and next_point["t"] - prev["t"] > GAP_SECONDS:
        predicted = True
        reset_ts = reset_in_gap(prev, next_point, prev["t"], next_point["t"])
        if reset_ts and ts >= reset_ts:
            return reset_value, True
        return prev["left"], True

    if not next_point and ts - prev["t"] > GAP_SECONDS:
        predicted = True
        reset_ts = reset_in_gap(prev, None, prev["t"], gap_end)
        if reset_ts and ts >= reset_ts:
            return reset_value, True
        return prev["left"], True

    return prev["left"], predicted


def read_records_if_due(state: MonitorState, force: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    if force or state.records is None or now >= state.next_read:
        try:
            state.records = read_snapshots(state.log_path, state.period, state.tz)
            state.last_records_read = now
            state.next_read = now + state.interval
            if state.records:
                state.last_update = float(state.records[-1]["t"])
                age = int(now - state.last_update)
                state.status = f"数据更新于 {age}s 前"
                state.error = None
            else:
                state.status = "等待数据"
                state.error = None
        except Exception as exc:
            state.records = state.records or []
            state.next_read = now + min(10, state.interval)
            state.status = "读取失败"
            state.error = str(exc)
    return list(state.records or [])


def current_accounts(state: MonitorState, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        return []
    latest = records[-1]
    accounts = latest.get("a", [])
    return list(accounts) if isinstance(accounts, list) else []


def current_index(state: MonitorState, records: list[dict[str, Any]]) -> int | str | None:
    if records:
        value = records[-1].get("current")
        if isinstance(value, (int, str)):
            return value
    return None


def records_for_period(records: list[dict[str, Any]], period: str) -> tuple[list[dict[str, Any]], int, int]:
    end_ts = int(time.time())
    if period == "all":
        start_ts = records[0]["t"]
    else:
        start_ts = end_ts - int(PERIOD_SECONDS[period] or 0)
    relevant = [record for record in records if record["t"] >= start_ts]
    context = [record for record in records if record["t"] < start_ts]
    if context:
        relevant.insert(0, context[-1])
    if not relevant:
        relevant = records[-1:]
    return relevant, start_ts, end_ts


def axis_value_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def series_chart_lines(
    points: dict[str, Any],
    start_ts: int,
    end_ts: int,
    width: int,
    height: int,
    curve_mode: str,
    max_value: float,
    value_getter: Any = value_at,
) -> list[str]:
    if width <= 10 or height <= 3:
        return [paint("暂无历史数据", "dim")]
    if not any(points.values()) or max_value <= 0:
        return [paint("暂无历史数据", "dim")]

    tick_values = [max_value * ratio for ratio in (1.0, 0.75, 0.5, 0.25, 0.0)]
    axis_width = max(4, max(visible_width(axis_value_text(value)) for value in tick_values) + 1)
    box_width = max(6, width - axis_width)
    plot_width = max(2, box_width - 4)
    chart_width = plot_width + 2
    chart_height = max(2, height - 3)

    grid: list[list[tuple[str, str, int]]] = [
        [(" ", "dim", 0) for _ in range(plot_width)]
        for _ in range(chart_height)
    ]

    def put_cell(row: int, column: int, char: str, color: str, priority: int) -> None:
        if not (0 <= row < chart_height and 0 <= column < plot_width):
            return
        old_char, _old_color, old_priority = grid[row][column]
        if old_char != " " and old_char != char:
            if priority >= old_priority:
                grid[row][column] = (char, color, priority)
        elif priority >= old_priority:
            grid[row][column] = (char, color, priority)

    for label, series in points.items():
        if not series:
            continue
        char = WINDOW_MARKERS[label]
        priority = WINDOW_PRIORITIES[label]
        previous_row: int | None = None
        previous_value: float | None = None
        for column in range(plot_width):
            ratio = column / max(1, plot_width - 1)
            ts = int(start_ts + (end_ts - start_ts) * ratio)
            value, _predicted = value_getter(series, ts, max_value)
            value = max(0, min(max_value, value))
            normalized = max(0.0, min(100.0, value / max_value * 100))
            row = round((max_value - value) / max_value * (chart_height - 1))
            if curve_mode == "connected" and previous_row is not None and previous_value is not None:
                row_span = abs(row - previous_row)
                if row_span == 0:
                    put_cell(row, column, char, percent_color(normalized), priority)
                else:
                    step = 1 if row > previous_row else -1
                    for filled_row in range(previous_row + step, row + step, step):
                        row_ratio = abs(filled_row - previous_row) / row_span
                        interpolated = previous_value + (value - previous_value) * row_ratio
                        interpolated_normalized = max(0.0, min(100.0, interpolated / max_value * 100))
                        put_cell(filled_row, column, char, percent_color(interpolated_normalized), priority)
            else:
                put_cell(row, column, char, percent_color(normalized), priority)
            previous_row = row
            previous_value = float(value)

    tick_rows = {
        round((max_value - value) / max_value * (chart_height - 1)): value
        for value in tick_values
    }
    lines: list[str] = []
    lines.append((" " * axis_width) + paint("┌" + "─" * chart_width + "┐", "dim"))
    for row_index, row in enumerate(grid):
        axis = tick_rows.get(row_index)
        prefix = paint(f"{axis_value_text(axis):>{axis_width - 1}} ", "dim") if axis is not None else " " * axis_width
        plot = "".join(paint(char, color) if char != " " else " " for char, color, _priority in row)
        line = prefix + paint("│", "dim") + " " + plot + " " + paint("│", "dim")
        lines.append(fit_ansi(line, width))
    lines.append((" " * axis_width) + paint("└" + "─" * chart_width + "┘", "dim"))
    lines.append(time_axis_line(start_ts, end_ts, plot_width, axis_width + 2))
    while len(lines) < height:
        lines.append("")
    return [fit_ansi(line, width) for line in lines[:height]]


def chart_lines(
    records: list[dict[str, Any]],
    index: int | str,
    period: str,
    width: int,
    height: int,
    curve_mode: str,
) -> list[str]:
    if not records:
        return [paint("暂无历史数据", "dim")]
    relevant, start_ts, end_ts = records_for_period(records, period)
    points = {
        "5h": window_points(relevant, index, "5h"),
        "7d": window_points(relevant, index, "7d"),
    }
    return series_chart_lines(points, start_ts, end_ts, width, height, curve_mode, 100.0)


def chart_box(lines: list[str], width: int, height: int) -> list[str]:
    if width < 8 or height < 4:
        return [fit_ansi(line, width) for line in lines[:height]]
    inner = width - 2
    color = "dim"
    boxed = [paint("┌" + "─" * inner + "┐", color)]
    for line in lines[: height - 2]:
        boxed.append(paint("│", color) + fit_ansi(line, inner) + paint("│", color))
    while len(boxed) < height - 1:
        boxed.append(paint("│", color) + (" " * inner) + paint("│", color))
    boxed.append(paint("└" + "─" * inner + "┘", color))
    return [fit_ansi(line, width) for line in boxed[:height]]


def account_lines(
    account: dict[str, Any],
    records: list[dict[str, Any]],
    panel_width: int,
    panel_height: int,
    period: str,
    current: int | str | None,
    curve_mode: str,
) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    index = account_index(account)
    lines: list[str] = []

    error = account_error(account)
    def add_blank() -> None:
        lines.append("")

    def add_text(line: str) -> None:
        lines.append(" " + fit_ansi(line, max(1, inner_width - 2)).rstrip())

    def add_section(title: str) -> None:
        add_blank()
        lines.append(section_rule(title, inner_width))
        add_blank()

    add_blank()
    add_text(f"{paint('邮箱', 'dim')}    {plain_fit(account_email(account), inner_width - 8)}")
    add_text(f"{paint('类型', 'dim')}    {plain_fit(account_plan(account), inner_width - 8)}")
    if error:
        add_section("错误")
        add_text(paint(plain_fit(error, inner_width - 2), "red"))
        return [fit_ansi(line, inner_width) for line in lines[:inner_height]]

    add_section("重置次数")
    for line in reset_rows(account, inner_width - 2):
        add_text(line)
    add_section("当前额度")
    for line in quota_rows(account, inner_width - 2):
        if line:
            add_text(line)
        else:
            add_blank()
    add_section("额度历史")

    chart_height = max(4, inner_height - len(lines))
    chart_width = max(8, inner_width - 2)
    if index is not None:
        chart = chart_lines(records, index, period, chart_width, chart_height, curve_mode)
    else:
        chart = chart_box([paint("暂无历史数据", "dim")], chart_width, chart_height)
    lines.extend(" " + fit_ansi(line, chart_width) for line in chart)

    if len(lines) < inner_height:
        lines.extend([""] * (inner_height - len(lines)))
    return [fit_ansi(line, inner_width) for line in lines[:inner_height]]


def account_summary_body(account: dict[str, Any], inner_width: int) -> list[str]:
    lines: list[str] = []

    def add_text(line: str) -> None:
        lines.append(" " + fit_ansi(line, max(1, inner_width - 2)).rstrip())

    add_text(f"{paint('邮箱', 'dim')}    {plain_fit(account_email(account), inner_width - 8)}")
    add_text(f"{paint('类型', 'dim')}    {plain_fit(account_plan(account), inner_width - 8)}")

    error = account_error(account)
    if error:
        lines.append(section_rule("错误", inner_width))
        add_text(paint(plain_fit(error, inner_width - 2), "red"))
    else:
        lines.append(section_rule("重置次数", inner_width))
        for line in reset_rows(account, inner_width - 2):
            add_text(line)
        lines.append(section_rule("当前额度", inner_width))
        for line in quota_rows(account, inner_width - 2, compact=True):
            add_text(line)

    return [fit_ansi(line, inner_width) for line in lines]


def account_summary_lines(account: dict[str, Any], panel_width: int, panel_height: int) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    lines = account_summary_body(account, inner_width)

    if len(lines) < inner_height:
        lines.extend([""] * (inner_height - len(lines)))
    return [fit_ansi(line, inner_width) for line in lines[:inner_height]]


def account_history_lines(
    account: dict[str, Any],
    records: list[dict[str, Any]],
    panel_width: int,
    panel_height: int,
    period: str,
    curve_mode: str,
) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    chart_width = max(8, inner_width - 2)
    index = account_index(account)
    if index is None:
        chart = chart_box([paint("暂无历史数据", "dim")], chart_width, inner_height)
    else:
        chart = chart_lines(records, index, period, chart_width, inner_height, curve_mode)
    lines = [" " + fit_ansi(line, chart_width) for line in chart]
    if len(lines) < inner_height:
        lines.extend([""] * (inner_height - len(lines)))
    return [fit_ansi(line, inner_width) for line in lines[:inner_height]]


def merged_account_reset_body(accounts: list[dict[str, Any]], current: int | str | None, inner_width: int) -> list[str]:
    lines: list[str] = []

    def add_text(line: str) -> None:
        lines.append(" " + fit_ansi(line, max(1, inner_width - 2)).rstrip())

    add_text(f"{paint('账号类型', 'dim')} {plain_fit(merged_plan_text(accounts), inner_width - 10)}")
    add_text(f"{paint('当前账号', 'dim')} {current_account_quota_summary(accounts, current)}")
    lines.append(section_rule("额度重置", inner_width))
    available = merged_available_resets(accounts)
    add_text(f"{paint('重置次数', 'dim')} 合计 {available if isinstance(available, int) else '-'} 次可用")
    expirations = merged_reset_expiration_rows(accounts, 3)
    day_width = reset_credit_day_width([expire_epoch - int(time.time()) for expire_epoch, _account_id, _title in expirations])
    for rank in range(1, 4):
        if rank <= len(expirations):
            expire_epoch, account_id, _title = expirations[rank - 1]
            remaining = max(0, expire_epoch - int(time.time()))
            countdown_text = reset_credit_countdown(remaining, day_width)
            row = f"{rank}. 来自 {account_id} 于 {countdown_text} 后过期"
            if visible_width(row) > inner_width:
                row = f"{rank}. {account_id} 于 {countdown_text} 后过期"
            lines.append(center_ansi(row, inner_width))
        else:
            lines.append(center_ansi(f"{rank}. -", inner_width))

    return [fit_ansi(line, inner_width) for line in lines]


def merged_account_reset_lines(accounts: list[dict[str, Any]], current: int | str | None, panel_width: int, panel_height: int) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    lines = merged_account_reset_body(accounts, current, inner_width)
    if len(lines) < inner_height:
        lines.extend([""] * (inner_height - len(lines)))
    return [fit_ansi(line, inner_width) for line in lines[:inner_height]]


def merged_quota_summary_lines(accounts: list[dict[str, Any]], current: int | str | None, panel_width: int, panel_height: int) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    lines: list[str] = []

    def add_text(line: str) -> None:
        lines.append(" " + fit_ansi(line, max(1, inner_width - 2)).rstrip())

    quota_lines = merged_quota_rows(accounts, current, inner_width - 2, compact=True)
    lines.append("")
    for line in quota_lines[:2]:
        add_text(line)
    lines.append("")
    for line in quota_lines[2:4]:
        add_text(line)
    lines.append("")
    if len(lines) < inner_height:
        lines.extend([""] * (inner_height - len(lines)))
    return [fit_ansi(line, inner_width) for line in lines[:inner_height]]


def merged_account_window_points(records: list[dict[str, Any]], window: str) -> dict[str, list[dict[str, Any]]]:
    account_points: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        timestamp = record.get("t")
        accounts = record.get("a", [])
        if not isinstance(timestamp, int) or not isinstance(accounts, list):
            continue
        for account in accounts:
            if not isinstance(account, dict) or account_error(account):
                continue
            index = account_index(account)
            if index is None:
                continue
            info = window_info(account, window)
            left = info.get("left")
            if not isinstance(left, (int, float)):
                continue
            reset_epoch = info.get("reset_epoch")
            account_points.setdefault(str(index), []).append(
                {
                    "t": timestamp,
                    "left": float(left),
                    "reset": reset_epoch if isinstance(reset_epoch, int) else None,
                }
            )
    return account_points


def merged_value_at(account_points: dict[str, list[dict[str, Any]]], ts: int, _max_value: float) -> tuple[float, bool]:
    total = 0.0
    found = False
    predicted = False
    for points in account_points.values():
        if not points or ts < points[0]["t"]:
            continue
        value, account_predicted = value_at(points, ts, 100.0)
        total += value
        found = True
        predicted = predicted or account_predicted
    return total, predicted or not found


def merged_max_value(points: dict[str, dict[str, list[dict[str, Any]]]]) -> float:
    account_count = max((len(series) for series in points.values()), default=1)
    return max(100.0, float(account_count * 100))


def merged_chart_lines(
    records: list[dict[str, Any]],
    period: str,
    width: int,
    height: int,
    curve_mode: str,
) -> list[str]:
    if not records:
        return [paint("暂无历史数据", "dim")]
    relevant, start_ts, end_ts = records_for_period(records, period)
    points = {
        "5h": merged_account_window_points(relevant, "5h"),
        "7d": merged_account_window_points(relevant, "7d"),
    }
    max_value = merged_max_value(points)
    return series_chart_lines(points, start_ts, end_ts, width, height, curve_mode, max_value, merged_value_at)


def merged_history_lines(
    records: list[dict[str, Any]],
    panel_width: int,
    panel_height: int,
    period: str,
    curve_mode: str,
) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    chart_width = max(8, inner_width - 2)
    chart = merged_chart_lines(records, period, chart_width, inner_height, curve_mode)
    lines = [" " + fit_ansi(line, chart_width) for line in chart]
    if len(lines) < inner_height:
        lines.extend([""] * (inner_height - len(lines)))
    return [fit_ansi(line, inner_width) for line in lines[:inner_height]]


def border_color(account: dict[str, Any], current: int | str | None) -> str:
    if account_error(account):
        return "dim"
    left = window_info(account, "5h").get("left")
    return percent_color(left) if isinstance(left, (int, float)) else "dim"


def merged_border_color(accounts: list[dict[str, Any]]) -> str:
    info = merged_window_info(accounts, "5h")
    ratio = merged_ratio_percent(info.get("left"), info.get("max_left"))
    return percent_color(ratio) if ratio is not None else "dim"


def panel(title: str, body: list[str], width: int, height: int, color: str) -> list[str]:
    width = max(12, width)
    height = max(4, height)
    inner = width - 2
    raw_title = f" {title} "
    title_width = visible_width(raw_title)
    side = max(0, inner - title_width)
    left = side // 2
    right = side - left
    top = "╭" + ("─" * left) + raw_title + ("─" * right) + "╮"
    bottom = "╰" + ("─" * inner) + "╯"
    rows = [paint(top, color)]
    for line in body[: height - 2]:
        rows.append(paint("│", color) + fit_ansi(line, inner) + paint("│", color))
    while len(rows) < height - 1:
        rows.append(paint("│", color) + (" " * inner) + paint("│", color))
    rows.append(paint(bottom, color))
    return [fit_ansi(row, width) for row in rows[:height]]


def compose_columns(blocks: list[list[str]], widths: list[int], height: int) -> list[str]:
    rows: list[str] = []
    for row_index in range(height):
        pieces = []
        for block, width in zip(blocks, widths):
            line = block[row_index] if row_index < len(block) else ""
            pieces.append(fit_ansi(line, width))
        rows.append("".join(pieces))
    return rows


def provider_name(account: dict[str, Any]) -> str:
    label = account.get("label")
    if isinstance(label, str) and label:
        return label
    index = account_index(account)
    return str(index) if index is not None else "-"


def selected_account(accounts: list[dict[str, Any]], current: int | str | None) -> dict[str, Any]:
    for account in accounts:
        index = account_index(account)
        if current is not None and index == current:
            return account
    for account in accounts:
        if is_current(account):
            return account
    return accounts[0]


def stacked_summary_panels(
    accounts: list[dict[str, Any]],
    current: int | str | None,
    width: int,
    height: int,
    offset: int,
    zones: list[ClickZone],
    x_origin: int,
) -> list[str]:
    if not accounts:
        return [" " * width for _ in range(height)]
    rows: list[str] = []
    inner_width = max(10, width - 2)

    panel_heights = [
        max(4, len(account_summary_body(account, inner_width)) + 2)
        for account in accounts
    ]
    overflow = sum(panel_heights) > height
    body_height = max(0, height - 2) if overflow else height

    if overflow:
        up_text = "▲ 上一个账号"
        down_text = "▼ 下一个账号"
        rows.append(fit_ansi(paint(center_ansi(up_text, width), "cyan", bold=True, reverse=True), width))
        zones.append(ClickZone(x_origin, x_origin + width - 1, 1, "summary_scroll", -1))
    remaining = body_height
    start = offset % len(accounts)
    ordered = accounts[start:] + accounts[:start]
    for account in ordered:
        panel_height = max(4, len(account_summary_body(account, inner_width)) + 2)
        title = provider_name(account)
        if current is not None and account_index(account) == current:
            title = f"【{title}】"
        body = account_summary_lines(account, width, panel_height)
        rendered = panel(title, body, width, panel_height, border_color(account, current))
        rows.extend(rendered[:remaining])
        remaining -= min(panel_height, remaining)
        if remaining <= 0:
            break
    if overflow:
        while len(rows) < height - 1:
            rows.append(" " * width)
        rows.append(fit_ansi(paint(center_ansi(down_text, width), "cyan", bold=True, reverse=True), width))
        zones.append(ClickZone(x_origin, x_origin + width - 1, height, "summary_scroll", 1))
    if len(rows) < height:
        rows.extend([" " * width] * (height - len(rows)))
    return [fit_ansi(line, width) for line in rows[:height]]


def interval_label(seconds: int) -> str:
    for label, value in INTERVAL_CHOICES:
        if value == seconds:
            return label
    return f"{seconds}s"


def render_sidebar(state: MonitorState, width: int, height: int, x_origin: int, zones: list[ClickZone]) -> list[str]:
    inner = max(8, width - 2)
    lines: list[str] = [paint("╭" + "─" * inner + "╮", "cyan")]

    def center_text(text: str) -> str:
        text_width = visible_width(text)
        pad = max(0, inner - text_width)
        left = pad // 2
        right = pad - left
        return " " * left + text + " " * right

    def add_plain(text: str = "", color: str | None = None, *, bold: bool = False) -> None:
        rendered = paint(center_text(text), color, bold=bold)
        lines.append(paint("│", "cyan") + fit_ansi(rendered, inner) + paint("│", "cyan"))

    def add_option(label: str, selected: bool, kind: str, value: Any) -> None:
        y = len(lines) + 1
        prefix = "● " if selected else "  "
        text = f"{prefix}{label}"
        centered = center_text(text)
        rendered = paint(centered, "green", bold=True, reverse=selected)
        lines.append(paint("│", "cyan") + fit_ansi(rendered, inner) + paint("│", "cyan"))
        text_start = max(1, (inner - visible_width(text)) // 2 + 1)
        zones.append(ClickZone(x_origin + text_start, x_origin + text_start + visible_width(text) - 1, y, kind, value))

    add_plain("CodexTOP", "cyan", bold=True)
    add_plain("")
    add_plain("更新间隔", "white", bold=True)
    for label, value in INTERVAL_CHOICES:
        add_option(label, state.interval == value, "interval", value)

    add_plain("")
    add_plain("历史长度", "white", bold=True)
    for period in PERIOD_CHOICES:
        add_option(period, state.period == period, "period", period)

    add_plain("")
    add_plain("曲线模式", "white", bold=True)
    for label, value in CURVE_MODE_CHOICES:
        add_option(label, state.curve_mode == value, "curve_mode", value)

    add_plain("")
    add_plain("展示范围", "white", bold=True)
    for label, value in DISPLAY_SCOPE_CHOICES:
        add_option(label, state.display_scope == value, "display_scope", value)

    add_plain("")
    add_plain("状态", "white", bold=True)
    add_plain(state.status, "yellow" if state.error else "green")
    if state.last_update:
        add_plain(datetime.fromtimestamp(state.last_update).strftime("%H:%M:%S"), "dim")
    if state.error:
        add_plain(plain_fit(state.error, inner), "red")
    else:
        remain = max(0, int(state.next_read - time.time()))
        add_plain(f"下次读取 {remain}s", "dim")
    add_plain(sampler_status(state.log_path.parent), "dim")

    while len(lines) < height - 5:
        add_plain("")

    y = len(lines) + 1
    exit_text = " F10 / 点击退出 "
    centered_exit = center_text(exit_text)
    lines.append(paint("│", "cyan") + fit_ansi(paint(centered_exit, "red", bold=True, reverse=True), inner) + paint("│", "cyan"))
    exit_start = max(1, (inner - visible_width(exit_text)) // 2 + 1)
    zones.append(ClickZone(x_origin + exit_start, x_origin + exit_start + visible_width(exit_text) - 1, y, "exit", None))
    add_plain("")
    add_plain("@JamesZhutheThird", "dim")
    add_plain(APP_VERSION, "dim")
    lines.append(paint("╰" + "─" * inner + "╯", "cyan"))
    return [fit_ansi(line, width) for line in lines[:height]]


def render_frame(state: MonitorState, term_width: int, term_height: int) -> tuple[list[str], list[ClickZone]]:
    zones: list[ClickZone] = []
    records = read_records_if_due(state)
    accounts = current_accounts(state, records)
    current = current_index(state, records)

    sidebar_width = 21
    main_width = max(40, term_width - sidebar_width)
    lines: list[str] = []

    content_height = max(6, term_height - 1)
    if not accounts:
        left_lines = [paint("正在加载 quota 数据...", "yellow")]
        left_lines.extend([""] * (content_height - 1))
        left_lines = [fit_ansi(line, main_width) for line in left_lines[:content_height]]
    elif state.display_scope == "current":
        active = selected_account(accounts, current)
        left_width = min(50, max(20, main_width // 3))
        right_width = max(12, main_width - left_width)
        left_width = main_width - right_width
        left_block = stacked_summary_panels(
            accounts,
            current,
            left_width,
            content_height,
            state.summary_offset,
            zones,
            1,
        )
        history_title = f"【{provider_name(active)}】额度历史"
        history_body = account_history_lines(
            active,
            records,
            right_width,
            content_height,
            state.period,
            state.curve_mode,
        )
        right_block = panel(
            history_title,
            history_body,
            right_width,
            content_height,
            border_color(active, current),
        )
        left_lines = compose_columns([left_block, right_block], [left_width, right_width], content_height)
    elif state.display_scope == "merged":
        color = merged_border_color(accounts)
        if content_height >= 17:
            top_height = 9
        elif content_height >= 14:
            top_height = 8
        else:
            top_height = max(4, content_height // 2)
        history_height = max(4, content_height - top_height)
        top_left_width = min(50, max(37, main_width // 3))
        top_right_width = max(24, main_width - top_left_width)
        top_left_width = main_width - top_right_width
        top_left_block = panel(
            "账号信息",
            merged_account_reset_lines(accounts, current, top_left_width, top_height),
            top_left_width,
            top_height,
            color,
        )
        top_right_block = panel(
            "总额度",
            merged_quota_summary_lines(accounts, current, top_right_width, top_height),
            top_right_width,
            top_height,
            color,
        )
        top_lines = compose_columns(
            [top_left_block, top_right_block],
            [top_left_width, top_right_width],
            top_height,
        )
        history_block = panel(
            "合并额度历史",
            merged_history_lines(records, main_width, history_height, state.period, state.curve_mode),
            main_width,
            history_height,
            color,
        )
        left_lines = top_lines + history_block
        if len(left_lines) < content_height:
            left_lines.extend([" " * main_width] * (content_height - len(left_lines)))
        left_lines = left_lines[:content_height]
    else:
        columns = min(3, len(accounts))
        row_count = max(1, (len(accounts) + columns - 1) // columns)
        base_row_height = max(8, content_height // row_count)
        left_lines = []
        for offset in range(0, len(accounts), columns):
            chunk = accounts[offset:offset + columns]
            row_height = base_row_height
            if offset + columns >= len(accounts):
                row_height = max(8, content_height - len(left_lines))
            panel_width = max(24, main_width // columns)
            widths = [panel_width] * columns
            widths[-1] += max(0, main_width - sum(widths))
            blocks = []
            for account, width in zip(chunk, widths):
                index = account_index(account)
                title = provider_name(account)
                if current is not None and index == current:
                    title = f"【{title}】"
                body = account_lines(account, records, width, row_height, state.period, current, state.curve_mode)
                blocks.append(panel(title, body, width, row_height, border_color(account, current)))
            while len(blocks) < columns:
                blocks.append([" " * panel_width for _ in range(row_height)])
            left_lines.extend(compose_columns(blocks, widths, row_height))
        if len(left_lines) < content_height:
            left_lines.extend([" " * main_width] * (content_height - len(left_lines)))
        left_lines = left_lines[:content_height]

    sidebar = render_sidebar(state, sidebar_width, content_height, main_width + 1, zones)
    for left, right in zip(left_lines, sidebar):
        lines.append(fit_ansi(left, main_width) + fit_ansi(right, sidebar_width))
    while len(lines) < term_height:
        lines.append(" " * term_width)
    return [fit_ansi(line, term_width) for line in lines[:term_height]], zones


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


def save_codextop_state(state: MonitorState) -> None:
    state.state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": int(time.time()),
        "period": state.period,
        "interval": max(1, int(state.interval)),
        "curve_mode": state.curve_mode,
        "display_scope": state.display_scope,
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


def handle_click(state: MonitorState, zones: list[ClickZone], x: int, y: int) -> bool:
    for zone in zones:
        if zone.y == y and zone.x1 <= x <= zone.x2:
            if zone.kind == "exit":
                return False
            if zone.kind == "period":
                period = str(zone.value)
                if state.period != period:
                    state.period = period
                    state.records = None
                    state.next_read = 0.0
            elif zone.kind == "curve_mode":
                state.curve_mode = str(zone.value)
            elif zone.kind == "display_scope":
                scope = str(zone.value)
                if state.display_scope != scope:
                    state.display_scope = scope
                    state.records = None
                    state.next_read = 0.0
                    if scope in {"all", "merged"}:
                        try:
                            send_sampler_interval(state, state.interval, sample_now=True, all_auth=True)
                            state.status = "已请求所有账号数据"
                            state.error = None
                        except Exception as exc:
                            state.status = "命令失败"
                            state.error = str(exc)
            elif zone.kind == "summary_scroll":
                records = list(state.records or [])
                accounts = current_accounts(state, records)
                if accounts:
                    state.summary_offset = (state.summary_offset + int(zone.value)) % len(accounts)
            elif zone.kind == "interval":
                state.interval = int(zone.value)
                state.next_read = 0.0
                try:
                    send_sampler_interval(state, state.interval)
                    state.status = "已发送间隔"
                    state.error = None
                except Exception as exc:
                    state.status = "命令失败"
                    state.error = str(exc)
            return True
    return True


class TerminalSession:
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.old: list[Any] | None = None
        self.buffer = b""

    def __enter__(self) -> "TerminalSession":
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h\x1b[2J\x1b[H")
        sys.stdout.flush()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if self.old is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def read(self) -> bytes:
        chunks = []
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if len(chunk) < 4096:
                break
        return b"".join(chunks)


MOUSE_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


def parse_input(session: TerminalSession) -> tuple[bool, list[tuple[int, int]]]:
    session.buffer += session.read()
    data = session.buffer
    keep_running = True
    clicks: list[tuple[int, int]] = []

    if b"\x03" in data or b"q" in data or b"Q" in data:
        keep_running = False
    if re.search(rb"\x1b\[21(?:;[0-9]+)?~", data):
        keep_running = False

    for match in MOUSE_RE.finditer(data):
        button = int(match.group(1))
        x = int(match.group(2))
        y = int(match.group(3))
        event = match.group(4)
        if event == b"M" and button & 3 == 0:
            clicks.append((x, y))

    last_escape = data.rfind(b"\x1b")
    if last_escape >= 0 and len(data) - last_escape < 16:
        session.buffer = data[last_escape:]
    else:
        session.buffer = b""
    return keep_running, clicks


def run_once(state: MonitorState) -> int:
    read_records_if_due(state, force=True)
    width, height = shutil.get_terminal_size((160, 48))
    lines, _zones = render_frame(state, width, height)
    print("\n".join(lines))
    return 0 if not state.error else 1


def run_tui(state: MonitorState) -> int:
    state.next_read = 0.0
    if state.interval != state.restore_interval or state.display_scope == "merged":
        try:
            send_sampler_interval(state, state.interval, sample_now=True, all_auth=True)
        except Exception as exc:
            state.status = "命令失败"
            state.error = str(exc)
    cleanup_errors: list[str] = []
    try:
        with TerminalSession() as session:
            running = True
            zones: list[ClickZone] = []
            while running:
                width, height = shutil.get_terminal_size((120, 36))
                lines, zones = render_frame(state, width, height)
                sys.stdout.write("\x1b[H" + "\n".join(lines))
                sys.stdout.flush()

                deadline = time.monotonic() + 0.2
                while time.monotonic() < deadline:
                    keep_running, clicks = parse_input(session)
                    if not keep_running:
                        running = False
                        break
                    for x, y in clicks:
                        running = handle_click(state, zones, x, y)
                        if not running:
                            break
                    if not running:
                        break
                    time.sleep(0.03)
    finally:
        try:
            save_codextop_state(state)
        except Exception as exc:
            cleanup_errors.append(f"保存 CodexTOP 设置失败: {exc}")
        try:
            send_sampler_interval(state, state.restore_interval, sample_now=False)
        except Exception as exc:
            cleanup_errors.append(f"恢复 sampler 间隔失败: {exc}")
    if cleanup_errors:
        print("\n".join(cleanup_errors), file=sys.stderr)
        return 1
    return 0


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


def main() -> int:
    parser = argparse.ArgumentParser(description="全屏 Codex quota 监控。")
    parser.add_argument("-p", "--period", choices=PERIOD_CHOICES, default=None, help="初始历史长度。")
    parser.add_argument("-i", "--interval", type=parse_interval, default=None, help="初始读取/后台更新间隔，如 30s 或 2m。")
    parser.add_argument("--curve-mode", choices=["connected", "points"], default=None, help="历史曲线模式：connected 连续，points 间断。")
    parser.add_argument("--display-scope", choices=["all", "current", "merged"], default=None, help="展示范围：all 全部账号，current 启用账号，merged 合并账号。")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="quota 历史日志目录。")
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="quota 历史日志基础文件名，用于匹配月度日志。")
    parser.add_argument("--control-file", default=DEFAULT_CONTROL_FILE, help="后台 sampler 控制文件名。")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="CodexTOP 状态文件名。")
    parser.add_argument("--tz", default="Asia/Shanghai", help="本地时区。")
    parser.add_argument("--once", action="store_true", help="渲染一帧后退出，用于调试。")
    args = parser.parse_args()

    ensure_runtime_layout()
    control_path = args.log_dir.expanduser() / args.control_file
    state_path = args.log_dir.expanduser() / args.state_file
    saved_state = read_codextop_state(state_path)
    restore_interval = read_sampler_interval(control_path, DEFAULT_SAMPLER_INTERVAL_SECONDS)
    state = MonitorState(
        period=args.period or saved_period(saved_state) or DEFAULT_PERIOD,
        interval=args.interval if args.interval is not None else (saved_interval(saved_state) or restore_interval),
        tz=args.tz,
        log_path=args.log_dir.expanduser() / args.log_file,
        restore_interval=restore_interval,
        state_path=state_path,
        curve_mode=args.curve_mode or saved_curve_mode(saved_state) or DEFAULT_CURVE_MODE,
        display_scope=args.display_scope or saved_display_scope(saved_state) or DEFAULT_DISPLAY_SCOPE,
        control_path=control_path,
    )
    if args.once:
        return run_once(state)
    return run_tui(state)


if __name__ == "__main__":
    raise SystemExit(main())
