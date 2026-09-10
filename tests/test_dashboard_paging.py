from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.constants import CURVE_MODE_CHOICES, SIDEBAR_WIDTH
from core.i18n import t
from core.state import saved_dashboard_density, saved_hide_account_info, saved_window_scope
from quota.quota_format import account_email, current_account_quota_summary, merged_quota_rows, merged_window_info, quota_rows
from ui.history import HistoryRecordList, HistorySeriesIndex
from ui.charts import chart_lines
from ui.app import _render_main_content
from ui.models import MonitorState
from ui.panels import (
    DASHBOARD_PANEL_HEIGHT,
    DASHBOARD_PANEL_WIDTH,
    account_lines,
    dashboard_grid_layout,
    focus_page_layout,
    merged_chart_lines,
)
from ui.settings import apply_setting_value, handle_click, render_sidebar, setting_items
from ui.terminal_text import chart_series_color, percent_color, strip_ansi


def account(number: int, now: int) -> dict:
    return {
        "i": f"openai-{number}",
        "cur": number == 1,
        "email": f"account-{number}@example.com",
        "plan": "plus",
        "u": [number * 1_000, now],
        "rc": 4,
        "r": [[f"credit-{index}", now + index * 1_000] for index in range(1, 5)],
        "q": {
            "5h": [80 - number, now + 1_000, 1_000, 18_000],
            "7d": [60 - number, now + 2_000, 2_000, 604_800],
        },
    }


def monitor_state(records: list[dict], display_scope: str) -> MonitorState:
    return MonitorState(
        period="5m",
        interval=60,
        tz="Asia/Shanghai",
        log_path=Path("/tmp/codextop-paging-test/quota.jsonl"),
        restore_interval=60,
        state_path=Path("/tmp/codextop-paging-test/state.json"),
        curve_mode="connected",
        display_scope=display_scope,
        window_scope="both",
        color_scheme="classic",
        records=records,
    )


class DashboardPagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = int(time.time())

    def records(self, count: int) -> tuple[list[dict], list[dict]]:
        accounts = [account(number, self.now) for number in range(1, count + 1)]
        return accounts, [{"t": self.now - 60, "current": "openai-1", "a": accounts}]

    def test_dashboard_uses_bounded_even_grid_in_column_major_order(self) -> None:
        accounts, records = self.records(9)
        state = monitor_state(records, "list")
        heights, widths, last_page_start = dashboard_grid_layout(99, 60, len(accounts))

        self.assertEqual(SIDEBAR_WIDTH, 23)
        self.assertEqual(DASHBOARD_PANEL_WIDTH, {"lower": 30, "default": 60, "upper": 180})
        self.assertEqual(DASHBOARD_PANEL_HEIGHT, {"lower": 20, "default": 40, "upper": 80})
        self.assertEqual((heights, widths, last_page_start), ([30, 30], [50, 49], 8))
        self.assertTrue(
            all(DASHBOARD_PANEL_WIDTH["lower"] <= value <= DASHBOARD_PANEL_WIDTH["upper"] for value in widths)
        )
        self.assertTrue(
            all(DASHBOARD_PANEL_HEIGHT["lower"] <= value <= DASHBOARD_PANEL_HEIGHT["upper"] for value in heights)
        )

        lines, width, height = _render_main_content(
            state, records, accounts, "openai-1", 120, 61, []
        )
        plain = "\n".join(strip_ansi(line) for line in lines)
        visible = range(1, 5)
        order = sorted(visible, key=lambda number: plain.index(f"openai-{number}"))

        self.assertEqual((len(lines), width, height), (60, 97, 60))
        self.assertEqual(order, [1, 3, 2, 4])
        self.assertNotIn(t("nav.previous_column"), plain)
        self.assertNotIn("openai-5", plain)

    def test_dashboard_density_uses_lower_default_upper_and_fill_layouts(self) -> None:
        accounts, records = self.records(9)
        state = monitor_state(records, "list")

        self.assertEqual(
            dashboard_grid_layout(99, 60, len(accounts), "compact"),
            ([20, 20, 20], [33, 33, 33], 0),
        )
        self.assertEqual(
            dashboard_grid_layout(99, 60, len(accounts), "default"),
            ([30, 30], [50, 49], 8),
        )
        self.assertEqual(
            dashboard_grid_layout(99, 60, len(accounts), "relaxed"),
            ([60], [99], 8),
        )
        self.assertEqual(
            dashboard_grid_layout(99, 60, len(accounts), "fill"),
            ([20, 20, 20], [33, 33, 33], 0),
        )
        self.assertEqual(
            dashboard_grid_layout(99, 60, 13, "fill"),
            dashboard_grid_layout(99, 60, 13, "default"),
        )
        choices = next(
            choices for key, _title, choices in setting_items(state) if key == "dashboard_density"
        )
        self.assertEqual(
            choices,
            [
                (t("choice.dashboard_density.default"), "default"),
                (t("choice.dashboard_density.compact"), "compact"),
                (t("choice.dashboard_density.relaxed"), "relaxed"),
                (t("choice.dashboard_density.fill"), "fill"),
            ],
        )

        state.summary_offset = 2
        state.main_cache_key = ("cached",)
        apply_setting_value(state, "dashboard_density", "compact")
        self.assertEqual((state.dashboard_density, state.summary_offset), ("compact", 0))
        self.assertIsNone(state.main_cache_key)
        self.assertEqual(saved_dashboard_density({"dashboard_density": "compact"}), "compact")
        self.assertIsNone(saved_dashboard_density({"dashboard_density": "invalid"}))

    def test_dashboard_navigation_moves_one_complete_page_and_stops_at_both_ends(self) -> None:
        accounts, records = self.records(10)
        state = monitor_state(records, "list")
        main_width = 120 - SIDEBAR_WIDTH
        sidebar_x = main_width + 1

        zones = []
        sidebar = render_sidebar(state, SIDEBAR_WIDTH, 60, sidebar_x, zones)
        navigation = [zone for zone in zones if zone.kind == "summary_scroll"]
        statistics = next(zone for zone in zones if zone.kind == "statistics_open")
        self.assertEqual([(zone.y, zone.value) for zone in navigation], [(statistics.y - 2, 4)])
        self.assertFalse(strip_ansi(sidebar[navigation[0].y]).strip("│ "))
        navigation_line = sidebar[navigation[0].y - 1]
        self.assertIn(t("nav.previous_page_short"), strip_ansi(navigation_line))
        self.assertIn(t("nav.next_page_short"), strip_ansi(navigation_line))
        self.assertIn("\x1b[2m", navigation_line)
        navigation_cells = strip_ansi(navigation_line).split("│")[1:3]
        for cell in navigation_cells:
            self.assertEqual(len(cell) - len(cell.lstrip()), len(cell) - len(cell.rstrip()))

        handle_click(state, zones, navigation[0].x1, navigation[0].y)
        self.assertEqual(state.summary_offset, 4)
        middle_zones = []
        render_sidebar(state, SIDEBAR_WIDTH, 60, sidebar_x, middle_zones)
        navigation = [zone for zone in middle_zones if zone.kind == "summary_scroll"]
        self.assertEqual([zone.value for zone in navigation], [0, 8])

        middle_lines, _width, _height = _render_main_content(
            state, records, accounts, "openai-1", 120, 61, []
        )
        middle_plain = "\n".join(strip_ansi(line) for line in middle_lines)
        for number in range(5, 9):
            self.assertIn(f"openai-{number}", middle_plain)
        for number in (*range(1, 5), 9, 10):
            self.assertNotIn(f"openai-{number}", middle_plain)

        next_zone = navigation[1]
        handle_click(state, middle_zones, next_zone.x1, next_zone.y)
        self.assertEqual(state.summary_offset, 8)

        lines, _width, _height = _render_main_content(
            state, records, accounts, "openai-1", 120, 61, []
        )
        final_plain = "\n".join(strip_ansi(line) for line in lines)
        self.assertIn("openai-9", final_plain)
        self.assertIn("openai-10", final_plain)
        self.assertNotIn("openai-8", final_plain)

        final_zones = []
        final_sidebar = render_sidebar(state, SIDEBAR_WIDTH, 60, sidebar_x, final_zones)
        navigation = [zone for zone in final_zones if zone.kind == "summary_scroll"]
        self.assertEqual([zone.value for zone in navigation], [4])
        final_line = final_sidebar[navigation[0].y - 1]
        self.assertIn("\x1b[2m", final_line)
        handle_click(state, middle_zones, next_zone.x1, next_zone.y)
        self.assertEqual(state.summary_offset, 8)

    def test_account_info_setting_masks_only_email_local_part(self) -> None:
        self.assertEqual(account_email({"email": "abcdef@example.com"}, True), "a****f@example.com")
        self.assertEqual(account_email({"email": "ab@example.com"}, True), "**@example.com")
        self.assertEqual(account_email({"email": "a@example.com"}, True), "*@example.com")
        self.assertEqual(account_email({"email": "abcdef@example.com"}), "abcdef@example.com")

        accounts, records = self.records(1)
        state = monitor_state(records, "list")
        choices = next(
            choices for key, _title, choices in setting_items(state) if key == "hide_account_info"
        )
        self.assertEqual(
            choices,
            [
                (t("choice.account_info.visible"), False),
                (t("choice.account_info.hidden"), True),
            ],
        )
        apply_setting_value(state, "hide_account_info", True)
        self.assertTrue(state.hide_account_info)
        self.assertEqual(saved_hide_account_info({"hide_account_info": True}), True)
        self.assertIsNone(saved_hide_account_info({"hide_account_info": "true"}))

        for display_scope in ("list", "focus"):
            with self.subTest(display_scope=display_scope):
                state.display_scope = display_scope
                rendered, _width, _height = _render_main_content(
                    state,
                    records,
                    accounts,
                    "openai-1",
                    120,
                    61,
                    [],
                )
                plain = "\n".join(strip_ansi(line) for line in rendered)
                expected = (
                    "a*******1@example.com"
                    if display_scope == "list"
                    else "a*******1@exam"
                )
                self.assertIn(expected, plain)
                self.assertNotIn("account-1@", plain)

    def test_focus_navigation_refreshes_complete_pages_without_partial_panels(self) -> None:
        accounts, records = self.records(4)
        state = monitor_state(records, "focus")
        left_width = 32
        heights, page_starts, page_index = focus_page_layout(
            accounts,
            left_width,
            51,
            state.curve_mode,
            state.window_scope,
        )

        self.assertEqual(heights, [16, 16, 16, 16])
        self.assertEqual((page_starts, page_index), ([0, 3], 0))

        first_page, _width, _height = _render_main_content(
            state, records, accounts, "openai-1", 120, 52, []
        )
        first_plain = [strip_ansi(line) for line in first_page]
        self.assertIn("openai-1", first_plain[0])
        self.assertIn("openai-2", first_plain[16])
        self.assertIn("openai-3", first_plain[32])
        self.assertFalse(any("openai-4" in line for line in first_plain))
        for top, bottom in ((0, 15), (16, 31), (32, 47)):
            self.assertEqual(first_plain[bottom][:left_width], "╰" + "─" * 30 + "╯")
            self.assertTrue(
                all(first_plain[row][1:left_width - 1].strip() for row in range(top + 1, bottom))
            )
        self.assertTrue(all(not line[:left_width].strip() for line in first_plain[48:]))

        zones = []
        sidebar = render_sidebar(state, SIDEBAR_WIDTH, 51, 98, zones)
        navigation = [zone for zone in zones if zone.kind == "summary_scroll"]
        statistics = next(zone for zone in zones if zone.kind == "statistics_open")
        self.assertEqual(
            [(zone.y, zone.value) for zone in navigation],
            [(statistics.y - 2, 3)],
        )
        self.assertFalse(strip_ansi(sidebar[navigation[0].y]).strip("│ "))
        navigation_line = sidebar[navigation[0].y - 1]
        self.assertIn(t("nav.previous_page_short"), strip_ansi(navigation_line))
        self.assertIn(t("nav.next_page_short"), strip_ansi(navigation_line))
        self.assertIn("\x1b[2m", navigation_line)

        next_page = navigation[0]
        handle_click(state, zones, next_page.x1, next_page.y)
        self.assertEqual(state.summary_offset, 3)

        second_page, _width, _height = _render_main_content(
            state, records, accounts, "openai-1", 120, 52, []
        )
        second_plain = [strip_ansi(line) for line in second_page]
        self.assertIn("openai-4", second_plain[0][:left_width])
        self.assertEqual(second_plain[15][:left_width], "╰" + "─" * 30 + "╯")
        self.assertTrue(all(not line[:left_width].strip() for line in second_plain[16:]))

        final_zones = []
        final_sidebar = render_sidebar(state, SIDEBAR_WIDTH, 51, 98, final_zones)
        navigation = [zone for zone in final_zones if zone.kind == "summary_scroll"]
        self.assertEqual([zone.value for zone in navigation], [0])
        self.assertIn("\x1b[2m", final_sidebar[navigation[0].y - 1])

    def test_focus_panel_height_tracks_reset_count_and_window_scope(self) -> None:
        accounts, records = self.records(4)
        two_resets = account(2, self.now)
        two_resets["rc"] = 2
        two_resets["r"] = two_resets["r"][:2]

        for scope, expected_heights, expected_starts in (
            ("both", [16, 14], [0]),
            ("5h", [14, 12], [0]),
        ):
            with self.subTest(scope=scope):
                state = monitor_state(records, "focus")
                state.window_scope = scope
                heights, page_starts, page_index = focus_page_layout(
                    [accounts[0], two_resets],
                    33,
                    51,
                    state.curve_mode,
                    scope,
                )
                self.assertEqual((heights, page_starts, page_index), (expected_heights, expected_starts, 0))

                rendered, _width, _height = _render_main_content(
                    state,
                    records,
                    [accounts[0], two_resets],
                    "openai-1",
                    120,
                    52,
                    [],
                )
                plain = [strip_ansi(line) for line in rendered]
                top = 0
                for panel_height in expected_heights:
                    bottom = top + panel_height - 1
                    self.assertTrue(
                        all(plain[row][1:31].strip() for row in range(top + 1, bottom))
                    )
                    self.assertEqual(plain[bottom][:32], "╰" + "─" * 30 + "╯")
                    top += panel_height
                self.assertTrue(all(not line[:32].strip() for line in plain[top:]))

        both_heights, both_starts, _page_index = focus_page_layout(
            accounts,
            33,
            43,
            "connected",
            "both",
        )
        single_heights, single_starts, _page_index = focus_page_layout(
            accounts,
            33,
            43,
            "connected",
            "5h",
        )
        self.assertEqual((both_heights, both_starts), ([16] * 4, [0, 2]))
        self.assertEqual((single_heights, single_starts), ([14] * 4, [0, 3]))

    def test_focus_chart_manual_navigation_and_provider_following(self) -> None:
        accounts, records = self.records(4)
        state = monitor_state(records, "focus")

        def render(current: str) -> list:
            records[-1]["current"] = current
            lines, _, _ = _render_main_content(state, records, accounts, current, 120, 52, [])
            self.assertIn(str(state.focus_account), strip_ansi(lines[0])[32:])
            zones = []
            render_sidebar(state, SIDEBAR_WIDTH, 51, 98, zones)
            return zones

        zones = render("openai-1")
        charts = [zone for zone in zones if zone.kind == "focus_chart"]
        paging = next(zone for zone in zones if zone.kind == "summary_scroll")
        self.assertEqual([(zone.y, zone.value) for zone in charts], [(paging.y - 2, "openai-2")])
        state.main_cache_key = ("cached",)
        handle_click(state, zones, charts[0].x1, charts[0].y)
        self.assertIsNone(state.main_cache_key)
        zones = render("openai-1")
        self.assertEqual((state.focus_account, state.summary_offset), ("openai-2", 0))
        charts = [zone for zone in zones if zone.kind == "focus_chart"]
        self.assertEqual([zone.value for zone in charts], ["openai-1", "openai-3"])
        handle_click(state, zones, charts[0].x1, charts[0].y)
        render("openai-1")
        self.assertEqual(state.focus_account, "openai-1")

        # API providers are absent from quota accounts; keep the displayed chart.
        render("api-provider")
        self.assertEqual(state.focus_account, "openai-1")
        zones = render("openai-4")
        self.assertEqual(state.focus_account, "openai-4")
        charts = [zone for zone in zones if zone.kind == "focus_chart"]
        self.assertEqual([zone.value for zone in charts], ["openai-3"])
        handle_click(state, zones, charts[0].x1, charts[0].y)
        render("api-provider")
        self.assertEqual(state.focus_account, "openai-3")
        render("openai-4")
        self.assertEqual(state.focus_account, "openai-4")

        # Removing the viewed account falls back to an available chart.
        accounts.pop()
        render("openai-4")
        self.assertEqual(state.focus_account, "openai-1")

    def test_small_dashboard_preserves_minimum_chart_height(self) -> None:
        accounts, records = self.records(4)
        state = monitor_state(records, "list")
        lines, width, height = _render_main_content(
            state, records, accounts, "openai-1", 120, 20, []
        )
        plain = [strip_ansi(line) for line in lines]
        chart_top = next(index for index, line in enumerate(plain) if "┌" in line)
        chart_bottom = next(index for index, line in enumerate(plain) if "└" in line)
        self.assertEqual((len(lines), width, height), (19, 97, 19))
        self.assertGreaterEqual(chart_bottom - chart_top + 1, 4)

    def test_auto_window_shows_available_quotas_and_uses_primary_color(self) -> None:
        state = monitor_state([], "focus")
        choices = next(choices for key, _, choices in setting_items(state) if key == "window_scope")
        self.assertIn((t("choice.window.auto"), "auto"), choices)
        apply_setting_value(state, "window_scope", "auto")
        self.assertEqual(state.window_scope, "auto")
        self.assertEqual(saved_window_scope({"window_scope": "auto"}), "auto")

        for windows in ((), ("5h",), ("7d",), ("5h", "7d")):
            active = account(1, self.now)
            active["q"] = {key: value for key, value in active["q"].items() if key in windows}
            records = [{"t": self.now - 60, "a": [active]}]
            with self.subTest(windows=windows):
                for rows in (
                    quota_rows(active, 60, window_scope="auto"),
                    merged_quota_rows([active], "openai-1", 60, window_scope="auto"),
                    [current_account_quota_summary([active], "openai-1", "auto")],
                ):
                    text = strip_ansi("\n".join(rows))
                    for key in ("5h", "7d"):
                        self.assertEqual(key in text, key in windows)
                for _, curve in CURVE_MODE_CHOICES:
                    for merged in (False, True):
                        with self.subTest(curve=curve, merged=merged), patch(
                            "ui.charts.chart_series_color", wraps=chart_series_color
                        ) as colors:
                            if merged:
                                lines = merged_chart_lines(records, "5m", 60, 18, curve, "auto", [active])
                            else:
                                lines = chart_lines(records, "openai-1", "5m", 60, 18, curve, "auto")
                            self.assertTrue(lines)
                            self.assertEqual({call.args[0] for call in colors.call_args_list}, set(windows))
                            self.assertTrue(all(
                                call.kwargs.get("primary") == (len(windows) == 1)
                                for call in colors.call_args_list
                            ))
        self.assertEqual(chart_series_color("7d", 50, primary=True), percent_color(50))
        self.assertEqual(chart_series_color("5h", 50), percent_color(50))
        self.assertNotEqual(chart_series_color("7d", 50), chart_series_color("5h", 50))

    def test_auto_excludes_synthetic_pro_quota_from_panels_and_merged_history(self) -> None:
        accounts, records = self.records(4)
        pro = accounts[-1]
        pro.update(plan="pro-20x", synthetic_windows=["5h"])
        pro["q"]["5h"][0] = 100
        pro["q"]["7d"][0] = 98
        accounts[0]["q"]["5h"][0] = 100  # A real 100% must still count.
        for scope, five_hour_max in (("auto", 3), ("both", 23), ("5h", 23), ("7d", 23)):
            self.assertEqual(merged_window_info(accounts, "5h", "actual", scope)["max_left"], five_hour_max)
            self.assertEqual(merged_window_info(accounts, "7d", "actual", scope)["max_left"], 23)
        cached = HistoryRecordList(HistorySeriesIndex())
        for record in records:
            cached.series_index.append_record(record)
            cached.append(record)
        for history in (records, cached):
            with patch("ui.panels.series_chart_lines", return_value=[]) as chart:
                merged_chart_lines(history, "5m", 60, 18, "connected", "auto", accounts, "actual")
                points = chart.call_args.args[0]
                self.assertNotIn("openai-4", points["5h"])
                self.assertEqual(set(points["5h"]), {"openai-1", "openai-2", "openai-3"})
                self.assertEqual(len(points["7d"]), 4)
            for scope, expected in (("auto", {"7d"}), ("both", {"5h", "7d"}), ("5h", {"5h"}), ("7d", {"7d"})):
                with patch("ui.charts.chart_series_color", wraps=chart_series_color) as colors:
                    chart_lines(history, "openai-4", "5m", 60, 18, "connected", scope)
                    self.assertEqual({call.args[0] for call in colors.call_args_list}, expected)
        pro_records = [{"t": self.now - 60, "current": "openai-4", "a": [pro]}]
        for mode in ("list", "focus", "merged"):
            state = monitor_state(pro_records, mode)
            state.window_scope = "auto"
            lines, _, _ = _render_main_content(state, pro_records, [pro], "openai-4", 160, 61, [])
            plain = strip_ansi("\n".join(lines))
            self.assertIn("7d", plain)
            self.assertNotIn("5h", plain)

    def test_dashboard_height_at_most_15_omits_upper_blank_rows(self) -> None:
        accounts, records = self.records(1)

        compact = [
            strip_ansi(line)
            for line in account_lines(
                accounts[0], records, 60, 15, "5m", "openai-1", "connected"
            )
        ]
        compact_chart_top = next(index for index, line in enumerate(compact) if "┌" in line)

        self.assertTrue(all(line.strip() for line in compact[:compact_chart_top]))


if __name__ == "__main__":
    unittest.main()
