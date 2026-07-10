"""Tests for one-sided quota history smoothing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ui.history import smooth_upward_spikes


def record(timestamp: int, value: int, reset_epoch: int, *, index: str = "openai-1") -> dict:
    return {
        "t": timestamp,
        "a": [
            {
                "i": index,
                "q": {
                    "5h": [value, reset_epoch, max(0, reset_epoch - timestamp), 18000],
                    "7d": [80, 700000, 700000 - timestamp, 604800],
                },
            }
        ],
    }


def values(records: list[dict], window: str = "5h") -> list[int | float]:
    return [item["a"][0]["q"][window][0] for item in records]


class HistorySmoothingTests(unittest.TestCase):
    def test_single_upward_spike_is_suppressed(self) -> None:
        records = [
            record(100, 40, 10000),
            record(110, 99, 18110),
            record(120, 39, 10000),
        ]

        smooth_upward_spikes(records)

        self.assertEqual(values(records), [40, 40.0, 39])
        self.assertEqual(records[1]["a"][0]["q"]["5h"][1:3], [10000, 9890])

    def test_downward_change_is_immediate(self) -> None:
        records = [record(100, 80, 10000), record(110, 45, 10000)]

        smooth_upward_spikes(records)

        self.assertEqual(values(records), [80, 45])

    def test_scheduled_reset_is_immediate(self) -> None:
        records = [record(100, 20, 110), record(111, 100, 18111)]

        smooth_upward_spikes(records)

        self.assertEqual(values(records), [20, 100])

    def test_persistent_stable_increase_is_confirmed(self) -> None:
        records = [
            record(100, 40, 10000),
            record(110, 90, 20000),
            record(120, 89, 20001),
            record(141, 88, 20000),
        ]

        smooth_upward_spikes(records)

        self.assertEqual(values(records), [40, 40.0, 40.0, 88])

    def test_alternating_server_values_never_confirm(self) -> None:
        records = [
            record(100, 37, 10000),
            record(110, 83, 10000),
            record(120, 37, 10000),
            record(130, 83, 10000),
            record(140, 83, 10000),
            record(150, 37, 10000),
        ]

        smooth_upward_spikes(records)

        self.assertEqual(values(records), [37, 37.0, 37, 37.0, 37.0, 37])

    def test_error_breaks_upward_confirmation(self) -> None:
        records = [
            record(100, 40, 10000),
            record(110, 90, 20000),
            {"t": 125, "a": [{"i": "openai-1", "err": "HTTP 503"}]},
            record(141, 89, 20000),
            record(151, 88, 20000),
        ]

        smooth_upward_spikes(records)

        observed = [
            item["a"][0]["q"]["5h"][0]
            for item in records
            if "q" in item["a"][0]
        ]
        self.assertEqual(observed, [40, 40.0, 40.0, 40.0])


if __name__ == "__main__":
    unittest.main()
