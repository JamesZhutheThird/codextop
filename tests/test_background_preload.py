import io
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
from ui.preload import BackgroundDataPreloader, HistoryPreloadResult
from ui.session_tokens import TrustedDirectoryTokenUsageMonitor
from ui.terminal_text import strip_ansi


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


class BackgroundPreloadTests(unittest.TestCase):
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

    def test_usage_all_only_preloads_hidden_quota_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            preloader = preloader_for(root, history_loader=lambda: empty_history_result(root))

            preloader.start("usage", "all")
            preloader.wait(2)

            self.assertTrue(preloader.history_scheduled)
            self.assertFalse(preloader.token_scheduled)

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
                self.assertIn("后台预加载 quota", "\n".join(strip_ansi(line) for line in lines))
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
                self.assertIn("后台预加载 Token", "\n".join(strip_ansi(line) for line in lines))
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

    def test_tui_starts_background_work_after_the_first_frame(self) -> None:
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

            class DummyTerminalSession:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            with (
                patch("ui.app.start_daily_update_check"),
                patch("ui.app.TerminalSession", return_value=DummyTerminalSession()),
                patch("ui.app.parse_input", side_effect=lambda _session: next(inputs)),
                patch("ui.app.render_frame", return_value=(["frame"], [])) as render,
                patch("ui.app.save_codextop_state"),
                patch("ui.app.send_sampler_interval"),
                patch("sys.stdout", new=io.StringIO()),
            ):
                result = run_tui(state)

            preloader.wait(2)
            self.assertEqual(result, 0)
            self.assertEqual(render.call_count, 2)
            self.assertTrue(state.background_preload_started)
            self.assertTrue(loaded.is_set())


if __name__ == "__main__":
    unittest.main()
