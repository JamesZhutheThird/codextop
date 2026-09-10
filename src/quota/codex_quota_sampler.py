#!/usr/bin/env python3
"""Append usage snapshots while refreshing reset credits independently."""

from __future__ import annotations

import argparse
import atexit
import fcntl
import json
import os
import signal
import sys
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quota import check_codex_quota as quota
from core.constants import RESET_CREDITS_INTERVAL_SECONDS
from core.i18n import t
from core.paths import default_paths, ensure_runtime_layout, monthly_log_path
from quota.reset_credits_cache import reset_credits_cache_path
from quota.token_usage_cache import token_usage_cache_path


DEFAULT_PATHS = default_paths()
DEFAULT_LOG_DIR = DEFAULT_PATHS.log_dir
DEFAULT_AUTH_FILE = DEFAULT_PATHS.active_auth_file
DEFAULT_AUTH_LIST = DEFAULT_PATHS.auth_list_dir
DEFAULT_LOG_FILE = "quota_snapshots.jsonl"
DEFAULT_CONTROL_FILE = "sampler_control.json"
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_RESET_CREDITS_INTERVAL_SECONDS = RESET_CREDITS_INTERVAL_SECONDS
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
    token_usage = account.get("token_usage")
    lifetime_tokens = token_usage.get("lifetime_tokens") if isinstance(token_usage, dict) else None
    if isinstance(lifetime_tokens, (int, float)) and lifetime_tokens >= 0:
        source_epoch = token_usage.get("checked_at_epoch") or token_usage.get("generated_at_epoch")
        item["u"] = [
            int(lifetime_tokens),
            int(source_epoch) if isinstance(source_epoch, (int, float)) else None,
        ]
    if account.get("email"):
        item["email"] = account.get("email")
    if account.get("plan_type"):
        item["plan"] = account.get("plan_type")
    if account.get("error"):
        item["err"] = account.get("error")
        return item

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
    # Listed windows are compatibility defaults; unlisted values came from the API.
    synthetic_windows = [key for key in q if account.get("quota", {}).get(key, {}).get("synthetic")]
    if synthetic_windows:
        item["synthetic_windows"] = synthetic_windows

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


def make_snapshot(
    auth_file: Path,
    auth_list: Path,
    all_auth: bool,
    tz_name: str,
    *,
    reset_cache_path: Path | None = None,
    reset_credits_interval_seconds: int = DEFAULT_RESET_CREDITS_INTERVAL_SECONDS,
    query_reset_credits: bool = True,
) -> dict[str, Any]:
    observed_ts = epoch_now()
    configs, current_index = quota.select_requested_configs(
        auth_file,
        auth_list,
        [],
        False,
        all_auth,
        False,
    )
    bundle = quota.collect_accounts(
        configs,
        current_index,
        tz_name,
        token_usage_cache_path(auth_list),
        observed_ts,
        reset_credits_cache_path=reset_cache_path,
        reset_credits_interval_seconds=reset_credits_interval_seconds,
        query_reset_credits=query_reset_credits,
    )
    return {
        "t": observed_ts,
        "current": bundle.get("current_index"),
        "a": [
            compact_account(account, observed_ts)
            for account in bundle.get("accounts", [])
        ],
    }


def refresh_reset_credits(
    auth_file: Path,
    auth_list: Path,
    all_auth: bool,
    cache_path: Path,
) -> dict[str, str]:
    configs, _current_index = quota.select_requested_configs(
        auth_file,
        auth_list,
        [],
        False,
        all_auth,
        False,
    )
    return quota.refresh_reset_credits_cache(configs, cache_path)


def append_snapshot(log_path: Path, snapshot: dict[str, Any], tz_name: str) -> None:
    monthly_path = monthly_log_path(log_path, snapshot.get("t"), tz_name)
    monthly_path.parent.mkdir(parents=True, exist_ok=True)
    with monthly_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
        handle.flush()


def control_path_for(log_dir: Path, control_file: str) -> Path:
    return log_dir.expanduser() / control_file


def write_control(
    control_path: Path,
    interval: int | None = None,
    sample_now: bool = True,
    all_auth: bool | None = None,
) -> None:
    control_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "updated_at": epoch_now(),
        "updated_at_ns": time.time_ns(),
        "sample_now": bool(sample_now),
    }
    if interval is not None:
        payload["interval"] = max(1, int(interval))
    if all_auth is not None:
        payload["all_auth"] = bool(all_auth)
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


def read_control_command(control_path: Path, fallback: int, last_seen_ns: int) -> tuple[int, int, bool, bool | None]:
    try:
        stat = control_path.stat()
    except OSError:
        return fallback, last_seen_ns, False, None

    try:
        payload = json.loads(control_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return fallback, stat.st_mtime_ns, False, None

    raw_seen_ns = payload.get("updated_at_ns") if isinstance(payload, dict) else None
    seen_ns = raw_seen_ns if isinstance(raw_seen_ns, int) and raw_seen_ns > 0 else stat.st_mtime_ns
    if seen_ns == last_seen_ns:
        return fallback, last_seen_ns, False, None

    interval = fallback
    raw_interval = payload.get("interval") if isinstance(payload, dict) else None
    if isinstance(raw_interval, (int, float)) and raw_interval > 0:
        interval = max(1, int(raw_interval))
    sample_now = bool(payload.get("sample_now")) if isinstance(payload, dict) else False
    all_auth_override = None
    raw_all_auth = payload.get("all_auth") if isinstance(payload, dict) else None
    if isinstance(raw_all_auth, bool):
        all_auth_override = raw_all_auth
    return interval, seen_ns, sample_now, all_auth_override


def acquire_pid_lock(pid_path: Path) -> None:
    """Hold an advisory lock for the sampler lifetime and publish its PID."""
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    handle = pid_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.seek(0)
        owner_pid = handle.read().strip()
        handle.close()
        owner = t("sampler.owner_pid", pid=owner_pid) if owner_pid else ""
        raise RuntimeError(t("sampler.already_running", owner=owner)) from exc

    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    os.fsync(handle.fileno())

    def cleanup() -> None:
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except FileNotFoundError:
            pass
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    atexit.register(cleanup)


def install_signal_handlers(stop: dict[str, bool]) -> None:
    def handle_signal(signum: int, _frame: Any) -> None:
        stop["value"] = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def run_once(
    log_path: Path,
    auth_file: Path,
    auth_list: Path,
    all_auth: bool,
    tz_name: str,
    *,
    reset_cache_path: Path | None = None,
    reset_credits_interval_seconds: int = DEFAULT_RESET_CREDITS_INTERVAL_SECONDS,
    query_reset_credits: bool = True,
) -> dict[str, Any]:
    try:
        snapshot = make_snapshot(
            auth_file,
            auth_list,
            all_auth,
            tz_name,
            reset_cache_path=reset_cache_path,
            reset_credits_interval_seconds=reset_credits_interval_seconds,
            query_reset_credits=query_reset_credits,
        )
    except Exception as exc:
        snapshot = {"t": epoch_now(), "err": str(exc)}
    append_snapshot(log_path, snapshot, tz_name)
    return snapshot


def run_loop(
    log_path: Path,
    auth_file: Path,
    auth_list: Path,
    all_auth: bool,
    tz_name: str,
    interval: int,
    control_path: Path,
    reset_credits_interval_seconds: int = DEFAULT_RESET_CREDITS_INTERVAL_SECONDS,
) -> None:
    stop = {"value": False}
    install_signal_handlers(stop)
    acquire_pid_lock(log_path.parent / "sampler.pid")

    current_interval = max(1, interval)
    current_reset_credits_interval = max(1, reset_credits_interval_seconds)
    current_all_auth = bool(all_auth)
    reset_cache = reset_credits_cache_path(auth_list)
    last_sample = 0.0
    last_reset_refresh = 0.0
    last_control_seen_ns = 0
    last_control_check = 0.0
    reset_future: Future[dict[str, str]] | None = None
    reset_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codextop-reset-credits")
    try:
        while not stop["value"]:
            now = time.monotonic()
            if reset_future is not None and reset_future.done():
                try:
                    reset_future.result()
                except Exception as exc:
                    print(t("sampler.reset_refresh_failed", error=exc), file=sys.stderr)
                reset_future = None
            if (
                reset_future is None
                and (last_reset_refresh <= 0 or now - last_reset_refresh >= current_reset_credits_interval)
            ):
                reset_future = reset_executor.submit(
                    refresh_reset_credits,
                    auth_file,
                    auth_list,
                    current_all_auth,
                    reset_cache,
                )
                last_reset_refresh = now
            if last_control_check <= 0 or now - last_control_check >= CONTROL_POLL_SECONDS:
                current_interval, last_control_seen_ns, sample_now, all_auth_override = read_control_command(
                    control_path,
                    current_interval,
                    last_control_seen_ns,
                )
                if all_auth_override is not None:
                    current_all_auth = all_auth_override
                last_control_check = now
                if sample_now:
                    last_sample = 0.0
                now = time.monotonic()
            if last_sample <= 0 or now - last_sample >= current_interval:
                run_once(
                    log_path,
                    auth_file,
                    auth_list,
                    current_all_auth,
                    tz_name,
                    reset_cache_path=reset_cache,
                    reset_credits_interval_seconds=current_reset_credits_interval,
                    query_reset_credits=False,
                )
                last_sample = time.monotonic()
            time.sleep(1.0)
    finally:
        reset_executor.shutdown(wait=True, cancel_futures=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=t("sampler.arg.description"))
    parser.add_argument(
        "--auth",
        type=Path,
        default=DEFAULT_AUTH_FILE,
        help=t("sampler.arg.auth"),
    )
    parser.add_argument(
        "--auth-list",
        type=Path,
        default=DEFAULT_AUTH_LIST,
        help=t("sampler.arg.auth_list"),
    )
    parser.add_argument(
        "--all-auth",
        action="store_true",
        help=t("sampler.arg.all_auth"),
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help=t("sampler.arg.log_dir"),
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE,
        help=t("sampler.arg.log_file", value=DEFAULT_LOG_FILE),
    )
    parser.add_argument(
        "--control-file",
        default=DEFAULT_CONTROL_FILE,
        help=t("sampler.arg.control_file", value=DEFAULT_CONTROL_FILE),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help=t("sampler.arg.interval", value=DEFAULT_INTERVAL_SECONDS),
    )
    parser.add_argument(
        "--reset-credits-interval",
        type=int,
        default=DEFAULT_RESET_CREDITS_INTERVAL_SECONDS,
        help=t("sampler.arg.reset_interval"),
    )
    parser.add_argument(
        "--tz",
        default="Asia/Shanghai",
        help=t("sampler.arg.timezone"),
    )
    parser.add_argument("--once", action="store_true", help=t("sampler.arg.once"))
    parser.add_argument("--set-interval", type=int, default=None, help=t("sampler.arg.set_interval"))
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
    reset_cache = reset_credits_cache_path(auth_list)
    if args.once:
        snapshot = run_once(
            log_path,
            auth_file,
            auth_list,
            args.all_auth,
            args.tz,
            reset_cache_path=reset_cache,
            reset_credits_interval_seconds=max(1, args.reset_credits_interval),
        )
        print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
        return 0

    run_loop(
        log_path,
        auth_file,
        auth_list,
        args.all_auth,
        args.tz,
        max(1, args.interval),
        control_path,
        max(1, args.reset_credits_interval),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
