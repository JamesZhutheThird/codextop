"""Period statistics and the modal summary shown over the dashboard."""

from __future__ import annotations

import bisect
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.i18n import t
from quota.plans import account_quota_multiplier
from quota.quota_format import (
    account_index,
    merged_available_resets,
    merged_reset_expiration_rows,
    merged_window_info,
    valid_quota_accounts,
    window_info,
)
from .history import HistorySeriesIndex
from .models import ClickZone, MonitorState
from .panels import panel
from .session_tokens import TokenUsageBreakdown, TrustedDirectoryTokenUsageMonitor
from .terminal_text import (
    center_ansi,
    char_width,
    fit_ansi,
    paint,
    section_rule,
    strip_ansi,
    visible_width,
)


STATISTICS_MODAL_WIDTH = 76
STATISTICS_MODAL_HEIGHT = 19
STATISTICS_AUTO_CLOSE_SECONDS = 60
STATISTICS_GRANULARITIES = ("day", "week", "month")


@dataclass(frozen=True, slots=True)
class QuotaAmount:
    percent: float | None
    multiple: float | None


@dataclass(frozen=True, slots=True)
class StatisticsPeriod:
    start: int
    end: int
    label: str


@dataclass(frozen=True, slots=True)
class StatisticsSummary:
    period: StatisticsPeriod
    quota_used: dict[str, QuotaAmount]
    system_tokens: int | None
    local_tokens: TokenUsageBreakdown | None
    current_quota: dict[str, QuotaAmount]
    reset_epochs: dict[str, int | None]
    reset_credits: int | None
    nearest_expiration_epoch: int | None


def local_today(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()


def statistics_period(
    anchor: date,
    granularity: str,
    week_start: int,
    tz_name: str,
) -> StatisticsPeriod:
    granularity = granularity if granularity in STATISTICS_GRANULARITIES else "day"
    if granularity == "week":
        start_date = anchor - timedelta(days=(anchor.weekday() - week_start) % 7)
        end_date = start_date + timedelta(days=7)
        label = f"{start_date.isoformat()} — {(end_date - timedelta(days=1)).isoformat()}"
    elif granularity == "month":
        start_date = anchor.replace(day=1)
        if start_date.month == 12:
            end_date = date(start_date.year + 1, 1, 1)
        else:
            end_date = date(start_date.year, start_date.month + 1, 1)
        label = start_date.strftime("%Y-%m")
    else:
        start_date = anchor
        end_date = anchor + timedelta(days=1)
        label = start_date.isoformat()
    tz = ZoneInfo(tz_name)
    start = int(datetime.combine(start_date, datetime.min.time(), tz).timestamp())
    end = int(datetime.combine(end_date, datetime.min.time(), tz).timestamp())
    return StatisticsPeriod(start, end, label)


def shifted_anchor(anchor: date, granularity: str, step: int) -> date:
    if granularity == "week":
        return anchor + timedelta(days=7 * step)
    if granularity == "month":
        month = anchor.month - 1 + step
        year = anchor.year + month // 12
        month = month % 12 + 1
        return date(year, month, 1)
    return anchor + timedelta(days=step)


def open_statistics(
    state: MonitorState,
    *,
    anchor: date | None = None,
    auto_close: bool = False,
    monotonic_now: float | None = None,
) -> None:
    state.statistics_open = True
    state.statistics_granularity = "day"
    state.statistics_anchor = (anchor or local_today(state.tz)).isoformat()
    state.statistics_auto_close_at = (
        (time.monotonic() if monotonic_now is None else monotonic_now) + STATISTICS_AUTO_CLOSE_SECONDS
        if auto_close
        else None
    )
    state.statistics_cache_key = None


def open_daily_statistics_if_due(
    state: MonitorState,
    *,
    today: date | None = None,
    monotonic_now: float | None = None,
) -> bool:
    observed = today or local_today(state.tz)
    if state.daily_summary_date == observed.isoformat():
        return False
    state.daily_summary_date = observed.isoformat()
    open_statistics(
        state,
        anchor=observed - timedelta(days=1),
        auto_close=True,
        monotonic_now=monotonic_now,
    )
    return True


def close_statistics(state: MonitorState) -> None:
    state.statistics_open = False
    state.statistics_auto_close_at = None


def close_expired_statistics(state: MonitorState, monotonic_now: float | None = None) -> bool:
    deadline = state.statistics_auto_close_at
    if not state.statistics_open or deadline is None:
        return False
    observed = time.monotonic() if monotonic_now is None else monotonic_now
    if observed < deadline:
        return False
    close_statistics(state)
    return True


def set_statistics_granularity(state: MonitorState, granularity: str) -> None:
    if granularity not in STATISTICS_GRANULARITIES:
        return
    state.statistics_granularity = granularity
    state.statistics_cache_key = None


def shift_statistics(state: MonitorState, step: int) -> None:
    try:
        anchor = date.fromisoformat(state.statistics_anchor or "")
    except ValueError:
        anchor = local_today(state.tz)
    state.statistics_anchor = shifted_anchor(anchor, state.statistics_granularity, step).isoformat()
    state.statistics_cache_key = None


def handle_statistics_key(state: MonitorState, key: str) -> None:
    if key in {"enter", "backspace"}:
        close_statistics(state)
    elif key == "left":
        shift_statistics(state, -1)
    elif key == "right":
        shift_statistics(state, 1)


def _quota_consumed(
    series: Any,
    start_timestamp: int,
    end_timestamp: int,
    fallback_multiplier: float,
) -> tuple[float, float, bool]:
    times = series.times
    values = series.values
    resets = series.resets
    first_in_range = bisect.bisect_left(times, start_timestamp)
    end = bisect.bisect_left(times, end_timestamp)
    if first_in_range >= end:
        return 0.0, 0.0, False
    previous = bisect.bisect_right(times, start_timestamp) - 1
    if previous < 0:
        previous = first_in_range
        first_in_range += 1
    else:
        first_in_range = max(first_in_range, previous + 1)
    percent = 0.0
    multiple = 0.0
    for position in range(first_in_range, end):
        before = float(values[previous])
        current = float(values[position])
        used = max(0.0, before - current)
        previous_reset = int(resets[previous])
        current_reset = int(resets[position])
        if (
            previous_reset >= 0
            and current_reset > previous_reset
            and int(times[position]) >= previous_reset - 5
        ):
            used = max(0.0, 100.0 - current)
        multiplier = series.multiplier_at(int(times[position]), fallback_multiplier)
        percent += used
        multiple += used / 100.0 * multiplier
        previous = position
    return percent, multiple, True


def _history_index(records: Any) -> HistorySeriesIndex | None:
    index = getattr(records, "series_index", None)
    if isinstance(index, HistorySeriesIndex):
        return index
    if not records:
        return None
    index = HistorySeriesIndex()
    for record in records:
        if isinstance(record, dict):
            index.append_record(record)
    return index


def build_statistics_summary(
    records: Any,
    accounts: list[dict[str, Any]],
    token_monitor: Any,
    period: StatisticsPeriod,
    *,
    local_tokens_ready: bool,
    window_scope: str = "both",
) -> StatisticsSummary:
    index = _history_index(records)
    weights = {
        str(account_index(account)): account_quota_multiplier(account)
        for account in accounts
        if isinstance(account, dict) and account_index(account) is not None
    }
    quota_used: dict[str, QuotaAmount] = {}
    for window in ("5h", "7d"):
        percent = 0.0
        multiple = 0.0
        observed = False
        points = index.merged_window(window) if index is not None else {}
        if window_scope == "auto":
            eligible = {
                str(account_index(account)) for account in accounts
                if isinstance(window_info(account, window, window_scope).get("left"), (int, float))
            }
            points = {account_id: series for account_id, series in points.items() if account_id in eligible}
        for account_id, series in points.items():
            used_percent, used_multiple, found = _quota_consumed(
                series,
                period.start,
                period.end,
                weights.get(str(account_id), 1.0),
            )
            percent += used_percent
            multiple += used_multiple
            observed = observed or found
        quota_used[window] = QuotaAmount(percent if observed else None, multiple if observed else None)

    system_tokens = index.system_token_increase(period.start, period.end) if index is not None else None
    local_tokens = None
    if local_tokens_ready and isinstance(token_monitor, TrustedDirectoryTokenUsageMonitor):
        local_tokens = token_monitor.usage_between(period.start, period.end, "all")

    current_quota: dict[str, QuotaAmount] = {}
    reset_epochs: dict[str, int | None] = {}
    for window in ("5h", "7d"):
        percent = merged_window_info(accounts, window, "percent", window_scope).get("left")
        multiple = merged_window_info(accounts, window, "actual", window_scope).get("left")
        current_quota[window] = QuotaAmount(
            float(percent) if isinstance(percent, (int, float)) else None,
            float(multiple) if isinstance(multiple, (int, float)) else None,
        )
        window_resets = [
            reset_epoch
            for account in valid_quota_accounts(accounts)
            for reset_epoch in [window_info(account, window, window_scope).get("reset_epoch")]
            if isinstance(reset_epoch, int)
        ]
        reset_epochs[window] = min(window_resets) if window_resets else None

    expirations = merged_reset_expiration_rows(accounts, 1)
    return StatisticsSummary(
        period,
        quota_used,
        system_tokens,
        local_tokens,
        current_quota,
        reset_epochs,
        merged_available_resets(accounts),
        expirations[0][0] if expirations else None,
    )


def summary_for_state(
    state: MonitorState,
    records: Any,
    accounts: list[dict[str, Any]],
    *,
    local_tokens_ready: bool,
) -> StatisticsSummary:
    try:
        anchor = date.fromisoformat(state.statistics_anchor or "")
    except ValueError:
        anchor = local_today(state.tz)
    period = statistics_period(
        anchor,
        state.statistics_granularity,
        state.statistics_week_start,
        state.tz,
    )
    key = (
        period.start,
        period.end,
        state.records_version,
        state.token_version,
        state.window_scope,
        local_tokens_ready,
    )
    if state.statistics_cache_key != key or not isinstance(state.statistics_cache, StatisticsSummary):
        state.statistics_cache = build_statistics_summary(
            records,
            accounts,
            state.token_monitor,
            period,
            local_tokens_ready=local_tokens_ready,
            window_scope=state.window_scope,
        )
        state.statistics_cache_key = key
    return state.statistics_cache


def _number(value: float | None, suffix: str) -> str:
    if value is None:
        return "-"
    rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{rendered}{suffix}"


def _tokens(value: int | None, *, increase: bool = False) -> str:
    if value is None:
        return "-"
    prefix = "+" if increase else ""
    return f"{prefix}{value:,}"


def _countdown_to(epoch: int | None) -> str:
    if epoch is None:
        return "-"
    remaining = max(0, epoch - int(time.time()))
    days, remainder = divmod(remaining, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    if days:
        return f"{days}d" + (f"{hours}h" if hours else "")
    if hours:
        return f"{hours}h" + (f"{minutes}m" if minutes else "")
    return f"{minutes}m"


def _amount_line(window: str, amount: QuotaAmount) -> str:
    return t(
        "statistics.quota_line",
        window=window,
        percent=_number(amount.percent, "%"),
        multiple=_number(amount.multiple, "x"),
    )


def _current_amount_line(window: str, amount: QuotaAmount, reset_epoch: int | None) -> str:
    return t(
        "statistics.current_quota_line",
        window=window,
        percent=_number(amount.percent, "%"),
        multiple=_number(amount.multiple, "x"),
        countdown=_countdown_to(reset_epoch),
    )


def _half_columns(left: str, right: str, width: int) -> str:
    left_width = width // 2
    return fit_ansi(left, left_width) + fit_ansi(right, width - left_width)


def _button_row(width: int, selected: str) -> tuple[str, list[tuple[int, int, str]]]:
    labels = [(granularity, t(f"statistics.granularity.{granularity}")) for granularity in STATISTICS_GRANULARITIES]
    gap = "   "
    total = sum(visible_width(label) for _key, label in labels) + visible_width(gap) * (len(labels) - 1)
    offset = max(0, (width - total) // 2)
    pieces = [" " * offset]
    zones: list[tuple[int, int, str]] = []
    cursor = offset
    for index, (key, label) in enumerate(labels):
        label_width = visible_width(label)
        pieces.append(paint(label, "cyan" if key == selected else "white", bold=True, reverse=key == selected))
        zones.append((cursor, cursor + label_width - 1, key))
        cursor += label_width
        if index != len(labels) - 1:
            pieces.append(gap)
            cursor += visible_width(gap)
    return fit_ansi("".join(pieces), width), zones


def _plain_columns(text: str, start: int, width: int) -> str:
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


def render_statistics_overlay(
    base_lines: list[str],
    state: MonitorState,
    records: Any,
    accounts: list[dict[str, Any]],
    term_width: int,
    term_height: int,
    *,
    local_tokens_ready: bool,
) -> tuple[list[str], list[ClickZone]]:
    summary = summary_for_state(state, records, accounts, local_tokens_ready=local_tokens_ready)
    width = STATISTICS_MODAL_WIDTH
    height = STATISTICS_MODAL_HEIGHT
    total_height = height + 1
    if term_width < width or term_height < total_height:
        message = paint(t("terminal.space_insufficient"), "red", bold=True)
        close_label = t("statistics.close")
        rows = [" " * term_width for _ in range(term_height)]
        middle = max(0, term_height // 2 - 1)
        rows[middle] = center_ansi(message, term_width)
        close_y = min(term_height, middle + 2)
        rows[close_y - 1] = center_ansi(paint(close_label, "red", bold=True, reverse=True), term_width)
        close_width = visible_width(close_label)
        close_x = max(1, (term_width - close_width) // 2 + 1)
        return rows, [ClickZone(close_x, close_x + close_width - 1, close_y, "statistics_close", None)]

    inner = width - 4
    nav_label = summary.period.label
    nav_gap = max(1, inner - 4 - visible_width(nav_label))
    left_gap = nav_gap // 2
    right_gap = nav_gap - left_gap
    body = [f"◀ {' ' * left_gap}{nav_label}{' ' * right_gap} ▶"]
    body.append(section_rule(t("statistics.quota_used"), inner))
    body.extend(
        _amount_line(window, summary.quota_used[window])
        for window in ("5h", "7d")
        if state.window_scope != "auto" or summary.quota_used[window].percent is not None
    )
    body.append(t("statistics.system_tokens", value=_tokens(summary.system_tokens, increase=True)))
    body.append("")
    body.append(section_rule(t("statistics.local_tokens"), inner))
    if summary.local_tokens is None:
        body.extend([t("statistics.loading"), "", ""])
    else:
        local = summary.local_tokens
        body.append(
            _half_columns(
                t(
                    "statistics.local_input",
                    value=_tokens(max(0, local.input_tokens - local.cached_input_tokens)),
                ),
                t("statistics.local_cached", value=_tokens(local.cached_input_tokens)),
                inner,
            )
        )
        body.append(
            _half_columns(
                t("statistics.local_output", value=_tokens(local.output_tokens)),
                t("statistics.local_reasoning", value=_tokens(local.reasoning_output_tokens)),
                inner,
            )
        )
        body.append(t("statistics.local_total", total=_tokens(local.total_tokens)))
    body.append("")
    body.append(section_rule(t("statistics.current"), inner))
    body.extend(
        _current_amount_line(window, summary.current_quota[window], summary.reset_epochs[window])
        for window in ("5h", "7d")
        if state.window_scope != "auto" or summary.current_quota[window].percent is not None
    )
    body.append(
        t(
            "statistics.reset_line",
            count=summary.reset_credits if summary.reset_credits is not None else "-",
            countdown=_countdown_to(summary.nearest_expiration_epoch),
        )
    )
    body.append("")
    granularity_row, granularity_zones = _button_row(inner, state.statistics_granularity)
    body.append(granularity_row)

    title = t(f"statistics.title.{state.statistics_granularity}")
    modal = panel(
        title,
        [" " + fit_ansi(line, inner) + " " for line in body],
        width,
        height,
        "magenta",
    )
    close_label = t("statistics.close")
    close_line = center_ansi(paint(close_label, "red", bold=True, reverse=True), width)
    modal.append(close_line)

    start_x = (term_width - width) // 2
    start_y = (term_height - total_height) // 2
    dimmed = [paint(strip_ansi(fit_ansi(line, term_width)), "dim") for line in base_lines[:term_height]]
    while len(dimmed) < term_height:
        dimmed.append(" " * term_width)
    for row, modal_line in enumerate(modal):
        target = start_y + row
        original = base_lines[target] if target < len(base_lines) else ""
        left = _plain_columns(original, 0, start_x)
        right_width = term_width - start_x - width
        right = _plain_columns(original, start_x + width, right_width)
        dimmed[target] = paint(left, "dim") + fit_ansi(modal_line, width) + paint(right, "dim")

    first_x = start_x + 1
    content_x = start_x + 3
    first_y = start_y + 1
    zones = [
        ClickZone(content_x, content_x + 1, first_y + 1, "statistics_shift", -1),
        ClickZone(content_x + inner - 2, content_x + inner - 1, first_y + 1, "statistics_shift", 1),
    ]
    button_y = first_y + len(body)
    for left, right, granularity in granularity_zones:
        zones.append(
            ClickZone(content_x + left, content_x + right, button_y, "statistics_granularity", granularity)
        )
    close_width = visible_width(close_label)
    close_x = first_x + (width - close_width) // 2
    zones.append(ClickZone(close_x, close_x + close_width - 1, first_y + height, "statistics_close", None))
    return [fit_ansi(line, term_width) for line in dimmed[:term_height]], zones
