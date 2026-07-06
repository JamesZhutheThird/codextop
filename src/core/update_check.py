"""Daily non-blocking update checks for the CodexTOP terminal UI."""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import atomic_write_json
from .updater import UpdateError, check_update


ALLOW_NON_GITHUB_ENV = "CODEXTOP_UPDATE_ALLOW_NON_GITHUB"
UPDATE_CHECK_FILE = "update_check.json"


@dataclass
class DailyUpdateCheck:
    checked_date: str
    update_available: bool = False
    latest_version: str | None = None
    reason: str | None = None
    error: str | None = None
    remote_revision: str | None = None
    local_revision: str | None = None


def today_key() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def read_daily_update_check(path: Path) -> DailyUpdateCheck | None:
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    checked_date = payload.get("checked_date")
    if not isinstance(checked_date, str) or not checked_date:
        return None
    return DailyUpdateCheck(
        checked_date=checked_date,
        update_available=bool(payload.get("update_available")),
        latest_version=payload.get("latest_version") if isinstance(payload.get("latest_version"), str) else None,
        reason=payload.get("reason") if isinstance(payload.get("reason"), str) else None,
        error=payload.get("error") if isinstance(payload.get("error"), str) else None,
        remote_revision=payload.get("remote_revision") if isinstance(payload.get("remote_revision"), str) else None,
        local_revision=payload.get("local_revision") if isinstance(payload.get("local_revision"), str) else None,
    )


def write_daily_update_check(path: Path, result: DailyUpdateCheck) -> None:
    atomic_write_json(path, asdict(result), 0o600)


def apply_update_check_to_state(state: Any, result: DailyUpdateCheck) -> None:
    state.update_checking = False
    state.update_available = bool(result.update_available)
    state.update_latest_version = result.latest_version
    state.update_reason = result.reason
    state.update_error = result.error
    if not result.update_available:
        state.update_confirming = False


def update_check_path(settings_dir: Path) -> Path:
    return settings_dir.expanduser() / UPDATE_CHECK_FILE


def should_allow_non_github() -> bool:
    return os.environ.get(ALLOW_NON_GITHUB_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def run_update_check(path: Path) -> DailyUpdateCheck:
    try:
        status = check_update(allow_non_github=should_allow_non_github())
        return DailyUpdateCheck(
            checked_date=today_key(),
            update_available=status.update_available,
            latest_version=status.latest_version,
            reason=status.reason,
            remote_revision=status.remote_revision,
            local_revision=status.local_revision,
        )
    except UpdateError as exc:
        return DailyUpdateCheck(checked_date=today_key(), error=str(exc))
    except Exception as exc:
        return DailyUpdateCheck(checked_date=today_key(), error=f"unexpected update check error: {exc}")


def start_daily_update_check(state: Any) -> None:
    path = state.update_check_path
    if path is None:
        return
    cached = read_daily_update_check(path)
    if cached is not None and cached.checked_date == today_key():
        apply_update_check_to_state(state, cached)
        return

    state.update_checking = True

    def worker() -> None:
        result = run_update_check(path)
        try:
            write_daily_update_check(path, result)
        except OSError as exc:
            result.error = f"{result.error or result.reason or 'update check complete'}; save failed: {exc}"
        apply_update_check_to_state(state, result)

    threading.Thread(target=worker, name="codextop-update-check", daemon=True).start()
