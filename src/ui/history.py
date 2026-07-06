"""Read CodexTOP snapshot logs and prepare historical quota series."""

from __future__ import annotations

import bisect
import json
import time
from pathlib import Path
from typing import Any

from core.constants import *
from .models import MonitorState
from core.paths import iter_snapshot_log_paths, recent_month_keys

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
