import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from ui.app import render_frame, run_tui
from ui.history import IncrementalHistoryCache
from ui.models import MonitorState
from ui.preload import (
    HISTORY_SNAPSHOT_FILE,
    TOKEN_SNAPSHOT_FILE,
    BackgroundDataPreloader,
    HistoryPreloadResult,
)
from ui.session_tokens import TrustedDirectoryTokenUsageMonitor
from ui.terminal_text import strip_ansi
from core.i18n import t


def state_for_test(root: Path, display_scope: str, token_monitor=None, preloader=None) -> MonitorState:
    return MonitorState(
        period="1h",
        interval=5,
        tz="Asia/Shanghai",
        log_path=root / "quota_snapshots.jsonl",
        restore_interval=60,
        state_path=root / "codextop_state.json",
        curve_mode="connected",
        display_scope=display_scope,
        window_scope="both",
        color_scheme="classic",
        usage_directory_scope="all",
        token_monitor=token_monitor,
        preloader=preloader,
    )


def empty_history_result(root: Path) -> HistoryPreloadResult:
    cache = IncrementalHistoryCache(root / "quota_snapshots.jsonl", "Asia/Shanghai")
    records, _changed = cache.read("all")
    return HistoryPreloadResult(cache, records, time.time())


def token_monitor(root: Path, scope: str = "all") -> TrustedDirectoryTokenUsageMonitor:
    return TrustedDirectoryTokenUsageMonitor(
        root / "sessions",
        root / "project",
        root / "config.toml",
        root / "settings" / "token_usage_directories.json",
        scope,
    )


def preloader_for(root: Path, **kwargs) -> BackgroundDataPreloader:
    return BackgroundDataPreloader(
        root / "quota_snapshots.jsonl",
        "Asia/Shanghai",
        root / "sessions",
        root / "project",
        root / "config.toml",
        root / "settings" / "token_usage_directories.json",
        start_delay=0,
        **kwargs,
    )


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def session_meta(project: Path) -> dict:
    return {
        "timestamp": 1_000,
        "type": "session_meta",
        "payload": {"id": "thread", "cwd": str(project), "source": "cli"},
    }


def token_event(total: int) -> dict:
    return {
        "timestamp": 1_060,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total,
                }
            },
        },
    }


class BackgroundPreloadTests(unittest.TestCase):
    def test_history_snapshot_reuses_index_and_reads_only_appended_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            monthly = root / "quota_snapshots_2026-08.jsonl"
            write_jsonl(monthly, [{"t": 100, "a": []}])
            cache_dir = root / "cache"

            first = preloader_for(root, cache_dir=cache_dir)._load_history()
            first_bytes = first.cache.bytes_read
            snapshot = cache_dir / HISTORY_SNAPSHOT_FILE
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)

            second = preloader_for(root, cache_dir=cache_dir)._load_history()
            self.assertEqual(second.cache.full_loads, 1)
            self.assertEqual(second.cache.incremental_reads, 1)
            self.assertEqual(second.cache.bytes_read, first_bytes)

            with monthly.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"t": 200, "a": []}) + "\n")
            third = preloader_for(root, cache_dir=cache_dir)._load_history()
            self.assertEqual(third.cache.full_loads, 1)
            self.assertEqual([record["t"] for record in third.records], [100, 200])
            self.assertGreater(third.cache.bytes_read, first_bytes)

            previous_mtime = monthly.stat().st_mtime_ns
            write_jsonl(monthly, [{"t": 300, "a": []}, {"t": 400, "a": []}])
            os.utime(monthly, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))
            fourth = preloader_for(root, cache_dir=cache_dir)._load_history()
            self.assertEqual(fourth.cache.full_loads, 2)
            self.assertEqual([record["t"] for record in fourth.records], [300, 400])

    def test_corrupt_history_snapshot_falls_back_to_raw_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_jsonl(root / "quota_snapshots_2026-08.jsonl", [{"t": 100, "a": []}])
            cache_dir = root / "cache"
            cache_dir.mkdir()
            snapshot = cache_dir / HISTORY_SNAPSHOT_FILE
            snapshot.write_bytes(b"not a pickle")
            snapshot.chmod(0o600)

            result = preloader_for(root, cache_dir=cache_dir)._load_history()

            self.assertEqual(len(result.records), 1)
            self.assertEqual(result.cache.full_loads, 1)
            self.assertGreater(snapshot.stat().st_size, len(b"not a pickle"))

    def test_history_snapshot_from_another_app_version_is_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_jsonl(root / "quota_snapshots_2026-08.jsonl", [{"t": 100, "a": []}])
            cache_dir = root / "cache"
            preloader_for(root, cache_dir=cache_dir)._load_history()

            with patch("ui.preload.PACKAGE_VERSION", "next-version"):
                result = preloader_for(root, cache_dir=cache_dir)._load_history()

            self.assertEqual(result.cache.full_loads, 1)
            self.assertEqual(result.cache.incremental_reads, 0)

    def test_token_snapshot_reuses_index_and_invalidates_same_size_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            project.mkdir()
            config = root / "config.toml"
            config.write_text(f'[projects."{project}"]\ntrust_level = "trusted"\n', encoding="utf-8")
            rollout = root / "sessions" / "rollout-thread.jsonl"
            write_jsonl(rollout, [session_meta(project), token_event(100)])
            cache_dir = root / "cache"

            first = preloader_for(root, cache_dir=cache_dir)._load_all_token_usage()
            snapshot = cache_dir / TOKEN_SNAPSHOT_FILE
            self.assertEqual(first.latest.total_tokens, 100)
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)

            second = preloader_for(root, cache_dir=cache_dir)._load_all_token_usage()
            self.assertEqual(second.full_loads, 1)
            self.assertEqual(second.bytes_read, first.bytes_read)

            previous_mtime = rollout.stat().st_mtime_ns
            write_jsonl(rollout, [session_meta(project), token_event(200)])
            os.utime(rollout, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))
            third = preloader_for(root, cache_dir=cache_dir)._load_all_token_usage()
            self.assertEqual(third.full_loads, 2)
            self.assertEqual(third.latest.total_tokens, 200)

    def test_single_worker_prioritizes_hidden_display_then_broader_token_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            order = []

            def load_history():
                order.append("history")
                return empty_history_result(root)

            def load_token():
                order.append("token")
                monitor = token_monitor(root)
                monitor.version = 1
                return monitor

            preloader = preloader_for(root, history_loader=load_history, token_loader=load_token)
            self.assertTrue(preloader.start("usage", "current"))
            preloader.wait(2)

            self.assertEqual(order, ["history", "token"])
            self.assertTrue(preloader.history_scheduled)
            self.assertTrue(preloader.token_scheduled)

    def test_usage_all_prioritizes_visible_token_before_hidden_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            order = []

            def load_history():
                order.append("history")
                return empty_history_result(root)

            def load_token():
                order.append("token")
                monitor = token_monitor(root)
                monitor.version = 1
                return monitor

            preloader = preloader_for(root, history_loader=load_history, token_loader=load_token)

            preloader.start("usage", "all")
            preloader.wait(2)

            self.assertEqual(order, ["token", "history"])
            self.assertTrue(preloader.history_scheduled)
            self.assertTrue(preloader.token_scheduled)

    def test_pending_history_switch_keeps_render_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            started = threading.Event()
            release = threading.Event()

            def load_history():
                started.set()
                release.wait(2)
                return empty_history_result(root)

            preloader = preloader_for(root, history_loader=load_history)
            preloader.start("usage", "all")
            self.assertTrue(started.wait(1))
            state = state_for_test(root, "all", preloader=preloader)
            try:
                with patch("ui.app.read_records_if_due") as read_records:
                    before = time.perf_counter()
                    lines, _zones = render_frame(state, 120, 36)
                    elapsed = time.perf_counter() - before

                read_records.assert_not_called()
                self.assertLess(elapsed, 0.2)
                self.assertTrue(state.history_preload_waiting)
                self.assertIn(t("status.quota_preloading"), "\n".join(strip_ansi(line) for line in lines))
            finally:
                release.set()
                preloader.wait(2)

    def test_pending_token_switch_keeps_render_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            started = threading.Event()
            release = threading.Event()
            placeholder = token_monitor(root, "current")

            def load_token():
                started.set()
                release.wait(2)
                monitor = token_monitor(root, "all")
                monitor.version = 1
                return monitor

            preloader = preloader_for(root, token_loader=load_token)
            preloader.start("all", "current")
            self.assertTrue(started.wait(1))
            state = state_for_test(root, "usage", token_monitor=placeholder, preloader=preloader)
            try:
                with patch.object(placeholder, "poll", wraps=placeholder.poll) as poll:
                    before = time.perf_counter()
                    lines, _zones = render_frame(state, 120, 36)
                    elapsed = time.perf_counter() - before

                poll.assert_not_called()
                self.assertLess(elapsed, 0.2)
                self.assertTrue(state.token_preload_waiting)
                self.assertIn(t("status.token_preloading"), "\n".join(strip_ansi(line) for line in lines))
            finally:
                release.set()
                preloader.wait(2)

    def test_ready_history_is_adopted_before_quota_render(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = empty_history_result(root)
            preloader = preloader_for(root, history_loader=lambda: result)
            preloader.start("usage", "all")
            preloader.wait(2)
            state = state_for_test(root, "all", preloader=preloader)

            render_frame(state, 120, 36)

            self.assertIs(state.history_cache, result.cache)
            self.assertIs(state.records, result.records)
            self.assertFalse(state.history_preload_waiting)
            self.assertFalse(preloader.history_scheduled)

    def test_ready_history_is_adopted_for_usage_border_color(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = empty_history_result(root)
            preloader = preloader_for(root, history_loader=lambda: result)
            preloader.start("usage", "all")
            preloader.wait(2)
            state = state_for_test(root, "usage", preloader=preloader)

            render_frame(state, 120, 36)

            self.assertIs(state.history_cache, result.cache)
            self.assertIs(state.records, result.records)
            self.assertFalse(preloader.history_scheduled)

    def test_ready_token_monitor_is_adopted_before_usage_render(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ready = token_monitor(root, "all")
            ready.version = 1
            preloader = preloader_for(root, token_loader=lambda: ready)
            preloader.start("all", "current")
            preloader.wait(2)
            placeholder = token_monitor(root, "current")
            state = state_for_test(root, "usage", token_monitor=placeholder, preloader=preloader)

            render_frame(state, 120, 36)

            self.assertIs(state.token_monitor, ready)
            self.assertEqual(state.token_version, ready.version)
            self.assertFalse(state.token_preload_waiting)
            self.assertFalse(preloader.token_scheduled)

    def test_tui_starts_background_work_before_the_first_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            loaded = threading.Event()

            def load_token():
                loaded.set()
                monitor = token_monitor(root, "all")
                monitor.version = 1
                return monitor

            preloader = preloader_for(root, token_loader=load_token)
            state = state_for_test(root, "all", token_monitor=token_monitor(root, "current"), preloader=preloader)
            inputs = iter([(True, [], ["f9"]), (False, [], [])])
            preload_started_at_render = []

            def render_frame(*_args):
                preload_started_at_render.append(state.background_preload_started)
                return ["frame"], []

            class DummyTerminalSession:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            with (
                patch("ui.app.start_daily_update_check"),
                patch("ui.app.TerminalSession", return_value=DummyTerminalSession()),
                patch("ui.app.parse_input", side_effect=lambda _session: next(inputs)),
                patch("ui.app.render_frame", side_effect=render_frame) as render,
                patch("ui.app.save_codextop_state"),
                patch("ui.app.send_sampler_interval"),
                patch.object(preloader, "persist", wraps=preloader.persist) as persist,
                patch("sys.stdout", new=io.StringIO()),
            ):
                result = run_tui(state)

            preloader.wait(2)
            self.assertEqual(result, 0)
            self.assertEqual(render.call_count, 2)
            self.assertTrue(all(preload_started_at_render))
            self.assertTrue(state.background_preload_started)
            self.assertTrue(loaded.is_set())
            persist.assert_called_once_with(state.history_cache, state.token_monitor)

    def test_tui_first_frame_shows_history_loading_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            release = threading.Event()

            def load_history():
                release.wait(2)
                return empty_history_result(root)

            preloader = preloader_for(root, history_loader=load_history)
            state = state_for_test(root, "all", token_monitor=token_monitor(root), preloader=preloader)
            state.daily_summary_date = time.strftime("%Y-%m-%d")

            class DummyTerminalSession:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            output = io.StringIO()
            try:
                with (
                    patch("ui.app.start_daily_update_check"),
                    patch("ui.app.TerminalSession", return_value=DummyTerminalSession()),
                    patch("ui.app.parse_input", return_value=(False, [], [])),
                    patch("ui.app.save_codextop_state"),
                    patch("ui.app.send_sampler_interval"),
                    patch("sys.stdout", new=output),
                ):
                    result = run_tui(state)
            finally:
                release.set()
                preloader.wait(2)

            self.assertEqual(result, 0)
            self.assertIn(t("app.quota_preloading"), strip_ansi(output.getvalue()))

    def test_usage_all_tui_first_frame_shows_token_loading_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            release = threading.Event()

            def load_token():
                release.wait(2)
                monitor = token_monitor(root)
                monitor.version = 1
                return monitor

            preloader = preloader_for(root, token_loader=load_token)
            state = state_for_test(root, "usage", token_monitor=token_monitor(root), preloader=preloader)
            state.daily_summary_date = time.strftime("%Y-%m-%d")

            class DummyTerminalSession:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            output = io.StringIO()
            try:
                with (
                    patch("ui.app.start_daily_update_check"),
                    patch("ui.app.TerminalSession", return_value=DummyTerminalSession()),
                    patch("ui.app.parse_input", return_value=(False, [], [])),
                    patch("ui.app.save_codextop_state"),
                    patch("ui.app.send_sampler_interval"),
                    patch("sys.stdout", new=output),
                ):
                    result = run_tui(state)
            finally:
                release.set()
                preloader.wait(2)

            self.assertEqual(result, 0)
            self.assertIn(t("app.token_preloading"), strip_ansi(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
