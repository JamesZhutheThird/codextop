"""Terminal text width, ANSI color, and compact formatting helpers."""

from __future__ import annotations

import colorsys
import unicodedata
from datetime import datetime
from typing import Any

from quota import check_codex_quota as quota
from . import color_schemes
from core.constants import *

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


def bg(color: str | None) -> str:
    if not color:
        return ""
    color = color.strip()
    if color.startswith("#") and len(color) == 7:
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        return f"\x1b[48;2;{r};{g};{b}m"
    named = {
        "dark_cyan": "\x1b[48;5;24m",
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


def paint_on(text: str, color: str | None, background: str | None, *, bold: bool = False, dim: bool = False) -> str:
    codes = []
    if bold:
        codes.append("\x1b[1m")
    if dim:
        codes.append("\x1b[2m")
    codes.append(fg(color))
    codes.append(bg(background))
    prefix = "".join(code for code in codes if code)
    return f"{prefix}{text}\x1b[0m" if prefix else text


def paint_style(text: str, style: str | None) -> str:
    if not style:
        return text
    tokens = style.split()
    color = next((token for token in tokens if token != "bold"), None)
    return paint(text, color, bold="bold" in tokens)


def percent_color(value: Any) -> str:
    return color_schemes.percent_gradient_style(value)


def adjust_hex_color(color: str, *, saturation: float = 1.0, value: float = 1.0) -> str:
    if not color.startswith("#") or len(color) != 7:
        return color
    red = int(color[1:3], 16) / 255.0
    green = int(color[3:5], 16) / 255.0
    blue = int(color[5:7], 16) / 255.0
    hue, current_saturation, current_value = colorsys.rgb_to_hsv(red, green, blue)
    current_saturation = max(0.0, min(1.0, current_saturation * saturation))
    current_value = max(0.0, min(1.0, current_value * value))
    red, green, blue = colorsys.hsv_to_rgb(hue, current_saturation, current_value)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def chart_series_color(label: str, value: Any, *, dimmed: bool = False) -> str:
    color = percent_color(value)
    if label == "5h":
        return adjust_hex_color(color, value=0.72) if dimmed else color
    if label == "7d":
        if dimmed:
            return adjust_hex_color(color, saturation=1.15, value=0.5)
        return adjust_hex_color(color, value=0.62)
    return color


def chart_row_percent(row: int, chart_height: int) -> float:
    if chart_height <= 1:
        return 100.0
    return max(0.0, min(100.0, (chart_height - 1 - row) / (chart_height - 1) * 100.0))


def window_marker_text(key: str, curve_mode: str) -> str:
    if curve_mode == "braille":
        marker = BRAILLE_LEGEND_MARKER
    elif curve_mode == "box":
        marker = BOX_LEGEND_MARKER
    elif curve_mode == "bar":
        marker = BAR_LEGEND_MARKER
    else:
        marker = WINDOW_MARKERS[key]
    return f"{key}({marker})"


def window_marker_label(key: str, curve_mode: str) -> str:
    if curve_mode == "braille" and key == "7d":
        return f"{key}({paint(BRAILLE_LEGEND_MARKER, 'dim')})"
    if curve_mode == "box" and key == "7d":
        return f"{key}({paint(BOX_LEGEND_MARKER, 'dim')})"
    if curve_mode == "bar" and key == "7d":
        return f"{key}({paint(BAR_LEGEND_MARKER, 'dim')})"
    return window_marker_text(key, curve_mode)


def window_keys(window_scope: str = DEFAULT_WINDOW_SCOPE) -> tuple[str, ...]:
    if window_scope in WINDOW_MARKERS:
        return (window_scope,)
    return WINDOW_KEYS


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
