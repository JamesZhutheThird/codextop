"""Tests for quota-window detection and display filtering."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota.check_codex_quota import border_style_for_account, normalize_rate_limit_windows
from quota.quota_format import merged_quota_rows, quota_rows
from ui.charts import has_single_active_series, series_chart_lines
from ui.history import normalize_snapshot_windows
from ui.terminal_text import (
    chart_series_color,
    fg,
    paint,
    strip_ansi,
    window_marker_label,
)


def api_window(used: int, seconds: int) -> dict:
    return {
        "used_percent": used,
        "limit_window_seconds": seconds,
        "reset_after_seconds": seconds - 60,
        "reset_at": 2_000_000_000,
    }


def compact_account(index: str = "openai-1") -> dict:
    return {
        "i": index,
        "q": {
            "5h": [80, 20_000, 10_000, 18_000],
            "7d": [60, 700_000, 600_000, 604_800],
        },
    }


class QuotaWindowNormalizationTests(unittest.TestCase):
    def test_primary_weekly_window_is_normalized_as_7d(self) -> None:
        windows = normalize_rate_limit_windows(
            {"primary_window": api_window(4, 604_800)},
            ZoneInfo("UTC"),
        )

        self.assertIsNone(windows["5h"]["remaining_percent"])
        self.assertEqual(windows["7d"]["remaining_percent"], 96)
        self.assertEqual(windows["7d"]["limit_window_seconds"], 604_800)

    def test_standard_two_window_payload_keeps_both_windows(self) -> None:
        windows = normalize_rate_limit_windows(
            {
                "primary_window": api_window(20, 18_000),
                "secondary_window": api_window(40, 604_800),
            },
            ZoneInfo("UTC"),
        )

        self.assertEqual(windows["5h"]["remaining_percent"], 80)
        self.assertEqual(windows["7d"]["remaining_percent"], 60)

    def test_account_border_uses_weekly_window_when_5h_is_absent(self) -> None:
        account = {
            "quota": {
                "5h": {"remaining_percent": None},
                "7d": {"remaining_percent": 96},
            }
        }

        self.assertNotEqual(border_style_for_account(account), "dim")

    def test_legacy_snapshot_is_remapped_by_recorded_duration(self) -> None:
        records = [
            {
                "t": 100,
                "a": [
                    {
                        "i": "openai-1",
                        "q": {
                            "5h": [96, 604_900, 604_800, 604_800],
                            "7d": [None, None, None, None],
                        },
                    }
                ],
            }
        ]

        normalize_snapshot_windows(records)

        quota = records[0]["a"][0]["q"]
        self.assertNotIn("5h", quota)
        self.assertEqual(quota["7d"], [96, 604_900, 604_800, 604_800])


class QuotaWindowDisplayTests(unittest.TestCase):
    def test_account_quota_rows_follow_single_window_scope(self) -> None:
        text = strip_ansi(
            "\n".join(quota_rows(compact_account(), 70, window_scope="7d"))
        )

        self.assertIn("7d(", text)
        self.assertNotIn("5h(", text)

    def test_merged_quota_rows_follow_single_window_scope(self) -> None:
        text = strip_ansi(
            "\n".join(
                merged_quota_rows(
                    [compact_account("openai-1"), compact_account("openai-2")],
                    "openai-1",
                    90,
                    window_scope="5h",
                )
            )
        )

        self.assertIn("5h(", text)
        self.assertNotIn("7d(", text)

    def test_single_7d_scope_uses_primary_marker_color(self) -> None:
        rows = quota_rows(
            compact_account(),
            70,
            curve_mode="braille",
            window_scope="7d",
        )
        marker = paint(
            window_marker_label("7d", "braille", primary=True),
            bold=True,
        )

        self.assertTrue(rows[0].startswith(marker))

    def test_only_available_7d_uses_primary_marker_color(self) -> None:
        account = compact_account()
        account["q"].pop("5h")
        rows = quota_rows(account, 70, curve_mode="braille")
        marker = paint(
            window_marker_label("7d", "braille", primary=True),
            bold=True,
        )

        self.assertTrue(rows[3].startswith(marker))

    def test_single_7d_series_uses_brighter_primary_palette(self) -> None:
        self.assertTrue(has_single_active_series({"5h": [], "7d": [1]}))
        self.assertEqual(
            chart_series_color("7d", 60, primary=True),
            chart_series_color("5h", 60),
        )
        self.assertEqual(
            chart_series_color("7d", 60, dimmed=True, primary=True),
            chart_series_color("5h", 60, dimmed=True),
        )

        chart = "\n".join(
            series_chart_lines(
                {
                    "5h": [],
                    "7d": [{"t": 0, "left": 60.0, "reset": None}],
                },
                0,
                60,
                40,
                8,
                "points",
                100.0,
            )
        )
        self.assertIn(fg(chart_series_color("7d", 60, primary=True)), chart)
        self.assertNotIn(fg(chart_series_color("7d", 60)), chart)

    def test_merged_only_available_7d_uses_primary_marker_color(self) -> None:
        accounts = [compact_account("openai-1"), compact_account("openai-2")]
        for account in accounts:
            account["q"].pop("5h")
        rows = merged_quota_rows(accounts, "openai-1", 90, curve_mode="bar")
        marker = paint(
            window_marker_label("7d", "bar", primary=True),
            bold=True,
        )

        self.assertTrue(rows[3].startswith(marker))


if __name__ == "__main__":
    unittest.main()
