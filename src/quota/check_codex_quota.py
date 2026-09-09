#!/usr/bin/env python3
"""Print Codex 5h/7d usage windows and reset credits.

The script reads $CODEXTOP_CODEX_DIR/auth.json by default, uses access tokens
in memory, and never prints tokens, cookies, account IDs, or credit IDs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.paths import (
    auth_keyword_from_path,
    auth_sort_key,
    current_provider_file_for_auth_list,
    default_paths,
    ensure_runtime_layout,
    normalize_auth_keyword,
)
from core.constants import HTTP_401_CREDENTIAL_HINT, RESET_CREDITS_INTERVAL_SECONDS
from core.i18n import t
from quota.reset_credits_cache import (
    cached_reset_credits_payload,
    compatible_reset_credits_entry,
    read_reset_credits_cache,
    reset_credits_query_due,
    sanitize_reset_credits_payload,
    write_reset_credits_cache,
)
from quota.token_usage_cache import (
    TOKEN_USAGE_CACHE_TTL_SECONDS,
    cached_token_usage,
    compatible_cache_entry,
    read_token_usage_cache,
    token_usage_cache_path,
    token_usage_query_due,
    write_token_usage_cache,
)
from quota.windows import WINDOW_SECONDS, window_key_for_seconds
from ui import color_schemes


CHATGPT_BACKEND = "https://chatgpt.com/backend-api"
USAGE_URL = f"{CHATGPT_BACKEND}/wham/usage"
RESET_CREDITS_URL = f"{CHATGPT_BACKEND}/wham/rate-limit-reset-credits"
TOKEN_USAGE_URL = f"{CHATGPT_BACKEND}/wham/profiles/me"
ACCOUNT_INFO_URL = f"{CHATGPT_BACKEND}/accounts/check/v4-2023-04-27"
AUTH_FILE_RE = re.compile(r"auth-([A-Za-z0-9][A-Za-z0-9_.-]*)\.json$")
MAX_QUERY_WORKERS = 8
DEFAULT_PATHS = default_paths()
DEFAULT_AUTH_FILE = DEFAULT_PATHS.active_auth_file
DEFAULT_AUTH_LIST = DEFAULT_PATHS.auth_list_dir


def load_auth_credentials(auth_path: Path) -> tuple[str, str | None]:
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(t("quota.error.auth_file_not_found", path=auth_path)) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(t("quota.error.auth_json_invalid", path=auth_path)) from exc

    if isinstance(data, dict) and (data.get("auth_mode") == "apikey" or data.get("OPENAI_API_KEY")):
        raise RuntimeError(t("quota.error.api_not_supported"))
    tokens = data.get("tokens", {})
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError(t("quota.error.token_missing", path=auth_path))
    account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    return token, account_id if isinstance(account_id, str) and account_id else None


def load_access_token(auth_path: Path) -> str:
    return load_auth_credentials(auth_path)[0]


def get_json(
    url: str,
    access_token: str,
    timeout: int = 30,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "Codex local quota checker",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = Request(
        url,
        headers=headers,
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError(t("quota.error.request_401", hint=HTTP_401_CREDENTIAL_HINT)) from exc
        if exc.code == 403:
            raise RuntimeError(t("quota.error.request_denied", code=exc.code)) from exc
        raise RuntimeError(t("quota.error.request_failed", code=exc.code)) from exc
    except URLError as exc:
        raise RuntimeError(t("quota.error.network", error=exc.reason)) from exc
    except TimeoutError as exc:
        raise RuntimeError(t("quota.error.timeout")) from exc


def parse_time(value: Any, tz: ZoneInfo) -> str | None:
    if value is None:
        return None
    dt = parse_datetime_utc(value)
    if dt is None:
        return str(value)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def parse_datetime_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def seconds_until(value: Any) -> int | None:
    dt = parse_datetime_utc(value)
    if dt is None:
        return None
    return int((dt - datetime.now(timezone.utc)).total_seconds())


def compact_time(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%m-%d %H:%M")
        except ValueError:
            pass
    return value


def format_duration(seconds: Any) -> str | None:
    if not isinstance(seconds, (int, float)):
        return None
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def compact_duration(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    return value.replace(" ", "")


def countdown_text(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "-"
    total = int(seconds)
    if total <= 0:
        return "expired"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days:2d}d {hours:2d}h"
    return f"{hours:2d}h {minutes:2d}m"


def percent_left(used_percent: Any) -> int | float | None:
    if not isinstance(used_percent, (int, float)):
        return None
    remaining = max(0, 100 - used_percent)
    if isinstance(used_percent, int):
        return int(remaining)
    return round(remaining, 2)


def normalize_window(name: str, raw: Any, tz: ZoneInfo) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    used = raw.get("used_percent")
    return {
        "window": name,
        "used_percent": used,
        "remaining_percent": percent_left(used),
        "limit_window_seconds": raw.get("limit_window_seconds"),
        "reset_after_seconds": raw.get("reset_after_seconds"),
        "reset_after": format_duration(raw.get("reset_after_seconds")),
        "reset_at": parse_time(raw.get("reset_at"), tz),
    }


def normalize_rate_limit_windows(rate_limit: Any, tz: ZoneInfo) -> dict[str, dict[str, Any]]:
    """Normalize API slots by duration instead of assuming primary means 5h."""
    rate_limit = rate_limit if isinstance(rate_limit, dict) else {}
    normalized = {
        "5h": normalize_window("5h", None, tz),
        "7d": normalize_window("7d", None, tz),
    }
    assigned: set[str] = set()
    for api_key, fallback in (("primary_window", "5h"), ("secondary_window", "7d")):
        raw = rate_limit.get(api_key)
        if not isinstance(raw, dict) or not raw:
            continue
        key = window_key_for_seconds(raw.get("limit_window_seconds"), fallback)
        if key is None:
            continue
        if key in assigned:
            key = fallback if fallback not in assigned else None
        if key is None:
            continue
        normalized[key] = normalize_window(key, raw, tz)
        assigned.add(key)
    if "7d" in assigned and "5h" not in assigned:
        seconds = WINDOW_SECONDS["5h"]
        normalized["5h"] = normalize_window(
            "5h",
            {
                "used_percent": 0,
                "limit_window_seconds": seconds,
                "reset_after_seconds": seconds,
            },
            tz,
        )
    return normalized


def normalize_credit(raw: Any, tz: ZoneInfo) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    granted_dt = parse_datetime_utc(raw.get("granted_at"))
    expires_dt = parse_datetime_utc(raw.get("expires_at"))
    expires_after_seconds = seconds_until(raw.get("expires_at"))
    lifetime_seconds = None
    expires_remaining_percent = None
    if granted_dt is not None and expires_dt is not None:
        lifetime_seconds = max(0, int((expires_dt - granted_dt).total_seconds()))
    if (
        isinstance(expires_after_seconds, int)
        and isinstance(lifetime_seconds, int)
        and lifetime_seconds > 0
    ):
        expires_remaining_percent = max(
            0,
            min(100, round(expires_after_seconds / lifetime_seconds * 100)),
        )
    return {
        "status": raw.get("status"),
        "title": raw.get("title"),
        "granted_at": parse_time(raw.get("granted_at"), tz),
        "expires_at": parse_time(raw.get("expires_at"), tz),
        "expires_after_seconds": expires_after_seconds,
        "expires_after": countdown_text(expires_after_seconds),
        "lifetime_seconds": lifetime_seconds,
        "expires_remaining_percent": expires_remaining_percent,
    }


def normalize_token_usage(payload: Any) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    stats = payload.get("stats")
    metadata = payload.get("metadata")
    stats = stats if isinstance(stats, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    lifetime_tokens = stats.get("lifetime_tokens")
    if not isinstance(lifetime_tokens, (int, float)) or lifetime_tokens < 0:
        raise RuntimeError(t("quota.error.token_stats_missing"))
    generated_at = metadata.get("generated_at") or payload.get("stats_as_of")
    generated_dt = parse_datetime_utc(generated_at)
    return {
        "lifetime_tokens": int(lifetime_tokens),
        "generated_at": generated_at if isinstance(generated_at, str) else None,
        "generated_at_epoch": int(generated_dt.timestamp()) if generated_dt is not None else None,
    }


def account_subscription_plan(payload: Any, account_id: str | None) -> str | None:
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, dict) or not accounts:
        return None
    account = accounts.get(account_id) if account_id else None
    if not isinstance(account, dict) and len(accounts) == 1:
        account = next(iter(accounts.values()))
    entitlement = account.get("entitlement") if isinstance(account, dict) else None
    plan = entitlement.get("subscription_plan") if isinstance(entitlement, dict) else None
    return plan.strip().lower() if isinstance(plan, str) and plan.strip() else None


def normalize_plan_type(plan_type: Any, subscription_plan: Any = None) -> str | None:
    raw = plan_type.strip().lower() if isinstance(plan_type, str) and plan_type.strip() else None
    subscription = (
        subscription_plan.strip().lower()
        if isinstance(subscription_plan, str) and subscription_plan.strip()
        else None
    )
    if raw == "pro":
        if subscription == "chatgptprolite":
            return "pro-5x"
        if subscription == "chatgptpro":
            return "pro-20x"
    return raw


def account_plan_query_due(
    entry: dict[str, Any] | None,
    observed_epoch: int,
    ttl_seconds: int = TOKEN_USAGE_CACHE_TTL_SECONDS,
) -> bool:
    checked_at = entry.get("plan_checked_at_epoch") if isinstance(entry, dict) else None
    if not isinstance(checked_at, (int, float)):
        return True
    return observed_epoch < checked_at or observed_epoch - checked_at >= max(1, ttl_seconds)


def normalize_quota_report(
    usage: Any,
    credits_payload: Any,
    tz_name: str,
    subscription_plan: str | None = None,
) -> dict[str, Any]:
    tz = ZoneInfo(tz_name)
    rate_limit = usage.get("rate_limit", {}) if isinstance(usage, dict) else {}
    credits = (
        credits_payload.get("credits", [])
        if isinstance(credits_payload, dict)
        else []
    )
    if not isinstance(credits, list):
        credits = []

    return {
        "email": usage.get("email") if isinstance(usage, dict) else None,
        "plan_type": normalize_plan_type(
            usage.get("plan_type") if isinstance(usage, dict) else None,
            subscription_plan,
        ),
        "allowed": rate_limit.get("allowed") if isinstance(rate_limit, dict) else None,
        "limit_reached": rate_limit.get("limit_reached")
        if isinstance(rate_limit, dict)
        else None,
        "quota": normalize_rate_limit_windows(rate_limit, tz),
        "reset_credits": {
            "available_count": credits_payload.get("available_count")
            if isinstance(credits_payload, dict)
            else None,
            "total_earned_count": credits_payload.get("total_earned_count")
            if isinstance(credits_payload, dict)
            else None,
            "count_by_status": count_by_status(credits),
            "credits": [normalize_credit(item, tz) for item in credits],
        },
    }


def collect_quota(auth_path: Path, tz_name: str) -> dict[str, Any]:
    access_token, account_id = load_auth_credentials(auth_path)
    with ThreadPoolExecutor(max_workers=3) as executor:
        usage_future = executor.submit(get_json, USAGE_URL, access_token)
        credits_future = executor.submit(get_json, RESET_CREDITS_URL, access_token)
        account_future = executor.submit(get_json, ACCOUNT_INFO_URL, access_token)
        reset_credits_error = None
        try:
            credits_payload = sanitize_reset_credits_payload(credits_future.result())
        except Exception as exc:
            credits_payload = {}
            reset_credits_error = str(exc)
        plan_type_error = None
        try:
            subscription_plan = account_subscription_plan(account_future.result(), account_id)
        except Exception as exc:
            subscription_plan = None
            plan_type_error = str(exc)
        report = normalize_quota_report(
            usage_future.result(),
            credits_payload,
            tz_name,
            subscription_plan,
        )
        if reset_credits_error:
            report["reset_credits_error"] = reset_credits_error
        if plan_type_error:
            report["plan_type_error"] = plan_type_error
        return report


def count_by_status(credits: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in credits:
        status = item.get("status") if isinstance(item, dict) else None
        key = str(status or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def discover_auth_configs(auth_list_dir: Path) -> list[tuple[str, Path]]:
    configs: list[tuple[str, Path]] = []
    for path in auth_list_dir.glob("auth-*.json"):
        keyword = auth_keyword_from_path(path)
        if keyword is not None:
            configs.append((keyword, path))
    return sorted(configs, key=lambda item: auth_sort_key(item[0]))


def auth_list_has_configs(auth_list_dir: Path) -> bool:
    return bool(discover_auth_configs(auth_list_dir))


def read_current_index(auth_list_dir: Path) -> str | None:
    config_path = current_provider_file_for_auth_list(auth_list_dir)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        payload = None
    if isinstance(payload, dict):
        keyword = normalize_auth_keyword(payload.get("auth_keyword"))
        if keyword is not None:
            return keyword
        value = payload.get("openai_provider_number")
        if isinstance(value, int) and value > 0:
            return f"openai-{value}"

    current_path = auth_list_dir / "current_provider.txt"
    try:
        text = current_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return f"openai-{value}" if value > 0 else None


def resolve_numeric_keyword(index: int, by_keyword: dict[str, Path]) -> str | None:
    for keyword in (str(index), f"openai-{index}"):
        if keyword in by_keyword:
            return keyword
    return None


def select_auth_configs(
    auth_list_dir: Path,
    indices: list[int],
    current_only: bool,
) -> tuple[list[tuple[str, Path]], str | None]:
    current_index = read_current_index(auth_list_dir)
    configs = discover_auth_configs(auth_list_dir)
    by_keyword = {keyword: path for keyword, path in configs}

    if current_only:
        if current_index is None:
            raise RuntimeError(t("quota.error.current_provider_not_found", path=auth_list_dir))
        if current_index not in by_keyword:
            raise RuntimeError(t("quota.error.current_provider_auth_missing", provider=current_index))
        return [(current_index, by_keyword[current_index])], current_index

    if indices:
        selected: list[tuple[str, Path]] = []
        missing: list[int] = []
        for index in indices:
            keyword = resolve_numeric_keyword(index, by_keyword)
            if keyword is None:
                missing.append(index)
            else:
                selected.append((keyword, by_keyword[keyword]))
        if missing:
            joined = ", ".join(str(index) for index in missing)
            raise RuntimeError(t("quota.error.config_index_not_found", index=joined))
        return selected, current_index

    if not configs:
        raise RuntimeError(t("quota.error.no_auth_files", path=auth_list_dir))
    return configs, current_index


def select_requested_configs(
    auth_file: Path,
    auth_list_dir: Path,
    indices: list[int],
    current_only: bool,
    all_auth: bool,
    explicit_auth_list: bool,
) -> tuple[list[tuple[str, Path]], str | None]:
    if current_only or indices:
        return select_auth_configs(auth_list_dir, indices, current_only)

    if all_auth or explicit_auth_list:
        if auth_list_has_configs(auth_list_dir):
            return select_auth_configs(auth_list_dir, indices, current_only)
        return [(0, auth_file)], None

    return [(0, auth_file)], None


def collect_account(
    index: str,
    auth_path: Path,
    current_index: str | None,
    tz_name: str,
) -> dict[str, Any]:
    try:
        report = collect_quota(auth_path, tz_name)
    except Exception as exc:
        report = {"error": str(exc)}
    report["index"] = index
    report["label"] = index
    report["current"] = index == current_index
    return report


def collect_accounts(
    configs: list[tuple[str, Path]],
    current_index: str | None,
    tz_name: str,
    token_cache_path: Path | None = None,
    observed_epoch: int | None = None,
    token_cache_ttl_seconds: int = TOKEN_USAGE_CACHE_TTL_SECONDS,
    reset_credits_cache_path: Path | None = None,
    reset_credits_interval_seconds: int = RESET_CREDITS_INTERVAL_SECONDS,
    query_reset_credits: bool = True,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    observed_epoch = int(datetime.now(timezone.utc).timestamp()) if observed_epoch is None else int(observed_epoch)
    token_cache = read_token_usage_cache(token_cache_path) if token_cache_path is not None else None
    cache_accounts = token_cache.get("accounts") if isinstance(token_cache, dict) else None
    reset_cache = (
        read_reset_credits_cache(reset_credits_cache_path)
        if reset_credits_cache_path is not None
        else None
    )
    reset_cache_accounts = reset_cache.get("accounts") if isinstance(reset_cache, dict) else None
    request_count = max(1, len(configs) * 4)
    cache_changed = False
    reset_cache_changed = False
    with ThreadPoolExecutor(max_workers=min(MAX_QUERY_WORKERS, request_count)) as executor:
        for index, path in configs:
            report: dict[str, Any] = {
                "index": index,
                "label": index,
                "current": index == current_index,
            }
            raw_cache_entry = (
                cache_accounts.get(str(index))
                if isinstance(cache_accounts, dict)
                else None
            )
            try:
                access_token, account_id = load_auth_credentials(path)
            except Exception as exc:
                if isinstance(raw_cache_entry, dict):
                    for key in ("email", "plan_type", "subscription_plan"):
                        if raw_cache_entry.get(key):
                            report[key] = raw_cache_entry[key]
                report["error"] = str(exc)
                entries.append({"report": report})
                continue
            cache_entry = (
                compatible_cache_entry(token_cache, index, account_id)
                if token_cache is not None
                else None
            )
            if isinstance(cache_entry, dict):
                for key in ("email", "plan_type", "subscription_plan"):
                    if cache_entry.get(key):
                        report[key] = cache_entry[key]
            cached_usage = cached_token_usage(cache_entry)
            if cached_usage is not None:
                report["token_usage"] = cached_usage
            plan_future = None
            if token_cache is None or account_plan_query_due(
                cache_entry,
                observed_epoch,
                token_cache_ttl_seconds,
            ):
                plan_future = executor.submit(get_json, ACCOUNT_INFO_URL, access_token)
            token_future = None
            if token_cache is not None and token_usage_query_due(
                cache_entry,
                observed_epoch,
                token_cache_ttl_seconds,
            ):
                headers = {"ChatGPT-Account-Id": account_id} if account_id else None
                token_future = executor.submit(
                    get_json,
                    TOKEN_USAGE_URL,
                    access_token,
                    30,
                    headers,
                )
            reset_cache_entry = (
                compatible_reset_credits_entry(reset_cache, index, account_id)
                if reset_cache is not None
                else None
            )
            reset_payload = cached_reset_credits_payload(reset_cache_entry)
            credits_future = None
            if query_reset_credits and (
                reset_cache is None
                or reset_credits_query_due(
                    reset_cache_entry,
                    observed_epoch,
                    reset_credits_interval_seconds,
                )
            ):
                credits_future = executor.submit(get_json, RESET_CREDITS_URL, access_token)
            entries.append(
                {
                    "report": report,
                    "index": index,
                    "account_id": account_id,
                    "cache_entry": cache_entry,
                    "reset_cache_entry": reset_cache_entry,
                    "reset_payload": reset_payload,
                    "usage": executor.submit(get_json, USAGE_URL, access_token),
                    "credits": credits_future,
                    "plan": plan_future,
                    "tokens": token_future,
                }
            )

        for entry in entries:
            report = entry["report"]
            plan_future = entry.get("plan")
            if plan_future is not None:
                try:
                    subscription_plan = account_subscription_plan(
                        plan_future.result(),
                        entry.get("account_id"),
                    )
                    if subscription_plan:
                        report["subscription_plan"] = subscription_plan
                except Exception as exc:
                    report["plan_type_error"] = str(exc)
                if isinstance(cache_accounts, dict):
                    stored_entry = cache_accounts.get(str(entry["index"]))
                    if not isinstance(stored_entry, dict):
                        stored_entry = entry.get("cache_entry")
                    updated_entry = dict(stored_entry) if isinstance(stored_entry, dict) else {}
                    if entry.get("account_id"):
                        updated_entry["account_id"] = entry["account_id"]
                    updated_entry["plan_checked_at_epoch"] = observed_epoch
                    if report.get("subscription_plan"):
                        updated_entry["subscription_plan"] = report["subscription_plan"]
                    cache_accounts[str(entry["index"])] = updated_entry
                    cache_changed = True
            token_future = entry.get("tokens")
            if token_future is not None and isinstance(cache_accounts, dict):
                cache_changed = True
                cache_entry = cache_accounts.get(str(entry["index"]))
                if not isinstance(cache_entry, dict):
                    cache_entry = entry.get("cache_entry")
                updated_entry = dict(cache_entry) if isinstance(cache_entry, dict) else {}
                if entry.get("account_id"):
                    updated_entry["account_id"] = entry["account_id"]
                updated_entry["checked_at_epoch"] = observed_epoch
                existing_usage = cached_token_usage(cache_entry)
                if existing_usage is not None:
                    updated_entry["token_usage"] = existing_usage
                for key in ("email", "plan_type", "subscription_plan"):
                    if report.get(key):
                        updated_entry[key] = report[key]
                try:
                    fresh_usage = normalize_token_usage(token_future.result())
                    fresh_usage["checked_at_epoch"] = observed_epoch
                    report["token_usage"] = fresh_usage
                    updated_entry["token_usage"] = fresh_usage
                except Exception as exc:
                    message = str(exc)
                    report["token_usage_error"] = message
                    updated_entry["error"] = message
                cache_accounts[str(entry["index"])] = updated_entry
            usage_future = entry.get("usage")
            credits_future = entry.get("credits")
            if usage_future is None:
                continue
            reset_payload = entry.get("reset_payload")
            if credits_future is not None:
                stored_reset = entry.get("reset_cache_entry")
                updated_reset = dict(stored_reset) if isinstance(stored_reset, dict) else {}
                if entry.get("account_id"):
                    updated_reset["account_id"] = entry["account_id"]
                updated_reset["attempted_at_epoch"] = observed_epoch
                try:
                    reset_payload = sanitize_reset_credits_payload(credits_future.result())
                    updated_reset["checked_at_epoch"] = observed_epoch
                    updated_reset["payload"] = reset_payload
                    updated_reset.pop("error", None)
                except Exception as exc:
                    message = str(exc)
                    report["reset_credits_error"] = message
                    updated_reset["error"] = message
                if isinstance(reset_cache_accounts, dict):
                    reset_cache_accounts[str(entry["index"])] = updated_reset
                    reset_cache_changed = True
            try:
                normalized = normalize_quota_report(
                    usage_future.result(),
                    reset_payload if isinstance(reset_payload, dict) else {},
                    tz_name,
                    report.get("subscription_plan"),
                )
                for key in ("email", "plan_type", "subscription_plan"):
                    if not normalized.get(key) and report.get(key):
                        normalized[key] = report[key]
                report.update(normalized)
            except Exception as exc:
                report["error"] = str(exc)

            if isinstance(cache_accounts, dict):
                stored_entry = cache_accounts.get(str(entry["index"]))
                if not isinstance(stored_entry, dict):
                    stored_entry = entry.get("cache_entry")
                updated_entry = dict(stored_entry) if isinstance(stored_entry, dict) else {}
                if entry.get("account_id"):
                    updated_entry["account_id"] = entry["account_id"]
                for key in ("email", "plan_type", "subscription_plan"):
                    if report.get(key):
                        updated_entry[key] = report[key]
                if updated_entry != stored_entry:
                    cache_accounts[str(entry["index"])] = updated_entry
                    cache_changed = True

    if cache_changed and token_cache_path is not None and token_cache is not None:
        try:
            write_token_usage_cache(token_cache_path, token_cache)
        except Exception as exc:
            for entry in entries:
                report = entry["report"]
                report.setdefault("token_usage_error", t("quota.error.token_cache_write", error=exc))

    if (
        reset_cache_changed
        and reset_credits_cache_path is not None
        and reset_cache is not None
    ):
        try:
            write_reset_credits_cache(reset_credits_cache_path, reset_cache)
        except Exception as exc:
            for entry in entries:
                report = entry["report"]
                report.setdefault("reset_credits_error", t("quota.error.reset_cache_write", error=exc))

    accounts = [entry["report"] for entry in entries]
    return {
        "current_index": current_index,
        "accounts": accounts,
    }


def refresh_reset_credits_cache(
    configs: list[tuple[str, Path]],
    cache_path: Path,
    observed_epoch: int | None = None,
) -> dict[str, str]:
    """Refresh reset credits without blocking or invalidating quota usage samples."""
    observed_epoch = int(datetime.now(timezone.utc).timestamp()) if observed_epoch is None else int(observed_epoch)
    cache = read_reset_credits_cache(cache_path)
    accounts = cache.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
        cache["accounts"] = accounts
    errors: dict[str, str] = {}
    pending: list[tuple[str, str | None, dict[str, Any] | None, Any]] = []
    with ThreadPoolExecutor(max_workers=min(MAX_QUERY_WORKERS, max(1, len(configs)))) as executor:
        for index, path in configs:
            raw_entry = accounts.get(str(index))
            try:
                access_token, account_id = load_auth_credentials(path)
            except Exception as exc:
                message = str(exc)
                updated = dict(raw_entry) if isinstance(raw_entry, dict) else {}
                updated["attempted_at_epoch"] = observed_epoch
                updated["error"] = message
                accounts[str(index)] = updated
                errors[str(index)] = message
                continue
            cache_entry = compatible_reset_credits_entry(cache, index, account_id)
            future = executor.submit(get_json, RESET_CREDITS_URL, access_token)
            pending.append((index, account_id, cache_entry, future))

        for index, account_id, cache_entry, future in pending:
            updated = dict(cache_entry) if isinstance(cache_entry, dict) else {}
            if account_id:
                updated["account_id"] = account_id
            updated["attempted_at_epoch"] = observed_epoch
            try:
                updated["payload"] = sanitize_reset_credits_payload(future.result())
                updated["checked_at_epoch"] = observed_epoch
                updated.pop("error", None)
            except Exception as exc:
                message = str(exc)
                updated["error"] = message
                errors[str(index)] = message
            accounts[str(index)] = updated

    write_reset_credits_cache(cache_path, cache)
    return errors


def print_text(report: dict[str, Any]) -> None:
    print(t("quota.plain.title"))
    print(f"{t('quota.plain.email')}: {report.get('email')}")
    print(f"{t('quota.plain.plan_type')}: {report.get('plan_type')}")
    print(f"{t('quota.plain.total_usage')}: {lifetime_token_text(report)}")
    print(f"{t('quota.plain.allowed')}: {report.get('allowed')}")
    print(f"{t('quota.plain.limit_reached')}: {report.get('limit_reached')}")
    print()

    quota = report.get("quota", {})
    for key in ("5h", "7d"):
        window = quota.get(key, {})
        print(t("quota.window_heading", window=key))
        print(f"  {t('quota.plain.used_percent')}: {window.get('used_percent')}")
        print(f"  {t('quota.plain.remaining_percent')}: {window.get('remaining_percent')}")
        print(f"  {t('quota.plain.reset_after')}: {window.get('reset_after')}")
        print(f"  {t('quota.plain.reset_at')}: {window.get('reset_at')}")
        print()

    reset = report.get("reset_credits", {})
    print(t("quota.reset_credits_heading"))
    print(f"  {t('quota.plain.available_count')}: {reset.get('available_count')}")
    print(f"  {t('quota.plain.total_earned_count')}: {reset.get('total_earned_count')}")
    print(f"  {t('quota.plain.count_by_status')}: {json.dumps(reset.get('count_by_status', {}), ensure_ascii=False)}")
    print()
    print(t("quota.reset_expiration_heading"))
    for index, credit in enumerate(reset.get("credits", []), start=1):
        print(f"  {index}.")
        print(f"     {t('quota.plain.status')}: {credit.get('status')}")
        print(f"     {t('quota.plain.credit_title')}: {credit.get('title')}")
        print(f"     {t('quota.plain.granted_at')}: {credit.get('granted_at')}")
        print(f"     {t('quota.plain.expires_at')}: {credit.get('expires_at')}")


def percent_text(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:g}%"
    return "-"


def used_style(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "dim"
    if value < 50:
        return "green"
    if value < 80:
        return "yellow"
    return "red bold"


def left_style(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "dim"
    if value >= 50:
        return "green"
    if value >= 20:
        return "yellow"
    return "red bold"


def percent_gradient_style(value: Any) -> str:
    return color_schemes.percent_gradient_style(value)


def reset_after_style(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "dim"
    if seconds <= 3600:
        return "magenta bold"
    if seconds <= 6 * 3600:
        return "cyan"
    if seconds <= 24 * 3600:
        return "yellow"
    return "blue"


def count_style(value: Any) -> str:
    if not isinstance(value, int):
        return "dim"
    if value <= 0:
        return "dim"
    if value <= 1:
        return "yellow"
    return "green"


def progress_bar(value: Any, width: int) -> "Text":
    from rich.text import Text

    width = max(4, width)
    if not isinstance(value, (int, float)):
        return Text("░" * width, style="dim")
    filled = max(0, min(width, round(width * value / 100)))
    if value <= 0:
        filled = 1
    color = percent_gradient_style(value)
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="dim")
    return bar


def status_style(status: Any) -> str:
    if status == "available":
        return "green"
    if status in {"used", "redeemed"}:
        return "dim"
    if status in {"expired", "canceled"}:
        return "red"
    return "yellow"


def quota_window_is_full(window: Any) -> bool:
    if not isinstance(window, dict):
        return False
    remaining = window.get("remaining_percent")
    percent_full = isinstance(remaining, (int, float)) and remaining >= 99
    reset_after = window.get("reset_after_seconds")
    limit_window = window.get("limit_window_seconds")
    if not isinstance(reset_after, (int, float)):
        return False
    if not isinstance(limit_window, (int, float)) or limit_window <= 0:
        return False
    reset_cycle_full = abs(limit_window - reset_after) < 5 * 60
    return percent_full and reset_cycle_full


def border_style_for_account(account: dict[str, Any]) -> str:
    if account.get("error"):
        return "dim"
    quota = account.get("quota", {})
    five_hour = quota.get("5h", {})
    seven_day = quota.get("7d", {})
    if quota_window_is_full(seven_day):
        return "blue"
    if quota_window_is_full(five_hour):
        return "bright_cyan"
    for window in (five_hour, seven_day):
        left = window.get("remaining_percent") if isinstance(window, dict) else None
        if isinstance(left, (int, float)):
            return percent_gradient_style(left)
    return "dim"


def compact_reset_title(title: Any) -> str:
    text = str(title or "reset")
    if "Weekly + 5 hr" in text or "7d+5h" in text:
        return "7d+5h"
    return text


def truncate_text(value: Any, max_width: int) -> str:
    text = str(value or "-")
    if len(text) <= max_width:
        return text
    if max_width <= 3:
        return text[:max_width]
    keep = max_width - 3
    head = max(1, keep // 2)
    tail = max(1, keep - head)
    return f"{text[:head]}...{text[-tail:]}"


def lifetime_token_text(account: dict[str, Any]) -> str:
    token_usage = account.get("token_usage")
    value = token_usage.get("lifetime_tokens") if isinstance(token_usage, dict) else None
    return f"{int(value):,} Tokens" if isinstance(value, (int, float)) and value >= 0 else "-"


def make_identity_section(account: dict[str, Any], panel_width: int) -> "Table":
    from rich.cells import cell_len
    from rich.table import Table

    table = Table.grid(expand=True)
    labels = (t("account.email"), t("account.type"), t("quota.total_usage"))
    label_width = max(map(cell_len, labels))
    table.add_column(justify="left", style="dim", no_wrap=True, width=label_width)
    table.add_column(justify="left")
    email_width = max(8, panel_width - label_width - 6)
    table.add_row(labels[0], truncate_text(account.get("email"), email_width))
    table.add_row(labels[1], str(account.get("plan_type") or "-"))
    table.add_row(labels[2], lifetime_token_text(account))
    return table


def make_quota_bar_row(label: str, percent: Any, panel_width: int) -> "Table":
    from rich.table import Table

    table = Table.grid(expand=True)
    table.add_column(justify="left", no_wrap=True, width=3)
    table.add_column(justify="left")
    table.add_column(justify="right", no_wrap=True, width=5)
    bar_width = max(8, panel_width - 12)
    table.add_row(
        f"[bold]{label}[/bold]",
        progress_bar(percent, bar_width),
        f"[{percent_gradient_style(percent)}]{percent_text(percent)}[/{percent_gradient_style(percent)}]",
    )
    return table


def make_right_line(text: str, style: str = "dim") -> "Table":
    from rich.table import Table

    table = Table.grid(expand=True)
    table.add_column(justify="right", no_wrap=True)
    table.add_row(f"[{style}]{text}[/{style}]")
    return table


def make_quota_section(account: dict[str, Any], panel_width: int) -> "Group":
    from rich.console import Group

    rows = []
    quota = account.get("quota", {})
    for key in ("5h", "7d"):
        window = quota.get(key, {})
        left = window.get("remaining_percent")
        reset_seconds = window.get("reset_after_seconds")
        rows.append(make_quota_bar_row(key, left, panel_width))
        reset_text = t(
            "quota.resets_after_at",
            countdown=countdown_text(reset_seconds),
            time=compact_time(window.get("reset_at")),
        )
        rows.append(make_right_line(reset_text, reset_after_style(reset_seconds)))
    return Group(*rows)


def make_reset_row(credit: dict[str, Any], panel_width: int) -> "Table":
    from rich.table import Table

    table = Table.grid(expand=True)
    table.add_column(justify="left", no_wrap=True, width=5)
    table.add_column(justify="left")
    table.add_column(justify="right", no_wrap=True, width=17)
    seconds = credit.get("expires_after_seconds")
    style = reset_after_style(seconds)
    remaining = credit.get("expires_remaining_percent")
    bar_width = max(6, panel_width - 28)
    table.add_row(
        f"[{status_style(credit.get('status'))}]{compact_reset_title(credit.get('title'))}[/{status_style(credit.get('status'))}]",
        progress_bar(remaining, bar_width),
        f"[{style}]{t('quota.expires_after', countdown=countdown_text(seconds))}[/{style}]",
    )
    return table


def make_resets_section(account: dict[str, Any], panel_width: int) -> "Group":
    from rich.console import Group

    reset = account.get("reset_credits", {})
    available_count = reset.get("available_count")
    credits = [
        credit for credit in reset.get("credits", [])
        if credit.get("status") == "available"
    ]

    if not credits:
        text = t("quota.no_reset_credits")
        if isinstance(available_count, int):
            text = f"{text} ({available_count})"
        return Group(make_right_line(text, "dim"))

    return Group(*[make_reset_row(credit, panel_width) for credit in credits])


def make_account_panel(account: dict[str, Any], width: int) -> "Panel":
    from rich import box
    from rich.console import Group
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.text import Text

    index = account.get("index")
    title = str(account.get("label") or index or "-")
    if account.get("current"):
        title = f"【{title}】"

    sections: list[Any] = [make_identity_section(account, width)]
    if account.get("error"):
        sections.extend(
            [
                Rule(t("common.error"), style="red"),
                Text(str(account["error"]), style="red", overflow="fold", no_wrap=False),
            ]
        )
    else:
        sections.extend(
            [
                Rule(style="dim"),
                make_quota_section(account, width),
                Rule(style="dim"),
                make_resets_section(account, width),
            ]
        )
    body = Group(*sections)
    return Panel(
        body,
        title=title,
        title_align="center",
        border_style=border_style_for_account(account),
        box=box.ROUNDED,
        width=width,
        padding=(0, 1),
    )


def account_panel_width(requested_width: int | None) -> int:
    if requested_width:
        return max(30, requested_width // 3)
    return 50


def print_rich(bundle: dict[str, Any], requested_width: int | None = None) -> None:
    from rich import box
    from rich.console import Console
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table

    accounts = bundle.get("accounts", [])
    if not accounts:
        return
    panel_width = account_panel_width(requested_width)
    columns = min(3, len(accounts))
    outer_width = panel_width * columns + 4
    console = Console(width=outer_width)
    rows = []
    for offset in range(0, len(accounts), columns):
        chunk = accounts[offset:offset + columns]
        row = Table.grid(padding=(0, 0))
        for _ in range(columns):
            row.add_column(width=panel_width)
        cells = [make_account_panel(account, panel_width) for account in chunk]
        cells.extend([""] * (columns - len(cells)))
        row.add_row(*cells)
        rows.append(row)
    current = bundle.get("current_index")
    title = t("quota.dashboard_title")
    if current is not None:
        title = t("quota.current_service_title", title=title, service=current)
    print()
    console.print(
        Panel(
            Group(*rows),
            title=title,
            border_style="white",
            box=box.ROUNDED,
            width=outer_width,
            padding=(0, 1),
        )
    )
    print()


def print_plain_bundle(bundle: dict[str, Any]) -> None:
    for account in bundle.get("accounts", []):
        print(str(account.get("label") or account.get("index") or "-"))
        if account.get("current"):
            print(f"{t('quota.plain.current')}: {t('common.yes')}")
        if account.get("error"):
            print(f"{t('quota.plain.email')}: {account.get('email')}")
            print(f"{t('quota.plain.plan_type')}: {account.get('plan_type')}")
            print(f"{t('quota.plain.total_usage')}: {lifetime_token_text(account)}")
            print(f"{t('common.error')}: {account.get('error')}")
        else:
            print_text(account)
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=t("quota.arg.description"))
    parser.add_argument(
        "indices",
        nargs="*",
        type=int,
        help=t("quota.arg.indices"),
    )
    parser.add_argument(
        "--current",
        action="store_true",
        help=t("quota.arg.current"),
    )
    parser.add_argument(
        "--auth",
        type=Path,
        default=DEFAULT_AUTH_FILE,
        help=t("quota.arg.auth"),
    )
    parser.add_argument(
        "--all-auth",
        action="store_true",
        help=t("quota.arg.all_auth"),
    )
    parser.add_argument(
        "--auth-list",
        type=Path,
        default=None,
        help=t("quota.arg.auth_list"),
    )
    parser.add_argument(
        "--tz",
        default="Asia/Shanghai",
        help=t("quota.arg.timezone"),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=t("quota.arg.json"),
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help=t("quota.arg.plain"),
    )
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help=t("quota.arg.width"),
    )
    parser.add_argument(
        "--color-scheme",
        choices=[value for _label, value in color_schemes.color_scheme_choices()],
        default=None,
        help=t("quota.arg.color_scheme"),
    )
    args = parser.parse_args()

    try:
        color_schemes.set_active_color_scheme(args.color_scheme)
        ensure_runtime_layout()
        auth_list = (args.auth_list or DEFAULT_AUTH_LIST).expanduser()
        configs, current_index = select_requested_configs(
            args.auth.expanduser(),
            auth_list,
            args.indices,
            args.current,
            args.all_auth,
            args.auth_list is not None,
        )
        bundle = collect_accounts(
            configs,
            current_index,
            args.tz,
            token_usage_cache_path(auth_list),
        )
    except Exception as exc:
        print(t("common.error_message", error=exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(bundle, ensure_ascii=False, indent=2))
    elif args.plain:
        print_plain_bundle(bundle)
    else:
        try:
            print_rich(bundle, args.width)
        except ImportError:
            print_plain_bundle(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
