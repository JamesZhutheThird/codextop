"""Persist the low-frequency reset-credit response independently of quota usage."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from core.paths import atomic_write_json
from core.i18n import t


RESET_CREDITS_CACHE_FILE = "reset_credits.json"
RESET_CREDITS_CACHE_VERSION = 1
_CREDIT_FIELDS = ("status", "title", "granted_at", "expires_at")


def reset_credits_cache_path(auth_list_dir: Path) -> Path:
    return auth_list_dir.expanduser().parent / "settings" / RESET_CREDITS_CACHE_FILE


def empty_reset_credits_cache() -> dict[str, Any]:
    return {"version": RESET_CREDITS_CACHE_VERSION, "accounts": {}}


def read_reset_credits_cache(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return empty_reset_credits_cache()
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, dict):
        return empty_reset_credits_cache()
    return {"version": RESET_CREDITS_CACHE_VERSION, "accounts": accounts}


def write_reset_credits_cache(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(
        path.expanduser(),
        {
            "version": RESET_CREDITS_CACHE_VERSION,
            "accounts": payload.get("accounts", {}),
        },
    )


def compatible_reset_credits_entry(
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


def cached_reset_credits_payload(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    payload = entry.get("payload") if isinstance(entry, dict) else None
    if not isinstance(payload, dict):
        return None
    credits = payload.get("credits")
    if not isinstance(credits, list):
        return None
    return copy.deepcopy(payload)


def reset_credits_query_due(
    entry: dict[str, Any] | None,
    observed_epoch: int,
    interval_seconds: int,
) -> bool:
    attempted_at = entry.get("attempted_at_epoch") if isinstance(entry, dict) else None
    if not isinstance(attempted_at, (int, float)):
        return True
    return observed_epoch < attempted_at or observed_epoch - attempted_at >= max(1, interval_seconds)


def sanitize_reset_credits_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError(t("quota.error.reset_response_object"))
    credits = payload.get("credits", [])
    if not isinstance(credits, list):
        raise RuntimeError(t("quota.error.reset_response_credits"))
    sanitized_credits = [
        {
            key: credit.get(key)
            for key in _CREDIT_FIELDS
            if credit.get(key) is not None
        }
        for credit in credits
        if isinstance(credit, dict)
    ]
    return {
        "available_count": payload.get("available_count"),
        "total_earned_count": payload.get("total_earned_count"),
        "credits": sanitized_credits,
    }
