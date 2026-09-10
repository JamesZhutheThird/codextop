"""Interactive settings sidebar and setting-change handlers."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import color_schemes
from core.constants import *
from core.i18n import available_languages, language_name, set_language, t
from core.update_check import start_daily_update_check
from .history import current_accounts, current_index
from .models import ClickZone, MonitorState
from .panels import account_index, dashboard_grid_layout, focus_history_account, focus_page_layout, panel
from core.state import sampler_status, send_sampler_interval
from .statistics_summary import close_statistics, open_statistics, set_statistics_granularity, shift_statistics
from .terminal_text import *

def interval_label(seconds: int) -> str:
    for label, value in INTERVAL_CHOICES:
        if value == seconds:
            return label
    return f"{seconds}s"


def localized_choices(choices: list[tuple[str, Any]], prefix: str) -> list[tuple[str, Any]]:
    return [(t(f"{prefix}.{value}"), value) for _label, value in choices]


SIDEBAR_SETTING_KEYS = {"period", "display_scope"}
SOFTWARE_VERSION_KEY = "software_version"
VERSION_MODAL_WIDTH = 76
UPDATE_MODAL_WIDTH = 76
UPDATE_MODAL_HEIGHT = 22
CODEXTOP_ASCII_ART = (
    "   ██████╗ ██████╗ ██████╗ ███████╗██╗  ██╗████████╗ ██████╗ ██████╗",
    "  ██╔════╝██╔═══██╗██╔══██╗██╔════╝╚██╗██╔╝╚══██╔══╝██╔═══██╗██╔══██╗",
    "  ██║     ██║   ██║██║  ██║█████╗   ╚███╔╝    ██║   ██║   ██║██████╔╝",
    "  ██║     ██║   ██║██║  ██║██╔══╝   ██╔██╗    ██║   ██║   ██║██╔═══╝",
    "  ╚██████╗╚██████╔╝██████╔╝███████╗██╔╝ ██╗   ██║   ╚██████╔╝██║",
    "   ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚═╝",
)
CHANGELOG_PATH = Path(__file__).resolve().parents[2] / "CHANGELOG.md"


def current_version_changelog(path: Path = CHANGELOG_PATH) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    heading = f"## {APP_VERSION}"
    start = next(
        (index + 1 for index, line in enumerate(lines) if line == heading or line.startswith(heading + " ")),
        None,
    )
    if start is None:
        return None
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip() or None


def _gradient_logo_rows(scheme_key: str) -> tuple[str, ...]:
    height = max(1, len(CODEXTOP_ASCII_ART) - 1)
    width = max(1, max(visible_width(line) for line in CODEXTOP_ASCII_ART) - 1)
    scores = [
        row / height + column / width
        for row, line in enumerate(CODEXTOP_ASCII_ART)
        for column, char in enumerate(line)
        if not char.isspace()
    ]
    low = min(scores)
    span = max(scores) - low or 1.0
    return tuple(
        "".join(
            char
            if char.isspace()
            else paint(
                char,
                color_schemes.percent_gradient_style(
                    ((row / height + column / width) - low) / span * 100,
                    key=scheme_key,
                ),
                bold=True,
            )
            for column, char in enumerate(line)
        )
        for row, line in enumerate(CODEXTOP_ASCII_ART)
    )


def setting_items(state: MonitorState) -> list[tuple[str, str, list[tuple[str, Any]]]]:
    return [
        ("interval", t("setting.interval"), list(INTERVAL_CHOICES)),
        ("period", t("setting.period"), [(period, period) for period in PERIOD_CHOICES]),
        (
            "display_scope",
            t("setting.display_scope"),
            localized_choices(DISPLAY_SCOPE_CHOICES, "choice.display"),
        ),
        (
            "language",
            t("setting.language"),
            [
                (f"{language_name(language)} ({language})", language)
                for language in available_languages()
            ],
        ),
        (
            "hide_account_info",
            t("setting.hide_account_info"),
            [
                (t("choice.account_info.visible"), False),
                (t("choice.account_info.hidden"), True),
            ],
        ),
        (
            "window_scope",
            t("setting.window_scope"),
            localized_choices(WINDOW_SCOPE_CHOICES, "choice.window"),
        ),
        (
            "dashboard_density",
            t("setting.dashboard_density"),
            localized_choices(DASHBOARD_DENSITY_CHOICES, "choice.dashboard_density"),
        ),
        (
            "merged_quota_mode",
            t("setting.merged_quota_mode"),
            localized_choices(MERGED_QUOTA_MODE_CHOICES, "choice.merged"),
        ),
        (
            "usage_directory_scope",
            t("setting.usage_directory_scope"),
            localized_choices(USAGE_DIRECTORY_SCOPE_CHOICES, "choice.directory"),
        ),
        (
            "usage_panel_layout",
            t("setting.usage_panel_layout"),
            localized_choices(USAGE_PANEL_LAYOUT_CHOICES, "choice.layout"),
        ),
        (
            "usage_axis_scale",
            t("setting.usage_axis_scale"),
            localized_choices(USAGE_AXIS_SCALE_CHOICES, "choice.axis"),
        ),
        (
            "curve_mode",
            t("setting.curve_mode"),
            localized_choices(CURVE_MODE_CHOICES, "choice.curve"),
        ),
        ("color_scheme", t("setting.color_scheme"), color_schemes.color_scheme_choices()),
    ]


def setting_categories(
    state: MonitorState,
) -> list[tuple[str, str, list[tuple[str, str, list[tuple[str, Any]]]]]]:
    items = [item for item in setting_items(state) if item[0] not in SIDEBAR_SETTING_KEYS]
    groups = (
        (
            "general",
            t("settings.category.general"),
            {"interval", "language", "hide_account_info"},
        ),
        (
            "display",
            t("settings.category.display"),
            {
                "dashboard_density",
                "usage_directory_scope",
                "usage_panel_layout",
                "usage_axis_scale",
                "merged_quota_mode",
                "window_scope",
            },
        ),
        (
            "appearance",
            t("settings.category.appearance"),
            {"curve_mode", "color_scheme"},
        ),
    )
    return [
        (key, title, [item for item in items if item[0] in keys])
        for key, title, keys in groups
        if any(item[0] in keys for item in items)
    ]


def settings_root_entries(
    state: MonitorState,
) -> list[tuple[str, str, list[tuple[str, str, list[tuple[str, Any]]]]]]:
    return [
        *setting_categories(state),
        (SOFTWARE_VERSION_KEY, t("settings.software_version"), []),
    ]


def setting_current_value(state: MonitorState, key: str) -> Any:
    if key == "interval":
        return state.interval
    if key == "period":
        return state.period
    if key == "usage_directory_scope":
        return state.usage_directory_scope
    if key == "usage_panel_layout":
        return state.usage_panel_layout
    if key == "usage_axis_scale":
        return state.usage_axis_scale
    if key == "curve_mode":
        return state.curve_mode
    if key == "window_scope":
        return state.window_scope
    if key == "merged_quota_mode":
        return state.merged_quota_mode
    if key == "color_scheme":
        return state.color_scheme
    if key == "language":
        return state.language
    if key == "display_scope":
        return state.display_scope
    if key == "dashboard_density":
        return getattr(state, "dashboard_density", DEFAULT_DASHBOARD_DENSITY)
    if key == "hide_account_info":
        return getattr(state, "hide_account_info", DEFAULT_HIDE_ACCOUNT_INFO)
    return None


def setting_index_for_key(state: MonitorState, key: str) -> int:
    for index, (item_key, _title, _choices) in enumerate(setting_items(state)):
        if item_key == key:
            return index
    return 0


def setting_current_option_index(state: MonitorState, key: str) -> int:
    current = setting_current_value(state, key)
    items = setting_items(state)
    for item_key, _title, choices in items:
        if item_key != key:
            continue
        for index, (_label, value) in enumerate(choices):
            if value == current:
                return index
    return 0


def setting_current_label(state: MonitorState, key: str) -> str:
    current = setting_current_value(state, key)
    for item_key, _title, choices in setting_items(state):
        if item_key != key:
            continue
        for label, value in choices:
            if value == current:
                return label
    return interval_label(current) if key == "interval" and isinstance(current, int) else str(current or "-")


def normalize_settings_state(state: MonitorState) -> list[tuple[str, str, list[tuple[str, Any]]]]:
    items = setting_items(state)
    if state.settings_mode == "select":
        state.settings_mode = "items"
    if state.settings_mode not in {"normal", "categories", "items", "options"}:
        state.settings_mode = "normal"
    if not items:
        state.settings_category_focus = 0
        state.settings_focus = 0
        state.settings_option_focus = 0
        return items

    state.settings_focus %= len(items)
    focused_key = items[state.settings_focus][0]
    if state.settings_mode == "options" and focused_key in SIDEBAR_SETTING_KEYS:
        choices = items[state.settings_focus][2]
        state.settings_option_focus %= len(choices)
        return items

    categories = setting_categories(state)
    roots = settings_root_entries(state)
    state.settings_category_focus %= len(roots)
    if state.settings_mode == "categories":
        return items
    if state.settings_category_focus >= len(categories):
        state.settings_category_focus = 0
    category_items = categories[state.settings_category_focus][2]
    category_keys = {item[0] for item in category_items}
    if items[state.settings_focus][0] not in category_keys:
        state.settings_focus = setting_index_for_key(state, category_items[0][0])
    choices = items[state.settings_focus][2]
    if choices:
        state.settings_option_focus %= len(choices)
    else:
        state.settings_option_focus = 0
    return items


def settings_page_ranges(
    row_heights: list[int], capacity: int, gap: int = 1
) -> list[tuple[int, int]]:
    if not row_heights or capacity <= 0:
        return []
    pages: list[tuple[int, int]] = []
    start = 0
    while start < len(row_heights):
        end = start
        used = 0
        while end < len(row_heights):
            height = row_heights[end]
            row_gap = gap if end > start else 0
            if end > start and used + row_gap + height > capacity:
                break
            used += row_gap + height
            end += 1
            if used >= capacity:
                break
        pages.append((start, end))
        start = end
    return pages


def settings_page_layout(
    row_heights: list[int], focus: int, capacity: int, gap: int = 1
) -> tuple[list[tuple[int, int]], int]:
    if not row_heights or capacity < min(row_heights):
        return [], 0
    focus = min(max(0, focus), len(row_heights) - 1)
    if row_heights[focus] > capacity:
        return [], 0
    pages = settings_page_ranges(row_heights, capacity, gap)
    if len(pages) > 1:
        if capacity - 1 < row_heights[focus]:
            return [next(page for page in pages if page[0] <= focus < page[1])], 0
        pages = settings_page_ranges(row_heights, capacity - 1, gap)
    if not pages:
        return [], 0
    page = next(
        (index for index, (start, end) in enumerate(pages) if start <= focus < end),
        len(pages) - 1,
    )
    return pages, page


def focused_setting(state: MonitorState) -> tuple[int, str, str, list[tuple[str, Any]]]:
    items = normalize_settings_state(state)
    index = state.settings_focus if items else 0
    if not items:
        return 0, "", "", []
    key, title, choices = items[index]
    return index, key, title, choices


def open_focused_setting(state: MonitorState) -> None:
    items = setting_items(state)
    state.settings_focus %= len(items)
    key = items[state.settings_focus][0]
    state.settings_mode = "options"
    state.settings_option_focus = setting_current_option_index(state, key)


def open_focused_category(state: MonitorState) -> None:
    roots = settings_root_entries(state)
    state.settings_category_focus %= len(roots)
    category_key, _category_title, category_items = roots[state.settings_category_focus]
    if category_key == SOFTWARE_VERSION_KEY:
        open_version_dialog(state)
        return
    state.settings_focus = setting_index_for_key(state, category_items[0][0])
    state.settings_mode = "items"
    normalize_settings_state(state)


def back_settings(state: MonitorState) -> None:
    if state.settings_mode == "options":
        key = setting_items(state)[state.settings_focus][0]
        state.settings_mode = "normal" if key in SIDEBAR_SETTING_KEYS else "items"
    elif state.settings_mode == "items":
        state.settings_mode = "categories"
    elif state.settings_mode == "categories":
        state.settings_mode = "normal"


def update_available_label() -> str:
    return t("update.available_label")


def open_version_dialog(state: MonitorState) -> bool:
    close_statistics(state)
    close_update_dialog(state)
    state.version_dialog_open = True
    return True


def close_version_dialog(state: MonitorState) -> None:
    state.version_dialog_open = False


def start_manual_update_check(state: MonitorState) -> bool:
    close_version_dialog(state)
    close_statistics(state)
    state.update_dialog_open = True
    state.update_notes_scroll = 0
    state.update_error = None
    state.update_reason = None
    if state.update_check_path is None:
        state.update_error = t("update.check_unavailable")
    else:
        start_daily_update_check(state, force=True)
    return True


def handle_version_dialog_key(state: MonitorState, key: str) -> bool:
    if key == "backspace":
        close_version_dialog(state)
    elif key == "f9":
        close_version_dialog(state)
        state.settings_mode = "normal"
    elif key == "enter":
        return start_manual_update_check(state)
    return True


def open_update_dialog(state: MonitorState) -> bool:
    if not state.update_available:
        return True
    close_statistics(state)
    close_version_dialog(state)
    state.update_dialog_open = True
    state.update_notes_scroll = 0
    state.settings_mode = "normal"
    return True


def close_update_dialog(state: MonitorState) -> None:
    state.update_dialog_open = False


def request_update_install(state: MonitorState) -> bool:
    if not state.update_available:
        close_update_dialog(state)
        return True
    state.update_requested = True
    return False


def handle_update_dialog_key(state: MonitorState, key: str) -> bool:
    if key == "backspace":
        close_update_dialog(state)
    elif key == "enter" and state.update_available and not state.update_checking and not state.update_error:
        return request_update_install(state)
    elif key in {"up", "down", "left", "right"} and state.update_available:
        step = -1 if key in {"up", "left"} else 1
        state.update_notes_scroll = max(0, state.update_notes_scroll + step)
    return True


def _release_note_rows(notes: str | None, width: int) -> list[str]:
    source = notes.strip() if isinstance(notes, str) else ""
    if not source:
        return [t("update.notes_empty")]
    rows: list[str] = []
    for line in source.splitlines():
        rows.extend(plain_wrap(line, width) if line else [""])
    return rows


def _overlay_plain_columns(text: str, start: int, width: int) -> str:
    plain = strip_ansi(text)
    output: list[str] = []
    position = 0
    used = 0
    for char in plain:
        size = char_width(char)
        next_position = position + size
        if next_position <= start:
            position = next_position
            continue
        if position >= start + width:
            break
        if position < start or next_position > start + width:
            output.append(" " * min(size, width - used))
            used += min(size, width - used)
        else:
            output.append(char)
            used += size
        position = next_position
    return "".join(output) + " " * max(0, width - used)


def _place_overlay(
    base_lines: list[str],
    modal: list[str],
    width: int,
    term_width: int,
    term_height: int,
) -> tuple[list[str], int, int]:
    start_x = (term_width - width) // 2
    start_y = (term_height - len(modal)) // 2
    dimmed = [paint(strip_ansi(fit_ansi(line, term_width)), "dim") for line in base_lines[:term_height]]
    while len(dimmed) < term_height:
        dimmed.append(" " * term_width)
    for row, modal_line in enumerate(modal):
        target = start_y + row
        original = base_lines[target] if target < len(base_lines) else ""
        left = _overlay_plain_columns(original, 0, start_x)
        right_width = term_width - start_x - width
        right = _overlay_plain_columns(original, start_x + width, right_width)
        dimmed[target] = paint(left, "dim") + fit_ansi(modal_line, width) + paint(right, "dim")
    return [fit_ansi(line, term_width) for line in dimmed[:term_height]], start_x, start_y


def _space_insufficient_overlay(
    term_width: int,
    term_height: int,
    close_label: str,
    close_kind: str,
) -> tuple[list[str], list[ClickZone]]:
    rows = [" " * term_width for _ in range(term_height)]
    middle = max(0, term_height // 2 - 1)
    rows[middle] = center_ansi(paint(t("terminal.space_insufficient"), "red", bold=True), term_width)
    close_y = min(term_height, middle + 2)
    rows[close_y - 1] = center_ansi(paint(close_label, "red", bold=True, reverse=True), term_width)
    close_width = visible_width(close_label)
    close_x = max(1, (term_width - close_width) // 2 + 1)
    return rows, [ClickZone(close_x, close_x + close_width - 1, close_y, close_kind, None)]


def render_version_overlay(
    base_lines: list[str],
    state: MonitorState,
    term_width: int,
    term_height: int,
) -> tuple[list[str], list[ClickZone]]:
    width = VERSION_MODAL_WIDTH
    close_label = t("update.close")
    check_label = t("version.check_update")
    inner = width - 2
    note_rows = _release_note_rows(current_version_changelog(), inner - 2)
    body_height = len(CODEXTOP_ASCII_ART) + len(note_rows) + 6
    height = body_height + 2
    if term_width < width or term_height < height + 1:
        return _space_insufficient_overlay(
            term_width,
            term_height,
            close_label,
            "version_close",
        )

    art_width = max(visible_width(line) for line in CODEXTOP_ASCII_ART)
    art_left = " " * max(0, (inner - art_width) // 2)
    body = ["", *[art_left + line for line in _gradient_logo_rows(state.color_scheme)]]
    body.extend(
        [
            "",
            center_ansi(paint("@JamesZhutheThird", "white", bold=True), inner),
            "",
            center_ansi(paint(APP_VERSION, "yellow", bold=True), inner),
            *[" " + fit_ansi(line, inner - 2) + " " for line in note_rows],
            "",
        ]
    )
    modal = panel(t("settings.software_version"), body, width, height, "yellow")
    gap = "   "
    action_line = paint(check_label, "yellow", bold=True, reverse=True) + gap + paint(
        close_label,
        "red",
        bold=True,
        reverse=True,
    )
    modal.append(center_ansi(action_line, width))
    rendered, start_x, start_y = _place_overlay(base_lines, modal, width, term_width, term_height)

    action_width = visible_width(check_label) + visible_width(gap) + visible_width(close_label)
    action_x = start_x + 1 + (width - action_width) // 2
    action_y = start_y + len(modal)
    check_width = visible_width(check_label)
    close_x = action_x + check_width + visible_width(gap)
    return rendered, [
        ClickZone(action_x, action_x + check_width - 1, action_y, "version_check", None),
        ClickZone(close_x, close_x + visible_width(close_label) - 1, action_y, "version_close", None),
    ]


def render_update_overlay(
    base_lines: list[str],
    state: MonitorState,
    term_width: int,
    term_height: int,
) -> tuple[list[str], list[ClickZone]]:
    width = UPDATE_MODAL_WIDTH
    close_label = t("update.close")
    inner = width - 4
    if state.update_checking or state.update_error or not state.update_available:
        if state.update_checking:
            message = t("update.checking")
        elif state.update_error:
            message = t("update.check_failed", error=state.update_error)
        else:
            message = t("update.already_current")
        status_rows = plain_wrap(message, inner)
        body = ["", *[center_ansi(row, inner) for row in status_rows], ""]
        height = len(body) + 2
        if term_width < width or term_height < height + 1:
            return _space_insufficient_overlay(term_width, term_height, close_label, "update_close")
        modal = panel(
            t("update.check_title"),
            [" " + fit_ansi(line, inner) + " " for line in body],
            width,
            height,
            "yellow",
        )
        modal.append(center_ansi(paint(close_label, "red", bold=True, reverse=True), width))
        rendered, start_x, start_y = _place_overlay(base_lines, modal, width, term_width, term_height)
        close_width = visible_width(close_label)
        close_x = start_x + 1 + (width - close_width) // 2
        close_y = start_y + len(modal)
        return rendered, [
            ClickZone(close_x, close_x + close_width - 1, close_y, "update_close", None)
        ]

    height = UPDATE_MODAL_HEIGHT
    total_height = height + 1
    if term_width < width or term_height < total_height:
        return _space_insufficient_overlay(term_width, term_height, close_label, "update_close")
    version = state.update_latest_version or "-"
    version = version if version.startswith("v") else f"v{version}"
    name = state.update_release_name or version
    note_rows = _release_note_rows(state.update_release_notes, inner)
    note_height = height - 6
    max_scroll = max(0, len(note_rows) - note_height)
    state.update_notes_scroll = min(max(0, state.update_notes_scroll), max_scroll)
    visible_notes = note_rows[state.update_notes_scroll:state.update_notes_scroll + note_height]
    visible_notes.extend([""] * (note_height - len(visible_notes)))
    position = t(
        "update.notes_scroll",
        current=state.update_notes_scroll + 1,
        total=max_scroll + 1,
    )
    body = [
        paint(fit_ansi(name, inner), "white", bold=True),
        section_rule(t("update.release_notes"), inner),
        *visible_notes,
        center_ansi(position, inner),
    ]
    modal = panel(
        t("update.release_title", version=version),
        [" " + fit_ansi(line, inner) + " " for line in body],
        width,
        height,
        "yellow",
    )
    install_label = t("update.install")
    gap = "   "
    action_line = paint(install_label, "yellow", bold=True, reverse=True) + gap + paint(
        close_label,
        "red",
        bold=True,
        reverse=True,
    )
    modal.append(center_ansi(action_line, width))

    rendered, start_x, start_y = _place_overlay(base_lines, modal, width, term_width, term_height)

    action_width = visible_width(install_label) + visible_width(gap) + visible_width(close_label)
    action_x = start_x + 1 + (width - action_width) // 2
    action_y = start_y + total_height
    install_width = visible_width(install_label)
    close_x = action_x + install_width + visible_width(gap)
    zones = [
        ClickZone(action_x, action_x + install_width - 1, action_y, "update_install", None),
        ClickZone(close_x, close_x + visible_width(close_label) - 1, action_y, "update_close", None),
    ]
    return rendered, zones


def render_sidebar(state: MonitorState, width: int, height: int, x_origin: int, zones: list[ClickZone]) -> list[str]:
    inner = max(8, width - 2)
    lines: list[str] = [paint("╭" + "─" * inner + "╮", "cyan")]
    if state.settings_mode != "normal":
        normalize_settings_state(state)

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

    def add_click_rows(
        text: str,
        color: str,
        kind: str,
        value: Any,
        *,
        selected: bool = False,
    ) -> int:
        rows = plain_wrap(text, inner)
        for row in rows:
            add_click_row(row, color, kind, value, selected=selected)
        return len(rows)

    def add_footer(
        buttons: tuple[
            tuple[str, str, str, Any] | None,
            tuple[str, str, str, Any] | None,
            tuple[str, str, str, Any] | None,
        ],
        rows: int,
        update_button: tuple[str, str, str, Any] | None = None,
    ) -> None:
        entries: list[tuple[str, str, str, Any] | str | None]
        full_entries = [buttons[0], "", buttons[1], "", buttons[2], "", "@JamesZhutheThird", APP_VERSION]
        tail: list[tuple[str, str, str, Any] | str | None] = ["@JamesZhutheThird", APP_VERSION]
        if update_button is not None:
            full_entries.extend(["", update_button])
            tail.extend(["", update_button])
        if rows >= len(full_entries):
            entries = full_entries
        else:
            if rows <= len(tail):
                entries = tail[-rows:] if rows else []
            else:
                head_rows = rows - len(tail)
                entries = list(buttons[:head_rows])
                entries.extend([""] * (head_rows - len(entries)))
                entries.extend(tail)
        for entry in entries:
            if isinstance(entry, tuple):
                add_click_row(*entry, selected=True)
            else:
                add_plain(entry or "")

    def add_navigation_row(
        previous_text: str,
        next_text: str,
        previous_enabled: bool,
        next_enabled: bool,
        previous_value: Any,
        next_value: Any,
        kind: str,
    ) -> None:
        y = len(lines) + 1
        previous_width = (inner - 1) // 2
        next_width = inner - previous_width - 1

        def button(text: str, button_width: int, enabled: bool) -> str:
            centered = center_ansi(text, button_width)
            if enabled:
                return paint(centered, "cyan", bold=True, reverse=True)
            return paint(centered, "dim")

        lines.append(
            paint("│", "cyan")
            + fit_ansi(button(previous_text, previous_width, previous_enabled), previous_width)
            + paint("│", "cyan")
            + fit_ansi(button(next_text, next_width, next_enabled), next_width)
            + paint("│", "cyan")
        )
        if previous_enabled:
            zones.append(
                ClickZone(
                    x_origin + 1,
                    x_origin + previous_width,
                    y,
                    kind,
                    previous_value,
                )
            )
        if next_enabled:
            zones.append(
                ClickZone(
                    x_origin + previous_width + 2,
                    x_origin + inner,
                    y,
                    kind,
                    next_value,
                )
            )

    def add_setting_title(title: str) -> int:
        rows = plain_wrap(title, inner)
        for row in rows:
            rendered = paint_on(center_text(row), "white", "dark_cyan", bold=True)
            lines.append(paint("│", "cyan") + fit_ansi(rendered, inner) + paint("│", "cyan"))
        return len(rows)

    def add_setting_item(index: int, key: str, title: str) -> int:
        focused = state.settings_mode == "items" and state.settings_focus == index
        rows = add_setting_title(title)
        current = setting_current_label(state, key)
        return rows + add_click_rows(
            current,
            "cyan" if focused else "green",
            "settings_item",
            index,
            selected=focused,
        )

    def setting_option_text(key: str, label: str, value: Any) -> str:
        return ("● " if value == setting_current_value(state, key) else "  ") + label

    def add_setting_option(key: str, option_index: int, label: str, value: Any) -> int:
        current = setting_current_value(state, key)
        focused = option_index == state.settings_option_focus
        color = "cyan" if focused else ("green" if value == current else "white")
        return add_click_rows(
            setting_option_text(key, label, value),
            color,
            "settings_option",
            (key, value),
            selected=focused,
        )

    if state.settings_mode != "normal":
        categories = setting_categories(state)
        roots = settings_root_entries(state)
        interior_rows = max(0, height - 2)
        footer_rows = min(8, interior_rows)
        settings_rows = interior_rows - footer_rows
        settings_end = 1 + settings_rows
        mode = state.settings_mode

        if mode == "categories":
            page_title = t("settings.types")
            entries = roots
            focus = state.settings_category_focus
            row_heights = [len(plain_wrap(title, inner)) for _key, title, _items in entries]
        elif mode == "items":
            _category_key, category_title, entries = categories[state.settings_category_focus]
            page_title = category_title
            keys = [item[0] for item in entries]
            focused_key = setting_items(state)[state.settings_focus][0]
            focus = keys.index(focused_key)
            row_heights = [
                len(plain_wrap(title, inner)) + len(plain_wrap(setting_current_label(state, key), inner))
                for key, title, _choices in entries
            ]
        else:
            _index, option_key, option_title, entries = focused_setting(state)
            page_title = option_title
            focus = state.settings_option_focus
            row_heights = [
                len(plain_wrap(setting_option_text(option_key, label, value), inner))
                for label, value in entries
            ]

        if len(lines) < settings_end:
            add_plain("CodexTOP", "cyan", bold=True)
        if len(lines) < settings_end:
            add_plain("")
        if len(lines) < settings_end:
            add_setting_title(page_title)
        if len(lines) < settings_end:
            add_plain("")
        if len(lines) < settings_end:
            add_plain("")

        pages, page_index = settings_page_layout(
            row_heights,
            focus,
            max(0, settings_end - len(lines)),
            gap=0 if mode == "options" else 1,
        )
        has_pages = len(pages) > 1
        if pages:
            start, end = pages[page_index]
            for index in range(start, end):
                if mode == "categories":
                    category_key, title, _category_items = entries[index]
                    color = "yellow" if category_key == SOFTWARE_VERSION_KEY else (
                        "cyan" if index == state.settings_category_focus else "white"
                    )
                    add_click_rows(
                        title,
                        color,
                        "settings_category",
                        index,
                        selected=index == state.settings_category_focus,
                    )
                elif mode == "items":
                    key, title, _choices = entries[index]
                    add_setting_item(setting_index_for_key(state, key), key, title)
                else:
                    label, value = entries[index]
                    add_setting_option(option_key, index, label, value)
                if mode != "options" and index + 1 < end:
                    add_plain("")

        before_navigation = settings_end - (1 if has_pages else 0)
        while len(lines) < before_navigation:
            add_plain("")
        if has_pages:
            previous_page = pages[max(0, page_index - 1)]
            next_page = pages[min(len(pages) - 1, page_index + 1)]

            def page_value(page: tuple[int, int]) -> tuple[str, Any]:
                target = page[0]
                if mode == "items":
                    target = entries[target][0]
                return mode, target

            add_navigation_row(
                t("nav.previous_page_short"),
                t("nav.next_page_short"),
                page_index > 0,
                page_index + 1 < len(pages),
                page_value(previous_page),
                page_value(next_page),
                "settings_page",
            )
        while len(lines) < settings_end:
            add_plain("")

        add_footer(
            (
                (t("settings.back"), "red", "settings_back", None),
                (t("settings.exit_mode"), "cyan", "settings_exit", None),
                None,
            ),
            footer_rows,
        )
        lines.append(paint("╰" + "─" * inner + "╯", "cyan"))
        return [fit_ansi(line, width) for line in lines[:height]]

    navigation = None
    chart_navigation = None
    records = getattr(state, "records", None) or []
    accounts = current_accounts(state, records)
    if accounts and state.display_scope == "list":
        heights, widths, last_page_start = dashboard_grid_layout(
            x_origin - 1,
            height,
            len(accounts),
            getattr(state, "dashboard_density", DEFAULT_DASHBOARD_DENSITY),
        )
        page_size = max(1, len(heights) * len(widths))
        state.summary_offset = min(
            max(0, state.summary_offset) // page_size * page_size,
            last_page_start,
        )
        if last_page_start:
            navigation = (
                t("nav.previous_page_short"),
                t("nav.next_page_short"),
                state.summary_offset > 0,
                state.summary_offset < last_page_start,
                max(0, state.summary_offset - page_size),
                min(last_page_start, state.summary_offset + page_size),
            )
    elif accounts and state.display_scope == "focus":
        active = focus_history_account(state, accounts, current_index(state, records))
        chart_index = accounts.index(active)
        chart_navigation = (
            t("nav.previous_chart_short"),
            t("nav.next_chart_short"),
            chart_index > 0,
            chart_index + 1 < len(accounts),
            account_index(accounts[max(0, chart_index - 1)]),
            account_index(accounts[min(len(accounts) - 1, chart_index + 1)]),
        )
        main_width = x_origin - 1
        left_width = min(50, max(20, main_width // 3))
        right_width = max(12, main_width - left_width)
        left_width = main_width - right_width
        _focus_heights, page_starts, page_index = focus_page_layout(
            accounts,
            left_width,
            height,
            state.curve_mode,
            state.window_scope,
            state.summary_offset,
        )
        state.summary_offset = page_starts[page_index]
        if len(page_starts) > 1:
            navigation = (
                t("nav.previous_page_short"),
                t("nav.next_page_short"),
                page_index > 0,
                page_index + 1 < len(page_starts),
                page_starts[max(0, page_index - 1)],
                page_starts[min(len(page_starts) - 1, page_index + 1)],
            )

    interior_rows = max(0, height - 2)
    footer_rows = min(10 if state.update_available else 8, interior_rows)
    content_end = 1 + interior_rows - footer_rows
    add_plain("CodexTOP", "cyan", bold=True)
    add_plain("")
    add_plain(t("settings.status"), "white", bold=True)
    add_plain(state.status, "yellow" if state.error else "green")
    if state.last_update:
        add_plain(datetime.fromtimestamp(state.last_update).strftime("%H:%M:%S"), "dim")
    if state.error:
        add_plain(plain_fit(state.error, inner), "red")
    else:
        remain = max(0, int(state.next_read - time.time()))
        add_plain(t("settings.next_read", seconds=remain), "dim")
    add_plain(sampler_status(state.log_path.parent), "dim")
    add_plain("")
    for key, title, _choices in setting_items(state):
        if key not in SIDEBAR_SETTING_KEYS:
            continue
        shortcut = "F1" if key == "period" else "F2"
        add_setting_item(setting_index_for_key(state, key), key, f"{shortcut} / {title}")
        add_plain("")

    navigation_rows = 2 if navigation and content_end - len(lines) >= 2 else 0
    chart_rows = 2 if chart_navigation and content_end - len(lines) - navigation_rows >= 2 else 0
    content_before_navigation = content_end - navigation_rows - chart_rows
    lines = lines[:content_before_navigation]
    zones[:] = [zone for zone in zones if zone.x1 < x_origin or zone.y <= content_before_navigation]
    while len(lines) < content_before_navigation:
        add_plain("")

    if chart_rows:
        add_navigation_row(*chart_navigation, "focus_chart")
        add_plain("")
    if navigation_rows:
        (
            previous_text,
            next_text,
            previous_enabled,
            next_enabled,
            previous_offset,
            next_offset,
        ) = navigation
        add_navigation_row(
            previous_text,
            next_text,
            previous_enabled,
            next_enabled,
            previous_offset,
            next_offset,
            "summary_scroll",
        )
        add_plain("")
    add_footer(
        (
            (t("statistics.sidebar"), "magenta", "statistics_open", None),
            (t("settings.mode"), "cyan", "settings_activate", None),
            (t("settings.exit"), "red", "exit", None),
        ),
        footer_rows,
        (update_available_label(), "yellow", "update_action", None) if state.update_available else None,
    )
    lines.append(paint("╰" + "─" * inner + "╯", "cyan"))
    return [fit_ansi(line, width) for line in lines[:height]]


def apply_setting_value(state: MonitorState, key: str, value: Any) -> None:
    if key == "language":
        language = set_language(str(value))
        if language != state.language:
            state.language = language
            state.status = t("status.language_changed")
            state.main_cache_key = None
            state.statistics_cache_key = None
    elif key == "period":
        period = str(value)
        if state.period != period:
            state.period = period
            state.next_read = 0.0
    elif key == "usage_directory_scope":
        scope = str(value)
        if state.usage_directory_scope != scope:
            state.usage_directory_scope = scope
            monitor = state.token_monitor
            if hasattr(monitor, "set_scope"):
                monitor.set_scope(scope)
            state.token_version += 1
            state.main_cache_key = None
    elif key == "usage_panel_layout":
        layout = str(value)
        if state.usage_panel_layout != layout:
            state.usage_panel_layout = layout
            state.main_cache_key = None
    elif key == "usage_axis_scale":
        scale = str(value)
        if state.usage_axis_scale != scale:
            state.usage_axis_scale = scale
            state.main_cache_key = None
    elif key == "dashboard_density":
        density = str(value)
        if getattr(state, "dashboard_density", DEFAULT_DASHBOARD_DENSITY) != density:
            state.dashboard_density = density
            state.summary_offset = 0
            state.main_cache_key = None
    elif key == "hide_account_info":
        hidden = bool(value)
        if getattr(state, "hide_account_info", DEFAULT_HIDE_ACCOUNT_INFO) != hidden:
            state.hide_account_info = hidden
            state.main_cache_key = None
    elif key == "curve_mode":
        state.curve_mode = str(value)
    elif key == "window_scope":
        state.window_scope = str(value)
    elif key == "merged_quota_mode":
        state.merged_quota_mode = str(value)
    elif key == "color_scheme":
        state.color_scheme = color_schemes.set_active_color_scheme(str(value))
    elif key == "display_scope":
        scope = str(value)
        if state.display_scope != scope:
            state.display_scope = scope
            state.summary_offset = 0
            state.main_cache_key = None
            if scope in {"list", "merged"}:
                try:
                    send_sampler_interval(state, state.interval, sample_now=True, all_auth=True)
                    state.status = t("status.requested_all_accounts")
                    state.error = None
                except Exception as exc:
                    state.status = t("common.command_failed")
                    state.error = str(exc)
    elif key == "interval":
        state.interval = int(value)
        state.next_read = 0.0
        try:
            send_sampler_interval(state, state.interval)
            state.status = t("status.interval_sent")
            state.error = None
        except Exception as exc:
            state.status = t("common.command_failed")
            state.error = str(exc)


def handle_setting_key(state: MonitorState, key: str) -> bool:
    if key == "f11" and state.update_available:
        return open_update_dialog(state)
    if key == "f9":
        if state.settings_mode != "normal":
            state.settings_mode = "normal"
            return True
        state.settings_mode = "categories"
        normalize_settings_state(state)
        return True
    if key == "backspace":
        back_settings(state)
        return True
    if state.settings_mode == "normal":
        if key in {"f1", "f2"}:
            quick_key = "period" if key == "f1" else "display_scope"
            state.settings_focus = setting_index_for_key(state, quick_key)
            open_focused_setting(state)
            return True
        if key == "f8":
            open_statistics(state)
        return True

    items = normalize_settings_state(state)
    if not items:
        return True
    if key in {"up", "down"}:
        step = -1 if key == "up" else 1
        if state.settings_mode == "categories":
            categories = settings_root_entries(state)
            state.settings_category_focus = min(
                max(0, state.settings_category_focus + step),
                len(categories) - 1,
            )
            normalize_settings_state(state)
        elif state.settings_mode == "options":
            choices = items[state.settings_focus][2]
            if choices:
                state.settings_option_focus = min(
                    max(0, state.settings_option_focus + step),
                    len(choices) - 1,
                )
        else:
            category_items = setting_categories(state)[state.settings_category_focus][2]
            category_keys = [item[0] for item in category_items]
            item_key = items[state.settings_focus][0]
            item_index = min(max(0, category_keys.index(item_key) + step), len(category_keys) - 1)
            item_key = category_keys[item_index]
            state.settings_focus = setting_index_for_key(state, item_key)
            state.settings_option_focus = setting_current_option_index(state, item_key)
        return True
    if key == "enter":
        if state.settings_mode == "categories":
            open_focused_category(state)
        elif state.settings_mode == "items":
            open_focused_setting(state)
        elif state.settings_mode == "options":
            _index, item_key, _title, choices = focused_setting(state)
            if choices:
                _label, value = choices[state.settings_option_focus % len(choices)]
                apply_setting_value(state, item_key, value)
            state.settings_mode = "normal" if item_key in SIDEBAR_SETTING_KEYS else "items"
            if state.settings_mode == "items":
                normalize_settings_state(state)
        return True
    return True


def handle_click(state: MonitorState, zones: list[ClickZone], x: int, y: int) -> bool:
    for zone in zones:
        if zone.y == y and zone.x1 <= x <= zone.x2:
            if zone.kind == "exit":
                return False
            if zone.kind == "version_close":
                close_version_dialog(state)
            elif zone.kind == "version_check":
                return start_manual_update_check(state)
            elif zone.kind == "update_close":
                close_update_dialog(state)
            elif zone.kind == "update_install":
                return request_update_install(state)
            elif zone.kind == "statistics_open":
                close_update_dialog(state)
                close_version_dialog(state)
                open_statistics(state)
            elif zone.kind == "statistics_close":
                close_statistics(state)
            elif zone.kind == "statistics_granularity":
                set_statistics_granularity(state, str(zone.value))
            elif zone.kind == "statistics_shift":
                shift_statistics(state, int(zone.value))
            elif zone.kind == "update_action":
                return open_update_dialog(state)
            elif zone.kind == "settings_activate":
                state.settings_mode = "categories"
                normalize_settings_state(state)
            elif zone.kind == "settings_back":
                back_settings(state)
            elif zone.kind == "settings_exit":
                state.settings_mode = "normal"
            elif zone.kind == "settings_category":
                state.settings_category_focus = int(zone.value)
                open_focused_category(state)
            elif zone.kind == "settings_item":
                state.settings_focus = int(zone.value)
                open_focused_setting(state)
            elif zone.kind == "settings_option":
                item_key, value = zone.value
                state.settings_focus = setting_index_for_key(state, str(item_key))
                apply_setting_value(state, str(item_key), value)
                state.settings_option_focus = setting_current_option_index(state, str(item_key))
                state.settings_mode = "normal" if item_key in SIDEBAR_SETTING_KEYS else "items"
                if state.settings_mode == "items":
                    normalize_settings_state(state)
            elif zone.kind == "settings_page":
                mode, value = zone.value
                if mode == "categories":
                    state.settings_category_focus = int(value)
                elif mode == "items":
                    state.settings_focus = setting_index_for_key(state, str(value))
                elif mode == "options":
                    state.settings_option_focus = int(value)
                normalize_settings_state(state)
            elif zone.kind in {
                "period",
                "usage_directory_scope",
                "usage_panel_layout",
                "usage_axis_scale",
                "dashboard_density",
                "hide_account_info",
                "curve_mode",
                "window_scope",
                "color_scheme",
                "language",
                "display_scope",
                "interval",
            }:
                apply_setting_value(state, zone.kind, zone.value)
            elif zone.kind == "summary_scroll":
                state.summary_offset = max(0, int(zone.value))
                state.main_cache_key = None
            elif zone.kind == "focus_chart":
                state.focus_account = zone.value
                state.main_cache_key = None
            return True
    return True
