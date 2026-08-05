#!/usr/bin/env python3
"""Benchmark initial loading and steady-state CodexTOP refresh latency."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ui import color_schemes
from ui.app import render_frame
from ui.history import read_records_if_due
from ui.models import MonitorState


def current_rss_mib() -> float:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (FileNotFoundError, OSError, ValueError):
        pass
    return 0.0


def measure(callable_, iterations: int = 1) -> list[float]:
    timings = []
    for _ in range(iterations):
        started = time.perf_counter()
        callable_()
        timings.append(time.perf_counter() - started)
    return timings


def benchmark(source_log: Path, iterations: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="codextop-benchmark-") as temp_dir:
        temp_root = Path(temp_dir)
        copied_log = temp_root / source_log.name
        shutil.copyfile(source_log, copied_log)
        base_log = temp_root / "quota_snapshots.jsonl"
        state = MonitorState(
            period="all",
            interval=5,
            tz="Asia/Shanghai",
            log_path=base_log,
            restore_interval=5,
            state_path=temp_root / "state.json",
            curve_mode="connected",
            display_scope="merged",
            window_scope="both",
            color_scheme="classic",
        )
        color_schemes.set_active_color_scheme("classic")

        initial_load = measure(lambda: read_records_if_due(state, force=True))[0]
        rss_after_load = current_rss_mib()
        cold_render = measure(lambda: render_frame(state, 160, 48))[0]
        rss_after_render = current_rss_mib()
        warm_render = measure(lambda: render_frame(state, 160, 48), iterations)

        appended = copy.deepcopy(state.records[-1])
        appended["t"] = int(appended["t"]) + 5
        with copied_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(appended, ensure_ascii=False, separators=(",", ":")) + "\n")

        incremental_refresh = measure(lambda: read_records_if_due(state, force=True))[0]
        post_append_render = measure(lambda: render_frame(state, 160, 48))[0]
        warm_after_append = measure(lambda: render_frame(state, 160, 48), iterations)
        cache = state.history_cache

        return {
            "source": str(source_log),
            "source_bytes": source_log.stat().st_size,
            "records": len(state.records or []),
            "initial_load_seconds": initial_load,
            "cold_render_seconds": cold_render,
            "warm_render_median_seconds": statistics.median(warm_render),
            "warm_render_max_seconds": max(warm_render),
            "incremental_refresh_seconds": incremental_refresh,
            "post_append_render_seconds": post_append_render,
            "append_to_visible_seconds": incremental_refresh + post_append_render,
            "warm_after_append_median_seconds": statistics.median(warm_after_append),
            "rss_after_load_mib": rss_after_load,
            "rss_after_render_mib": rss_after_render,
            "cache_full_loads": getattr(cache, "full_loads", None),
            "cache_incremental_reads": getattr(cache, "incremental_reads", None),
            "cache_bytes_read": getattr(cache, "bytes_read", None),
            "target_below_half_second": incremental_refresh + post_append_render < 0.5,
            "cpu_count": os.cpu_count() or 1,
            "allowed_parallel_workers": min(8, max(1, (os.cpu_count() or 1) // 2)),
            "parallel_workers_used": 1,
        }


def write_synthetic_log(path: Path, samples: int) -> None:
    start = int(time.time()) - samples * 5
    with path.open("w", encoding="utf-8") as handle:
        for sample in range(samples):
            timestamp = start + sample * 5
            record = {
                "t": timestamp,
                "current": "openai-1",
                "a": [
                    {
                        "i": account,
                        "cur": account == "openai-1",
                        "label": account,
                        "email": f"{account}@example.com",
                        "plan": "plus",
                        "ok": True,
                        "q": {
                            "5h": [100 - sample % 101, timestamp + 18000, 18000, 18000],
                            "7d": [100 - (sample // 10) % 101, timestamp + 604800, 604800, 604800],
                        },
                        "rc": 0,
                        "r": [],
                    }
                    for account in ("openai-1", "openai-2", "openai-3")
                ],
            }
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_log", type=Path, nargs="?")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--synthetic-samples", type=int, default=0)
    args = parser.parse_args()
    if args.synthetic_samples > 0:
        with tempfile.TemporaryDirectory(prefix="codextop-synthetic-") as temp_dir:
            source_log = Path(temp_dir) / "quota_snapshots_2026-07.jsonl"
            write_synthetic_log(source_log, args.synthetic_samples)
            result = benchmark(source_log, max(1, args.iterations))
            result["source"] = f"synthetic:{args.synthetic_samples}x5s"
    elif args.source_log is not None:
        result = benchmark(args.source_log.expanduser(), max(1, args.iterations))
    else:
        parser.error("source_log or --synthetic-samples is required")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["target_below_half_second"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
