"""Render the current project Token usage as a dedicated full-size chart."""

from __future__ import annotations

import bisect
import math
import time
from typing import Any

from core.constants import (
    BAR_LEGEND_MARKER,
    BRAILLE_DOT_BITS,
    BRAILLE_LEGEND_MARKER,
    PERIOD_SECONDS,
    TOKEN_RATE_HOURLY_THRESHOLD_SECONDS,
)
from core.i18n import t
from quota.quota_format import compact_token_count
from . import color_schemes
from .history import metric_value_at
from .terminal_text import compact_ansi, fit_ansi, paint, time_axis_line, visible_width


TOKEN_KEYS = ("input", "cached", "output", "total")
TOKEN_MARKERS = {
    "input": "●",
    "cached": "◆",
    "output": "▲",
    "total": "■",
}
TOKEN_PLOT_KEYS = ("output", "input", "cached", "total")
TOKEN_STACK_KEYS = TOKEN_PLOT_KEYS[:-1]
TOKEN_PRIORITIES = {key: index for index, key in enumerate(TOKEN_PLOT_KEYS, 1)}
SPLIT_FULL_AXIS_MIN_HEIGHT = 11
LOG_AXIS_MIN_EXPONENT = 6


def token_label(key: str) -> str:
    return t(f"token.{key}")


def token_rate_unit(seconds_per_column: float) -> tuple[int, str]:
    if seconds_per_column > TOKEN_RATE_HOURLY_THRESHOLD_SECONDS:
        return 3600, "tok/h"
    return 60, "tok/min"


def scientific_axis_max(value: float) -> tuple[float, int, float]:
    if not isinstance(value, (int, float)) or value <= 0:
        return 2.5, 0, 2.5
    exponent = math.floor(math.log10(value))
    base = float(10 ** exponent)
    coefficient = value / base
    tick_max = next(candidate for candidate in (2.5, 5.0, 10.0) if coefficient <= candidate)
    return tick_max * base, exponent, tick_max


def scientific_tick_label(value: float) -> str:
    if value == 0:
        return "0"
    if value < 1:
        return f"{value:g}".lstrip("0")
    return f"{value:g}"


def logarithmic_axis_exponent(value: float) -> int:
    """Return the smallest power-of-ten ceiling, with a 10^6 minimum."""

    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 1:
        return LOG_AXIS_MIN_EXPONENT
    return max(LOG_AXIS_MIN_EXPONENT, math.ceil(math.log10(value)))


def _logarithmic_axis_width(exponent: int) -> int:
    return max(4, len(f"10^{exponent}") + 1)


def _axis_ratio(value: float, axis_max: float, log_exponent: int | None = None) -> float:
    if log_exponent is not None:
        if value <= 1:
            return 0.0
        return min(1.0, max(0.0, math.log10(value) / log_exponent))
    if axis_max <= 0:
        return 0.0
    return min(1.0, max(0.0, value / axis_max))


def _logarithmic_tick_exponents(exponent: int, chart_height: int) -> tuple[int, ...]:
    tick_count = min(exponent + 1, max(2, chart_height))
    values = {
        round(index * exponent / max(1, tick_count - 1))
        for index in range(tick_count)
    }
    values.update((0, exponent))
    return tuple(sorted(values))


def _logarithmic_tick_rows(
    chart_height: int,
    exponent: int,
    tick_exponents: tuple[int, ...] | None = None,
) -> dict[int, str]:
    if tick_exponents is None:
        tick_exponents = _logarithmic_tick_exponents(exponent, chart_height)
    return {
        round((exponent - tick_exponent) / exponent * (chart_height - 1)): f"10^{tick_exponent}"
        for tick_exponent in tick_exponents
    }


def token_chart_bounds(rate_series: dict[str, list[dict[str, float]]], period: str) -> tuple[int, int]:
    end_ts = int(time.time())
    if period != "all":
        return end_ts - int(PERIOD_SECONDS[period] or 0), end_ts
    timestamps = [
        int(points[0]["t"])
        for points in rate_series.values()
        if points
    ]
    return (min(timestamps) if timestamps else end_ts - 300), end_ts


def _usage_values(latest: Any) -> dict[str, int]:
    if latest is None or not hasattr(latest, "chart_values"):
        return {key: 0 for key in TOKEN_KEYS}
    values = latest.chart_values()
    return {
        key: int(values.get(key, 0)) if isinstance(values.get(key), (int, float)) else 0
        for key in TOKEN_KEYS
    }


def token_legend(
    rate_series: dict[str, list[dict[str, float]]],
    latest: Any,
    timestamp: int,
    factor: int,
    width: int,
    curve_mode: str,
    colors: dict[str, str],
) -> str:
    totals = _usage_values(latest)
    pieces = []
    for key in TOKEN_KEYS:
        if curve_mode == "bar" and key in TOKEN_STACK_KEYS:
            marker = BAR_LEGEND_MARKER
        elif curve_mode == "fine_bar" and key in TOKEN_STACK_KEYS:
            marker = BRAILLE_LEGEND_MARKER
        else:
            marker = TOKEN_MARKERS[key]
        current = _series_value(rate_series.get(key, []), timestamp, factor)
        pieces.append(
            paint(f"{marker} {token_label(key)}", colors[key], bold=True)
            + f" {compact_token_count(current)} ({compact_token_count(totals[key])})"
        )
    return fit_ansi("  ".join(pieces), width)


def _series_value(points: list[dict[str, float]], timestamp: int, factor: int) -> float:
    value, _predicted = metric_value_at(points, timestamp, 0.0)
    return max(0.0, float(value) * factor)


def _visible_max(
    rate_series: dict[str, list[dict[str, float]]],
    start_ts: int,
    end_ts: int,
    plot_width: int,
    factor: int,
    stacked: bool,
) -> float:
    maximum = 0.0
    for column in range(plot_width):
        ratio = column / max(1, plot_width - 1)
        timestamp = int(start_ts + (end_ts - start_ts) * ratio)
        if stacked:
            value = sum(_series_value(rate_series.get(key, []), timestamp, factor) for key in TOKEN_STACK_KEYS)
            maximum = max(maximum, value)
        else:
            for key in TOKEN_KEYS:
                maximum = max(maximum, _series_value(rate_series.get(key, []), timestamp, factor))
    return maximum


def _visible_series_max(
    points: list[dict[str, float]],
    start_ts: int,
    end_ts: int,
    factor: int,
) -> float:
    maximum = _series_value(points, start_ts, factor)
    first = bisect.bisect_left(points, start_ts, key=lambda point: point["t"])
    last = bisect.bisect_right(points, end_ts, key=lambda point: point["t"])
    for point in points[first:last]:
        maximum = max(maximum, max(0.0, float(point["value"]) * factor))
    return maximum


def _shared_logarithmic_axis(
    rate_series: dict[str, list[dict[str, float]]],
    start_ts: int,
    end_ts: int,
    widths: list[int],
) -> tuple[int, int, str]:
    exponent = LOG_AXIS_MIN_EXPONENT
    factor, unit = 60, "tok/min"
    for _attempt in range(4):
        axis_width = _logarithmic_axis_width(exponent)
        plot_widths = [max(2, width - axis_width - 4) for width in widths]
        reference_width = min(plot_widths)
        seconds_per_column = max(0, end_ts - start_ts) / max(1, reference_width)
        factor, unit = token_rate_unit(seconds_per_column)
        maximum = _visible_series_max(
            rate_series.get("total", []),
            start_ts,
            end_ts,
            factor,
        )
        next_exponent = logarithmic_axis_exponent(maximum)
        if next_exponent == exponent:
            break
        exponent = next_exponent
    return exponent, factor, unit


def _box_segment_char(previous_row: int, row: int, filled_row: int) -> str:
    if row == previous_row:
        return "─"
    if filled_row == previous_row:
        return "╮" if row > previous_row else "╯"
    if filled_row == row:
        return "╰" if row > previous_row else "╭"
    return "│"


def _independent_grid(
    rate_series: dict[str, list[dict[str, float]]],
    start_ts: int,
    end_ts: int,
    plot_width: int,
    chart_height: int,
    axis_max: float,
    factor: int,
    curve_mode: str,
    colors: dict[str, str],
    log_exponent: int | None = None,
) -> list[str]:
    grid: list[list[tuple[str, str, int]]] = [
        [(" ", "dim", 0) for _ in range(plot_width)]
        for _ in range(chart_height)
    ]

    def put(row: int, column: int, char: str, color: str, priority: int) -> None:
        if not (0 <= row < chart_height and 0 <= column < plot_width):
            return
        old_char, _old_color, old_priority = grid[row][column]
        if old_char == " " or priority >= old_priority:
            grid[row][column] = (char, color, priority)

    for key in TOKEN_PLOT_KEYS:
        points = rate_series.get(key, [])
        if not points:
            continue
        previous_row: int | None = None
        for column in range(plot_width):
            ratio = column / max(1, plot_width - 1)
            timestamp = int(start_ts + (end_ts - start_ts) * ratio)
            value = _series_value(points, timestamp, factor)
            row = round((1.0 - _axis_ratio(value, axis_max, log_exponent)) * (chart_height - 1))
            priority = TOKEN_PRIORITIES[key]
            if curve_mode == "points":
                put(row, column, TOKEN_MARKERS[key], colors[key], priority)
            elif previous_row is None:
                put(row, column, "─" if curve_mode == "box" else TOKEN_MARKERS[key], colors[key], priority)
            elif row == previous_row:
                put(row, column, "─" if curve_mode == "box" else TOKEN_MARKERS[key], colors[key], priority)
            else:
                step = 1 if row > previous_row else -1
                for filled_row in range(previous_row, row + step, step):
                    char = (
                        _box_segment_char(previous_row, row, filled_row)
                        if curve_mode == "box"
                        else TOKEN_MARKERS[key]
                    )
                    put(filled_row, column, char, colors[key], priority)
            previous_row = row
    return [
        "".join(paint(char, color) if char != " " else " " for char, color, _priority in row)
        for row in grid
    ]


def _braille_grid(
    rate_series: dict[str, list[dict[str, float]]],
    start_ts: int,
    end_ts: int,
    plot_width: int,
    chart_height: int,
    axis_max: float,
    factor: int,
    colors: dict[str, str],
    log_exponent: int | None = None,
) -> list[str]:
    dot_width = plot_width * 2
    dot_height = chart_height * 4
    grid: list[list[tuple[int, str, int]]] = [
        [(0, "dim", 0) for _ in range(plot_width)]
        for _ in range(chart_height)
    ]

    def put(dot_row: int, dot_column: int, color: str, priority: int) -> None:
        if not (0 <= dot_row < dot_height and 0 <= dot_column < dot_width):
            return
        row = dot_row // 4
        column = dot_column // 2
        bit = BRAILLE_DOT_BITS[(dot_column % 2, dot_row % 4)]
        old_mask, old_color, old_priority = grid[row][column]
        grid[row][column] = (
            old_mask | bit,
            color if priority >= old_priority else old_color,
            max(priority, old_priority),
        )

    for key in TOKEN_PLOT_KEYS:
        points = rate_series.get(key, [])
        if not points:
            continue
        previous: tuple[int, int] | None = None
        for dot_column in range(dot_width):
            ratio = dot_column / max(1, dot_width - 1)
            timestamp = int(start_ts + (end_ts - start_ts) * ratio)
            value = _series_value(points, timestamp, factor)
            dot_row = round((1.0 - _axis_ratio(value, axis_max, log_exponent)) * (dot_height - 1))
            if previous is None:
                put(dot_row, dot_column, colors[key], TOKEN_PRIORITIES[key])
            else:
                previous_column, previous_row = previous
                steps = max(abs(dot_column - previous_column), abs(dot_row - previous_row), 1)
                for step_index in range(1, steps + 1):
                    step_ratio = step_index / steps
                    put(
                        round(previous_row + (dot_row - previous_row) * step_ratio),
                        round(previous_column + (dot_column - previous_column) * step_ratio),
                        colors[key],
                        TOKEN_PRIORITIES[key],
                    )
            previous = dot_column, dot_row
    return [
        "".join(paint(chr(0x2800 + mask), color) if mask else " " for mask, color, _priority in row)
        for row in grid
    ]


def _bar_grid(
    rate_series: dict[str, list[dict[str, float]]],
    start_ts: int,
    end_ts: int,
    plot_width: int,
    chart_height: int,
    axis_max: float,
    factor: int,
    colors: dict[str, str],
    keys: tuple[str, ...],
    log_exponent: int | None = None,
) -> list[str]:
    grid: list[list[tuple[str, str]]] = [
        [(" ", "dim") for _ in range(plot_width)]
        for _ in range(chart_height)
    ]
    for column in range(plot_width):
        ratio = column / max(1, plot_width - 1)
        timestamp = int(start_ts + (end_ts - start_ts) * ratio)
        values = {
            key: _series_value(rate_series.get(key, []), timestamp, factor)
            for key in keys
        }
        cumulative = 0.0
        for key in keys:
            start = cumulative
            cumulative += values[key]
            if cumulative <= start:
                continue
            top_row = round(
                (1.0 - _axis_ratio(cumulative, axis_max, log_exponent)) * (chart_height - 1)
            )
            bottom_row = round(
                (1.0 - _axis_ratio(start, axis_max, log_exponent)) * (chart_height - 1)
            )
            for row in range(top_row, bottom_row + 1):
                grid[row][column] = ("█", colors[key])
    return [
        "".join(paint(char, color) if char != " " else " " for char, color in row)
        for row in grid
    ]


def _fine_bar_grid(
    rate_series: dict[str, list[dict[str, float]]],
    start_ts: int,
    end_ts: int,
    plot_width: int,
    chart_height: int,
    axis_max: float,
    factor: int,
    colors: dict[str, str],
    keys: tuple[str, ...],
    log_exponent: int | None = None,
) -> list[str]:
    dot_width = plot_width * 2
    dot_height = chart_height * 4
    grid: list[list[tuple[int, str, int]]] = [
        [(0, "dim", dot_height) for _ in range(plot_width)]
        for _ in range(chart_height)
    ]

    def put_dot(dot_row: int, dot_column: int, color: str) -> None:
        if not (0 <= dot_row < dot_height and 0 <= dot_column < dot_width):
            return
        row = dot_row // 4
        column = dot_column // 2
        bit = BRAILLE_DOT_BITS[(dot_column % 2, dot_row % 4)]
        old_mask, old_color, old_color_row = grid[row][column]
        grid[row][column] = (
            old_mask | bit,
            color if old_mask == 0 or dot_row <= old_color_row else old_color,
            min(dot_row, old_color_row),
        )

    for dot_column in range(dot_width):
        ratio = dot_column / max(1, dot_width - 1)
        timestamp = int(start_ts + (end_ts - start_ts) * ratio)
        cumulative = 0.0
        for key in keys:
            value = _series_value(rate_series.get(key, []), timestamp, factor)
            start_units = round(_axis_ratio(cumulative, axis_max, log_exponent) * dot_height)
            cumulative += value
            end_units = round(_axis_ratio(cumulative, axis_max, log_exponent) * dot_height)
            if value > 0 and end_units <= start_units:
                end_units = min(dot_height, start_units + 1)
            top_row = dot_height - end_units
            bottom_row = dot_height - start_units
            for dot_row in range(top_row, bottom_row):
                put_dot(dot_row, dot_column, colors[key])

    return [
        "".join(
            paint(chr(0x2800 + mask), color) if mask else " "
            for mask, color, _color_row in row
        )
        for row in grid
    ]


def token_split_shape(width: int, height: int) -> tuple[int, int]:
    """Choose rows and columns that keep four terminal charts readable."""

    target_aspect = 2.5
    candidates = ((1, 4), (2, 2), (4, 1))
    best_shape = (2, 2)
    best_score = float("inf")
    for rows, columns in candidates:
        cell_width = max(1.0, (width - (columns - 1)) / columns)
        cell_height = max(1.0, height / rows)
        aspect_score = abs(math.log((cell_width / cell_height) / target_aspect))
        size_penalty = (
            max(0.0, 18.0 - cell_width) / 18.0
            + max(0.0, 8.0 - cell_height) / 8.0
        ) * 10.0
        score = aspect_score + size_penalty
        if score < best_score:
            best_score = score
            best_shape = rows, columns
    return best_shape


def _partition_size(total: int, parts: int) -> list[int]:
    base, remainder = divmod(max(0, total), parts)
    return [base + (1 if index < remainder else 0) for index in range(parts)]


def _single_token_chart_lines(
    rate_series: dict[str, list[dict[str, float]]],
    latest: Any,
    key: str,
    start_ts: int,
    end_ts: int,
    width: int,
    height: int,
    curve_mode: str,
    colors: dict[str, str],
    usage_axis_scale: str = "default",
    shared_log_exponent: int | None = None,
    shared_rate_scale: tuple[int, str] | None = None,
    shared_log_ticks: tuple[int, ...] | None = None,
) -> list[str]:
    if width <= 14 or height <= 6:
        return [fit_ansi(paint(t("token.space_insufficient", label=token_label(key)), "dim"), width)] + [
            " " * width for _ in range(max(0, height - 1))
        ]

    log_exponent: int | None = None
    if usage_axis_scale == "log":
        if shared_log_exponent is None or shared_rate_scale is None:
            shared_log_exponent, factor, unit = _shared_logarithmic_axis(
                rate_series,
                start_ts,
                end_ts,
                [width],
            )
        else:
            factor, unit = shared_rate_scale
        log_exponent = shared_log_exponent
        axis_width = _logarithmic_axis_width(log_exponent)
    else:
        axis_width = 4
    plot_width = max(2, width - axis_width - 4)
    chart_width = plot_width + 2
    chart_height = max(2, height - 4)
    if usage_axis_scale != "log":
        seconds_per_column = max(0, end_ts - start_ts) / max(1, plot_width)
        factor, unit = token_rate_unit(seconds_per_column)
    single_series = {key: rate_series.get(key, [])}
    if log_exponent is not None:
        axis_max = float(10 ** log_exponent)
        exponent = log_exponent
        tick_max = axis_max
    else:
        visible_max = _visible_max(
            single_series,
            start_ts,
            end_ts,
            plot_width,
            factor,
            False,
        )
        axis_max, exponent, tick_max = scientific_axis_max(visible_max)

    if curve_mode == "bar":
        grid = _bar_grid(
            single_series,
            start_ts,
            end_ts,
            plot_width,
            chart_height,
            axis_max,
            factor,
            colors,
            (key,),
            log_exponent,
        )
    elif curve_mode == "fine_bar":
        grid = _fine_bar_grid(
            single_series,
            start_ts,
            end_ts,
            plot_width,
            chart_height,
            axis_max,
            factor,
            colors,
            (key,),
            log_exponent,
        )
    elif curve_mode == "braille":
        grid = _braille_grid(
            single_series,
            start_ts,
            end_ts,
            plot_width,
            chart_height,
            axis_max,
            factor,
            colors,
            log_exponent,
        )
    else:
        grid = _independent_grid(
            single_series,
            start_ts,
            end_ts,
            plot_width,
            chart_height,
            axis_max,
            factor,
            curve_mode,
            colors,
            log_exponent,
        )

    totals = _usage_values(latest)
    current = _series_value(single_series.get(key, []), end_ts, factor)
    if curve_mode == "bar":
        marker = BAR_LEGEND_MARKER
    elif curve_mode == "fine_bar":
        marker = BRAILLE_LEGEND_MARKER
    else:
        marker = TOKEN_MARKERS[key]
    legend_text = (
        paint(f"{marker} {token_label(key)}", colors[key], bold=True)
        + f" {compact_token_count(current)} ({compact_token_count(totals[key])})"
    )
    header_width = chart_width + 2
    scale_text = (
        t("token.axis_log", unit=unit)
        if log_exponent is not None
        else t("token.axis_power", unit=unit, exponent=exponent)
    )
    legend_width = max(0, header_width - visible_width(scale_text) - 1)
    legend_text = compact_ansi(legend_text, legend_width)
    header_gap = max(1, header_width - visible_width(scale_text) - visible_width(legend_text))
    lines = [
        (" " * axis_width)
        + fit_ansi(scale_text + " " * header_gap + legend_text, header_width),
        (" " * axis_width) + paint("┌" + "─" * chart_width + "┐", "dim"),
    ]
    if log_exponent is not None:
        tick_rows = _logarithmic_tick_rows(chart_height, log_exponent, shared_log_ticks)
    elif chart_height >= SPLIT_FULL_AXIS_MIN_HEIGHT:
        tick_ratios = (1.0, 0.8, 0.6, 0.4, 0.2, 0.0)
        tick_rows = {
            round((1.0 - ratio) * (chart_height - 1)): scientific_tick_label(tick_max * ratio)
            for ratio in tick_ratios
        }
    else:
        tick_rows = {
            0: scientific_tick_label(tick_max),
            chart_height - 1: "0",
        }
    for row_index, plot in enumerate(grid):
        label = tick_rows.get(row_index, "")
        lines.append(
            paint(f"{label:>{axis_width - 1}} ", "dim")
            + paint("│", "dim")
            + " "
            + plot
            + " "
            + paint("│", "dim")
        )
    lines.append((" " * axis_width) + paint("└" + "─" * chart_width + "┘", "dim"))
    lines.append(time_axis_line(start_ts, end_ts, plot_width, axis_width + 2))
    return [fit_ansi(line, width) for line in lines[:height]]


def split_token_usage_chart_lines(
    rate_series: dict[str, list[dict[str, float]]],
    latest: Any,
    period: str,
    width: int,
    height: int,
    curve_mode: str,
    color_scheme: str | None = None,
    usage_axis_scale: str = "default",
) -> list[str]:
    if width <= 14 or height <= 7:
        return [paint(t("terminal.space_insufficient"), "dim")]
    colors = color_schemes.token_series_colors(key=color_scheme)
    start_ts, end_ts = token_chart_bounds(rate_series, period)
    rows, columns = token_split_shape(width, height)
    widths = _partition_size(width - (columns - 1), columns)
    heights = _partition_size(height, rows)
    shared_log_exponent: int | None = None
    shared_rate_scale: tuple[int, str] | None = None
    shared_log_ticks: tuple[int, ...] | None = None
    if usage_axis_scale == "log":
        shared_log_exponent, factor, unit = _shared_logarithmic_axis(
            rate_series,
            start_ts,
            end_ts,
            widths,
        )
        shared_rate_scale = factor, unit
        shared_log_ticks = _logarithmic_tick_exponents(
            shared_log_exponent,
            min(max(2, cell_height - 4) for cell_height in heights),
        )
    result: list[str] = []
    for row_index in range(rows):
        row_keys = TOKEN_KEYS[row_index * columns:(row_index + 1) * columns]
        blocks = [
            _single_token_chart_lines(
                rate_series,
                latest,
                key,
                start_ts,
                end_ts,
                widths[column_index],
                heights[row_index],
                curve_mode,
                colors,
                usage_axis_scale,
                shared_log_exponent,
                shared_rate_scale,
                shared_log_ticks,
            )
            for column_index, key in enumerate(row_keys)
        ]
        for line_index in range(heights[row_index]):
            result.append(
                " ".join(
                    fit_ansi(block[line_index], widths[column_index])
                    for column_index, block in enumerate(blocks)
                )
            )
    return [fit_ansi(line, width) for line in result[:height]]


def token_usage_chart_lines(
    rate_series: dict[str, list[dict[str, float]]],
    latest: Any,
    period: str,
    width: int,
    height: int,
    curve_mode: str,
    color_scheme: str | None = None,
    usage_panel_layout: str = "combined",
    usage_axis_scale: str = "default",
) -> list[str]:
    if usage_panel_layout == "split":
        return split_token_usage_chart_lines(
            rate_series,
            latest,
            period,
            width,
            height,
            curve_mode,
            color_scheme,
            usage_axis_scale,
        )
    if width <= 14 or height <= 7:
        return [paint(t("terminal.space_insufficient"), "dim")]
    colors = color_schemes.token_series_colors(key=color_scheme)
    start_ts, end_ts = token_chart_bounds(rate_series, period)
    log_exponent: int | None = None
    if usage_axis_scale == "log":
        log_exponent, factor, unit = _shared_logarithmic_axis(
            rate_series,
            start_ts,
            end_ts,
            [width],
        )
        axis_width = _logarithmic_axis_width(log_exponent)
    else:
        axis_width = 4
    plot_width = max(2, width - axis_width - 4)
    chart_width = plot_width + 2
    chart_height = max(2, height - 5)
    if log_exponent is not None:
        axis_max = float(10 ** log_exponent)
        exponent = log_exponent
        tick_max = axis_max
    else:
        seconds_per_column = max(0, end_ts - start_ts) / max(1, plot_width)
        factor, unit = token_rate_unit(seconds_per_column)
        visible_max = _visible_max(
            rate_series,
            start_ts,
            end_ts,
            plot_width,
            factor,
            curve_mode in {"bar", "fine_bar"},
        )
        axis_max, exponent, tick_max = scientific_axis_max(visible_max)

    if curve_mode == "bar":
        grid = _bar_grid(
            rate_series,
            start_ts,
            end_ts,
            plot_width,
            chart_height,
            axis_max,
            factor,
            colors,
            TOKEN_STACK_KEYS,
            log_exponent,
        )
    elif curve_mode == "fine_bar":
        grid = _fine_bar_grid(
            rate_series,
            start_ts,
            end_ts,
            plot_width,
            chart_height,
            axis_max,
            factor,
            colors,
            TOKEN_STACK_KEYS,
            log_exponent,
        )
    elif curve_mode == "braille":
        grid = _braille_grid(
            rate_series,
            start_ts,
            end_ts,
            plot_width,
            chart_height,
            axis_max,
            factor,
            colors,
            log_exponent,
        )
    else:
        # Non-bar modes draw the four raw series independently.  Stacking is
        # deliberately limited to the two bar renderers above.
        grid = _independent_grid(
            rate_series,
            start_ts,
            end_ts,
            plot_width,
            chart_height,
            axis_max,
            factor,
            curve_mode,
            colors,
            log_exponent,
        )

    if log_exponent is not None:
        tick_labels = _logarithmic_tick_rows(chart_height, log_exponent)
    else:
        tick_ratios = (1.0, 0.8, 0.6, 0.4, 0.2, 0.0)
        tick_rows = {
            round((1.0 - ratio) * (chart_height - 1)): ratio
            for ratio in tick_ratios
        }
        ratio_labels = {
            ratio: scientific_tick_label(tick_max * ratio)
            for ratio in tick_ratios
        }
        tick_labels = {
            row: ratio_labels[ratio]
            for row, ratio in tick_rows.items()
        }
    header_width = chart_width + 2
    scale_text = (
        t("token.axis_log", unit=unit)
        if log_exponent is not None
        else t("token.axis_power", unit=unit, exponent=exponent)
    )
    legend_width = max(0, header_width - visible_width(scale_text) - 1)
    legend_text = compact_ansi(
        token_legend(
            rate_series,
            latest,
            end_ts,
            factor,
            legend_width,
            curve_mode,
            colors,
        ),
        legend_width,
    )
    header_gap = max(1, header_width - visible_width(scale_text) - visible_width(legend_text))
    lines = [""]
    lines.append(
        (" " * axis_width)
        + fit_ansi(scale_text + " " * header_gap + legend_text, header_width)
    )
    lines.append((" " * axis_width) + paint("┌" + "─" * chart_width + "┐", "dim"))
    for row_index, plot in enumerate(grid):
        label = tick_labels.get(row_index, "")
        lines.append(
            paint(f"{label:>{axis_width - 1}} ", "dim")
            + paint("│", "dim")
            + " "
            + plot
            + " "
            + paint("│", "dim")
        )
    lines.append((" " * axis_width) + paint("└" + "─" * chart_width + "┘", "dim"))
    lines.append(time_axis_line(start_ts, end_ts, plot_width, axis_width + 2))
    while len(lines) < height:
        lines.append("")
    return [fit_ansi(line, width) for line in lines[:height]]
