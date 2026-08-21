from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ui.app import _render_main_content
from ui.settings import handle_click
from ui.terminal_text import strip_ansi


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


class DashboardPagingTests(unittest.TestCase):
    def test_small_dashboard_pages_accounts_and_preserves_chart_height(self) -> None:
        now = int(time.time())
        accounts = [account(number, now) for number in range(1, 5)]
        records = [{"t": now - 60, "current": "openai-1", "a": accounts}]
        state = SimpleNamespace(
            token_monitor=None,
            display_scope="all",
            period="5m",
            curve_mode="connected",
            color_scheme="classic",
            window_scope="both",
            summary_offset=0,
            records=records,
        )
        zones = []

        lines, width, height = _render_main_content(
            state, records, accounts, "openai-1", 120, 20, zones
        )
        plain = [strip_ansi(line) for line in lines]

        self.assertEqual((len(lines), width, height), (19, 99, 19))
        self.assertIn("▲ 上一页", plain[0])
        self.assertIn("▼ 下一页", plain[-1])
        self.assertIn("openai-1", "\n".join(plain))
        self.assertIn("openai-3", "\n".join(plain))
        self.assertNotIn("openai-4", "\n".join(plain))
        chart_top = next(index for index, line in enumerate(plain) if "┌" in line)
        chart_bottom = next(index for index, line in enumerate(plain) if "└" in line)
        self.assertGreaterEqual(chart_bottom - chart_top + 1, 4)
        self.assertEqual(
            [(zone.y, zone.value) for zone in zones if zone.kind == "summary_scroll"],
            [(1, 3), (19, 3)],
        )

        handle_click(state, zones, 1, 19)
        self.assertEqual(state.summary_offset, 3)
        next_lines, _width, _height = _render_main_content(
            state, records, accounts, "openai-1", 120, 20, []
        )
        next_page = "\n".join(strip_ansi(line) for line in next_lines)
        self.assertIn("openai-4", next_page)
        self.assertNotIn("openai-1", next_page)
        self.assertNotIn("openai-3", next_page)


if __name__ == "__main__":
    unittest.main()
