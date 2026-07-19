"""Render quota history charts in multiple terminal styles."""

from __future__ import annotations

from typing import Any

from core.constants import *
from .history import period_bounds, period_context_timestamp, records_for_period, value_at, window_points
from .terminal_text import *

def axis_value_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def chart_series_priority(
    points: dict[str, Any],
    label: str,
    ts: int,
    value: float,
    max_value: float,
    value_getter: Any,
) -> int:
    ranked: list[tuple[float, int, str]] = []
    for other_label, series in points.items():
        if not series:
            continue
        if other_label == label:
            raw_value = value
        else:
            raw_value, _predicted = value_getter(series, ts, max_value)
        if not isinstance(raw_value, (int, float)):
            continue
        bounded = max(0.0, min(max_value, float(raw_value)))
        ranked.append((bounded, -WINDOW_PRIORITIES.get(other_label, 0), other_label))
    ranked.sort()
    for rank, (_value, _fallback, ranked_label) in enumerate(ranked):
        if ranked_label == label:
            return len(ranked) - rank
    return WINDOW_PRIORITIES.get(label, 0)


def has_single_active_series(points: dict[str, Any]) -> bool:
    return sum(bool(series) for series in points.values()) == 1


def braille_char(mask: int) -> str:
    return chr(0x2800 + mask) if mask else " "


def bar_cell_char(mask: int) -> str:
    return BAR_QUADRANT_CHARS[mask] if 0 <= mask < len(BAR_QUADRANT_CHARS) else " "


def braille_series_chart_lines(
    points: dict[str, Any],
    start_ts: int,
    end_ts: int,
    width: int,
    height: int,
    max_value: float,
    value_getter: Any,
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
    chart_height = max(1, height - 3)
    dot_width = plot_width * 2
    dot_height = chart_height * 4
    primary_color = has_single_active_series(points)

    grid: list[list[tuple[int, str, int]]] = [
        [(0, "dim", 0) for _ in range(plot_width)]
        for _ in range(chart_height)
    ]

    def put_dot(dot_row: int, dot_column: int, color: str, priority: int) -> None:
        if not (0 <= dot_row < dot_height and 0 <= dot_column < dot_width):
            return
        cell_row = dot_row // 4
        cell_column = dot_column // 2
        bit = BRAILLE_DOT_BITS[(dot_column % 2, dot_row % 4)]
        old_mask, old_color, old_priority = grid[cell_row][cell_column]
        new_mask = old_mask | bit
        if old_mask == 0 or priority >= old_priority:
            grid[cell_row][cell_column] = (new_mask, color, priority)
        else:
            grid[cell_row][cell_column] = (new_mask, old_color, old_priority)

    def value_row(value: float) -> int:
        return round((max_value - value) / max_value * (dot_height - 1))

    def draw_segment(
        label: str,
        previous_column: int,
        previous_row: int,
        previous_value: float,
        column: int,
        row: int,
        value: float,
        ts: int,
    ) -> None:
        steps = max(abs(column - previous_column), abs(row - previous_row), 1)
        for step_index in range(1, steps + 1):
            ratio = step_index / steps
            dot_column = round(previous_column + (column - previous_column) * ratio)
            dot_row = round(previous_row + (row - previous_row) * ratio)
            interpolated = previous_value + (value - previous_value) * ratio
            normalized = max(0.0, min(100.0, interpolated / max_value * 100))
            priority = chart_series_priority(points, label, ts, interpolated, max_value, value_getter)
            put_dot(
                dot_row,
                dot_column,
                chart_series_color(label, normalized, primary=primary_color),
                priority,
            )

    for label, series in points.items():
        if not series:
            continue
        previous_column: int | None = None
        previous_row: int | None = None
        previous_value: float | None = None
        for dot_column in range(dot_width):
            ratio = dot_column / max(1, dot_width - 1)
            ts = int(start_ts + (end_ts - start_ts) * ratio)
            value, _predicted = value_getter(series, ts, max_value)
            value = max(0, min(max_value, value))
            row = value_row(value)
            if previous_column is None or previous_row is None or previous_value is None:
                normalized = max(0.0, min(100.0, value / max_value * 100))
                priority = chart_series_priority(points, label, ts, float(value), max_value, value_getter)
                put_dot(
                    row,
                    dot_column,
                    chart_series_color(label, normalized, primary=primary_color),
                    priority,
                )
            else:
                draw_segment(
                    label,
                    previous_column,
                    previous_row,
                    previous_value,
                    dot_column,
                    row,
                    float(value),
                    ts,
                )
            previous_column = dot_column
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
        plot = "".join(paint(braille_char(mask), color) if mask else " " for mask, color, _priority in row)
        line = prefix + paint("│", "dim") + " " + plot + " " + paint("│", "dim")
        lines.append(fit_ansi(line, width))
    lines.append((" " * axis_width) + paint("└" + "─" * chart_width + "┘", "dim"))
    lines.append(time_axis_line(start_ts, end_ts, plot_width, axis_width + 2))
    while len(lines) < height:
        lines.append("")
    return [fit_ansi(line, width) for line in lines[:height]]


def bar_series_chart_lines(
    points: dict[str, Any],
    start_ts: int,
    end_ts: int,
    width: int,
    height: int,
    max_value: float,
    value_getter: Any,
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
    sub_width = plot_width * 2
    sub_height = chart_height * 2
    primary_color = has_single_active_series(points)

    grid: list[list[tuple[int, str, int, int]]] = [
        [(0, "dim", 0, sub_height) for _ in range(plot_width)]
        for _ in range(chart_height)
    ]

    def put_subcell(sub_row: int, sub_column: int, color: str, priority: int) -> None:
        if not (0 <= sub_row < sub_height and 0 <= sub_column < sub_width):
            return
        cell_row = sub_row // 2
        cell_column = sub_column // 2
        bit = BAR_QUADRANT_BITS[(sub_column % 2, sub_row % 2)]
        old_mask, old_color, old_priority, old_color_row = grid[cell_row][cell_column]
        new_mask = old_mask | bit
        if old_mask == 0 or priority > old_priority or (priority == old_priority and sub_row <= old_color_row):
            grid[cell_row][cell_column] = (new_mask, color, priority, sub_row)
        else:
            grid[cell_row][cell_column] = (new_mask, old_color, old_priority, old_color_row)

    for label, series in points.items():
        if not series:
            continue
        for sub_column in range(sub_width):
            ratio = sub_column / max(1, sub_width - 1)
            ts = int(start_ts + (end_ts - start_ts) * ratio)
            value, _predicted = value_getter(series, ts, max_value)
            value = max(0, min(max_value, value))
            priority = chart_series_priority(points, label, ts, float(value), max_value, value_getter)
            filled_units = max(1, min(sub_height, round(float(value) / max_value * sub_height)))
            start_sub_row = sub_height - filled_units
            for sub_row in range(start_sub_row, sub_height):
                row_normalized = chart_row_percent(sub_row, sub_height)
                put_subcell(
                    sub_row,
                    sub_column,
                    chart_series_color(
                        label,
                        row_normalized,
                        dimmed=True,
                        primary=primary_color,
                    ),
                    priority,
                )

    tick_rows = {
        round((max_value - value) / max_value * (chart_height - 1)): value
        for value in tick_values
    }
    lines: list[str] = []
    lines.append((" " * axis_width) + paint("┌" + "─" * chart_width + "┐", "dim"))
    for row_index, row in enumerate(grid):
        axis = tick_rows.get(row_index)
        prefix = paint(f"{axis_value_text(axis):>{axis_width - 1}} ", "dim") if axis is not None else " " * axis_width
        plot = "".join(paint(bar_cell_char(mask), color) if mask else " " for mask, color, _priority, _color_row in row)
        line = prefix + paint("│", "dim") + " " + plot + " " + paint("│", "dim")
        lines.append(fit_ansi(line, width))
    lines.append((" " * axis_width) + paint("└" + "─" * chart_width + "┘", "dim"))
    lines.append(time_axis_line(start_ts, end_ts, plot_width, axis_width + 2))
    while len(lines) < height:
        lines.append("")
    return [fit_ansi(line, width) for line in lines[:height]]


def fine_bar_series_chart_lines(
    points: dict[str, Any],
    start_ts: int,
    end_ts: int,
    width: int,
    height: int,
    max_value: float,
    value_getter: Any,
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
    dot_width = plot_width * 2
    dot_height = chart_height * 4
    primary_color = has_single_active_series(points)

    grid: list[list[tuple[int, str, int, int]]] = [
        [(0, "dim", 0, dot_height) for _ in range(plot_width)]
        for _ in range(chart_height)
    ]

    def put_dot(dot_row: int, dot_column: int, color: str, priority: int) -> None:
        if not (0 <= dot_row < dot_height and 0 <= dot_column < dot_width):
            return
        cell_row = dot_row // 4
        cell_column = dot_column // 2
        bit = BRAILLE_DOT_BITS[(dot_column % 2, dot_row % 4)]
        old_mask, old_color, old_priority, old_color_row = grid[cell_row][cell_column]
        new_mask = old_mask | bit
        if old_mask == 0 or priority > old_priority or (priority == old_priority and dot_row <= old_color_row):
            grid[cell_row][cell_column] = (new_mask, color, priority, dot_row)
        else:
            grid[cell_row][cell_column] = (new_mask, old_color, old_priority, old_color_row)

    for label, series in points.items():
        if not series:
            continue
        for dot_column in range(dot_width):
            ratio = dot_column / max(1, dot_width - 1)
            ts = int(start_ts + (end_ts - start_ts) * ratio)
            value, _predicted = value_getter(series, ts, max_value)
            value = max(0, min(max_value, value))
            priority = chart_series_priority(points, label, ts, float(value), max_value, value_getter)
            filled_units = max(1, min(dot_height, round(float(value) / max_value * dot_height)))
            start_dot_row = dot_height - filled_units
            for dot_row in range(start_dot_row, dot_height):
                put_dot(
                    dot_row,
                    dot_column,
                    chart_series_color(
                        label,
                        chart_row_percent(dot_row, dot_height),
                        dimmed=True,
                        primary=primary_color,
                    ),
                    priority,
                )

    tick_rows = {
        round((max_value - value) / max_value * (chart_height - 1)): value
        for value in tick_values
    }
    lines: list[str] = []
    lines.append((" " * axis_width) + paint("┌" + "─" * chart_width + "┐", "dim"))
    for row_index, row in enumerate(grid):
        axis = tick_rows.get(row_index)
        prefix = paint(f"{axis_value_text(axis):>{axis_width - 1}} ", "dim") if axis is not None else " " * axis_width
        plot = "".join(
            paint(braille_char(mask), color) if mask else " "
            for mask, color, _priority, _color_row in row
        )
        lines.append(fit_ansi(prefix + paint("│", "dim") + " " + plot + " " + paint("│", "dim"), width))
    lines.append((" " * axis_width) + paint("└" + "─" * chart_width + "┘", "dim"))
    lines.append(time_axis_line(start_ts, end_ts, plot_width, axis_width + 2))
    while len(lines) < height:
        lines.append("")
    return [fit_ansi(line, width) for line in lines[:height]]


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
    if curve_mode == "braille":
        return braille_series_chart_lines(points, start_ts, end_ts, width, height, max_value, value_getter)
    if curve_mode == "bar":
        return bar_series_chart_lines(points, start_ts, end_ts, width, height, max_value, value_getter)
    if curve_mode == "fine_bar":
        return fine_bar_series_chart_lines(points, start_ts, end_ts, width, height, max_value, value_getter)

    tick_values = [max_value * ratio for ratio in (1.0, 0.75, 0.5, 0.25, 0.0)]
    axis_width = max(4, max(visible_width(axis_value_text(value)) for value in tick_values) + 1)
    box_width = max(6, width - axis_width)
    plot_width = max(2, box_width - 4)
    chart_width = plot_width + 2
    chart_height = max(2, height - 3)
    primary_color = has_single_active_series(points)

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

    def box_segment_char(previous_row: int, row: int, filled_row: int) -> str:
        if row == previous_row:
            return "─"
        if filled_row == previous_row:
            return "╮" if row > previous_row else "╯"
        if filled_row == row:
            return "╰" if row > previous_row else "╭"
        return "│"

    for label, series in points.items():
        if not series:
            continue
        char = WINDOW_MARKERS[label]
        previous_row: int | None = None
        previous_value: float | None = None
        for column in range(plot_width):
            ratio = column / max(1, plot_width - 1)
            ts = int(start_ts + (end_ts - start_ts) * ratio)
            value, _predicted = value_getter(series, ts, max_value)
            value = max(0, min(max_value, value))
            normalized = max(0.0, min(100.0, value / max_value * 100))
            row = round((max_value - value) / max_value * (chart_height - 1))
            if curve_mode == "box" and previous_row is not None and previous_value is not None:
                row_span = abs(row - previous_row)
                if row_span == 0:
                    priority = chart_series_priority(points, label, ts, float(value), max_value, value_getter)
                    put_cell(
                        row,
                        column,
                        "─",
                        chart_series_color(label, normalized, primary=primary_color),
                        priority,
                    )
                else:
                    step = 1 if row > previous_row else -1
                    for filled_row in range(previous_row, row + step, step):
                        row_ratio = abs(filled_row - previous_row) / row_span
                        interpolated = previous_value + (value - previous_value) * row_ratio
                        interpolated_normalized = max(0.0, min(100.0, interpolated / max_value * 100))
                        priority = chart_series_priority(
                            points,
                            label,
                            ts,
                            interpolated,
                            max_value,
                            value_getter,
                        )
                        put_cell(
                            filled_row,
                            column,
                            box_segment_char(previous_row, row, filled_row),
                            chart_series_color(
                                label,
                                interpolated_normalized,
                                primary=primary_color,
                            ),
                            priority,
                        )
            elif curve_mode == "connected" and previous_row is not None and previous_value is not None:
                row_span = abs(row - previous_row)
                if row_span == 0:
                    priority = chart_series_priority(points, label, ts, float(value), max_value, value_getter)
                    put_cell(
                        row,
                        column,
                        char,
                        chart_series_color(label, normalized, primary=primary_color),
                        priority,
                    )
                else:
                    step = 1 if row > previous_row else -1
                    for filled_row in range(previous_row + step, row + step, step):
                        row_ratio = abs(filled_row - previous_row) / row_span
                        interpolated = previous_value + (value - previous_value) * row_ratio
                        interpolated_normalized = max(0.0, min(100.0, interpolated / max_value * 100))
                        priority = chart_series_priority(
                            points,
                            label,
                            ts,
                            interpolated,
                            max_value,
                            value_getter,
                        )
                        put_cell(
                            filled_row,
                            column,
                            char,
                            chart_series_color(
                                label,
                                interpolated_normalized,
                                primary=primary_color,
                            ),
                            priority,
                        )
            else:
                point_char = "─" if curve_mode == "box" else char
                priority = chart_series_priority(points, label, ts, float(value), max_value, value_getter)
                put_cell(
                    row,
                    column,
                    point_char,
                    chart_series_color(label, normalized, primary=primary_color),
                    priority,
                )
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
    window_scope: str = DEFAULT_WINDOW_SCOPE,
) -> list[str]:
    if not records:
        return [paint("暂无历史数据", "dim")]
    series_index = getattr(records, "series_index", None)
    if series_index is not None:
        start_ts, end_ts = period_bounds(records, period)
        context_timestamp = period_context_timestamp(records, period, start_ts)
        points = {
            key: series_index.account_window(index, key, context_timestamp)
            for key in window_keys(window_scope)
        }
    else:
        relevant, start_ts, end_ts = records_for_period(records, period)
        points = {
            key: window_points(relevant, index, key)
            for key in window_keys(window_scope)
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
