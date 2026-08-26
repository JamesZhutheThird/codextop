"""Compose CodexTOP account, quota, and merged summary panels."""

from __future__ import annotations

import bisect
import time
from typing import Any

from .charts import *
from core.constants import *
from core.i18n import t
from quota.quota_format import *
from quota.plans import recorded_quota_multiplier
from .terminal_text import *

DASHBOARD_PANEL_WIDTH = {
    "lower": 30,
    "default": 60,
    "upper": 180,
}
DASHBOARD_PANEL_HEIGHT = {
    "lower": 20,
    "default": 40,
    "upper": 80,
}


def account_lines(
    account: dict[str, Any],
    records: list[dict[str, Any]],
    panel_width: int,
    panel_height: int,
    period: str,
    current: int | str | None,
    curve_mode: str,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
    hide_account_info: bool = DEFAULT_HIDE_ACCOUNT_INFO,
) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    index = account_index(account)
    lines: list[str] = []
    compact = panel_height <= 30

    error = account_error(account)
    def add_blank() -> None:
        lines.append("")

    def add_text(line: str) -> None:
        lines.append(" " + fit_ansi(line, max(1, inner_width - 2)).rstrip())

    def add_section(title: str) -> None:
        if not compact:
            add_blank()
        lines.append(section_rule(title, inner_width))
        if not compact:
            add_blank()

    if not compact:
        add_blank()
    email_label = t("account.email")
    plan_label = t("account.type")
    add_text(
        f"{paint(email_label, 'dim')} "
        f"{plain_fit(account_email(account, hide_account_info), inner_width - visible_width(email_label) - 3)}"
    )
    add_text(
        f"{paint(plan_label, 'dim')} "
        f"{plain_fit(account_plan(account), inner_width - visible_width(plan_label) - 3)}"
    )
    for line in token_usage_rows(account_lifetime_tokens(account), inner_width - 2):
        add_text(line)
    if error:
        add_section(t("common.error"))
        for error_line in plain_wrap(error, inner_width - 2):
            add_text(paint(error_line, "red"))
        return [fit_ansi(line, inner_width) for line in lines[:inner_height]]

    add_section(t("section.reset_credits"))
    for line in reset_rows(account, inner_width - 2):
        add_text(line)
    add_section(t("section.current_quota"))
    for line in quota_rows(
        account,
        inner_width - 2,
        compact=compact,
        curve_mode=curve_mode,
        window_scope=window_scope,
    ):
        if line:
            add_text(line)
        else:
            add_blank()
    add_section(t("section.quota_history"))

    chart_height = max(4, inner_height - len(lines))
    chart_width = max(8, inner_width - 2)
    if index is not None and records:
        chart = chart_lines(
            records,
            index,
            period,
            chart_width,
            chart_height,
            curve_mode,
            window_scope,
        )
    else:
        chart = chart_box([paint(t("common.no_history"), "dim")], chart_width, chart_height)
    detail_height = max(0, inner_height - len(chart))
    if len(lines) > detail_height:
        lines = lines[:max(0, detail_height - 1)]
        if detail_height:
            lines.append(section_rule(t("section.quota_history"), inner_width))
    lines.extend(" " + fit_ansi(line, chart_width) for line in chart)

    if len(lines) < inner_height:
        lines.extend([""] * (inner_height - len(lines)))
    return [fit_ansi(line, inner_width) for line in lines[:inner_height]]


def account_summary_body(
    account: dict[str, Any],
    inner_width: int,
    curve_mode: str = DEFAULT_CURVE_MODE,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
    hide_account_info: bool = DEFAULT_HIDE_ACCOUNT_INFO,
) -> list[str]:
    lines: list[str] = []

    def add_text(line: str) -> None:
        lines.append(" " + fit_ansi(line, max(1, inner_width - 2)).rstrip())

    email_label = t("account.email")
    plan_label = t("account.type")
    add_text(
        f"{paint(email_label, 'dim')} "
        f"{plain_fit(account_email(account, hide_account_info), inner_width - visible_width(email_label) - 3)}"
    )
    add_text(
        f"{paint(plan_label, 'dim')} "
        f"{plain_fit(account_plan(account), inner_width - visible_width(plan_label) - 3)}"
    )
    for line in token_usage_rows(account_lifetime_tokens(account), inner_width - 2):
        add_text(line)

    error = account_error(account)
    if error:
        lines.append(section_rule(t("common.error"), inner_width))
        for error_line in plain_wrap(error, inner_width - 2):
            add_text(paint(error_line, "red"))
    else:
        lines.append(section_rule(t("section.reset_credits"), inner_width))
        for line in reset_rows(account, inner_width - 2):
            add_text(line)
        lines.append(section_rule(t("section.current_quota"), inner_width))
        for line in quota_rows(
            account,
            inner_width - 2,
            compact=True,
            curve_mode=curve_mode,
            window_scope=window_scope,
        ):
            add_text(line)

    return [fit_ansi(line, inner_width) for line in lines]


def account_summary_lines(
    account: dict[str, Any],
    panel_width: int,
    panel_height: int,
    curve_mode: str = DEFAULT_CURVE_MODE,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
    hide_account_info: bool = DEFAULT_HIDE_ACCOUNT_INFO,
) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    lines = account_summary_body(
        account,
        inner_width,
        curve_mode,
        window_scope,
        hide_account_info,
    )

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
    window_scope: str = DEFAULT_WINDOW_SCOPE,
) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    chart_width = max(8, inner_width - 2)
    index = account_index(account)
    if index is None:
        chart = chart_box([paint(t("common.no_history"), "dim")], chart_width, inner_height)
    else:
        chart = chart_lines(
            records,
            index,
            period,
            chart_width,
            inner_height,
            curve_mode,
            window_scope,
        )
    lines = [" " + fit_ansi(line, chart_width) for line in chart]
    if len(lines) < inner_height:
        lines.extend([""] * (inner_height - len(lines)))
    return [fit_ansi(line, inner_width) for line in lines[:inner_height]]


def merged_account_reset_body(
    accounts: list[dict[str, Any]],
    current: int | str | None,
    inner_width: int,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
) -> list[str]:
    lines: list[str] = []

    def add_text(line: str) -> None:
        lines.append(" " + fit_ansi(line, max(1, inner_width - 2)).rstrip())

    plan_label = t("account.type")
    plan_width = max(1, inner_width - visible_width(plan_label) - 3)
    plan_lines: list[str] = []
    for part in merged_plan_text(accounts).split(" / "):
        candidate = f"{plan_lines[-1]} / {part}" if plan_lines else part
        if plan_lines and visible_width(candidate) <= plan_width:
            plan_lines[-1] = candidate
        else:
            plan_lines.extend(plain_wrap(part, plan_width))
    for line_index, line in enumerate(plan_lines):
        label = paint(plan_label, "dim") if line_index == 0 else " " * visible_width(plan_label)
        add_text(f"{label} {line}")
    for line in token_usage_rows(merged_lifetime_tokens(accounts), inner_width - 2):
        add_text(line)
    add_text(
        f"{paint(t('account.current'), 'dim')} "
        f"{current_account_quota_summary(accounts, current, window_scope)}"
    )
    errors = [
        (account, account_error(account))
        for account in accounts
        if isinstance(account, dict) and account_error(account)
    ]
    if errors:
        lines.append(section_rule(t("section.data_status"), inner_width))
        for account, error in errors:
            account_id = account_index(account)
            prefix = f"{account_id if account_id is not None else '-'}: "
            error_lines = plain_wrap(error, max(1, inner_width - 2 - visible_width(prefix)))
            for line_index, error_line in enumerate(error_lines):
                line_prefix = prefix if line_index == 0 else " " * visible_width(prefix)
                add_text(paint(f"{line_prefix}{error_line}", "red"))
        return [fit_ansi(line, inner_width) for line in lines]

    lines.append(section_rule(t("section.quota_resets"), inner_width))
    available = merged_available_resets(accounts)
    reset_label = t("section.reset_credits")
    reset_count = available if isinstance(available, int) else "-"
    add_text(f"{paint(reset_label, 'dim')} {t('quota.reset_total', count=reset_count)}")
    expirations = merged_reset_expiration_rows(accounts, 3)
    day_width = reset_credit_day_width(
        [expire_epoch - int(time.time()) for expire_epoch, _account_id, _title in expirations]
    )
    for rank in range(1, 4):
        if rank <= len(expirations):
            expire_epoch, account_id, _title = expirations[rank - 1]
            remaining = max(0, expire_epoch - int(time.time()))
            countdown_text = reset_credit_countdown(remaining, day_width)
            row = t("quota.expiration_from", rank=rank, account=account_id, countdown=countdown_text)
            if visible_width(row) > inner_width:
                row = t("quota.expiration_compact", rank=rank, account=account_id, countdown=countdown_text)
            lines.append(center_ansi(row, inner_width))
        else:
            lines.append(center_ansi(f"{rank}. -", inner_width))

    return [fit_ansi(line, inner_width) for line in lines]


def merged_account_reset_lines(
    accounts: list[dict[str, Any]],
    current: int | str | None,
    panel_width: int,
    panel_height: int,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    lines = merged_account_reset_body(accounts, current, inner_width, window_scope)
    if len(lines) < inner_height:
        lines.extend([""] * (inner_height - len(lines)))
    return [fit_ansi(line, inner_width) for line in lines[:inner_height]]


def merged_quota_summary_lines(
    accounts: list[dict[str, Any]],
    current: int | str | None,
    panel_width: int,
    panel_height: int,
    curve_mode: str = DEFAULT_CURVE_MODE,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
    merged_quota_mode: str = DEFAULT_MERGED_QUOTA_MODE,
) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    lines: list[str] = []

    def add_text(line: str) -> None:
        lines.append(" " + fit_ansi(line, max(1, inner_width - 2)).rstrip())

    quota_lines = merged_quota_rows(
        accounts,
        current,
        inner_width - 2,
        compact=True,
        curve_mode=curve_mode,
        window_scope=window_scope,
        merged_quota_mode=merged_quota_mode,
    )
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
        if not isinstance(timestamp, int) or not isinstance(accounts, (list, tuple)):
            continue
        for account in accounts:
            if not hasattr(account, "get") or account_error(account):
                continue
            index = account_index(account)
            if index is None:
                continue
            raw = account.get("q", {}).get(window)
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                left = raw[0]
                reset_epoch = raw[1]
            else:
                full_window = account.get("quota", {}).get(window, {})
                left = full_window.get("remaining_percent") if isinstance(full_window, dict) else None
                reset_after = full_window.get("reset_after_seconds") if isinstance(full_window, dict) else None
                reset_epoch = timestamp + int(reset_after) if isinstance(reset_after, (int, float)) else None
            if not isinstance(left, (int, float)):
                continue
            multiplier = recorded_quota_multiplier(account)
            account_points.setdefault(str(index), []).append(
                {
                    "t": timestamp,
                    "left": float(left),
                    "reset": reset_epoch if isinstance(reset_epoch, int) else None,
                    "multiplier": multiplier,
                }
            )
    return account_points


def historical_multiplier_at(points: Any, ts: int, fallback: float) -> float:
    if hasattr(points, "multiplier_at"):
        return points.multiplier_at(ts, fallback)
    position = bisect.bisect_right(points, ts, key=lambda point: point["t"]) - 1
    point = points[max(0, position)]
    multiplier = point.get("multiplier")
    return float(multiplier) if isinstance(multiplier, (int, float)) and multiplier > 0 else fallback


def maximum_historical_multiplier(points: Any, fallback: float) -> float:
    if hasattr(points, "maximum_multiplier"):
        return points.maximum_multiplier(fallback)
    return max(
        (
            float(point["multiplier"])
            if isinstance(point.get("multiplier"), (int, float)) and point["multiplier"] > 0
            else fallback
            for point in points
        ),
        default=fallback,
    )


def merged_value_at(
    account_points: dict[str, list[dict[str, Any]]],
    ts: int,
    _max_value: float,
    account_weights: dict[str, float] | None = None,
) -> tuple[float, bool]:
    total = 0.0
    found = False
    predicted = False
    for account_id, points in account_points.items():
        if not points:
            continue
        if hasattr(points, "first_timestamp"):
            first_timestamp = points.first_timestamp
        elif hasattr(points, "times"):
            first_timestamp = int(points.times[0])
        else:
            first_timestamp = points[0]["t"]
        if ts < first_timestamp:
            continue
        value, account_predicted = value_at(points, ts, 100.0)
        if account_weights is None:
            total += value
        else:
            fallback = account_weights.get(account_id, 1.0)
            total += value / 100.0 * historical_multiplier_at(points, ts, fallback)
        found = True
        predicted = predicted or account_predicted
    return total, predicted or not found


def merged_max_value(
    points: dict[str, dict[str, list[dict[str, Any]]]],
    account_weights: dict[str, float] | None = None,
) -> float:
    if account_weights is not None:
        return max(
            1.0,
            max(
                (
                    sum(
                        maximum_historical_multiplier(
                            account_points,
                            account_weights.get(account_id, 1.0),
                        )
                        for account_id, account_points in series.items()
                    )
                    for series in points.values()
                ),
                default=1.0,
            ),
        )
    account_count = max((len(series) for series in points.values()), default=1)
    return max(100.0, float(account_count * 100))


def merged_chart_lines(
    records: list[dict[str, Any]],
    period: str,
    width: int,
    height: int,
    curve_mode: str,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
    accounts: list[dict[str, Any]] | None = None,
    merged_quota_mode: str = DEFAULT_MERGED_QUOTA_MODE,
) -> list[str]:
    if not records:
        return [paint(t("common.no_history"), "dim")]
    series_index = getattr(records, "series_index", None)
    if series_index is not None:
        start_ts, end_ts = period_bounds(records, period)
        context_timestamp = period_context_timestamp(records, period, start_ts)
        points = {
            key: series_index.merged_window(key, context_timestamp)
            for key in window_keys(window_scope)
        }
    else:
        relevant, start_ts, end_ts = records_for_period(records, period)
        points = {
            key: merged_account_window_points(relevant, key)
            for key in window_keys(window_scope)
        }
    account_weights = None
    if merged_quota_mode == "actual":
        account_weights = {
            str(index): account_quota_multiplier(account)
            for account in accounts or []
            if isinstance(account, dict)
            for index in [account_index(account)]
            if index is not None
        }
    max_value = merged_max_value(points, account_weights)

    def weighted_value_at(series: Any, ts: int, maximum: float) -> tuple[float, bool]:
        return merged_value_at(
            series,
            ts,
            maximum,
            account_weights or {},
        )

    value_getter = weighted_value_at if account_weights is not None else merged_value_at
    return series_chart_lines(
        points,
        start_ts,
        end_ts,
        width,
        height,
        curve_mode,
        max_value,
        value_getter,
        "x" if account_weights is not None else "",
        account_weights is not None,
    )


def merged_history_lines(
    records: list[dict[str, Any]],
    panel_width: int,
    panel_height: int,
    period: str,
    curve_mode: str,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
    accounts: list[dict[str, Any]] | None = None,
    merged_quota_mode: str = DEFAULT_MERGED_QUOTA_MODE,
) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    chart_width = max(8, inner_width - 2)
    chart = merged_chart_lines(
        records,
        period,
        chart_width,
        inner_height,
        curve_mode,
        window_scope,
        accounts,
        merged_quota_mode,
    )
    lines = [" " + fit_ansi(line, chart_width) for line in chart]
    if len(lines) < inner_height:
        lines.extend([""] * (inner_height - len(lines)))
    return [fit_ansi(line, inner_width) for line in lines[:inner_height]]


def border_color(
    account: dict[str, Any],
    current: int | str | None,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
) -> str:
    if account_error(account):
        return "dim"
    for key in window_keys(window_scope):
        left = window_info(account, key).get("left")
        if isinstance(left, (int, float)):
            return percent_color(left)
    return "dim"


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


def _even_sizes(total: int, count: int) -> list[int]:
    base, extra = divmod(total, count)
    return [base + (index < extra) for index in range(count)]


def dashboard_grid_layout(
    width: int,
    height: int,
    account_count: int,
    density: str = DEFAULT_DASHBOARD_DENSITY,
) -> tuple[list[int], list[int], int]:
    lower_width = DASHBOARD_PANEL_WIDTH["lower"]
    default_width = DASHBOARD_PANEL_WIDTH["default"]
    upper_width = DASHBOARD_PANEL_WIDTH["upper"]
    lower_height = DASHBOARD_PANEL_HEIGHT["lower"]
    default_height = DASHBOARD_PANEL_HEIGHT["default"]
    upper_height = DASHBOARD_PANEL_HEIGHT["upper"]

    def lower_count(total: int, lower: int) -> int:
        return max(1, total // lower)

    def default_count(total: int, default: int) -> int:
        return max(1, (total + default // 2) // default)

    def upper_count(total: int, upper: int) -> int:
        return max(1, (total + upper - 1) // upper)

    if density == "compact":
        columns = lower_count(width, lower_width)
        rows = lower_count(height, lower_height)
    elif density == "relaxed":
        columns = upper_count(width, upper_width)
        rows = upper_count(height, upper_height)
    else:
        columns = default_count(width, default_width)
        rows = default_count(height, default_height)

    if density == "fill":
        min_columns = upper_count(width, upper_width)
        min_rows = upper_count(height, upper_height)
        max_columns = lower_count(width, lower_width)
        max_rows = lower_count(height, lower_height)
        if account_count <= max_columns * max_rows:
            candidates = [
                (candidate_rows, candidate_columns)
                for candidate_rows in range(min_rows, max_rows + 1)
                for candidate_columns in range(min_columns, max_columns + 1)
                if candidate_rows * candidate_columns >= account_count
            ]

            def fill_score(shape: tuple[int, int]) -> tuple[int, float]:
                candidate_rows, candidate_columns = shape
                empty_slots = candidate_rows * candidate_columns - account_count
                size_distance = (
                    abs(width / candidate_columns - default_width) / default_width
                    + abs(height / candidate_rows - default_height) / default_height
                )
                return -empty_slots, -size_distance

            rows, columns = max(candidates, key=fill_score)
    widths = _even_sizes(width, columns)
    heights = _even_sizes(height, rows)
    page_size = rows * columns
    last_page_start = max(0, (account_count - 1) // page_size * page_size)
    return heights, widths, last_page_start


def focus_page_layout(
    accounts: list[dict[str, Any]],
    width: int,
    height: int,
    curve_mode: str,
    window_scope: str,
    offset: int = 0,
) -> tuple[list[int], list[int], int]:
    inner_width = max(10, width - 2)
    panel_heights = [
        max(4, len(account_summary_body(account, inner_width, curve_mode, window_scope)) + 2)
        for account in accounts
    ]
    if not panel_heights:
        return [], [], 0

    page_starts = [0]
    used_height = 0
    for position, panel_height in enumerate(panel_heights):
        if used_height and used_height + panel_height > height:
            page_starts.append(position)
            used_height = 0
        used_height += panel_height
    page_index = max(0, bisect.bisect_right(page_starts, max(0, offset)) - 1)
    return panel_heights, page_starts, page_index


def current_account_border_color(
    accounts: list[dict[str, Any]],
    current: int | str | None,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
) -> str:
    if not accounts:
        return "dim"
    return border_color(selected_account(accounts, current), current, window_scope)


def stacked_summary_panels(
    accounts: list[dict[str, Any]],
    current: int | str | None,
    width: int,
    height: int,
    offset: int,
    curve_mode: str,
    window_scope: str,
    hide_account_info: bool = DEFAULT_HIDE_ACCOUNT_INFO,
) -> list[str]:
    if not accounts:
        return [" " * width for _ in range(height)]
    rows: list[str] = []
    panel_heights, page_starts, page_index = focus_page_layout(
        accounts,
        width,
        height,
        curve_mode,
        window_scope,
        offset,
    )
    start = page_starts[page_index]
    stop = page_starts[page_index + 1] if page_index + 1 < len(page_starts) else len(accounts)

    for account_position in range(start, stop):
        panel_height = panel_heights[account_position]
        if len(rows) + panel_height > height:
            break
        account = accounts[account_position]
        title = provider_name(account)
        if current is not None and account_index(account) == current:
            title = f"【{title}】"
        body = account_summary_lines(
            account,
            width,
            panel_height,
            curve_mode,
            window_scope,
            hide_account_info,
        )
        rows.extend(
            panel(
                title,
                body,
                width,
                panel_height,
                border_color(account, current, window_scope),
            )
        )
    rows.extend([" " * width for _ in range(height - len(rows))])
    return rows
