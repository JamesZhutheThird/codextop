from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.project_config import load_project_config
from core.i18n import t
from core.state import read_codextop_state, save_codextop_state, saved_daily_summary_date
from ui.app import render_frame
from ui.history import HistorySeriesIndex
from ui.models import MonitorState
from ui.preload import BackgroundDataPreloader
from ui.session_tokens import TrustedDirectoryTokenUsageMonitor
from ui.settings import handle_click, handle_setting_key, render_sidebar
from ui.statistics_summary import (
    STATISTICS_AUTO_CLOSE_SECONDS,
    STATISTICS_MODAL_HEIGHT,
    STATISTICS_MODAL_WIDTH,
    StatisticsPeriod,
    build_statistics_summary,
    close_expired_statistics,
    handle_statistics_key,
    _countdown_to,
    _half_columns,
    local_today,
    open_daily_statistics_if_due,
    statistics_period,
)
from ui.terminal_text import strip_ansi, visible_width
from ui.terminal_io import parse_input


def monitor_state(root: Path) -> MonitorState:
    return MonitorState(
        period="1d",
        interval=60,
        tz="Asia/Shanghai",
        log_path=root / "quota_snapshots.jsonl",
        restore_interval=60,
        state_path=root / "codextop_state.json",
        curve_mode="connected",
        display_scope="list",
        window_scope="both",
        color_scheme="classic",
    )


def session_meta(cwd: Path) -> dict:
    return {
        "timestamp": 1_000,
        "type": "session_meta",
        "payload": {"id": "thread", "cwd": str(cwd), "source": "cli"},
    }


def token_event(timestamp: int, total: int, cached: int, output: int, reasoning: int) -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total - output,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": reasoning,
                    "total_tokens": total,
                }
            },
        },
    }


class StatisticsRangeTests(unittest.TestCase):
    def test_local_token_pairs_use_aligned_half_width_columns(self) -> None:
        first = _half_columns("输入 1,234", "缓存 567", 40)
        second = _half_columns("输出 89", "推理 10", 40)
        self.assertEqual(visible_width(first), 40)
        self.assertEqual(visible_width(second), 40)
        self.assertEqual(visible_width(first[:first.index("缓存")]), 20)
        self.assertEqual(visible_width(second[:second.index("推理")]), 20)

    def test_statistics_countdowns_are_compact(self) -> None:
        with patch("ui.statistics_summary.time.time", return_value=1_000):
            self.assertEqual(_countdown_to(1_000 + 4 * 3_600), "4h")
            self.assertEqual(_countdown_to(1_000 + 27 * 86_400 + 20 * 3_600), "27d20h")

    def test_week_defaults_to_monday_and_is_configurable(self) -> None:
        monday = statistics_period(date(2026, 8, 19), "week", 0, "Asia/Shanghai")
        sunday = statistics_period(date(2026, 8, 19), "week", 6, "Asia/Shanghai")
        self.assertEqual(monday.label, "2026-08-17 — 2026-08-23")
        self.assertEqual(sunday.label, "2026-08-16 — 2026-08-22")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pyproject.toml"
            path.write_text('[tool.codextop]\nweek_start = "sunday"\n', encoding="utf-8")
            self.assertEqual(load_project_config(path).week_start, 6)
            path.write_text('[tool.codextop]\nweek_start = "invalid"\n', encoding="utf-8")
            self.assertEqual(load_project_config(path).week_start, 0)

    def test_daily_popup_is_once_per_day_and_expires_after_sixty_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = monitor_state(Path(temp_dir))
            self.assertTrue(
                open_daily_statistics_if_due(state, today=date(2026, 8, 22), monotonic_now=100)
            )
            self.assertEqual(state.statistics_anchor, "2026-08-21")
            self.assertEqual(state.daily_summary_date, "2026-08-22")
            self.assertEqual(state.statistics_auto_close_at, 100 + STATISTICS_AUTO_CLOSE_SECONDS)
            self.assertFalse(
                open_daily_statistics_if_due(state, today=date(2026, 8, 22), monotonic_now=120)
            )
            self.assertFalse(close_expired_statistics(state, 159.9))
            self.assertTrue(close_expired_statistics(state, 160))
            self.assertFalse(state.statistics_open)

    def test_daily_popup_date_is_persisted_with_ui_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = monitor_state(root)
            state.daily_summary_date = "2026-08-22"
            save_codextop_state(state)
            payload = read_codextop_state(state.state_path)
            self.assertEqual(saved_daily_summary_date(payload), "2026-08-22")
            self.assertIsNone(saved_daily_summary_date({"daily_summary_date": "not-a-date"}))


class StatisticsAggregationTests(unittest.TestCase):
    @staticmethod
    def account(index: str, plan: str, left: float, reset: int, tokens: int, token_time: int) -> dict:
        return {
            "i": index,
            "plan": plan,
            "q": {
                "5h": [left, reset, max(0, reset - token_time), 18_000],
                "7d": [left, reset + 500_000, 500_000, 604_800],
            },
            "u": [tokens, token_time],
        }

    def test_quota_system_tokens_and_current_status_use_requested_units(self) -> None:
        index = HistorySeriesIndex()
        index.append_record(
            {
                "t": 900,
                "a": [
                    self.account("plus", "plus", 100, 1_500, 10_000, 800),
                    self.account("pro", "pro-5x", 100, 1_500, 20_000, 800),
                ],
            }
        )
        index.append_record(
            {
                "t": 1_100,
                "a": [
                    self.account("plus", "plus", 80, 1_500, 10_300, 1_100),
                    self.account("pro", "pro-5x", 90, 1_500, 20_100, 1_100),
                ],
            }
        )
        index.append_record(
            {
                "t": 1_510,
                "a": [
                    self.account("plus", "plus", 100, 19_500, 10_300, 1_100),
                    self.account("pro", "pro-5x", 85, 20_000, 20_100, 1_100),
                ],
            }
        )
        latest = [
            {
                **self.account("plus", "plus", 90, 19_500, 10_300, 1_100),
                "rc": 2,
                "r": [["one", 2_200]],
            },
            {
                **self.account("pro", "pro-5x", 80, 20_000, 20_100, 1_100),
                "rc": 3,
                "r": [["two", 2_300]],
            },
        ]
        index.append_record({"t": 1_600, "a": latest})

        summary = build_statistics_summary(
            SimpleNamespace(series_index=index),
            latest,
            None,
            StatisticsPeriod(1_000, 2_000, "test"),
            local_tokens_ready=False,
        )

        self.assertEqual(summary.quota_used["5h"].percent, 60)
        self.assertAlmostEqual(summary.quota_used["5h"].multiple, 1.8)
        self.assertEqual(summary.system_tokens, 400)
        self.assertEqual(summary.current_quota["5h"].percent, 170)
        self.assertAlmostEqual(summary.current_quota["5h"].multiple, 4.9)
        self.assertEqual(summary.reset_epochs, {"5h": 19_500, "7d": 519_500})
        self.assertEqual(summary.reset_credits, 5)
        self.assertEqual(summary.nearest_expiration_epoch, 2_200)

    def test_quota_usage_after_an_unobserved_reset_uses_new_cycle_remaining(self) -> None:
        index = HistorySeriesIndex()
        index.append_record(
            {
                "t": 900,
                "a": [self.account("plus", "plus", 20, 1_500, 10_000, 800)],
            }
        )
        latest = self.account("plus", "plus", 10, 19_500, 10_100, 1_510)
        index.append_record({"t": 1_510, "a": [latest]})
        summary = build_statistics_summary(
            SimpleNamespace(series_index=index),
            [latest],
            None,
            StatisticsPeriod(1_000, 2_000, "test"),
            local_tokens_ready=False,
        )
        self.assertEqual(summary.quota_used["5h"].percent, 90)
        self.assertAlmostEqual(summary.quota_used["5h"].multiple, 0.9)

    def test_local_token_categories_count_first_event_and_idle_gap_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            sessions = root / "sessions"
            config = root / "config.toml"
            registry = root / "settings" / "token_usage_directories.json"
            config.write_text(f'[projects."{project}"]\ntrust_level = "trusted"\n', encoding="utf-8")
            rollout = sessions / "rollout-test.jsonl"
            rollout.parent.mkdir(parents=True)
            records = [
                session_meta(project),
                token_event(1_100, 100, 40, 10, 4),
                token_event(1_200, 160, 60, 20, 8),
                token_event(1_700, 300, 100, 40, 16),
            ]
            rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            monitor = TrustedDirectoryTokenUsageMonitor(sessions, project, config, registry, "all")
            monitor.poll(force_discovery=True, now=0)

            first = monitor.usage_between(1_000, 1_500)
            later = monitor.usage_between(1_500, 2_000)
            self.assertEqual(
                (first.input_tokens, first.cached_input_tokens, first.output_tokens, first.reasoning_output_tokens, first.total_tokens),
                (140, 60, 20, 8, 160),
            )
            self.assertEqual(
                (later.input_tokens, later.cached_input_tokens, later.output_tokens, later.reasoning_output_tokens, later.total_tokens),
                (120, 40, 20, 8, 140),
            )


class StatisticsModalTests(unittest.TestCase):
    def test_modal_has_fixed_geometry_and_only_modal_click_zones(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = monitor_state(Path(temp_dir))
            state.statistics_open = True
            state.statistics_anchor = "2026-08-22"
            state.records = []
            state.records_version = 1
            with patch("ui.app.read_records_if_due", return_value=[]):
                lines, zones = render_frame(state, 120, 36)
            plain = [strip_ansi(line) for line in lines]
            top = next(index for index, line in enumerate(plain) if t("statistics.title.day") in line)
            bottom = next(index for index, line in enumerate(plain[top:], top) if "╰" in line and "╯" in line)
            start = plain[top].index("╭")
            end = plain[top].rindex("╮")
            self.assertEqual(visible_width(plain[top][start:end + 1]), STATISTICS_MODAL_WIDTH)
            self.assertEqual(bottom - top + 1, STATISTICS_MODAL_HEIGHT)
            for row in plain[top + 1:bottom]:
                self.assertEqual(row[start + 1], " ")
                self.assertEqual(row[end - 1], " ")
            self.assertIn(t("statistics.granularity.day"), plain[bottom - 1])
            self.assertEqual(
                {zone.kind for zone in zones},
                {"statistics_shift", "statistics_granularity", "statistics_close"},
            )

            handle_click(state, zones, 1, 1)
            self.assertTrue(state.statistics_open)
            close = next(zone for zone in zones if zone.kind == "statistics_close")
            handle_click(state, zones, close.x1, close.y)
            self.assertFalse(state.statistics_open)

    def test_small_terminal_keeps_only_a_working_close_button(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = monitor_state(Path(temp_dir))
            state.statistics_open = True
            state.statistics_anchor = "2026-08-22"
            with patch("ui.app.read_records_if_due", return_value=[]):
                _lines, zones = render_frame(state, 60, 18)
            self.assertEqual([zone.kind for zone in zones], ["statistics_close"])
            close = zones[0]
            handle_click(state, zones, close.x1, close.y)
            self.assertFalse(state.statistics_open)

    def test_usage_first_frame_does_not_block_on_all_directory_token_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            monitor = TrustedDirectoryTokenUsageMonitor(
                root / "sessions",
                root / "project",
                root / "config.toml",
                root / "settings" / "registry.json",
                "current",
            )
            preloader = BackgroundDataPreloader(
                root / "quota.jsonl",
                "Asia/Shanghai",
                root / "sessions",
                root / "project",
                root / "config.toml",
                root / "settings" / "registry.json",
            )
            state = monitor_state(root)
            state.display_scope = "usage"
            state.statistics_open = True
            state.statistics_anchor = "2026-08-22"
            state.token_monitor = monitor
            state.preloader = preloader
            with patch.object(monitor, "poll", wraps=monitor.poll) as poll:
                render_frame(state, 120, 36)
            poll.assert_not_called()

    def test_sidebar_statistics_button_opens_today_and_modal_controls_change_period(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = monitor_state(Path(temp_dir))
            zones = []
            lines = render_sidebar(state, 21, 35, 100, zones)
            button = next(zone for zone in zones if zone.kind == "statistics_open")
            settings = next(zone for zone in zones if zone.kind == "settings_activate")
            self.assertEqual(settings.y, button.y + 2)
            self.assertFalse(strip_ansi(lines[button.y]).strip("│ "))
            self.assertIn("F8", strip_ansi(lines[button.y - 1]))
            handle_click(state, zones, button.x1, button.y)
            self.assertTrue(state.statistics_open)
            self.assertEqual(state.statistics_anchor, local_today(state.tz).isoformat())

            state.statistics_anchor = "2026-08-19"
            with patch("ui.app.read_records_if_due", return_value=[]):
                _lines, modal_zones = render_frame(state, 120, 36)
            week = next(
                zone for zone in modal_zones
                if zone.kind == "statistics_granularity" and zone.value == "week"
            )
            handle_click(state, modal_zones, week.x1, week.y)
            previous = next(
                zone for zone in modal_zones
                if zone.kind == "statistics_shift" and zone.value == -1
            )
            handle_click(state, modal_zones, previous.x1, previous.y)
            self.assertEqual(state.statistics_granularity, "week")
            self.assertEqual(state.statistics_anchor, "2026-08-12")

    def test_f8_key_opens_today_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = monitor_state(Path(temp_dir))
            session = SimpleNamespace(buffer=b"", read=lambda: b"\x1b[19~")
            keep_running, clicks, keys = parse_input(session)
            self.assertTrue(keep_running)
            self.assertEqual(clicks, [])
            self.assertEqual(keys, ["f8"])

            handle_setting_key(state, keys[0])
            self.assertTrue(state.statistics_open)
            self.assertEqual(state.statistics_anchor, local_today(state.tz).isoformat())
            handle_statistics_key(state, "esc")
            self.assertTrue(state.statistics_open)
            handle_statistics_key(state, "backspace")
            self.assertFalse(state.statistics_open)


if __name__ == "__main__":
    unittest.main()
