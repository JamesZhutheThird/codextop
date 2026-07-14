"""Interactive settings sidebar and setting-change handlers."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from . import color_schemes
from core.constants import *
from .history import current_accounts
from .models import ClickZone, MonitorState
from core.state import sampler_status, send_sampler_interval
from .terminal_text import *

def interval_label(seconds: int) -> str:
    for label, value in INTERVAL_CHOICES:
        if value == seconds:
            return label
    return f"{seconds}s"


def setting_items() -> list[tuple[str, str, list[tuple[str, Any]]]]:
    return [
        ("interval", "更新间隔", list(INTERVAL_CHOICES)),
        ("period", "历史长度", [(period, period) for period in PERIOD_CHOICES]),
        ("curve_mode", "曲线模式", list(CURVE_MODE_CHOICES)),
        ("window_scope", "额度窗口", list(WINDOW_SCOPE_CHOICES)),
        ("color_scheme", "配色方案", color_schemes.color_scheme_choices()),
        ("display_scope", "展示范围", list(DISPLAY_SCOPE_CHOICES)),
    ]


def setting_current_value(state: MonitorState, key: str) -> Any:
    if key == "interval":
        return state.interval
    if key == "period":
        return state.period
    if key == "curve_mode":
        return state.curve_mode
    if key == "window_scope":
        return state.window_scope
    if key == "color_scheme":
        return state.color_scheme
    if key == "display_scope":
        return state.display_scope
    return None


def setting_index_for_key(key: str) -> int:
    for index, (item_key, _title, _choices) in enumerate(setting_items()):
        if item_key == key:
            return index
    return 0


def setting_current_option_index(state: MonitorState, key: str) -> int:
    current = setting_current_value(state, key)
    items = setting_items()
    for item_key, _title, choices in items:
        if item_key != key:
            continue
        for index, (_label, value) in enumerate(choices):
            if value == current:
                return index
    return 0


def setting_current_label(state: MonitorState, key: str) -> str:
    current = setting_current_value(state, key)
    for item_key, _title, choices in setting_items():
        if item_key != key:
            continue
        for label, value in choices:
            if value == current:
                return label
    return interval_label(current) if key == "interval" and isinstance(current, int) else str(current or "-")


def normalize_settings_state(state: MonitorState) -> list[tuple[str, str, list[tuple[str, Any]]]]:
    items = setting_items()
    if state.settings_mode not in {"normal", "select", "options"}:
        state.settings_mode = "normal"
    if not items:
        state.settings_focus = 0
        state.settings_option_focus = 0
        return items
    state.settings_focus %= len(items)
    choices = items[state.settings_focus][2]
    if choices:
        state.settings_option_focus %= len(choices)
    else:
        state.settings_option_focus = 0
    return items


def focused_setting(state: MonitorState) -> tuple[int, str, str, list[tuple[str, Any]]]:
    items = normalize_settings_state(state)
    index = state.settings_focus if items else 0
    if not items:
        return 0, "", "", []
    key, title, choices = items[index]
    return index, key, title, choices


def open_focused_setting(state: MonitorState) -> None:
    _index, key, _title, _choices = focused_setting(state)
    state.settings_mode = "options"
    state.settings_option_focus = setting_current_option_index(state, key)


def update_available_label(state: MonitorState) -> str:
    version = state.update_latest_version
    if isinstance(version, str) and version:
        version = version if version.startswith("v") else f"v{version}"
    else:
        version = "新版本"
    return f"{version} 可用（F11）"


def request_update_action(state: MonitorState) -> bool:
    if not state.update_available:
        return True
    if state.update_confirming:
        state.update_requested = True
        return False
    state.update_confirming = True
    state.settings_mode = "normal"
    return True


def render_sidebar(state: MonitorState, width: int, height: int, x_origin: int, zones: list[ClickZone]) -> list[str]:
    inner = max(8, width - 2)
    lines: list[str] = [paint("╭" + "─" * inner + "╮", "cyan")]
    items = normalize_settings_state(state)

    def center_text(text: str) -> str:
        text_width = visible_width(text)
        pad = max(0, inner - text_width)
        left = pad // 2
        right = pad - left
        return " " * left + text + " " * right

    def add_plain(
        text: str = "",
        color: str | None = None,
        *,
        bold: bool = False,
        dim: bool = False,
        reverse: bool = False,
    ) -> int:
        y = len(lines) + 1
        rendered = paint(center_text(text), color, bold=bold, dim=dim, reverse=reverse)
        lines.append(paint("│", "cyan") + fit_ansi(rendered, inner) + paint("│", "cyan"))
        return y

    def add_click_row(text: str, color: str, kind: str, value: Any, *, selected: bool = False) -> None:
        y = len(lines) + 1
        centered = center_text(text)
        rendered = paint(centered, color, bold=True, reverse=selected)
        lines.append(paint("│", "cyan") + fit_ansi(rendered, inner) + paint("│", "cyan"))
        zones.append(ClickZone(x_origin + 1, x_origin + inner, y, kind, value))

    def add_setting_title(title: str) -> None:
        rendered = paint_on(center_text(title), "white", "dark_cyan", bold=True)
        lines.append(paint("│", "cyan") + fit_ansi(rendered, inner) + paint("│", "cyan"))

    def add_setting_item(index: int, key: str, title: str) -> None:
        focused = state.settings_mode == "select" and state.settings_focus == index
        add_setting_title(title)
        current = setting_current_label(state, key)
        add_click_row(current, "cyan" if focused else "green", "settings_item", index, selected=focused)

    def add_setting_options(key: str, title: str, choices: list[tuple[str, Any]]) -> None:
        add_setting_title(title)
        add_plain("")
        current = setting_current_value(state, key)
        for option_index, (label, value) in enumerate(choices):
            focused = option_index == state.settings_option_focus
            marker = "● " if value == current else "  "
            color = "cyan" if focused else ("green" if value == current else "white")
            add_click_row(f"{marker}{label}", color, "settings_option", (key, value), selected=focused)

    update_rows = 1 if state.update_available else 0
    status_bottom_rows = (13 if state.last_update else 12) + update_rows
    settings_limit = max(1, height - status_bottom_rows)

    add_plain("CodexTOP", "cyan", bold=True)
    add_plain("")
    if state.settings_mode == "options":
        _index, key, title, choices = focused_setting(state)
        add_setting_options(key, title, choices)
    else:
        for index, (key, title, _choices) in enumerate(items):
            add_setting_item(index, key, title)
            if index != len(items) - 1:
                add_plain("")

    lines = lines[:settings_limit]
    zones[:] = [
        zone for zone in zones
        if not (
            zone.x1 >= x_origin
            and zone.kind in {"settings_item", "settings_option"}
            and zone.y > settings_limit
        )
    ]
    while len(lines) < settings_limit:
        add_plain("")

    y = len(lines) + 1
    settings_text = " F9 / 设置模式 "
    centered_settings = center_text(settings_text)
    lines.append(
        paint("│", "cyan")
        + fit_ansi(paint(centered_settings, "cyan", bold=True, reverse=True), inner)
        + paint("│", "cyan")
    )
    settings_start = max(1, (inner - visible_width(settings_text)) // 2 + 1)
    zones.append(
        ClickZone(
            x_origin + settings_start,
            x_origin + settings_start + visible_width(settings_text) - 1,
            y,
            "settings_activate",
            None,
        )
    )
    add_plain("")

    y = len(lines) + 1
    exit_text = " F10 / 点击退出 "
    centered_exit = center_text(exit_text)
    lines.append(
        paint("│", "cyan")
        + fit_ansi(paint(centered_exit, "red", bold=True, reverse=True), inner)
        + paint("│", "cyan")
    )
    exit_start = max(1, (inner - visible_width(exit_text)) // 2 + 1)
    zones.append(
        ClickZone(
            x_origin + exit_start,
            x_origin + exit_start + visible_width(exit_text) - 1,
            y,
            "exit",
            None,
        )
    )
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
    add_plain("")
    add_plain("@JamesZhutheThird")
    add_plain(APP_VERSION)
    if state.update_available:
        text = "确认更新？（Enter）" if state.update_confirming else update_available_label(state)
        y = len(lines) + 1
        rendered = paint(center_text(text), "yellow", bold=True, reverse=state.update_confirming)
        lines.append(paint("│", "cyan") + fit_ansi(rendered, inner) + paint("│", "cyan"))
        zones.append(ClickZone(x_origin + 1, x_origin + inner, y, "update_action", None))
    lines.append(paint("╰" + "─" * inner + "╯", "cyan"))
    return [fit_ansi(line, width) for line in lines[:height]]


def apply_setting_value(state: MonitorState, key: str, value: Any) -> None:
    if key == "period":
        period = str(value)
        if state.period != period:
            state.period = period
            state.records = None
            state.next_read = 0.0
    elif key == "curve_mode":
        state.curve_mode = str(value)
    elif key == "window_scope":
        state.window_scope = str(value)
    elif key == "color_scheme":
        state.color_scheme = color_schemes.set_active_color_scheme(str(value))
    elif key == "display_scope":
        scope = str(value)
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
    elif key == "interval":
        state.interval = int(value)
        state.next_read = 0.0
        try:
            send_sampler_interval(state, state.interval)
            state.status = "已发送间隔"
            state.error = None
        except Exception as exc:
            state.status = "命令失败"
            state.error = str(exc)


def handle_setting_key(state: MonitorState, key: str) -> bool:
    if key == "f11":
        return request_update_action(state)
    if key == "enter" and state.update_confirming:
        return request_update_action(state)
    if key == "f9":
        state.settings_mode = "select"
        normalize_settings_state(state)
        return True
    if key == "esc":
        if state.update_confirming:
            state.update_confirming = False
            return True
        if state.settings_mode == "options":
            state.settings_mode = "select"
        elif state.settings_mode == "select":
            state.settings_mode = "normal"
        return True
    if state.settings_mode == "normal":
        return True

    items = normalize_settings_state(state)
    if not items:
        return True
    if key in {"up", "down"}:
        step = -1 if key == "up" else 1
        if state.settings_mode == "options":
            choices = items[state.settings_focus][2]
            if choices:
                state.settings_option_focus = (state.settings_option_focus + step) % len(choices)
        else:
            state.settings_focus = (state.settings_focus + step) % len(items)
            item_key = items[state.settings_focus][0]
            state.settings_option_focus = setting_current_option_index(state, item_key)
        return True
    if key == "enter":
        if state.settings_mode == "select":
            open_focused_setting(state)
        elif state.settings_mode == "options":
            _index, item_key, _title, choices = focused_setting(state)
            if choices:
                _label, value = choices[state.settings_option_focus % len(choices)]
                apply_setting_value(state, item_key, value)
            state.settings_mode = "select"
        return True
    return True


def handle_click(state: MonitorState, zones: list[ClickZone], x: int, y: int) -> bool:
    for zone in zones:
        if zone.y == y and zone.x1 <= x <= zone.x2:
            if zone.kind == "exit":
                return False
            if zone.kind == "update_action":
                return request_update_action(state)
            if zone.kind == "settings_activate":
                state.settings_mode = "select"
                normalize_settings_state(state)
            elif zone.kind == "settings_item":
                state.settings_focus = int(zone.value)
                open_focused_setting(state)
            elif zone.kind == "settings_option":
                item_key, value = zone.value
                state.settings_focus = setting_index_for_key(str(item_key))
                apply_setting_value(state, str(item_key), value)
                state.settings_option_focus = setting_current_option_index(state, str(item_key))
                state.settings_mode = "normal"
            elif zone.kind in {"period", "curve_mode", "window_scope", "color_scheme", "display_scope", "interval"}:
                apply_setting_value(state, zone.kind, zone.value)
            elif zone.kind == "summary_scroll":
                records = list(state.records or [])
                accounts = current_accounts(state, records)
                if accounts:
                    state.summary_offset = (state.summary_offset + int(zone.value)) % len(accounts)
            return True
    return True
