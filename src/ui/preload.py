"""Low-priority background loading for startup and hidden UI data."""

from __future__ import annotations

import os
import pickle
import stat
import tempfile
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.version import PACKAGE_VERSION
from .history import HistoryRecordList, IncrementalHistoryCache
from .session_tokens import TrustedDirectoryTokenUsageMonitor
from .trusted_directories import resolved_path


DEFAULT_PRELOAD_DELAY_SECONDS = 0.5
PRELOAD_TASK_GAP_SECONDS = 0.05
SNAPSHOT_FORMAT_VERSION = 1
HISTORY_SNAPSHOT_FILE = "history_index.pickle"
TOKEN_SNAPSHOT_FILE = "token_index.pickle"


def _read_snapshot(path: Path, kind: str) -> object | None:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            return None
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            return None
        if metadata.st_mode & 0o077:
            return None
        with path.open("rb") as handle:
            payload = pickle.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("version") != SNAPSHOT_FORMAT_VERSION
        or payload.get("app_version") != PACKAGE_VERSION
        or payload.get("kind") != kind
    ):
        return None
    return payload.get("value")


def _write_snapshot(path: Path, kind: str, value: object) -> bool:
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        descriptor, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp_path = Path(tmp_name)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            pickle.dump(
                {
                    "version": SNAPSHOT_FORMAT_VERSION,
                    "app_version": PACKAGE_VERSION,
                    "kind": kind,
                    "value": value,
                },
                handle,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        return True
    except Exception:
        return False
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True, slots=True)
class HistoryPreloadResult:
    cache: IncrementalHistoryCache
    records: HistoryRecordList
    completed_at: float


class BackgroundDataPreloader:
    """Build startup and hidden-mode caches on one daemon worker."""

    def __init__(
        self,
        log_path: Path,
        tz_name: str,
        sessions_root: Path,
        project_cwd: Path,
        config_path: Path,
        directory_registry_path: Path,
        *,
        cache_dir: Path | None = None,
        start_delay: float = DEFAULT_PRELOAD_DELAY_SECONDS,
        history_loader: Callable[[], HistoryPreloadResult] | None = None,
        token_loader: Callable[[], TrustedDirectoryTokenUsageMonitor] | None = None,
    ) -> None:
        self.log_path = log_path.expanduser()
        self.tz_name = tz_name
        self.sessions_root = sessions_root.expanduser()
        self.project_cwd = project_cwd.expanduser()
        self.config_path = config_path.expanduser()
        self.directory_registry_path = directory_registry_path.expanduser()
        self.cache_dir = cache_dir.expanduser() if cache_dir is not None else None
        self.start_delay = max(0.0, float(start_delay))
        self._history_loader = history_loader or self._load_history
        self._token_loader = token_loader or self._load_all_token_usage
        self._history_future: Future[HistoryPreloadResult] | None = None
        self._token_future: Future[TrustedDirectoryTokenUsageMonitor] | None = None
        self._history_taken = False
        self._token_taken = False
        self._started = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _load_history(self) -> HistoryPreloadResult:
        cached = _read_snapshot(self.cache_dir / HISTORY_SNAPSHOT_FILE, "history") if self.cache_dir else None
        cache = cached if isinstance(cached, IncrementalHistoryCache) and cached.compatible_with(self.log_path, self.tz_name) else None
        restored = cache is not None
        if cache is None:
            cache = IncrementalHistoryCache(
                self.log_path,
                self.tz_name,
                cooperative_yield_seconds=0.001,
            )
        try:
            records, changed = cache.read("all")
        except Exception:
            if not restored:
                raise
            cache = IncrementalHistoryCache(
                self.log_path,
                self.tz_name,
                cooperative_yield_seconds=0.001,
            )
            records, changed = cache.read("all")
            restored = False
        if self.cache_dir is not None and (changed or not restored):
            _write_snapshot(self.cache_dir / HISTORY_SNAPSHOT_FILE, "history", cache)
        return HistoryPreloadResult(cache, records, time.time())

    def _load_all_token_usage(self) -> TrustedDirectoryTokenUsageMonitor:
        cached = _read_snapshot(self.cache_dir / TOKEN_SNAPSHOT_FILE, "token") if self.cache_dir else None
        monitor = cached if isinstance(cached, TrustedDirectoryTokenUsageMonitor) else None
        if monitor is not None and not (
            monitor.sessions_root == self.sessions_root
            and monitor.registry.config_path == self.config_path
            and monitor.registry.registry_path == self.directory_registry_path
        ):
            monitor = None
        restored = monitor is not None
        if monitor is None:
            monitor = TrustedDirectoryTokenUsageMonitor(
                self.sessions_root,
                self.project_cwd,
                self.config_path,
                self.directory_registry_path,
                "all",
            )
        else:
            monitor.project_cwd = resolved_path(self.project_cwd)
            monitor.set_scope("all")
        try:
            changed = monitor.poll(force_discovery=True)
        except Exception:
            if not restored:
                raise
            monitor = TrustedDirectoryTokenUsageMonitor(
                self.sessions_root,
                self.project_cwd,
                self.config_path,
                self.directory_registry_path,
                "all",
            )
            changed = monitor.poll(force_discovery=True)
            restored = False
        if self.cache_dir is not None and (changed or not restored):
            _write_snapshot(self.cache_dir / TOKEN_SNAPSHOT_FILE, "token", monitor)
        return monitor

    def persist(self, history_cache: object, token_monitor: object) -> None:
        """Save adopted caches on clean exit; failures only cost the next cold load."""
        if self.cache_dir is None:
            return
        if (
            isinstance(history_cache, IncrementalHistoryCache)
            and history_cache.compatible_with(self.log_path, self.tz_name)
            and history_cache._initialized
        ):
            _write_snapshot(self.cache_dir / HISTORY_SNAPSHOT_FILE, "history", history_cache)
        if isinstance(token_monitor, TrustedDirectoryTokenUsageMonitor) and token_monitor.version > 0:
            _write_snapshot(self.cache_dir / TOKEN_SNAPSHOT_FILE, "token", token_monitor)

    @staticmethod
    def _complete_future(future: Future, loader: Callable[[], object]) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(loader())
        except Exception as exc:
            future.set_exception(exc)

    def _run(self, tasks: list[tuple[Future, Callable[[], object]]]) -> None:
        if self.start_delay:
            time.sleep(self.start_delay)
        for index, (future, loader) in enumerate(tasks):
            if index:
                time.sleep(PRELOAD_TASK_GAP_SECONDS)
            self._complete_future(future, loader)

    def start(self, initial_display_scope: str, initial_usage_scope: str) -> bool:
        """Queue startup and hidden data, with one worker to limit contention."""
        with self._lock:
            if self._started:
                return False
            self._started = True
            tasks: list[tuple[Future, Callable[[], object]]] = []
            if initial_display_scope == "usage" and initial_usage_scope == "all":
                self._token_future = Future()
                tasks.append((self._token_future, self._token_loader))
            self._history_future = Future()
            tasks.append((self._history_future, self._history_loader))
            if self._token_future is None:
                self._token_future = Future()
                tasks.append((self._token_future, self._token_loader))
            self._thread = threading.Thread(
                target=self._run,
                args=(tasks,),
                name="codextop-preloader",
                daemon=True,
            )
            self._thread.start()
            return True

    @property
    def started(self) -> bool:
        return self._started

    @property
    def history_scheduled(self) -> bool:
        return self._history_future is not None and not self._history_taken

    @property
    def token_scheduled(self) -> bool:
        return self._token_future is not None and not self._token_taken

    @property
    def history_pending(self) -> bool:
        return self.history_scheduled and not self._history_future.done()

    @property
    def token_pending(self) -> bool:
        return self.token_scheduled and not self._token_future.done()

    def take_history_if_ready(self) -> HistoryPreloadResult | None:
        with self._lock:
            future = self._history_future
            if future is None or self._history_taken or not future.done():
                return None
            self._history_taken = True
        return future.result()

    def take_token_if_ready(self) -> TrustedDirectoryTokenUsageMonitor | None:
        with self._lock:
            future = self._token_future
            if future is None or self._token_taken or not future.done():
                return None
            self._token_taken = True
        return future.result()

    def wait(self, timeout: float | None = None) -> None:
        """Test/benchmark helper; the TUI never waits for background work."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
