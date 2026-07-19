from __future__ import annotations

import sys
from contextlib import redirect_stdout
from io import StringIO
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota import check_codex_quota as quota
from quota.codex_quota_sampler import compact_account
from quota.quota_format import format_token_count, token_usage_rows
from quota.token_usage_cache import DailyTokenUsageCacheReader
from core.state import saved_usage_directory_scope, saved_usage_panel_layout
from core.version import APP_VERSION, PACKAGE_VERSION
from ui import color_schemes
from ui.app import _render_main_content
from ui.charts import series_chart_lines
from ui.history import HistorySeriesIndex, metric_value_at
from ui.session_tokens import (
    ProjectTokenUsageMonitor,
    TokenUsageBreakdown,
    TrustedDirectoryTokenUsageMonitor,
)
from ui.settings import setting_items
from ui.terminal_text import strip_ansi, visible_width
from ui.token_charts import (
    _single_token_chart_lines,
    _visible_max,
    scientific_axis_max,
    token_split_shape,
    token_rate_unit,
    token_usage_chart_lines,
)
from ui.trusted_directories import TrustedDirectoryRegistry, path_key


def quota_payload() -> dict:
    return {
        "email": "codex@example.com",
        "plan_type": "plus",
        "rate_limit": {
            "allowed": True,
            "primary_window": {
                "used_percent": 20,
                "limit_window_seconds": 18_000,
                "reset_after_seconds": 17_000,
            },
            "secondary_window": {
                "used_percent": 40,
                "limit_window_seconds": 604_800,
                "reset_after_seconds": 500_000,
            },
        },
    }


class CollectionTests(unittest.TestCase):
    def test_token_total_display_uses_tokens_suffix(self) -> None:
        self.assertEqual(format_token_count(1_234_567), "1,234,567 Tokens")
        self.assertEqual(format_token_count(None), "-")
        output = StringIO()
        with redirect_stdout(output):
            quota.print_text({"token_usage": {"lifetime_tokens": 1_234_567}})
        self.assertIn("total_usage: 1,234,567 Tokens", output.getvalue())

    def test_two_quota_endpoints_are_bounded_and_concurrent(self) -> None:
        active = 0
        peak = 0
        lock = threading.Lock()

        def fake_get_json(url: str, _token: str, *_args) -> dict:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            if url == quota.USAGE_URL:
                return quota_payload()
            if url == quota.RESET_CREDITS_URL:
                return {"available_count": 2, "credits": []}
            return {"available_count": 2, "credits": []}

        configs = [("openai-1", Path("one")), ("openai-2", Path("two"))]
        with (
            patch.object(quota, "load_auth_credentials", return_value=("secret", "account")),
            patch.object(quota, "get_json", side_effect=fake_get_json),
        ):
            bundle = quota.collect_accounts(configs, "openai-1", "Asia/Shanghai")

        self.assertGreaterEqual(peak, 2)
        self.assertEqual([item["index"] for item in bundle["accounts"]], ["openai-1", "openai-2"])
        self.assertNotIn("token_usage", bundle["accounts"][0])
        self.assertEqual(bundle["accounts"][0]["quota"]["5h"]["remaining_percent"], 80)

    def test_collection_only_calls_quota_and_reset_endpoints(self) -> None:
        urls: list[str] = []

        def fake_get_json(url: str, _token: str, *_args) -> dict:
            urls.append(url)
            if url == quota.USAGE_URL:
                return quota_payload()
            return {"available_count": 0, "credits": []}

        with (
            patch.object(quota, "load_auth_credentials", return_value=("secret", "account")),
            patch.object(quota, "get_json", side_effect=fake_get_json),
        ):
            account = quota.collect_accounts(
                [("openai-1", Path("one"))],
                "openai-1",
                "Asia/Shanghai",
            )["accounts"][0]

        self.assertNotIn("error", account)
        self.assertEqual(set(urls), {quota.USAGE_URL, quota.RESET_CREDITS_URL})
        self.assertEqual(account["quota"]["7d"]["remaining_percent"], 60)

    def test_daily_token_total_queries_once_per_24_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "token_usage_daily.json"
            urls: list[str] = []

            def fake_get_json(url: str, _token: str, *_args) -> dict:
                urls.append(url)
                if url == quota.USAGE_URL:
                    return quota_payload()
                if url == quota.RESET_CREDITS_URL:
                    return {"available_count": 0, "credits": []}
                return {
                    "stats": {"lifetime_tokens": 987_654_321},
                    "metadata": {"generated_at": "2026-07-19T00:00:00Z"},
                }

            with (
                patch.object(quota, "load_auth_credentials", return_value=("secret", "account")),
                patch.object(quota, "get_json", side_effect=fake_get_json),
            ):
                first = quota.collect_accounts(
                    [("openai-1", Path("one"))],
                    "openai-1",
                    "Asia/Shanghai",
                    cache_path,
                    observed_epoch=1_000,
                )["accounts"][0]
                second = quota.collect_accounts(
                    [("openai-1", Path("one"))],
                    "openai-1",
                    "Asia/Shanghai",
                    cache_path,
                    observed_epoch=1_001,
                )["accounts"][0]
                third = quota.collect_accounts(
                    [("openai-1", Path("one"))],
                    "openai-1",
                    "Asia/Shanghai",
                    cache_path,
                    observed_epoch=1_000 + 86_400,
                )["accounts"][0]

            self.assertEqual(urls.count(quota.TOKEN_USAGE_URL), 2)
            self.assertEqual(first["token_usage"]["lifetime_tokens"], 987_654_321)
            self.assertEqual(second["token_usage"]["lifetime_tokens"], 987_654_321)
            self.assertEqual(third["token_usage"]["checked_at_epoch"], 87_400)
            reader = DailyTokenUsageCacheReader(cache_path)
            self.assertTrue(reader.poll())
            self.assertEqual(reader.totals["openai-1"], (987_654_321, 87_400))

    def test_failed_daily_query_is_not_retried_until_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "token_usage_daily.json"
            token_calls = 0

            def fake_get_json(url: str, _token: str, *_args) -> dict:
                nonlocal token_calls
                if url == quota.TOKEN_USAGE_URL:
                    token_calls += 1
                    raise RuntimeError("token endpoint unavailable")
                if url == quota.USAGE_URL:
                    return quota_payload()
                return {"available_count": 0, "credits": []}

            with (
                patch.object(quota, "load_auth_credentials", return_value=("secret", "account")),
                patch.object(quota, "get_json", side_effect=fake_get_json),
            ):
                first = quota.collect_accounts(
                    [("openai-1", Path("one"))], "openai-1", "Asia/Shanghai", cache_path, 1_000
                )["accounts"][0]
                second = quota.collect_accounts(
                    [("openai-1", Path("one"))], "openai-1", "Asia/Shanghai", cache_path, 2_000
                )["accounts"][0]

            self.assertEqual(token_calls, 1)
            self.assertIn("token endpoint unavailable", first["token_usage_error"])
            self.assertNotIn("token_usage", second)

    def test_compact_account_keeps_daily_token_total_even_on_quota_error(self) -> None:
        item = compact_account(
            {
                "index": "openai-1",
                "current": True,
                "error": "quota failed",
                "token_usage": {
                    "lifetime_tokens": 987_654_321,
                    "generated_at_epoch": 1_700_000_000,
                },
            },
            1_700_000_010,
        )
        self.assertEqual(item["u"], [987_654_321, 1_700_000_000])
        self.assertEqual(item["err"], "quota failed")


def session_meta(cwd: Path, thread_id: str, source: object = "cli") -> dict:
    return {
        "timestamp": 1_000,
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "session_id": thread_id,
            "cwd": str(cwd),
            "source": source,
        },
    }


def token_event(timestamp: float, total: int, cached: int | None = None) -> dict:
    cached_tokens = min(total, total // 2) if cached is None else cached
    output_tokens = min(total, max(0, total // 10))
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total - output_tokens,
                    "cached_input_tokens": cached_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": output_tokens // 2,
                    "total_tokens": total,
                }
            },
        },
    }


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def write_trusted_config(path: Path, directories: list[tuple[Path, str]]) -> None:
    lines = []
    for directory, trust_level in directories:
        escaped = str(directory).replace("\\", "\\\\").replace('"', '\\"')
        lines.extend([f'[projects."{escaped}"]', f'trust_level = "{trust_level}"', ""])
    path.write_text("\n".join(lines), encoding="utf-8")


class ProjectTokenMonitorTests(unittest.TestCase):
    def test_latest_root_thread_is_selected_and_subagent_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            sessions = root / "sessions"
            old_path = sessions / "2026" / "07" / "19" / "rollout-old.jsonl"
            new_path = sessions / "2026" / "07" / "19" / "rollout-new.jsonl"
            child_path = sessions / "2026" / "07" / "19" / "rollout-child.jsonl"
            write_jsonl(old_path, [session_meta(project, "old"), token_event(1_000, 100)])
            write_jsonl(new_path, [session_meta(project, "new"), token_event(1_060, 300)])
            write_jsonl(
                child_path,
                [session_meta(project, "child", {"subagent": {"other": "guardian"}}), token_event(1_120, 900)],
            )
            os.utime(old_path, ns=(1_000, 1_000))
            os.utime(new_path, ns=(2_000, 2_000))
            os.utime(child_path, ns=(3_000, 3_000))

            monitor = ProjectTokenUsageMonitor(sessions, project)
            self.assertTrue(monitor.poll(force_discovery=True, now=0))

            self.assertEqual(monitor.active_path, new_path)
            self.assertEqual(monitor.active_thread_id, "new")
            self.assertEqual(monitor.latest.total_tokens, 300)

    def test_preferred_thread_id_wins_over_newer_project_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            sessions = root / "sessions"
            preferred = sessions / "rollout-preferred.jsonl"
            newer = sessions / "rollout-newer.jsonl"
            write_jsonl(preferred, [session_meta(project, "preferred"), token_event(1_000, 100)])
            write_jsonl(newer, [session_meta(project, "newer"), token_event(1_000, 200)])
            os.utime(preferred, ns=(1_000, 1_000))
            os.utime(newer, ns=(2_000, 2_000))

            monitor = ProjectTokenUsageMonitor(sessions, project, thread_id="preferred")
            monitor.poll(force_discovery=True, now=0)

            self.assertEqual(monitor.active_path, preferred)
            self.assertEqual(monitor.latest.total_tokens, 100)

    def test_incremental_append_uses_cursor_and_computes_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            sessions = root / "sessions"
            path = sessions / "rollout-current.jsonl"
            write_jsonl(
                path,
                [session_meta(project, "current"), token_event(1_000, 100), token_event(1_060, 160)],
            )
            monitor = ProjectTokenUsageMonitor(sessions, project, discovery_interval=10)
            monitor.poll(force_discovery=True, now=0)
            initial_bytes = monitor.bytes_read
            appended = json.dumps(token_event(1_120, 280)).encode() + b"\n"
            with path.open("ab") as handle:
                handle.write(appended)

            self.assertTrue(monitor.poll(now=1))

            self.assertEqual(monitor.latest.total_tokens, 280)
            self.assertEqual(
                [round(point["value"], 3) for point in monitor.rate_points],
                [1.0, 1.35, 0.0],
            )
            self.assertEqual(metric_value_at(monitor.rate_points, 1_121)[0], 0.0)
            self.assertEqual(set(monitor.rate_series), {"input", "cached", "output", "total"})
            self.assertEqual(monitor.bytes_read - initial_bytes, len(appended))
            self.assertEqual(monitor.full_loads, 1)
            self.assertEqual(monitor.incremental_reads, 1)

    def test_incomplete_tail_waits_for_newline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            sessions = root / "sessions"
            path = sessions / "rollout-current.jsonl"
            write_jsonl(path, [session_meta(project, "current"), token_event(1_000, 100)])
            monitor = ProjectTokenUsageMonitor(sessions, project, discovery_interval=10)
            monitor.poll(force_discovery=True, now=0)
            encoded = json.dumps(token_event(1_060, 220)).encode()
            with path.open("ab") as handle:
                handle.write(encoded)

            self.assertFalse(monitor.poll(now=1))
            self.assertEqual(monitor.latest.total_tokens, 100)
            with path.open("ab") as handle:
                handle.write(b"\n")
            self.assertTrue(monitor.poll(now=2))
            self.assertEqual(monitor.latest.total_tokens, 220)

    def test_total_drop_restarts_rate_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            sessions = root / "sessions"
            path = sessions / "rollout-current.jsonl"
            write_jsonl(
                path,
                [
                    session_meta(project, "current"),
                    token_event(1_000, 100),
                    token_event(1_060, 160),
                    token_event(1_120, 20),
                    token_event(1_180, 80),
                ],
            )
            monitor = ProjectTokenUsageMonitor(sessions, project)
            monitor.poll(force_discovery=True, now=0)

            self.assertEqual(
                [point["value"] for point in monitor.rate_points],
                [1.0, 0.0, 1.0, 0.0],
            )
            self.assertEqual(monitor.latest.total_tokens, 80)

    def test_long_idle_gap_is_zero_and_resets_smoothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            sessions = root / "sessions"
            write_jsonl(
                sessions / "rollout-current.jsonl",
                [
                    session_meta(project, "current"),
                    token_event(1_000, 100),
                    token_event(1_060, 160),
                    token_event(1_360, 500),
                    token_event(1_420, 620),
                ],
            )
            monitor = ProjectTokenUsageMonitor(sessions, project)
            monitor.poll(force_discovery=True, now=0)

            self.assertEqual(
                monitor.rate_points,
                [
                    {"t": 1_000.0, "value": 1.0},
                    {"t": 1_060.0, "value": 0.0},
                    {"t": 1_360.0, "value": 2.0},
                    {"t": 1_420.0, "value": 0.0},
                ],
            )
            self.assertEqual(metric_value_at(monitor.rate_points, 1_200)[0], 0.0)

    def test_account_card_usage_row_only_contains_online_total(self) -> None:
        rows = token_usage_rows(1_234_000, 80)
        plain = [strip_ansi(row) for row in rows]
        self.assertEqual(plain[0], "使用总量 1,234,000 Tokens")
        self.assertEqual(len(plain), 1)


class TrustedDirectoryMonitorTests(unittest.TestCase):
    def test_first_start_syncs_registry_and_preserves_manual_disable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.toml"
            registry_path = root / "settings" / "token_usage_directories.json"
            project = root / "project"
            child = project / "child"
            ignored = root / "ignored"
            write_trusted_config(
                config,
                [(project, "trusted"), (child, "trusted"), (ignored, "untrusted")],
            )
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "directories": [
                            {"path": str(project), "disable": True},
                            {"path": str(root / "stale"), "disable": True},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            registry = TrustedDirectoryRegistry(config, registry_path)
            self.assertTrue(registry.poll(force=True))

            payload = json.loads(registry_path.read_text(encoding="utf-8"))
            entries = payload["directories"]
            self.assertEqual([path_key(entry["path"]) for entry in entries], [path_key(project), path_key(child)])
            self.assertTrue(entries[0]["disable"])
            self.assertFalse(entries[1]["disable"])
            self.assertEqual(registry.owner_for(child / "work").key, path_key(child))

    def test_current_and_all_scopes_aggregate_root_threads_and_honor_disable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sessions = root / "sessions"
            current = root / "current"
            other = root / "other"
            config = root / "config.toml"
            registry_path = root / "settings" / "token_usage_directories.json"
            write_trusted_config(config, [(current, "trusted"), (other, "trusted")])
            current_one = sessions / "rollout-current-one.jsonl"
            current_two = sessions / "rollout-current-two.jsonl"
            other_one = sessions / "rollout-other-one.jsonl"
            child = sessions / "rollout-child.jsonl"
            write_jsonl(current_one, [session_meta(current, "one"), token_event(1_000, 100), token_event(1_060, 160)])
            write_jsonl(current_two, [session_meta(current / "subdir", "two"), token_event(1_000, 300), token_event(1_060, 360)])
            write_jsonl(other_one, [session_meta(other, "three"), token_event(1_000, 500), token_event(1_060, 620)])
            write_jsonl(child, [session_meta(current, "child", {"subagent": {"other": "worker"}}), token_event(1_060, 900)])

            monitor = TrustedDirectoryTokenUsageMonitor(
                sessions,
                current,
                config,
                registry_path,
                "current",
                discovery_interval=10,
            )
            self.assertTrue(registry_path.exists())
            self.assertTrue(monitor.poll(force_discovery=True, now=0))
            self.assertEqual(monitor.latest.total_tokens, 520)
            self.assertEqual([point["value"] for point in monitor.rate_points], [2.0, 0.0])
            self.assertEqual(monitor.loaded_rollouts, 2)

            monitor.set_scope("all")
            self.assertTrue(monitor.poll(now=1))
            self.assertEqual(monitor.latest.total_tokens, 1_140)
            self.assertEqual([point["value"] for point in monitor.rate_points], [4.0, 0.0])
            self.assertEqual(monitor.loaded_rollouts, 3)

            payload = json.loads(registry_path.read_text(encoding="utf-8"))
            for entry in payload["directories"]:
                if path_key(entry["path"]) == path_key(other):
                    entry["disable"] = True
            registry_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(monitor.poll(force_discovery=True, now=2))
            self.assertEqual(monitor.latest.total_tokens, 520)
            self.assertEqual([point["value"] for point in monitor.rate_points], [2.0, 0.0])

    def test_unchanged_logs_are_not_reloaded_before_incremental_append(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sessions = root / "sessions"
            project = root / "project"
            config = root / "config.toml"
            registry_path = root / "settings" / "token_usage_directories.json"
            write_trusted_config(config, [(project, "trusted")])
            rollout = sessions / "rollout-current.jsonl"
            write_jsonl(rollout, [session_meta(project, "current"), token_event(1_000, 100), token_event(1_060, 160)])
            monitor = TrustedDirectoryTokenUsageMonitor(
                sessions,
                project,
                config,
                registry_path,
                discovery_interval=10,
            )
            monitor.poll(force_discovery=True, now=0)
            original_bytes = monitor.bytes_read
            original_full_loads = monitor.full_loads

            self.assertFalse(monitor.poll(now=1))
            self.assertEqual(monitor.bytes_read, original_bytes)
            self.assertEqual(monitor.full_loads, original_full_loads)

            appended = json.dumps(token_event(1_120, 280)).encode() + b"\n"
            with rollout.open("ab") as handle:
                handle.write(appended)
            self.assertTrue(monitor.poll(now=2))
            self.assertEqual(monitor.bytes_read - original_bytes, len(appended))
            self.assertEqual(monitor.incremental_reads, 1)
            self.assertEqual(monitor.latest.total_tokens, 280)

    def test_aggregate_does_not_backfill_nonzero_rate_across_idle_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sessions = root / "sessions"
            project = root / "project"
            config = root / "config.toml"
            registry_path = root / "settings" / "token_usage_directories.json"
            write_trusted_config(config, [(project, "trusted")])
            write_jsonl(
                sessions / "rollout-current.jsonl",
                [
                    session_meta(project, "current"),
                    token_event(1_000, 100),
                    token_event(1_060, 160),
                    token_event(1_360, 500),
                    token_event(1_420, 620),
                ],
            )
            monitor = TrustedDirectoryTokenUsageMonitor(
                sessions, project, config, registry_path, discovery_interval=10
            )
            monitor.poll(force_discovery=True, now=0)

            self.assertEqual(
                monitor.rate_points,
                [
                    {"t": 1_000.0, "value": 1.0},
                    {"t": 1_060.0, "value": 0.0},
                    {"t": 1_360.0, "value": 2.0},
                    {"t": 1_420.0, "value": 0.0},
                ],
            )
            self.assertEqual(metric_value_at(monitor.rate_points, 1_200)[0], 0.0)
            self.assertEqual(monitor.latest.total_tokens, 620)

    def test_usage_directory_scope_state_validation(self) -> None:
        self.assertEqual(saved_usage_directory_scope({"usage_directory_scope": "all"}), "all")
        self.assertIsNone(saved_usage_directory_scope({"usage_directory_scope": "invalid"}))
        self.assertEqual(saved_usage_panel_layout({"usage_panel_layout": "split"}), "split")
        self.assertIsNone(saved_usage_panel_layout({"usage_panel_layout": "invalid"}))


class LegacyTokenHistoryTests(unittest.TestCase):
    @staticmethod
    def record(timestamp: int, one: int, two: int) -> dict:
        return {
            "t": timestamp,
            "a": [
                {
                    "i": "openai-1",
                    "u": [one, timestamp],
                    "q": {"5h": [80, timestamp + 1_000, 1_000, 18_000]},
                },
                {
                    "i": "openai-2",
                    "u": [two, timestamp],
                    "q": {"5h": [60, timestamp + 1_000, 1_000, 18_000]},
                },
            ],
        }

    def test_legacy_u_fields_do_not_break_quota_history(self) -> None:
        records = [self.record(1_000, 10_000, 20_000), self.record(1_060, 10_600, 21_200)]
        index = HistorySeriesIndex()
        for record in records:
            index.append_record(record)

        account = index.account_window("openai-1", "5h")
        merged = index.merged_window("5h")
        self.assertIsNotNone(account)
        self.assertEqual(list(account.values), [80.0, 80.0])
        self.assertEqual(set(merged), {"openai-1", "openai-2"})


class SettingsTests(unittest.TestCase):
    @staticmethod
    def keys(display_scope: str) -> list[str]:
        state = SimpleNamespace(display_scope=display_scope)
        return [key for key, _title, _choices in setting_items(state)]

    def test_display_scope_is_third_and_mode_specific_items_are_last(self) -> None:
        usage = self.keys("usage")
        self.assertEqual(usage[:3], ["interval", "period", "display_scope"])
        self.assertEqual(usage[-2:], ["usage_directory_scope", "usage_panel_layout"])
        self.assertNotIn("window_scope", usage)

        for display_scope in ("all", "current", "merged"):
            with self.subTest(display_scope=display_scope):
                quota = self.keys(display_scope)
                self.assertEqual(quota[:3], ["interval", "period", "display_scope"])
                self.assertEqual(quota[-1], "window_scope")
                self.assertNotIn("usage_directory_scope", quota)
                self.assertNotIn("usage_panel_layout", quota)

    def test_fine_bar_style_and_release_version_are_available(self) -> None:
        state = SimpleNamespace(display_scope="usage")
        curve_choices = next(
            choices for key, _title, choices in setting_items(state) if key == "curve_mode"
        )
        self.assertIn(("精细柱状", "fine_bar"), curve_choices)
        self.assertEqual((PACKAGE_VERSION, APP_VERSION), ("2.3.0", "v2.3.0"))


class TokenChartTests(unittest.TestCase):
    def test_usage_mode_restores_outer_panel_border(self) -> None:
        state = SimpleNamespace(
            token_monitor=SimpleNamespace(
                latest=TokenUsageBreakdown(3_000, 2_000, 500, 100, 3_500),
                rate_series=self.rate_series(),
            ),
            display_scope="usage",
            period="5m",
            curve_mode="connected",
            color_scheme="classic",
            usage_panel_layout="split",
        )
        with patch("ui.token_charts.time.time", return_value=1_120):
            lines, width, height = _render_main_content(
                state, [], [], None, 120, 36, []
            )
        plain = [strip_ansi(line) for line in lines]
        self.assertTrue(plain[0].startswith("╭"))
        self.assertIn("Token 用量", plain[0])
        self.assertTrue(plain[-1].startswith("╰"))
        self.assertEqual((len(lines), width, height), (35, 99, 35))

    def test_split_shape_adapts_without_vertical_gaps(self) -> None:
        self.assertEqual(token_split_shape(240, 30), (1, 4))
        self.assertEqual(token_split_shape(100, 36), (2, 2))
        self.assertEqual(token_split_shape(60, 100), (4, 1))

        with patch("ui.token_charts.time.time", return_value=1_120):
            lines = token_usage_chart_lines(
                self.rate_series(),
                TokenUsageBreakdown(3_000, 2_000, 500, 100, 3_500),
                "5m",
                60,
                100,
                "connected",
                "classic",
                "split",
            )
        self.assertEqual(len(lines), 100)
        self.assertTrue(all(strip_ansi(line).strip() for line in lines))

    def test_split_mode_axis_density_adapts_to_available_height(self) -> None:
        latest = TokenUsageBreakdown(3_000, 2_000, 500, 100, 3_500)
        colors = color_schemes.token_series_colors(key="classic")
        compact_lines = _single_token_chart_lines(
            self.rate_series(),
            latest,
            "input",
            820,
            1_120,
            50,
            10,
            "connected",
            colors,
        )
        compact_labels = [strip_ansi(line)[:4].strip() for line in compact_lines]
        self.assertEqual([label for label in compact_labels if label], ["10", "0"])

        tall_lines = _single_token_chart_lines(
            self.rate_series(),
            latest,
            "input",
            820,
            1_120,
            50,
            18,
            "connected",
            colors,
        )
        tall_labels = [strip_ansi(line)[:4].strip() for line in tall_lines]
        self.assertEqual([label for label in tall_labels if label], ["10", "8", "6", "4", "2", "0"])

        with patch("ui.token_charts.time.time", return_value=1_120):
            split_lines = token_usage_chart_lines(
                self.rate_series(), latest, "5m", 100, 36, "bar", "classic", "split"
            )
        plain = "\n".join(strip_ansi(line) for line in split_lines)
        self.assertEqual(plain.count("┌"), 4)
        for label in ("输入", "缓存", "输出", "总量"):
            self.assertIn(label, plain)

    def test_only_bar_mode_uses_stacked_values(self) -> None:
        series = {
            "input": [{"t": 1_000, "value": 2.0}],
            "cached": [{"t": 1_000, "value": 2.0}],
            "output": [{"t": 1_000, "value": 1.0}],
            "total": [{"t": 1_000, "value": 2.0}],
        }
        self.assertEqual(_visible_max(series, 1_000, 1_000, 1, 1, False), 2.0)
        self.assertEqual(_visible_max(series, 1_000, 1_000, 1, 1, True), 5.0)

    def test_box_style_does_not_put_a_legend_marker_at_the_left_edge(self) -> None:
        colors = color_schemes.token_series_colors(key="classic")
        lines = _single_token_chart_lines(
            {"input": [{"t": 820, "value": 10.0}]},
            TokenUsageBreakdown(1_000, 0, 0, 0, 1_000),
            "input",
            820,
            1_120,
            50,
            18,
            "box",
            colors,
        )
        plot = "\n".join(strip_ansi(line) for line in lines[2:-2])
        self.assertNotIn("●", plot)
        self.assertIn("─", plot)

    def test_token_axis_uses_requested_six_tick_scales(self) -> None:
        self.assertEqual(scientific_axis_max(2_070_000), (2_500_000.0, 6, 2.5))
        self.assertEqual(scientific_axis_max(4_100_000), (5_000_000.0, 6, 5.0))
        self.assertEqual(scientific_axis_max(8_100_000), (10_000_000.0, 6, 10.0))

        with patch("ui.token_charts.time.time", return_value=1_120):
            lines = token_usage_chart_lines(
                self.rate_series(),
                TokenUsageBreakdown(3_000, 2_000, 500, 100, 3_500),
                "5m",
                100,
                24,
                "connected",
                "classic",
            )
        labels = [strip_ansi(line)[:4].strip() for line in lines]
        self.assertTrue({"2.5", "2", "1.5", "1", ".5", "0"}.issubset(set(labels)))

    def test_token_header_places_scale_left_and_legend_right_one_row_lower(self) -> None:
        with patch("ui.token_charts.time.time", return_value=1_120):
            lines = token_usage_chart_lines(
                self.rate_series(), None, "5m", 100, 24, "connected", "classic"
            )
        plain = [strip_ansi(line) for line in lines]
        self.assertEqual(plain[0].strip(), "")
        self.assertEqual(plain[1].find("tok/min ×10^3"), 4)
        self.assertTrue(plain[1].rstrip().endswith("■ 总量 0 (0)"))

    def test_token_legend_shows_current_rate_and_cumulative_total(self) -> None:
        current_series = {
            "input": [{"t": 1_120, "value": 10.0}],
            "cached": [{"t": 1_120, "value": 20.0}],
            "output": [{"t": 1_120, "value": 5.0}],
            "total": [{"t": 1_120, "value": 35.0}],
        }
        with patch("ui.token_charts.time.time", return_value=1_120):
            lines = token_usage_chart_lines(
                current_series,
                TokenUsageBreakdown(3_000, 2_000, 500, 100, 3_500),
                "5m",
                120,
                24,
                "connected",
                "classic",
            )
        header = strip_ansi(lines[1])
        self.assertIn("● 输入 600 (1.0K)", header)
        self.assertIn("◆ 缓存 1.2K (2.0K)", header)
        self.assertIn("▲ 输出 300 (500)", header)
        self.assertIn("■ 总量 2.1K (3.5K)", header)
        self.assertNotIn("●输入", header)

    def test_unit_boundary_is_strictly_greater_than_fifteen_minutes(self) -> None:
        self.assertEqual(token_rate_unit(899.9), (60, "tok/min"))
        self.assertEqual(token_rate_unit(900), (60, "tok/min"))
        self.assertEqual(token_rate_unit(900.1), (3600, "tok/h"))

    @staticmethod
    def rate_series() -> dict[str, list[dict[str, float]]]:
        return {
            "input": [{"t": 1_000, "value": 10.0}, {"t": 1_060, "value": 0.0}],
            "cached": [{"t": 1_000, "value": 20.0}, {"t": 1_060, "value": 0.0}],
            "output": [{"t": 1_000, "value": 5.0}, {"t": 1_060, "value": 0.0}],
            "total": [{"t": 1_000, "value": 35.0}, {"t": 1_060, "value": 0.0}],
        }

    def test_all_usage_curve_modes_render_without_quota_series(self) -> None:
        latest = TokenUsageBreakdown(3_000, 2_000, 500, 100, 3_500)
        color_schemes.set_active_color_scheme("classic")
        with patch("ui.token_charts.time.time", return_value=1_120):
            for mode in ("connected", "points", "braille", "box", "bar", "fine_bar"):
                with self.subTest(mode=mode):
                    lines = token_usage_chart_lines(
                        self.rate_series(), latest, "5m", 100, 22, mode, "classic"
                    )
                    plain = "\n".join(strip_ansi(line) for line in lines)
                    self.assertIn("输入", plain)
                    self.assertIn("缓存", plain)
                    self.assertIn("输出", plain)
                    self.assertIn("总量", plain)
                    self.assertNotIn("5h", plain)
                    self.assertNotIn("7d", plain)
                    self.assertTrue(all(visible_width(line) <= 100 for line in lines))

    def test_fine_bar_uses_braille_fill_in_combined_and_split_usage_charts(self) -> None:
        latest = TokenUsageBreakdown(3_000, 2_000, 500, 100, 3_500)
        with patch("ui.token_charts.time.time", return_value=1_030):
            for layout in ("combined", "split"):
                with self.subTest(layout=layout):
                    lines = token_usage_chart_lines(
                        self.rate_series(), latest, "5m", 100, 36,
                        "fine_bar", "classic", layout,
                    )
                    plain = "".join(strip_ansi(line) for line in lines)
                    self.assertTrue(any(0x2800 < ord(char) <= 0x28FF for char in plain))

    def test_usage_colors_are_fixed_by_scheme(self) -> None:
        colors = color_schemes.token_series_colors(key="redblue")
        self.assertEqual(len(set(colors.values())), 4)
        with patch("ui.token_charts.time.time", return_value=1_120):
            first = "\n".join(
                token_usage_chart_lines(
                    self.rate_series(),
                    TokenUsageBreakdown(3_000, 2_000, 500, 100, 3_500),
                    "5m",
                    100,
                    22,
                    "connected",
                    "redblue",
                )
            )
            changed = {key: [{"t": 1_000, "value": value * 100}] for key, value in {
                "input": 10.0, "cached": 20.0, "output": 5.0, "total": 35.0
            }.items()}
            second = "\n".join(
                token_usage_chart_lines(
                    changed,
                    TokenUsageBreakdown(30_000, 20_000, 5_000, 1_000, 35_000),
                    "5m",
                    100,
                    22,
                    "connected",
                    "redblue",
                )
            )
        for color in colors.values():
            rgb = color.lstrip("#")
            sequence = f"38;2;{int(rgb[0:2], 16)};{int(rgb[2:4], 16)};{int(rgb[4:6], 16)}"
            self.assertIn(sequence, first)
            self.assertIn(sequence, second)

    def test_quota_chart_restores_original_single_axis(self) -> None:
        quota_points = {
            "5h": [
                {"t": 0, "left": 80.0, "reset": None},
                {"t": 3_000_000, "left": 70.0, "reset": None},
            ]
        }
        for mode in ("connected", "points", "braille", "box", "bar", "fine_bar"):
            with self.subTest(mode=mode):
                lines = series_chart_lines(
                    quota_points,
                    0,
                    3_000_000,
                    100,
                    18,
                    mode,
                    100.0,
                )
                plain = [strip_ansi(line) for line in lines]
                self.assertNotIn("tok/", "\n".join(plain))
                self.assertNotIn("percent", "\n".join(plain))
                self.assertEqual(plain[0].index("┌"), 4)
                self.assertTrue(all(line.endswith("│") for line in plain[1:-2]))
                self.assertTrue(all(visible_width(line) <= 100 for line in lines))


if __name__ == "__main__":
    unittest.main()
