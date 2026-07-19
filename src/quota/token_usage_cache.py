"""Persist and cheaply read the once-daily account Token usage cache."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.paths import atomic_write_json


TOKEN_USAGE_CACHE_FILE = "token_usage_daily.json"
TOKEN_USAGE_CACHE_TTL_SECONDS = 24 * 60 * 60
TOKEN_USAGE_CACHE_VERSION = 1


def token_usage_cache_path(auth_list_dir: Path) -> Path:
    return auth_list_dir.expanduser().parent / "settings" / TOKEN_USAGE_CACHE_FILE


def empty_token_usage_cache() -> dict[str, Any]:
    return {"version": TOKEN_USAGE_CACHE_VERSION, "accounts": {}}


def read_token_usage_cache(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return empty_token_usage_cache()
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, dict):
        return empty_token_usage_cache()
    return {"version": TOKEN_USAGE_CACHE_VERSION, "accounts": accounts}


def write_token_usage_cache(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(
        path.expanduser(),
        {
            "version": TOKEN_USAGE_CACHE_VERSION,
            "accounts": payload.get("accounts", {}),
        },
    )


def compatible_cache_entry(
    payload: dict[str, Any],
    index: str,
    account_id: str | None,
) -> dict[str, Any] | None:
    accounts = payload.get("accounts")
    entry = accounts.get(str(index)) if isinstance(accounts, dict) else None
    if not isinstance(entry, dict):
        return None
    cached_account_id = entry.get("account_id")
    if (
        isinstance(cached_account_id, str)
        and isinstance(account_id, str)
        and cached_account_id != account_id
    ):
        return None
    return entry


def cached_token_usage(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    usage = entry.get("token_usage") if isinstance(entry, dict) else None
    total = usage.get("lifetime_tokens") if isinstance(usage, dict) else None
    if not isinstance(total, (int, float)) or total < 0:
        return None
    return dict(usage)


def token_usage_query_due(
    entry: dict[str, Any] | None,
    observed_epoch: int,
    ttl_seconds: int = TOKEN_USAGE_CACHE_TTL_SECONDS,
) -> bool:
    checked_at = entry.get("checked_at_epoch") if isinstance(entry, dict) else None
    if not isinstance(checked_at, (int, float)):
        return True
    return observed_epoch < checked_at or observed_epoch - checked_at >= max(1, ttl_seconds)


def cached_token_totals(payload: dict[str, Any]) -> dict[str, tuple[int, int | None]]:
    totals: dict[str, tuple[int, int | None]] = {}
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        return totals
    for index, raw_entry in accounts.items():
        usage = cached_token_usage(raw_entry if isinstance(raw_entry, dict) else None)
        if usage is None:
            continue
        checked_at = raw_entry.get("checked_at_epoch")
        totals[str(index)] = (
            int(usage["lifetime_tokens"]),
            int(checked_at) if isinstance(checked_at, (int, float)) else None,
        )
    return totals


@dataclass(slots=True)
class DailyTokenUsageCacheReader:
    path: Path
    totals: dict[str, tuple[int, int | None]] = field(default_factory=dict)
    version: int = 0
    _mtime_ns: int | None = None

    def poll(self) -> bool:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if mtime_ns == self._mtime_ns:
            return False
        self._mtime_ns = mtime_ns
        totals = cached_token_totals(read_token_usage_cache(self.path)) if mtime_ns is not None else {}
        if totals == self.totals:
            return False
        self.totals = totals
        self.version += 1
        return True
