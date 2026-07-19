"""Compose CodexTOP account, quota, and merged summary panels."""

from __future__ import annotations

import time
from typing import Any

from .charts import *
from core.constants import *
from .models import ClickZone
from quota.quota_format import *
from .terminal_text import *

def account_lines(
    account: dict[str, Any],
    records: list[dict[str, Any]],
    panel_width: int,
    panel_height: int,
    period: str,
    current: int | str | None,
    curve_mode: str,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
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
    add_text(f"{paint('账号邮箱', 'dim')} {plain_fit(account_email(account), inner_width - 11)}")
    add_text(f"{paint('账号类型', 'dim')} {plain_fit(account_plan(account), inner_width - 11)}")
    for line in token_usage_rows(account_lifetime_tokens(account), inner_width - 2):
        add_text(line)
    if error:
        add_section("错误")
        add_text(paint(plain_fit(error, inner_width - 2), "red"))
        return [fit_ansi(line, inner_width) for line in lines[:inner_height]]

    add_section("重置次数")
    for line in reset_rows(account, inner_width - 2):
        add_text(line)
    add_section("当前额度")
    for line in quota_rows(
        account,
        inner_width - 2,
        curve_mode=curve_mode,
        window_scope=window_scope,
    ):
        if line:
            add_text(line)
        else:
            add_blank()
    add_section("额度历史")

    chart_height = max(4, inner_height - len(lines))
    chart_width = max(8, inner_width - 2)
    if index is not None:
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
        chart = chart_box([paint("暂无历史数据", "dim")], chart_width, chart_height)
    lines.extend(" " + fit_ansi(line, chart_width) for line in chart)

    if len(lines) < inner_height:
        lines.extend([""] * (inner_height - len(lines)))
    return [fit_ansi(line, inner_width) for line in lines[:inner_height]]


def account_summary_body(
    account: dict[str, Any],
    inner_width: int,
    curve_mode: str = DEFAULT_CURVE_MODE,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
) -> list[str]:
    lines: list[str] = []

    def add_text(line: str) -> None:
        lines.append(" " + fit_ansi(line, max(1, inner_width - 2)).rstrip())

    add_text(f"{paint('账号邮箱', 'dim')} {plain_fit(account_email(account), inner_width - 11)}")
    add_text(f"{paint('账号类型', 'dim')} {plain_fit(account_plan(account), inner_width - 11)}")
    for line in token_usage_rows(account_lifetime_tokens(account), inner_width - 2):
        add_text(line)

    error = account_error(account)
    if error:
        lines.append(section_rule("错误", inner_width))
        add_text(paint(plain_fit(error, inner_width - 2), "red"))
    else:
        lines.append(section_rule("重置次数", inner_width))
        for line in reset_rows(account, inner_width - 2):
            add_text(line)
        lines.append(section_rule("当前额度", inner_width))
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
) -> list[str]:
    inner_width = max(10, panel_width - 2)
    inner_height = max(4, panel_height - 2)
    lines = account_summary_body(account, inner_width, curve_mode, window_scope)

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
        chart = chart_box([paint("暂无历史数据", "dim")], chart_width, inner_height)
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

    add_text(f"{paint('账号类型', 'dim')} {plain_fit(merged_plan_text(accounts), inner_width - 10)}")
    for line in token_usage_rows(merged_lifetime_tokens(accounts), inner_width - 2):
        add_text(line)
    add_text(
        f"{paint('当前账号', 'dim')} "
        f"{current_account_quota_summary(accounts, current, window_scope)}"
    )
    lines.append(section_rule("额度重置", inner_width))
    available = merged_available_resets(accounts)
    add_text(f"{paint('重置次数', 'dim')} 合计 {available if isinstance(available, int) else '-'} 次可用")
    expirations = merged_reset_expiration_rows(accounts, 3)
    day_width = reset_credit_day_width(
        [expire_epoch - int(time.time()) for expire_epoch, _account_id, _title in expirations]
    )
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
    window_scope: str = DEFAULT_WINDOW_SCOPE,
) -> list[str]:
    if not records:
        return [paint("暂无历史数据", "dim")]
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
    max_value = merged_max_value(points)
    return series_chart_lines(
        points,
        start_ts,
        end_ts,
        width,
        height,
        curve_mode,
        max_value,
        merged_value_at,
    )


def merged_history_lines(
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
    chart = merged_chart_lines(
        records,
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


def merged_border_color(
    accounts: list[dict[str, Any]],
    window_scope: str = DEFAULT_WINDOW_SCOPE,
) -> str:
    for key in window_keys(window_scope):
        info = merged_window_info(accounts, key)
        ratio = merged_ratio_percent(info.get("left"), info.get("max_left"))
        if ratio is not None:
            return percent_color(ratio)
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


def stacked_summary_panels(
    accounts: list[dict[str, Any]],
    current: int | str | None,
    width: int,
    height: int,
    offset: int,
    curve_mode: str,
    window_scope: str,
    zones: list[ClickZone],
    x_origin: int,
) -> list[str]:
    if not accounts:
        return [" " * width for _ in range(height)]
    rows: list[str] = []
    inner_width = max(10, width - 2)

    panel_heights = [
        max(4, len(account_summary_body(account, inner_width, curve_mode, window_scope)) + 2)
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
        panel_height = max(
            4,
            len(account_summary_body(account, inner_width, curve_mode, window_scope)) + 2,
        )
        title = provider_name(account)
        if current is not None and account_index(account) == current:
            title = f"【{title}】"
        body = account_summary_lines(
            account,
            width,
            panel_height,
            curve_mode,
            window_scope,
        )
        rendered = panel(
            title,
            body,
            width,
            panel_height,
            border_color(account, current, window_scope),
        )
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
