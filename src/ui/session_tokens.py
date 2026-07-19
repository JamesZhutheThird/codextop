"""Incrementally read token usage from the active Codex project thread."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.constants import GAP_SECONDS, TOKEN_RATE_SMOOTHING_ALPHA
from .trusted_directories import TrustedDirectoryRegistry, resolved_path


DISCOVERY_INTERVAL_SECONDS = 2.0
INTERACTIVE_THREAD_SOURCES = {"cli", "vscode", "appServer"}
TOKEN_RATE_KEYS = ("input", "cached", "output", "total")
HOT_ROLLOUT_SECONDS = 10 * 60


def normalized_path(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def event_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class TokenUsageBreakdown:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int

    def chart_values(self) -> dict[str, int]:
        return {
            "input": max(0, self.input_tokens - self.cached_input_tokens),
            "cached": self.cached_input_tokens,
            "output": self.output_tokens,
            "total": self.total_tokens,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "TokenUsageBreakdown | None":
        if not isinstance(payload, dict):
            return None
        keys = (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        )
        values = [payload.get(key) for key in keys]
        if not all(isinstance(value, int) and value >= 0 for value in values):
            return None
        return cls(*values)


@dataclass(frozen=True, slots=True)
class ThreadMetadata:
    cwd: str
    source: str
    thread_id: str | None


@dataclass(slots=True)
class _RolloutCursor:
    device: int
    inode: int
    offset: int = 0
    mtime_ns: int = 0


class ProjectTokenUsageMonitor:
    """Track one current root thread without rescanning completed transcript data."""

    def __init__(
        self,
        sessions_root: Path,
        project_cwd: Path,
        thread_id: str | None = None,
        discovery_interval: float = DISCOVERY_INTERVAL_SECONDS,
    ) -> None:
        self.sessions_root = sessions_root.expanduser()
        self.project_cwd = normalized_path(project_cwd)
        self.preferred_thread_id = thread_id.strip() if isinstance(thread_id, str) and thread_id.strip() else None
        self.discovery_interval = max(0.1, float(discovery_interval))
        self.active_path: Path | None = None
        self.active_thread_id: str | None = None
        self.latest: TokenUsageBreakdown | None = None
        self.latest_event_at: float | None = None
        self.rate_series: dict[str, list[dict[str, float]]] = {
            key: [] for key in TOKEN_RATE_KEYS
        }
        self.version = 0
        self.error: str | None = None
        self.bytes_read = 0
        self.full_loads = 0
        self.incremental_reads = 0
        self._cursor: _RolloutCursor | None = None
        self._metadata_cache: dict[Path, ThreadMetadata] = {}
        self._previous_timestamp: float | None = None
        self._previous_usage: TokenUsageBreakdown | None = None
        self._smoothed_rates: dict[str, float | None] = {
            key: None for key in TOKEN_RATE_KEYS
        }
        self._next_discovery = 0.0

    @property
    def rate_points(self) -> list[dict[str, float]]:
        return self.rate_series["total"]

    def _reset_rates(self) -> None:
        self.rate_series = {key: [] for key in TOKEN_RATE_KEYS}
        self._smoothed_rates = {key: None for key in TOKEN_RATE_KEYS}

    def _write_rate_point(self, key: str, timestamp: float, value: float) -> None:
        points = self.rate_series[key]
        point = {"t": timestamp, "value": max(0.0, float(value))}
        if points and points[-1]["t"] == timestamp:
            points[-1] = point
        elif not points or points[-1]["value"] != point["value"]:
            points.append(point)

    def _append_rate_interval(
        self,
        start_timestamp: float,
        end_timestamp: float,
        previous: TokenUsageBreakdown,
        current: TokenUsageBreakdown,
    ) -> None:
        elapsed = end_timestamp - start_timestamp
        if elapsed <= 0:
            return
        previous_values = previous.chart_values()
        current_values = current.chart_values()
        for key in TOKEN_RATE_KEYS:
            delta = current_values[key] - previous_values[key]
            if delta <= 0:
                smoothed = 0.0
                self._smoothed_rates[key] = None
            else:
                raw_rate = delta / elapsed
                previous_rate = self._smoothed_rates[key]
                smoothed = (
                    raw_rate
                    if previous_rate is None
                    else TOKEN_RATE_SMOOTHING_ALPHA * raw_rate
                    + (1.0 - TOKEN_RATE_SMOOTHING_ALPHA) * previous_rate
                )
                self._smoothed_rates[key] = smoothed
            self._write_rate_point(key, start_timestamp, smoothed)
            self._write_rate_point(key, end_timestamp, 0.0)

    def _read_metadata(self, path: Path) -> ThreadMetadata | None:
        cached = self._metadata_cache.get(path)
        if cached is not None:
            return cached
        try:
            with path.open("rb") as handle:
                raw = handle.readline()
        except OSError:
            return None
        if not raw.endswith(b"\n"):
            return None
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        payload = record.get("payload") if isinstance(record, dict) else None
        if record.get("type") != "session_meta" or not isinstance(payload, dict):
            return None
        cwd = payload.get("cwd")
        source = payload.get("source")
        thread_id = payload.get("id") or payload.get("session_id")
        if not isinstance(cwd, str) or not isinstance(source, str):
            return None
        metadata = ThreadMetadata(
            normalized_path(cwd),
            source,
            str(thread_id) if isinstance(thread_id, str) and thread_id else None,
        )
        self._metadata_cache[path] = metadata
        return metadata

    def _rollout_candidates(self) -> list[tuple[int, Path]]:
        candidates: list[tuple[int, Path]] = []
        if not self.sessions_root.exists():
            return candidates
        for path in self.sessions_root.rglob("rollout-*.jsonl"):
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                continue
            candidates.append((mtime_ns, path))
        candidates.sort(reverse=True)
        return candidates

    def _matches_project(self, metadata: ThreadMetadata | None) -> bool:
        return (
            metadata is not None
            and metadata.cwd == self.project_cwd
            and metadata.source in INTERACTIVE_THREAD_SOURCES
        )

    def _discover_active(self) -> tuple[Path, ThreadMetadata] | None:
        if (
            self.preferred_thread_id
            and self.active_path is not None
            and self.active_thread_id == self.preferred_thread_id
            and self.active_path.exists()
        ):
            metadata = self._read_metadata(self.active_path)
            if self._matches_project(metadata):
                return self.active_path, metadata

        candidates = self._rollout_candidates()
        if not candidates:
            return None
        if self.preferred_thread_id:
            likely = [
                item for item in candidates
                if self.preferred_thread_id in item[1].name
            ]
            remaining = [item for item in candidates if item not in likely]
            for _mtime, path in likely + remaining:
                metadata = self._read_metadata(path)
                if (
                    self._matches_project(metadata)
                    and metadata.thread_id == self.preferred_thread_id
                ):
                    return path, metadata
        for _mtime, path in candidates:
            metadata = self._read_metadata(path)
            if self._matches_project(metadata):
                return path, metadata
        return None

    def _select_active(self, path: Path | None, metadata: ThreadMetadata | None = None) -> None:
        if path == self.active_path:
            return
        self.active_path = path
        self.active_thread_id = metadata.thread_id if metadata is not None else None
        self.latest = None
        self.latest_event_at = None
        self._reset_rates()
        self._cursor = None
        self._previous_timestamp = None
        self._previous_usage = None
        self.version += 1

    def _parse_usage_record(self, raw: bytes) -> tuple[float, TokenUsageBreakdown] | None:
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            return None
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            return None
        info = payload.get("info")
        if not isinstance(info, dict):
            return None
        usage = TokenUsageBreakdown.from_payload(info.get("total_token_usage"))
        timestamp = event_timestamp(record.get("timestamp"))
        if usage is None or timestamp is None:
            return None
        return timestamp, usage

    def _append_usage(self, timestamp: float, usage: TokenUsageBreakdown) -> bool:
        changed = usage != self.latest or timestamp != self.latest_event_at
        previous_timestamp = self._previous_timestamp
        previous_usage = self._previous_usage
        if previous_timestamp is None or previous_usage is None:
            for key in TOKEN_RATE_KEYS:
                self._write_rate_point(key, timestamp, 0.0)
        elif timestamp > previous_timestamp:
            elapsed = timestamp - previous_timestamp
            if usage.total_tokens < previous_usage.total_tokens or elapsed > GAP_SECONDS:
                self._smoothed_rates = {key: None for key in TOKEN_RATE_KEYS}
                for key in TOKEN_RATE_KEYS:
                    self._write_rate_point(key, timestamp, 0.0)
            else:
                self._append_rate_interval(previous_timestamp, timestamp, previous_usage, usage)
            self._previous_timestamp = timestamp
            self._previous_usage = usage
        if previous_timestamp is None:
            self._previous_timestamp = timestamp
            self._previous_usage = usage
        self.latest = usage
        self.latest_event_at = timestamp
        return changed

    def _read_active(self) -> bool:
        path = self.active_path
        if path is None:
            return False
        try:
            stat = path.stat()
        except OSError as exc:
            self.error = str(exc)
            return False
        cursor = self._cursor
        if (
            cursor is None
            or cursor.device != stat.st_dev
            or cursor.inode != stat.st_ino
            or stat.st_size < cursor.offset
        ):
            self.latest = None
            self.latest_event_at = None
            self._reset_rates()
            self._previous_timestamp = None
            self._previous_usage = None
            cursor = _RolloutCursor(stat.st_dev, stat.st_ino)
            self._cursor = cursor
            self.full_loads += 1
        elif stat.st_size == cursor.offset and stat.st_mtime_ns == cursor.mtime_ns:
            return False
        else:
            self.incremental_reads += 1

        changed = False
        try:
            with path.open("rb") as handle:
                handle.seek(cursor.offset)
                while True:
                    line_start = handle.tell()
                    raw = handle.readline()
                    if not raw or not raw.endswith(b"\n"):
                        cursor.offset = line_start
                        break
                    cursor.offset = handle.tell()
                    self.bytes_read += len(raw)
                    parsed = self._parse_usage_record(raw)
                    if parsed is not None:
                        changed = self._append_usage(*parsed) or changed
                cursor.mtime_ns = os.fstat(handle.fileno()).st_mtime_ns
        except OSError as exc:
            self.error = str(exc)
            return False
        self.error = None
        if changed:
            self.version += 1
        return changed

    def poll(self, force_discovery: bool = False, now: float | None = None) -> bool:
        observed = time.monotonic() if now is None else float(now)
        changed = False
        if force_discovery or observed >= self._next_discovery or self.active_path is None:
            discovered = self._discover_active()
            if discovered is None:
                if self.active_path is not None:
                    self._select_active(None)
                    changed = True
            else:
                path, metadata = discovered
                if path != self.active_path:
                    self._select_active(path, metadata)
                    changed = True
            self._next_discovery = observed + self.discovery_interval
        return self._read_active() or changed


@dataclass(frozen=True, slots=True)
class _UsageInterval:
    start: float
    end: float
    rates: tuple[float, float, float, float]


@dataclass(slots=True)
class _TrackedRollout:
    path: Path
    metadata: ThreadMetadata
    owner_key: str | None
    device: int
    inode: int
    offset: int = 0
    mtime_ns: int = 0
    size: int = 0
    loaded: bool = False
    latest: TokenUsageBreakdown | None = None
    latest_event_at: float | None = None
    previous_timestamp: float | None = None
    previous_usage: TokenUsageBreakdown | None = None
    smoothed_rates: dict[str, float | None] = field(
        default_factory=lambda: {key: None for key in TOKEN_RATE_KEYS}
    )
    intervals: list[_UsageInterval] = field(default_factory=list)

    def reset_content(self, device: int, inode: int) -> None:
        self.device = device
        self.inode = inode
        self.offset = 0
        self.mtime_ns = 0
        self.size = 0
        self.loaded = False
        self.latest = None
        self.latest_event_at = None
        self.previous_timestamp = None
        self.previous_usage = None
        self.smoothed_rates = {key: None for key in TOKEN_RATE_KEYS}
        self.intervals.clear()


class TrustedDirectoryTokenUsageMonitor:
    """Aggregate root-thread Token usage for one or all trusted directories."""

    def __init__(
        self,
        sessions_root: Path,
        project_cwd: Path,
        config_path: Path,
        directory_registry_path: Path,
        scope: str = "current",
        discovery_interval: float = DISCOVERY_INTERVAL_SECONDS,
    ) -> None:
        self.sessions_root = sessions_root.expanduser()
        self.project_cwd = resolved_path(project_cwd)
        self.scope = scope if scope in {"current", "all"} else "current"
        self.discovery_interval = max(0.1, float(discovery_interval))
        self.registry = TrustedDirectoryRegistry(config_path.expanduser(), directory_registry_path.expanduser())
        self.latest: TokenUsageBreakdown | None = None
        self.latest_event_at: float | None = None
        self.rate_series: dict[str, list[dict[str, float]]] = {
            key: [] for key in TOKEN_RATE_KEYS
        }
        self.active_path: Path | None = None
        self.active_thread_id: str | None = None
        self.version = 0
        self.error: str | None = None
        self.bytes_read = 0
        self.full_loads = 0
        self.incremental_reads = 0
        self.stat_checks = 0
        self.metadata_reads = 0
        self._states: dict[Path, _TrackedRollout] = {}
        self._hot_paths: set[Path] = set()
        self._next_discovery = 0.0
        self._scope_changed = True
        self.registry.poll(force=True)
        self.error = self.registry.error

    @property
    def rate_points(self) -> list[dict[str, float]]:
        return self.rate_series["total"]

    @property
    def loaded_rollouts(self) -> int:
        return sum(state.loaded for state in self._states.values())

    def set_scope(self, scope: str) -> bool:
        normalized = scope if scope in {"current", "all"} else "current"
        if normalized == self.scope:
            return False
        self.scope = normalized
        self._scope_changed = True
        self._next_discovery = 0.0
        return True

    def sync_directories(self, force: bool = False) -> bool:
        changed = self.registry.poll(force=force)
        if changed:
            self._refresh_owners()
            self._scope_changed = True
            self._next_discovery = 0.0
        self.error = self.registry.error
        return changed

    def _read_metadata(self, path: Path) -> ThreadMetadata | None:
        try:
            with path.open("rb") as handle:
                raw = handle.readline()
        except OSError:
            return None
        self.metadata_reads += 1
        if not raw.endswith(b"\n"):
            return None
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        payload = record.get("payload") if isinstance(record, dict) else None
        if record.get("type") != "session_meta" or not isinstance(payload, dict):
            return None
        cwd = payload.get("cwd")
        source = payload.get("source")
        thread_id = payload.get("id") or payload.get("session_id")
        if not isinstance(cwd, str) or not isinstance(source, str):
            return None
        return ThreadMetadata(
            resolved_path(cwd),
            source,
            str(thread_id) if isinstance(thread_id, str) and thread_id else None,
        )

    def _current_owner_key(self) -> str | None:
        owner = self.registry.owner_for(self.project_cwd)
        return owner.key if owner is not None else None

    def _included(self, state: _TrackedRollout) -> bool:
        if state.metadata.source not in INTERACTIVE_THREAD_SOURCES:
            return False
        owner = self.registry.by_key(state.owner_key)
        if owner is None or owner.disable:
            return False
        return self.scope == "all" or owner.key == self._current_owner_key()

    @staticmethod
    def _parse_usage_record(raw: bytes) -> tuple[float, TokenUsageBreakdown] | None:
        if b'"token_count"' not in raw:
            return None
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            return None
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            return None
        info = payload.get("info")
        if not isinstance(info, dict):
            return None
        usage = TokenUsageBreakdown.from_payload(info.get("total_token_usage"))
        timestamp = event_timestamp(record.get("timestamp"))
        if usage is None or timestamp is None:
            return None
        return timestamp, usage

    @staticmethod
    def _append_usage(state: _TrackedRollout, timestamp: float, usage: TokenUsageBreakdown) -> bool:
        changed = usage != state.latest or timestamp != state.latest_event_at
        previous_timestamp = state.previous_timestamp
        previous_usage = state.previous_usage
        if previous_timestamp is not None and previous_usage is not None and timestamp > previous_timestamp:
            elapsed = timestamp - previous_timestamp
            if usage.total_tokens < previous_usage.total_tokens or elapsed > GAP_SECONDS:
                state.smoothed_rates = {key: None for key in TOKEN_RATE_KEYS}
            else:
                previous_values = previous_usage.chart_values()
                current_values = usage.chart_values()
                rates: list[float] = []
                for key in TOKEN_RATE_KEYS:
                    delta = current_values[key] - previous_values[key]
                    if delta <= 0:
                        smoothed = 0.0
                        state.smoothed_rates[key] = None
                    else:
                        raw_rate = delta / elapsed
                        previous_rate = state.smoothed_rates[key]
                        smoothed = (
                            raw_rate
                            if previous_rate is None
                            else TOKEN_RATE_SMOOTHING_ALPHA * raw_rate
                            + (1.0 - TOKEN_RATE_SMOOTHING_ALPHA) * previous_rate
                        )
                        state.smoothed_rates[key] = smoothed
                    rates.append(max(0.0, smoothed))
                if any(rates):
                    state.intervals.append(_UsageInterval(previous_timestamp, timestamp, tuple(rates)))
            state.previous_timestamp = timestamp
            state.previous_usage = usage
        elif previous_timestamp is None:
            state.previous_timestamp = timestamp
            state.previous_usage = usage
        state.latest = usage
        state.latest_event_at = timestamp
        return changed

    def _read_state(self, state: _TrackedRollout, stat: os.stat_result) -> bool:
        if state.loaded and stat.st_size == state.size and stat.st_mtime_ns == state.mtime_ns:
            return False
        if (
            not state.loaded
            or state.device != stat.st_dev
            or state.inode != stat.st_ino
            or stat.st_size < state.offset
        ):
            state.reset_content(stat.st_dev, stat.st_ino)
            self.full_loads += 1
        else:
            self.incremental_reads += 1

        changed = False
        try:
            with state.path.open("rb") as handle:
                handle.seek(state.offset)
                while True:
                    line_start = handle.tell()
                    raw = handle.readline()
                    if not raw or not raw.endswith(b"\n"):
                        state.offset = line_start
                        break
                    state.offset = handle.tell()
                    self.bytes_read += len(raw)
                    parsed = self._parse_usage_record(raw)
                    if parsed is not None:
                        changed = self._append_usage(state, *parsed) or changed
                state.mtime_ns = os.fstat(handle.fileno()).st_mtime_ns
                state.size = stat.st_size
                state.loaded = True
        except OSError as exc:
            self.error = str(exc)
            return False
        return changed

    def _refresh_path(self, path: Path, force_load: bool = False) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        self.stat_checks += 1
        state = self._states.get(path)
        if state is None:
            metadata = self._read_metadata(path)
            if metadata is None:
                return False
            owner = self.registry.owner_for(metadata.cwd)
            state = _TrackedRollout(
                path,
                metadata,
                owner.key if owner is not None else None,
                stat.st_dev,
                stat.st_ino,
            )
            self._states[path] = state
        if stat.st_mtime >= time.time() - HOT_ROLLOUT_SECONDS:
            self._hot_paths.add(path)
        elif path in self._hot_paths:
            self._hot_paths.discard(path)
        if not self._included(state):
            return False
        if not force_load and state.loaded and stat.st_size == state.size and stat.st_mtime_ns == state.mtime_ns:
            return False
        return self._read_state(state, stat)

    def _refresh_owners(self) -> None:
        for state in self._states.values():
            owner = self.registry.owner_for(state.metadata.cwd)
            state.owner_key = owner.key if owner is not None else None

    def _discover(self) -> bool:
        changed = False
        seen: set[Path] = set()
        if self.sessions_root.exists():
            for path in self.sessions_root.rglob("rollout-*.jsonl"):
                seen.add(path)
                changed = self._refresh_path(path, force_load=self._scope_changed) or changed
        removed = set(self._states) - seen
        if removed:
            for path in removed:
                self._states.pop(path, None)
                self._hot_paths.discard(path)
            changed = True
        return changed

    def _rebuild_aggregate(self) -> None:
        included = [state for state in self._states.values() if state.loaded and self._included(state)]
        totals = [0, 0, 0, 0, 0]
        latest_state: _TrackedRollout | None = None
        rate_changes: dict[str, dict[float, float]] = {
            key: {} for key in TOKEN_RATE_KEYS
        }
        for state in included:
            if state.latest is not None:
                values = (
                    state.latest.input_tokens,
                    state.latest.cached_input_tokens,
                    state.latest.output_tokens,
                    state.latest.reasoning_output_tokens,
                    state.latest.total_tokens,
                )
                for index, value in enumerate(values):
                    totals[index] += value
                if latest_state is None or (state.latest_event_at or 0) > (latest_state.latest_event_at or 0):
                    latest_state = state
            for interval in state.intervals:
                for index, key in enumerate(TOKEN_RATE_KEYS):
                    rate = interval.rates[index]
                    if rate <= 0:
                        continue
                    changes = rate_changes[key]
                    changes[interval.start] = changes.get(interval.start, 0.0) + rate
                    changes[interval.end] = changes.get(interval.end, 0.0) - rate

        self.latest = TokenUsageBreakdown(*totals) if included else None
        self.latest_event_at = latest_state.latest_event_at if latest_state is not None else None
        self.active_path = latest_state.path if latest_state is not None else None
        self.active_thread_id = latest_state.metadata.thread_id if latest_state is not None else None
        rebuilt: dict[str, list[dict[str, float]]] = {}
        for key, changes in rate_changes.items():
            running = 0.0
            points: list[dict[str, float]] = []
            for timestamp in sorted(changes):
                running = max(0.0, running + changes[timestamp])
                value = 0.0 if abs(running) < 1e-9 else running
                if points and points[-1]["value"] == value:
                    continue
                points.append({"t": timestamp, "value": value})
            rebuilt[key] = points
        self.rate_series = rebuilt

    def poll(self, force_discovery: bool = False, now: float | None = None) -> bool:
        observed = time.monotonic() if now is None else float(now)
        registry_changed = self.sync_directories(force=force_discovery or not self.registry.directories)

        changed = registry_changed or self._scope_changed
        if force_discovery or observed >= self._next_discovery or not self._states or self._scope_changed:
            changed = self._discover() or changed
            self._next_discovery = observed + self.discovery_interval
        else:
            for path in tuple(self._hot_paths):
                changed = self._refresh_path(path) or changed

        if changed:
            self._rebuild_aggregate()
            self.version += 1
        self._scope_changed = False
        return changed
