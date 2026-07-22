import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ui.app import render_frame, run_once
from ui.models import MonitorState


def state_for_test(display_scope: str) -> MonitorState:
    root = Path(tempfile.gettempdir())
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
    )


class UsageModeLazyHistoryTests(unittest.TestCase):
    def test_usage_frame_does_not_load_unrelated_quota_history(self) -> None:
        state = state_for_test("usage")

        with patch("ui.app.read_records_if_due") as read_records:
            render_frame(state, 120, 36)

        read_records.assert_not_called()
        self.assertIsNone(state.records)

    def test_quota_frame_still_loads_history(self) -> None:
        state = state_for_test("all")

        with patch("ui.app.read_records_if_due", return_value=[]) as read_records:
            render_frame(state, 120, 36)

        read_records.assert_called_once_with(state)

    def test_once_mode_uses_the_same_lazy_render_path(self) -> None:
        state = state_for_test("usage")

        with (
            patch("ui.app.read_records_if_due") as read_records,
            patch("ui.app.shutil.get_terminal_size", return_value=(120, 36)),
            patch("builtins.print"),
        ):
            result = run_once(state)

        self.assertEqual(result, 0)
        read_records.assert_not_called()


if __name__ == "__main__":
    unittest.main()
