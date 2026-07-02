#!/usr/bin/env python3
"""Append compact Codex quota snapshots to JSONL once per interval."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOLKIT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLKIT_DIR))
import check_codex_quota as quota
try:
    from .paths import default_paths, ensure_runtime_layout
except ImportError:
    from paths import default_paths, ensure_runtime_layout


DEFAULT_PATHS = default_paths()
DEFAULT_LOG_DIR = DEFAULT_PATHS.log_dir
DEFAULT_AUTH_FILE = DEFAULT_PATHS.active_auth_file
DEFAULT_AUTH_LIST = DEFAULT_PATHS.auth_list_dir
DEFAULT_LOG_FILE = "quota_snapshots.jsonl"
DEFAULT_CONTROL_FILE = "sampler_control.json"
DEFAULT_INTERVAL_SECONDS = 60
CONTROL_POLL_SECONDS = 5.0


def epoch_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def reset_epoch(observed_ts: int, window: dict[str, Any]) -> int | None:
    seconds = window.get("reset_after_seconds")
    if isinstance(seconds, (int, float)):
        return observed_ts + int(seconds)
    return None


def expire_epoch(observed_ts: int, credit: dict[str, Any]) -> int | None:
    seconds = credit.get("expires_after_seconds")
    if isinstance(seconds, (int, float)):
        return observed_ts + int(seconds)
    return None


def compact_credit_title(title: Any) -> str:
    return quota.compact_reset_title(title)


def compact_account(account: dict[str, Any], observed_ts: int) -> dict[str, Any]:
    item: dict[str, Any] = {
        "i": account.get("index"),
        "cur": bool(account.get("current")),
    }
    if account.get("label"):
        item["label"] = account.get("label")
    if account.get("error"):
        item["err"] = account.get("error")
        return item

    item["email"] = account.get("email")
    item["plan"] = account.get("plan_type")
    item["ok"] = account.get("allowed")

    q: dict[str, list[Any]] = {}
    for key in ("5h", "7d"):
        window = account.get("quota", {}).get(key, {})
        q[key] = [
            window.get("remaining_percent"),
            reset_epoch(observed_ts, window),
            window.get("reset_after_seconds"),
            window.get("limit_window_seconds"),
        ]
    item["q"] = q

    reset = account.get("reset_credits", {})
    item["rc"] = reset.get("available_count")
    credits = []
    for credit in reset.get("credits", []):
        if credit.get("status") != "available":
            continue
        credits.append(
            [
                compact_credit_title(credit.get("title")),
                expire_epoch(observed_ts, credit),
                credit.get("expires_remaining_percent"),
            ]
        )
    item["r"] = credits
    return item


def make_snapshot(auth_file: Path, auth_list: Path, all_auth: bool, tz_name: str) -> dict[str, Any]:
    observed_ts = epoch_now()
    configs, current_index = quota.select_requested_configs(
        auth_file,
        auth_list,
        [],
        False,
        all_auth,
        False,
    )
    bundle = quota.collect_accounts(configs, current_index, tz_name)
    return {
        "t": observed_ts,
        "current": bundle.get("current_index"),
        "a": [
            compact_account(account, observed_ts)
            for account in bundle.get("accounts", [])
        ],
    }


def append_snapshot(log_path: Path, snapshot: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
        handle.flush()


def control_path_for(log_dir: Path, control_file: str) -> Path:
    return log_dir.expanduser() / control_file


def write_control(control_path: Path, interval: int | None = None, sample_now: bool = True) -> None:
    control_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"updated_at": epoch_now(), "sample_now": bool(sample_now)}
    if interval is not None:
        payload["interval"] = max(1, int(interval))
    fd, tmp_name = tempfile.mkstemp(prefix=f".{control_path.name}.", dir=control_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, control_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def read_control_interval(control_path: Path, fallback: int) -> int:
    try:
        payload = json.loads(control_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback
    interval = payload.get("interval") if isinstance(payload, dict) else None
    if isinstance(interval, (int, float)) and interval > 0:
        return max(1, int(interval))
    return fallback


def read_control_command(control_path: Path, fallback: int, last_seen_ns: int) -> tuple[int, int, bool]:
    try:
        stat = control_path.stat()
    except OSError:
        return fallback, last_seen_ns, False
    seen_ns = stat.st_mtime_ns
    if seen_ns == last_seen_ns:
        return fallback, last_seen_ns, False

    try:
        payload = json.loads(control_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return fallback, seen_ns, False

    interval = fallback
    raw_interval = payload.get("interval") if isinstance(payload, dict) else None
    if isinstance(raw_interval, (int, float)) and raw_interval > 0:
        interval = max(1, int(raw_interval))
    sample_now = bool(payload.get("sample_now")) if isinstance(payload, dict) else False
    return interval, seen_ns, sample_now


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_pid_lock(pid_path: Path) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            old_pid = -1
        if old_pid > 0 and old_pid != os.getpid() and pid_is_running(old_pid):
            raise RuntimeError(f"sampler already running with pid {old_pid}")
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def cleanup() -> None:
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except FileNotFoundError:
            pass

    atexit.register(cleanup)


def install_signal_handlers(stop: dict[str, bool]) -> None:
    def handle_signal(signum: int, _frame: Any) -> None:
        stop["value"] = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def run_once(log_path: Path, auth_file: Path, auth_list: Path, all_auth: bool, tz_name: str) -> dict[str, Any]:
    try:
        snapshot = make_snapshot(auth_file, auth_list, all_auth, tz_name)
    except Exception as exc:
        snapshot = {"t": epoch_now(), "err": str(exc)}
    append_snapshot(log_path, snapshot)
    return snapshot


def run_loop(
    log_path: Path,
    auth_file: Path,
    auth_list: Path,
    all_auth: bool,
    tz_name: str,
    interval: int,
    control_path: Path,
) -> None:
    stop = {"value": False}
    install_signal_handlers(stop)
    acquire_pid_lock(log_path.parent / "sampler.pid")

    current_interval = max(1, interval)
    last_sample = 0.0
    last_control_seen_ns = 0
    last_control_check = 0.0
    while not stop["value"]:
        now = time.monotonic()
        if last_control_check <= 0 or now - last_control_check >= CONTROL_POLL_SECONDS:
            current_interval, last_control_seen_ns, sample_now = read_control_command(
                control_path,
                current_interval,
                last_control_seen_ns,
            )
            last_control_check = now
            if sample_now:
                last_sample = 0.0
            now = time.monotonic()
        if last_sample <= 0 or now - last_sample >= current_interval:
            run_once(log_path, auth_file, auth_list, all_auth, tz_name)
            last_sample = time.monotonic()
        time.sleep(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample Codex quotas into JSONL.")
    parser.add_argument(
        "--auth",
        type=Path,
        default=DEFAULT_AUTH_FILE,
        help="Path to Codex auth JSON. Default: $CODEXTOP_CODEX_DIR/auth.json",
    )
    parser.add_argument(
        "--auth-list",
        type=Path,
        default=DEFAULT_AUTH_LIST,
        help=(
            "Path to Codex auth_list. Used when --all-auth is set. Default: "
            "$CODEXTOP_CODEX_DIR/codextop/auth_list"
        ),
    )
    parser.add_argument(
        "--all-auth",
        action="store_true",
        help="Prefer auth_list mode and sample every auth-*.json entry when available.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory for quota JSONL logs. Default: $CODEXTOP_CODEX_DIR/codextop/log",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE,
        help=f"Quota JSONL file name. Default: {DEFAULT_LOG_FILE}",
    )
    parser.add_argument(
        "--control-file",
        default=DEFAULT_CONTROL_FILE,
        help=f"Sampler control file name. Default: {DEFAULT_CONTROL_FILE}",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"Sampling interval in seconds. Default: {DEFAULT_INTERVAL_SECONDS}",
    )
    parser.add_argument(
        "--tz",
        default="Asia/Shanghai",
        help="Timezone for collected reset timestamps. Default: Asia/Shanghai",
    )
    parser.add_argument("--once", action="store_true", help="Collect one snapshot and exit.")
    parser.add_argument("--set-interval", type=int, default=None, help="Send a new interval command to the running sampler and exit.")
    args = parser.parse_args()

    ensure_runtime_layout()
    log_path = args.log_dir.expanduser() / args.log_file
    control_path = control_path_for(args.log_dir, args.control_file)
    if args.set_interval is not None:
        write_control(control_path, args.set_interval)
        print(json.dumps({"ok": True, "interval": max(1, int(args.set_interval)), "control": str(control_path)}, ensure_ascii=False))
        return 0

    auth_file = args.auth.expanduser()
    auth_list = args.auth_list.expanduser()
    if args.once:
        snapshot = run_once(log_path, auth_file, auth_list, args.all_auth, args.tz)
        print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
        return 0

    run_loop(log_path, auth_file, auth_list, args.all_auth, args.tz, max(1, args.interval), control_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
