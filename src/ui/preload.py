"""Low-priority background loading for data hidden by the current UI mode."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .history import HistoryRecordList, IncrementalHistoryCache
from .session_tokens import TrustedDirectoryTokenUsageMonitor


DEFAULT_PRELOAD_DELAY_SECONDS = 0.5
PRELOAD_TASK_GAP_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class HistoryPreloadResult:
    cache: IncrementalHistoryCache
    records: HistoryRecordList
    completed_at: float


class BackgroundDataPreloader:
    """Build hidden-mode caches on one daemon worker after the first frame."""

    def __init__(
        self,
        log_path: Path,
        tz_name: str,
        sessions_root: Path,
        project_cwd: Path,
        config_path: Path,
        directory_registry_path: Path,
        *,
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
        cache = IncrementalHistoryCache(
            self.log_path,
            self.tz_name,
            cooperative_yield_seconds=0.001,
        )
        records, _changed = cache.read("all")
        return HistoryPreloadResult(cache, records, time.time())

    def _load_all_token_usage(self) -> TrustedDirectoryTokenUsageMonitor:
        monitor = TrustedDirectoryTokenUsageMonitor(
            self.sessions_root,
            self.project_cwd,
            self.config_path,
            self.directory_registry_path,
            "all",
        )
        monitor.poll(force_discovery=True)
        return monitor

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
        """Queue only hidden or broader data, with one worker to limit contention."""
        with self._lock:
            if self._started:
                return False
            self._started = True
            tasks: list[tuple[Future, Callable[[], object]]] = []
            if initial_display_scope == "usage":
                self._history_future = Future()
                tasks.append((self._history_future, self._history_loader))
                if initial_usage_scope != "all":
                    self._token_future = Future()
                    tasks.append((self._token_future, self._token_loader))
            else:
                self._token_future = Future()
                tasks.append((self._token_future, self._token_loader))
            if not tasks:
                return True
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
