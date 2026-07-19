"""Read CodexTOP snapshot logs and prepare historical quota series."""

from __future__ import annotations

import bisect
import copy
import json
import os
import time
from array import array
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.constants import *
from quota.windows import window_key_for_seconds
from .models import MonitorState
from core.paths import iter_snapshot_log_paths, recent_month_keys

UPWARD_JUMP_THRESHOLD = 1.0
UPWARD_CONFIRMATION_SAMPLES = 3
UPWARD_CONFIRMATION_SECONDS = 30
UPWARD_VALUE_TOLERANCE = 10.0
UPWARD_RESET_TOLERANCE_SECONDS = 10
RESET_DUE_GRACE_SECONDS = 5


def normalize_snapshot_record(record: dict[str, Any]) -> dict[str, Any]:
    """Remap one legacy compact snapshot using each window's duration."""
    accounts = record.get("a")
    if not isinstance(accounts, list):
        return record
    for account in accounts:
        if not isinstance(account, dict):
            continue
        quota = account.get("q")
        if not isinstance(quota, dict):
            continue
        normalized: dict[str, list[Any]] = {}
        sources: dict[str, str] = {}
        for fallback in ("5h", "7d"):
            raw = quota.get(fallback)
            if not isinstance(raw, list) or len(raw) < 4:
                continue
            if not any(value is not None for value in raw):
                continue
            key = window_key_for_seconds(raw[3], fallback)
            if key is None:
                continue
            previous_source = sources.get(key)
            if previous_source is not None and not (
                fallback == key and previous_source != key
            ):
                continue
            normalized[key] = raw
            sources[key] = fallback
        account["q"] = normalized
    return record


def normalize_snapshot_windows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remap legacy compact snapshots using each window's recorded duration."""
    for record in records:
        normalize_snapshot_record(record)
    return records


def _candidate_matches(pending: dict[str, Any], value: float, reset_epoch: int | None) -> bool:
    if abs(value - pending["value"]) > UPWARD_VALUE_TOLERANCE:
        return False
    pending_reset = pending.get("reset")
    if pending_reset is None or reset_epoch is None:
        return pending_reset is reset_epoch
    return abs(reset_epoch - pending_reset) <= UPWARD_RESET_TOLERANCE_SECONDS


def _replace_with_accepted(raw: list[Any], timestamp: int, state: dict[str, Any]) -> None:
    raw[0] = state["value"]
    reset_epoch = state.get("reset")
    raw[1] = reset_epoch
    if isinstance(reset_epoch, int):
        raw[2] = max(0, reset_epoch - timestamp)


def smooth_snapshot_record(
    record: dict[str, Any],
    states: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Apply upward-spike smoothing to one normalized snapshot."""
    timestamp = record.get("t")
    accounts = record.get("a")
    if not isinstance(timestamp, int) or not isinstance(accounts, list):
        return record
    for account in accounts:
        if not isinstance(account, dict):
            continue
        index = account.get("i")
        if not isinstance(index, (int, str)):
            continue
        if account.get("err"):
            for window in ("5h", "7d"):
                state = states.get((str(index), window))
                if state is not None:
                    state["pending"] = None
            continue
        quota = account.get("q")
        if not isinstance(quota, dict):
            continue
        for window in ("5h", "7d"):
            raw = quota.get(window)
            if not isinstance(raw, list) or len(raw) < 4:
                continue
            value = raw[0]
            if not isinstance(value, (int, float)):
                continue
            value = float(value)
            reset_epoch = raw[1] if isinstance(raw[1], int) else None
            key = (str(index), window)
            state = states.get(key)
            if state is None:
                states[key] = {"value": value, "reset": reset_epoch, "pending": None}
                continue

            accepted_value = float(state["value"])
            accepted_reset = state.get("reset")
            reset_is_due = isinstance(accepted_reset, int) and timestamp >= accepted_reset - RESET_DUE_GRACE_SECONDS
            if value - accepted_value < UPWARD_JUMP_THRESHOLD or reset_is_due:
                state.update(value=value, reset=reset_epoch, pending=None)
                continue

            pending = state.get("pending")
            if not isinstance(pending, dict) or not _candidate_matches(pending, value, reset_epoch):
                pending = {
                    "first_t": timestamp,
                    "count": 1,
                    "value": value,
                    "reset": reset_epoch,
                }
                state["pending"] = pending
            else:
                pending["count"] += 1
                pending["value"] = value

            confirmed = (
                pending["count"] >= UPWARD_CONFIRMATION_SAMPLES
                and timestamp - pending["first_t"] >= UPWARD_CONFIRMATION_SECONDS
            )
            if confirmed:
                state.update(value=value, reset=reset_epoch, pending=None)
            else:
                _replace_with_accepted(raw, timestamp, state)
    return record


def smooth_upward_spikes(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suppress unconfirmed quota increases while preserving raw JSONL on disk."""
    states: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        smooth_snapshot_record(record, states)
    return records


@dataclass(slots=True)
class CompactHistoryRecord:
    timestamp: int

    def get(self, key: str, default: Any = None) -> Any:
        if key == "t":
            return self.timestamp
        if key == "a":
            return ()
        return default

    def __getitem__(self, key: str) -> Any:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value


@dataclass(slots=True)
class HistorySeries:
    times: array
    values: array
    resets: array

    @classmethod
    def empty(cls) -> "HistorySeries":
        return cls(array("q"), array("d"), array("q"))

    def __len__(self) -> int:
        return len(self.times)

    @property
    def first_timestamp(self) -> int:
        return int(self.times[0])

    def append(self, timestamp: int, value: float, reset_epoch: int | None) -> None:
        self.times.append(timestamp)
        self.values.append(value)
        self.resets.append(reset_epoch if isinstance(reset_epoch, int) else -1)

    def remove_timestamp(self, timestamp: int) -> None:
        while self.times and self.times[-1] == timestamp:
            self.times.pop()
            self.values.pop()
            self.resets.pop()

    def value_at(self, timestamp: int, reset_value: float, start: int = 0) -> tuple[float, bool]:
        position = bisect.bisect_right(self.times, timestamp, lo=start) - 1
        if position < start:
            return float(self.values[start]), True

        previous_timestamp = int(self.times[position])
        next_position = position + 1
        has_next = next_position < len(self.times)
        next_timestamp = int(self.times[next_position]) if has_next else timestamp
        gap_end = next_timestamp if has_next else timestamp
        is_gap = (has_next and next_timestamp - previous_timestamp > GAP_SECONDS) or (
            not has_next and timestamp - previous_timestamp > GAP_SECONDS
        )
        if is_gap:
            candidates = []
            previous_reset = int(self.resets[position])
            if previous_reset >= 0 and previous_timestamp < previous_reset < gap_end:
                candidates.append(previous_reset)
            if has_next:
                next_reset = int(self.resets[next_position])
                if next_reset >= 0 and previous_timestamp < next_reset < gap_end:
                    candidates.append(next_reset)
            if candidates:
                midpoint = (previous_timestamp + gap_end) / 2
                reset_timestamp = min(candidates, key=lambda item: abs(item - midpoint))
                if timestamp >= reset_timestamp:
                    return reset_value, True
            return float(self.values[position]), True
        return float(self.values[position]), False


@dataclass(slots=True)
class HistorySeriesView:
    series: HistorySeries
    start: int

    def __len__(self) -> int:
        return len(self.series) - self.start

    @property
    def first_timestamp(self) -> int:
        return int(self.series.times[self.start])

    def value_at(self, timestamp: int, reset_value: float) -> tuple[float, bool]:
        return self.series.value_at(timestamp, reset_value, self.start)


@dataclass(slots=True)
class MetricSeries:
    """Compact step series for non-quota metrics such as tokens/second."""

    times: array
    values: array

    @classmethod
    def empty(cls) -> "MetricSeries":
        return cls(array("q"), array("d"))

    def __len__(self) -> int:
        return len(self.times)

    @property
    def first_timestamp(self) -> int:
        return int(self.times[0])

    def append(self, timestamp: int, value: float) -> None:
        self.times.append(timestamp)
        self.values.append(value)

    def remove_timestamp(self, timestamp: int) -> None:
        while self.times and self.times[-1] == timestamp:
            self.times.pop()
            self.values.pop()

    def value_at(self, timestamp: int, start: int = 0) -> tuple[float, bool]:
        position = bisect.bisect_right(self.times, timestamp, lo=start) - 1
        if position < start:
            return 0.0, True
        sample_timestamp = int(self.times[position])
        return float(self.values[position]), timestamp - sample_timestamp > GAP_SECONDS


@dataclass(slots=True)
class MetricSeriesView:
    series: MetricSeries
    start: int

    def __len__(self) -> int:
        return len(self.series) - self.start

    @property
    def first_timestamp(self) -> int:
        return int(self.series.times[self.start])

    def value_at(self, timestamp: int) -> tuple[float, bool]:
        return self.series.value_at(timestamp, self.start)


class HistorySeriesIndex:
    def __init__(self) -> None:
        self._accounts: dict[int | str, dict[str, HistorySeries]] = {}

    def append_record(self, record: dict[str, Any]) -> None:
        timestamp = record.get("t")
        accounts = record.get("a")
        if not isinstance(timestamp, int) or not isinstance(accounts, list):
            return
        for account in accounts:
            if not isinstance(account, dict):
                continue
            index = account.get("i")
            if not isinstance(index, (int, str)):
                continue
            if account.get("err"):
                continue
            quota = account.get("q")
            if not isinstance(quota, dict):
                continue
            windows = self._accounts.setdefault(index, {})
            for window in ("5h", "7d"):
                raw = quota.get(window)
                if not isinstance(raw, list) or len(raw) < 2 or not isinstance(raw[0], (int, float)):
                    continue
                series = windows.get(window)
                if series is None:
                    series = HistorySeries.empty()
                    windows[window] = series
                series.append(timestamp, float(raw[0]), raw[1] if isinstance(raw[1], int) else None)

    def replace_timestamp(self, timestamp: int, record: dict[str, Any]) -> None:
        for windows in self._accounts.values():
            for series in windows.values():
                series.remove_timestamp(timestamp)
        self.append_record(record)

    @staticmethod
    def _bounded(
        series: HistorySeries | None,
        minimum_timestamp: int | None,
    ) -> HistorySeries | HistorySeriesView | None:
        if series is None or minimum_timestamp is None:
            return series
        start = bisect.bisect_left(series.times, minimum_timestamp)
        if start >= len(series):
            return None
        return HistorySeriesView(series, start)

    def account_window(
        self,
        index: int | str,
        window: str,
        minimum_timestamp: int | None = None,
    ) -> HistorySeries | HistorySeriesView | None:
        return self._bounded(self._accounts.get(index, {}).get(window), minimum_timestamp)

    def merged_window(
        self,
        window: str,
        minimum_timestamp: int | None = None,
    ) -> dict[str, HistorySeries | HistorySeriesView]:
        merged: dict[str, HistorySeries | HistorySeriesView] = {}
        for index, windows in self._accounts.items():
            bounded = self._bounded(windows.get(window), minimum_timestamp)
            if bounded:
                merged[str(index)] = bounded
        return merged

class HistoryRecordList(Sequence):
    __slots__ = ("series_index", "timestamps", "_latest")

    def __init__(self, series_index: HistorySeriesIndex) -> None:
        self.series_index = series_index
        self.timestamps = array("q")
        self._latest: dict[str, Any] | None = None

    def __len__(self) -> int:
        return len(self.timestamps)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        position = index + len(self) if index < 0 else index
        if not 0 <= position < len(self):
            raise IndexError(index)
        if position == len(self) - 1:
            return self._latest
        return CompactHistoryRecord(int(self.timestamps[position]))

    def __setitem__(self, index: int, record: dict[str, Any]) -> None:
        position = index + len(self) if index < 0 else index
        if position != len(self) - 1:
            raise IndexError("only the latest history record can be replaced")
        timestamp = record.get("t")
        if timestamp != self.timestamps[position]:
            raise ValueError("replacement timestamp must match the latest record")
        self._latest = record

    def append(self, record: dict[str, Any]) -> None:
        timestamp = record.get("t")
        if not isinstance(timestamp, int):
            raise ValueError("history record timestamp must be an integer")
        self.timestamps.append(timestamp)
        self._latest = record

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Sequence):
            return False
        return list(self) == list(other)


def compact_history_record(record: dict[str, Any] | CompactHistoryRecord) -> CompactHistoryRecord:
    """Keep only the timestamp; exact historical values live in compact arrays."""
    if isinstance(record, CompactHistoryRecord):
        return record
    timestamp = record.get("t")
    return CompactHistoryRecord(int(timestamp))


@dataclass
class _LogCursor:
    device: int
    inode: int
    offset: int = 0
    boundary: bytes = b""
    mtime_ns: int = 0


class _OutOfOrderSnapshot(RuntimeError):
    pass


class IncrementalHistoryCache:
    """Append-aware history cache that preserves legacy JSONL read semantics."""

    def __init__(self, log_path: Path, tz_name: str) -> None:
        self.log_path = log_path.expanduser()
        self.tz_name = tz_name
        self._series_index = HistorySeriesIndex()
        self.records: HistoryRecordList = HistoryRecordList(self._series_index)
        self.version = 0
        self.full_loads = 0
        self.incremental_reads = 0
        self.bytes_read = 0
        self.invalid_lines = 0
        self._paths: tuple[Path, ...] = ()
        self._cursors: dict[Path, _LogCursor] = {}
        self._smooth_states: dict[tuple[str, str], dict[str, Any]] = {}
        self._state_before_last: dict[tuple[str, str], dict[str, Any]] = {}
        self._initialized = False

    def compatible_with(self, log_path: Path, tz_name: str) -> bool:
        return self.log_path == log_path.expanduser() and self.tz_name == tz_name

    def _selected_paths(self, period: str) -> tuple[Path, ...]:
        months = None if period == "all" else recent_month_keys(self.tz_name, 2)
        return tuple(iter_snapshot_log_paths(self.log_path, months))

    def _reset_processed_state(self) -> None:
        self._series_index = HistorySeriesIndex()
        self.records = HistoryRecordList(self._series_index)
        self._smooth_states = {}
        self._state_before_last = {}

    def _process_batch(self, batch: list[dict[str, Any]]) -> bool:
        if not batch:
            return False
        deduplicated: list[dict[str, Any]] = []
        for record in batch:
            timestamp = record["t"]
            if deduplicated:
                previous = deduplicated[-1]["t"]
                if timestamp < previous:
                    raise _OutOfOrderSnapshot(f"snapshot timestamp moved backward: {timestamp} < {previous}")
                if timestamp == previous:
                    deduplicated[-1] = record
                    continue
            deduplicated.append(record)

        if self.records:
            cached_timestamp = self.records[-1].get("t")
            first_timestamp = deduplicated[0]["t"]
            if not isinstance(cached_timestamp, int):
                raise _OutOfOrderSnapshot("cached record has no timestamp")
            if first_timestamp < cached_timestamp:
                raise _OutOfOrderSnapshot(f"snapshot timestamp moved backward: {first_timestamp} < {cached_timestamp}")
            if first_timestamp == cached_timestamp:
                replacement = deduplicated.pop(0)
                states = copy.deepcopy(self._state_before_last)
                normalize_snapshot_record(replacement)
                smooth_snapshot_record(replacement, states)
                self.records[-1] = replacement
                self._smooth_states = states
                self._series_index.replace_timestamp(first_timestamp, replacement)

        for index, record in enumerate(deduplicated):
            normalize_snapshot_record(record)
            if index == len(deduplicated) - 1:
                self._state_before_last = copy.deepcopy(self._smooth_states)
            smooth_snapshot_record(record, self._smooth_states)
            self._series_index.append_record(record)
            self.records.append(record)
        return True

    def _parse_line(self, raw_line: bytes) -> dict[str, Any] | None:
        if not raw_line.strip():
            return None
        try:
            record = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.invalid_lines += 1
            return None
        if isinstance(record, dict) and isinstance(record.get("t"), int) and isinstance(record.get("a"), list):
            return record
        return None

    def _read_complete_tail(self, path: Path, cursor: _LogCursor) -> Iterator[dict[str, Any]]:
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            if stat.st_dev != cursor.device or stat.st_ino != cursor.inode or stat.st_size < cursor.offset:
                raise _OutOfOrderSnapshot(f"snapshot log replaced or truncated: {path}")
            if cursor.offset and cursor.boundary:
                handle.seek(cursor.offset - len(cursor.boundary))
                if handle.read(len(cursor.boundary)) != cursor.boundary:
                    raise _OutOfOrderSnapshot(f"snapshot log changed before cursor: {path}")
            handle.seek(cursor.offset)
            while True:
                raw_line = handle.readline()
                if not raw_line or not raw_line.endswith(b"\n"):
                    break
                cursor.offset += len(raw_line)
                self.bytes_read += len(raw_line)
                record = self._parse_line(raw_line)
                if record is not None:
                    yield record
            boundary_length = min(64, cursor.offset)
            handle.seek(cursor.offset - boundary_length)
            cursor.boundary = handle.read(boundary_length)
            cursor.mtime_ns = os.fstat(handle.fileno()).st_mtime_ns

    def _consume_path(self, path: Path, cursor: _LogCursor) -> bool:
        changed = False
        batch: list[dict[str, Any]] = []
        for record in self._read_complete_tail(path, cursor):
            batch.append(record)
            if len(batch) >= 1024:
                changed = self._process_batch(batch) or changed
                batch = []
        return self._process_batch(batch) or changed

    def _new_cursor(self, path: Path) -> _LogCursor:
        stat = path.stat()
        return _LogCursor(stat.st_dev, stat.st_ino, 0, b"", stat.st_mtime_ns)

    def _load_sorted_fallback(self, paths: tuple[Path, ...]) -> None:
        locations: dict[int, tuple[Path, int, int]] = {}
        self._reset_processed_state()
        self._cursors = {}
        for path in paths:
            cursor = self._new_cursor(path)
            self._cursors[path] = cursor
            with path.open("rb") as handle:
                while True:
                    offset = handle.tell()
                    raw_line = handle.readline()
                    if not raw_line or not raw_line.endswith(b"\n"):
                        break
                    cursor.offset += len(raw_line)
                    self.bytes_read += len(raw_line)
                    record = self._parse_line(raw_line)
                    if record is not None:
                        locations[record["t"]] = (path, offset, len(raw_line))
                boundary_length = min(64, cursor.offset)
                handle.seek(cursor.offset - boundary_length)
                cursor.boundary = handle.read(boundary_length)
                cursor.mtime_ns = os.fstat(handle.fileno()).st_mtime_ns

        handles = {path: path.open("rb") for path in paths}
        try:
            batch: list[dict[str, Any]] = []
            for timestamp in sorted(locations):
                path, offset, length = locations[timestamp]
                handle = handles[path]
                handle.seek(offset)
                raw_line = handle.read(length)
                self.bytes_read += len(raw_line)
                record = self._parse_line(raw_line)
                if record is None:
                    continue
                batch.append(record)
                if len(batch) >= 1024:
                    self._process_batch(batch)
                    batch = []
            self._process_batch(batch)
        finally:
            for handle in handles.values():
                handle.close()

    def _rebuild(self, paths: tuple[Path, ...]) -> None:
        self.full_loads += 1
        self._reset_processed_state()
        self._cursors = {}
        try:
            for path in paths:
                cursor = self._new_cursor(path)
                self._cursors[path] = cursor
                self._consume_path(path, cursor)
        except _OutOfOrderSnapshot:
            self._load_sorted_fallback(paths)
        self._paths = paths
        self._initialized = True
        self.version += 1

    def _requires_rebuild(self, paths: tuple[Path, ...]) -> bool:
        if not self._initialized:
            return True
        if paths != self._paths:
            return True
        for path in paths:
            cursor = self._cursors.get(path)
            if cursor is None:
                return True
            try:
                stat = path.stat()
            except OSError:
                return True
            if stat.st_dev != cursor.device or stat.st_ino != cursor.inode or stat.st_size < cursor.offset:
                return True
            if stat.st_size == cursor.offset and stat.st_mtime_ns != cursor.mtime_ns:
                return True
        return False

    def read(self, period: str) -> tuple[HistoryRecordList, bool]:
        requested_paths = self._selected_paths(period)
        if (
            self._initialized
            and set(requested_paths).issubset(self._paths)
            and all(path.exists() for path in self._paths)
        ):
            paths = self._paths
        else:
            paths = requested_paths
        if self._requires_rebuild(paths):
            self._rebuild(paths)
            return self.records, True

        changed = False
        self.incremental_reads += 1
        try:
            for path in paths:
                cursor = self._cursors[path]
                changed = self._consume_path(path, cursor) or changed
        except _OutOfOrderSnapshot:
            self._rebuild(paths)
            return self.records, True
        if changed:
            self.version += 1
        return self.records, changed


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
    records = [records_by_time[timestamp] for timestamp in sorted(records_by_time)]
    return smooth_upward_spikes(normalize_snapshot_windows(records))


def account_at(record: Any, index: int | str) -> Any | None:
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
        if not isinstance(raw, (list, tuple)) or len(raw) < 4:
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


def metric_value_at(points: Any, ts: int, _max_value: float = 0.0) -> tuple[float, bool]:
    if isinstance(points, (MetricSeries, MetricSeriesView)):
        return points.value_at(ts)
    if not points:
        return 0.0, True
    pos = bisect.bisect_right(points, ts, key=lambda point: point["t"]) - 1
    if pos < 0:
        return 0.0, True
    point = points[pos]
    timestamp = point["t"]
    return float(point["value"]), ts - timestamp > GAP_SECONDS


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


def value_at(points: Any, ts: int, reset_value: float = 100.0) -> tuple[float, bool]:
    if isinstance(points, (HistorySeries, HistorySeriesView)):
        return points.value_at(ts, reset_value)
    pos = bisect.bisect_right(points, ts, key=lambda point: point["t"]) - 1
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
            cache = state.history_cache
            if not isinstance(cache, IncrementalHistoryCache) or not cache.compatible_with(state.log_path, state.tz):
                cache = IncrementalHistoryCache(state.log_path, state.tz)
                state.history_cache = cache
            state.records, changed = cache.read(state.period)
            if changed or state.records_version == 0:
                state.records_version = cache.version
                state.main_cache_key = None
            completed_at = time.time()
            state.last_records_read = completed_at
            state.next_read = completed_at + state.interval
            if state.records:
                state.last_update = float(state.records[-1]["t"])
                age = int(completed_at - state.last_update)
                state.status = f"数据更新于 {age}s 前"
                state.error = None
            else:
                state.status = "等待数据"
                state.error = None
        except Exception as exc:
            state.records = state.records or []
            state.next_read = time.time() + min(10, state.interval)
            state.status = "读取失败"
            state.error = str(exc)
    return state.records or []


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


def period_bounds(records: list[Any], period: str) -> tuple[int, int]:
    end_ts = int(time.time())
    if period == "all":
        timestamps = getattr(records, "timestamps", None)
        return (int(timestamps[0]) if timestamps is not None else records[0]["t"]), end_ts
    return end_ts - int(PERIOD_SECONDS[period] or 0), end_ts


def period_context_timestamp(records: list[Any], period: str, start_ts: int) -> int | None:
    if period == "all":
        return None
    timestamps = getattr(records, "timestamps", None)
    if timestamps is not None:
        position = bisect.bisect_left(timestamps, start_ts)
        return int(timestamps[max(0, position - 1)])
    position = bisect.bisect_left(records, start_ts, key=lambda record: record["t"])
    return int(records[max(0, position - 1)]["t"])


def records_for_period(records: list[Any], period: str) -> tuple[list[Any], int, int]:
    start_ts, end_ts = period_bounds(records, period)
    if period == "all":
        return records, start_ts, end_ts
    position = bisect.bisect_left(records, start_ts, key=lambda record: record["t"])
    context_position = max(0, position - 1)
    relevant = records[context_position:]
    if not relevant:
        relevant = records[-1:]
    return relevant, start_ts, end_ts
