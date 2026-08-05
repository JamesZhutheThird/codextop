#!/usr/bin/env python3
"""Compare optimized history/cache rendering against the legacy full reader."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ui.charts import chart_lines
from ui.history import IncrementalHistoryCache, read_snapshots, window_points
from ui.panels import merged_chart_lines


CURVE_MODES = ("connected", "points", "braille", "box", "bar")
PERIODS = ("5h", "7d", "all")
WINDOW_SCOPES = ("both", "5h", "7d")


def stable_copy(source: Path, destination: Path) -> int:
    payload = source.read_bytes()
    newline = payload.rfind(b"\n")
    complete = payload[: newline + 1] if newline >= 0 else b""
    destination.write_bytes(complete)
    return len(complete)


def verify(source: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="codextop-compat-") as temp_dir:
        root = Path(temp_dir)
        copied = root / source.name
        stable_bytes = stable_copy(source, copied)
        base = root / "quota_snapshots.jsonl"
        digest_before = hashlib.sha256(copied.read_bytes()).hexdigest()

        legacy = read_snapshots(base, "all", "Asia/Shanghai")
        optimized, _changed = IncrementalHistoryCache(base, "Asia/Shanghai").read("all")
        if [record["t"] for record in optimized] != [record["t"] for record in legacy]:
            raise AssertionError("timestamp sequence differs")
        if optimized[-1] != legacy[-1]:
            raise AssertionError("latest full record differs")

        accounts = [
            account.get("i")
            for account in legacy[-1].get("a", [])
            if isinstance(account, dict) and isinstance(account.get("i"), (int, str))
        ]
        series_checks = 0
        for account in accounts:
            for window in ("5h", "7d"):
                expected = window_points(legacy, account, window)
                actual = optimized.series_index.account_window(account, window)
                if not expected:
                    if actual:
                        raise AssertionError(f"unexpected series: {account}/{window}")
                    continue
                if actual is None:
                    raise AssertionError(f"missing series: {account}/{window}")
                expected_times = [point["t"] for point in expected]
                expected_values = [point["left"] for point in expected]
                expected_resets = [point["reset"] if point["reset"] is not None else -1 for point in expected]
                if list(actual.times) != expected_times:
                    raise AssertionError(f"series timestamps differ: {account}/{window}")
                if list(actual.values) != expected_values:
                    raise AssertionError(f"series values differ: {account}/{window}")
                if list(actual.resets) != expected_resets:
                    raise AssertionError(f"series resets differ: {account}/{window}")
                series_checks += 1

        chart_checks = 0
        fixed_now = int(legacy[-1]["t"]) + 5
        with patch("ui.history.time.time", return_value=fixed_now):
            for period in PERIODS:
                for mode in CURVE_MODES:
                    for window_scope in WINDOW_SCOPES:
                        for account in accounts:
                            if chart_lines(optimized, account, period, 120, 30, mode, window_scope) != chart_lines(
                                legacy, account, period, 120, 30, mode, window_scope
                            ):
                                raise AssertionError(
                                    f"account chart differs: {account}/{period}/{mode}/{window_scope}"
                                )
                            chart_checks += 1
                        if merged_chart_lines(
                            optimized, period, 120, 30, mode, window_scope
                        ) != merged_chart_lines(legacy, period, 120, 30, mode, window_scope):
                            raise AssertionError(f"merged chart differs: {period}/{mode}/{window_scope}")
                        chart_checks += 1

        digest_after = hashlib.sha256(copied.read_bytes()).hexdigest()
        if digest_before != digest_after:
            raise AssertionError("history file changed during verification")
        return {
            "source": str(source),
            "stable_bytes": stable_bytes,
            "records": len(legacy),
            "accounts": accounts,
            "series_checks": series_checks,
            "chart_checks": chart_checks,
            "raw_sha256_unchanged": True,
            "compatible": True,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_log", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.source_log.expanduser()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
