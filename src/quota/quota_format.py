"""Format account quota and reset-credit data for terminal panels."""

from __future__ import annotations

import time
from typing import Any

from quota import check_codex_quota as quota
from quota.plans import account_plan_key, account_quota_multiplier
from core.constants import *
from ui.terminal_text import *

def account_index(account: dict[str, Any]) -> int | str | None:
    index = account.get("index", account.get("i"))
    return index if isinstance(index, (int, str)) else None


def is_current(account: dict[str, Any]) -> bool:
    return bool(account.get("current", account.get("cur")))


def account_error(account: dict[str, Any]) -> str | None:
    error = account.get("error", account.get("err"))
    if not error:
        return None
    rendered = str(error)
    if "HTTP 401" in rendered and HTTP_401_CREDENTIAL_HINT not in rendered:
        rendered = f"{rendered}\n{HTTP_401_CREDENTIAL_HINT}"
    return rendered


def account_email(account: dict[str, Any]) -> str:
    return str(account.get("email") or "-")


def account_plan(account: dict[str, Any]) -> str:
    plan = account_plan_key(account)
    names = {
        "plus": "Plus",
        "chatgptplus": "Plus",
        "pro": "Pro(20x)",
        "pro-5x": "Pro(5x)",
        "pro_5x": "Pro(5x)",
        "pro5x": "Pro(5x)",
        "chatgptprolite": "Pro(5x)",
        "pro-20x": "Pro(20x)",
        "pro_20x": "Pro(20x)",
        "pro20x": "Pro(20x)",
        "chatgptpro": "Pro(20x)",
    }
    return names.get(plan, str(account.get("plan_type") or account.get("plan") or "-"))


def account_lifetime_tokens(account: dict[str, Any]) -> int | None:
    raw = account.get("u")
    value = raw[0] if isinstance(raw, (list, tuple)) and raw else None
    if value is None:
        token_usage = account.get("token_usage")
        value = token_usage.get("lifetime_tokens") if isinstance(token_usage, dict) else None
    return int(value) if isinstance(value, (int, float)) and value >= 0 else None


def merged_lifetime_tokens(accounts: list[dict[str, Any]]) -> int | None:
    totals = [
        total
        for account in accounts
        if isinstance(account, dict)
        for total in [account_lifetime_tokens(account)]
        if total is not None
    ]
    return sum(totals) if totals else None


def format_token_count(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"{int(value):,} Tokens"


def compact_token_count(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    number = max(0.0, float(value))
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if number >= divisor:
            scaled = number / divisor
            precision = 0 if scaled >= 100 else 1
            return f"{scaled:.{precision}f}{suffix}"
    return str(int(number))


def token_usage_rows(total: Any, _width: int) -> list[str]:
    return [f"{paint('使用总量', 'dim')} {format_token_count(total)}"]


def window_info(account: dict[str, Any], key: str) -> dict[str, Any]:
    if "quota" in account:
        window = account.get("quota", {}).get(key, {})
        reset_after = window.get("reset_after_seconds")
        reset_epoch = int(time.time() + reset_after) if isinstance(reset_after, (int, float)) else None
        return {
            "left": window.get("remaining_percent"),
            "reset_after": reset_after,
            "reset_at": compact_reset_at(window.get("reset_at")),
            "reset_epoch": reset_epoch,
        }
    raw = account.get("q", {}).get(key)
    if isinstance(raw, list) and len(raw) >= 4:
        left, reset_epoch, reset_after, _limit = raw[:4]
        return {
            "left": left,
            "reset_after": reset_after,
            "reset_at": compact_reset_at(None, reset_epoch if isinstance(reset_epoch, int) else None),
            "reset_epoch": reset_epoch if isinstance(reset_epoch, int) else None,
        }
    return {"left": None, "reset_after": None, "reset_at": "-", "reset_epoch": None}


def reset_rows(account: dict[str, Any], width: int) -> list[str]:
    rows: list[str] = []
    if "reset_credits" in account:
        reset = account.get("reset_credits", {})
        available = reset.get("available_count")
        credits = [
            credit for credit in reset.get("credits", [])
            if credit.get("status") == "available"
        ]
        rows.append(center_ansi(f"剩余 {available if isinstance(available, int) else '-'} 次可用重置次数", width))
        if not credits:
            detail = "无可用重置次数" if available == 0 else "额度详情暂无新数据"
            rows.append(center_ansi(paint(detail, "dim"), width))
            return rows
        reset_items: list[tuple[str, Any, Any]] = []
        for credit in credits[:4]:
            title = quota.compact_reset_title(credit.get("title"))
            seconds = credit.get("expires_after_seconds")
            remaining = credit.get("expires_remaining_percent")
            reset_items.append((title, remaining, seconds))
        day_width = reset_credit_day_width([item[2] for item in reset_items])
        for title, remaining, seconds in reset_items:
            rows.append(reset_credit_row(title, remaining, seconds, width, day_width))
        return rows

    available = account.get("rc")
    credits = account.get("r", [])
    rows.append(center_ansi(f"剩余 {available if isinstance(available, int) else '-'} 次可用重置次数", width))
    if not credits:
        detail = "无可用重置次数" if available == 0 else "额度详情暂无新数据"
        rows.append(center_ansi(paint(detail, "dim"), width))
        return rows
    now = int(time.time())
    reset_items: list[tuple[str, Any, Any]] = []
    for credit in credits[:4]:
        if not isinstance(credit, list) or len(credit) < 2:
            continue
        title, expires_epoch = credit[:2]
        remaining_percent = credit[2] if len(credit) > 2 else None
        remaining = expires_epoch - now if isinstance(expires_epoch, int) else None
        reset_items.append((str(title), remaining_percent, remaining))
    day_width = reset_credit_day_width([item[2] for item in reset_items])
    for title, remaining_percent, remaining in reset_items:
        rows.append(reset_credit_row(title, remaining_percent, remaining, width, day_width))
    return rows


def reset_credit_row(title: str, remaining_percent: Any, seconds: Any, width: int, day_width: int = 1) -> str:
    left_width = RESET_CREDIT_TITLE_WIDTH
    right_text = f"于 {reset_credit_countdown(seconds, day_width)} 后过期"
    right_width = min(
        reset_credit_right_width(day_width),
        max(0, width - left_width - RESET_CREDIT_MIN_BAR_WIDTH - 2),
    )
    bar_width = max(RESET_CREDIT_MIN_BAR_WIDTH, width - left_width - right_width - 2)
    style = quota.reset_after_style(seconds)
    left = f"{plain_fit(title, left_width):<{left_width}} {progress_bar(remaining_percent, bar_width)}"
    right = fit_ansi(paint_style(right_text, style), right_width) if right_width else ""
    row = f"{left} {right}" if right else left
    return fit_ansi(row, width)


def quota_rows(
    account: dict[str, Any],
    width: int,
    *,
    compact: bool = False,
    curve_mode: str = DEFAULT_CURVE_MODE,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
) -> list[str]:
    rows: list[str] = []
    keys = window_keys(window_scope)
    infos = {key: window_info(account, key) for key in keys}
    primary_color = len(keys) == 1 or sum(
        isinstance(info.get("left"), (int, float)) for info in infos.values()
    ) == 1
    for key in keys:
        info = infos[key]
        left = info["left"]
        label_text = window_marker_label(key, curve_mode, primary=primary_color)
        label = paint(label_text, bold=True)
        pct = paint(percent_text(left).rjust(4), percent_color(left))
        bar_width = max(8, width - visible_width(label_text) - 1 - 1 - 4)
        rows.append(f"{label} {progress_bar(left, bar_width)} {pct}")
        reset_line = f"于 {countdown(info['reset_after'])} 后在 {info['reset_at']} 重置"
        rows.append(right_ansi(paint_style(reset_line, quota.reset_after_style(info["reset_after"])), width))
        if key != keys[-1] and not compact:
            rows.append("")
    return rows


def percent_sum_text(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "--%"
    if float(value).is_integer():
        return f"{int(value)}%"
    return f"{value:g}%"


def quota_multiple_text(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "--x"
    if float(value).is_integer():
        return f"{int(value)}x"
    return f"{value:g}x"


def merged_ratio_percent(value: Any, max_value: Any) -> float | None:
    if not isinstance(value, (int, float)) or not isinstance(max_value, (int, float)) or max_value <= 0:
        return None
    return max(0.0, min(100.0, float(value) / float(max_value) * 100))


def valid_quota_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        account for account in accounts
        if isinstance(account, dict) and not account_error(account)
    ]


def merged_plan_text(accounts: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for account in accounts:
        if not isinstance(account, dict):
            continue
        plan = account_plan(account)
        if not plan or plan == "-":
            continue
        counts[plan] = counts.get(plan, 0) + 1
    if not counts:
        return "-"
    order = {"Plus": 0, "Pro(5x)": 1, "Pro(20x)": 2}
    plans = sorted(counts.items(), key=lambda item: (order.get(item[0], 3), item[0]))
    return " / ".join(f"{plan} x{count}" for plan, count in plans)


def merged_available_resets(accounts: list[dict[str, Any]]) -> int | None:
    total = 0
    found = False
    for account in valid_quota_accounts(accounts):
        if "reset_credits" in account:
            available = account.get("reset_credits", {}).get("available_count")
        else:
            available = account.get("rc")
        if isinstance(available, int):
            total += available
            found = True
    return total if found else None


def current_account_id(accounts: list[dict[str, Any]], current: int | str | None) -> str:
    if current is not None:
        return str(current)
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if is_current(account):
            index = account_index(account)
            return str(index) if index is not None else "-"
    return "-"


def current_account_obj(accounts: list[dict[str, Any]], current: int | str | None) -> dict[str, Any] | None:
    if current is not None:
        for account in accounts:
            if not isinstance(account, dict):
                continue
            if account_index(account) == current:
                return account
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if is_current(account):
            return account
    return None


def current_account_quota_summary(
    accounts: list[dict[str, Any]],
    current: int | str | None,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
) -> str:
    account = current_account_obj(accounts, current)
    account_id = current_account_id(accounts, current)
    if not account or account_error(account):
        return account_id
    parts = []
    for key in window_keys(window_scope):
        left = window_info(account, key).get("left")
        parts.append(f"{key} {paint(percent_text(left), percent_color(left))}")
    return f"{account_id} ({' '.join(parts)})"


def credit_expire_epoch(credit: Any, observed_ts: int) -> int | None:
    if isinstance(credit, list) and len(credit) >= 2 and isinstance(credit[1], int):
        return credit[1]
    if not isinstance(credit, dict):
        return None
    seconds = credit.get("expires_after_seconds")
    if isinstance(seconds, (int, float)):
        return observed_ts + int(seconds)
    expires_dt = quota.parse_datetime_utc(credit.get("expires_at"))
    return int(expires_dt.timestamp()) if expires_dt is not None else None


def merged_reset_expiration_rows(accounts: list[dict[str, Any]], limit: int = 3) -> list[tuple[int, str, str]]:
    now = int(time.time())
    rows: list[tuple[int, str, str]] = []
    for account in valid_quota_accounts(accounts):
        index = account_index(account)
        account_id = str(index) if index is not None else "-"
        if "reset_credits" in account:
            credits = [
                credit for credit in account.get("reset_credits", {}).get("credits", [])
                if isinstance(credit, dict) and credit.get("status") == "available"
            ]
        else:
            credits = account.get("r", [])
        if not isinstance(credits, list):
            continue
        for credit in credits:
            expire_epoch = credit_expire_epoch(credit, now)
            if expire_epoch is None:
                continue
            title = (
                quota.compact_reset_title(credit.get("title"))
                if isinstance(credit, dict)
                else str(credit[0] if credit else "-")
            )
            rows.append((expire_epoch, account_id, title))
    return sorted(rows, key=lambda item: item[0])[:limit]


def merged_window_info(
    accounts: list[dict[str, Any]],
    key: str,
    merged_quota_mode: str = DEFAULT_MERGED_QUOTA_MODE,
) -> dict[str, Any]:
    total = 0.0
    max_left = 0.0
    contributors = 0
    reset_candidates: list[int] = []
    for account in valid_quota_accounts(accounts):
        info = window_info(account, key)
        left = info.get("left")
        if not isinstance(left, (int, float)):
            continue
        if merged_quota_mode == "actual":
            multiplier = account_quota_multiplier(account)
            total += float(left) / 100.0 * multiplier
            max_left += multiplier
        else:
            total += float(left)
            max_left += 100.0
        contributors += 1
        reset_epoch = info.get("reset_epoch")
        if isinstance(reset_epoch, int):
            reset_candidates.append(reset_epoch)

    if contributors == 0:
        return {
            "left": None,
            "max_left": None,
            "contributors": 0,
            "reset_after": None,
            "reset_at": "-",
            "reset_epoch": None,
        }

    nearest_reset = min(reset_candidates) if reset_candidates else None
    reset_after = max(0, nearest_reset - int(time.time())) if nearest_reset is not None else None
    left: int | float = int(total) if total.is_integer() else round(total, 2)
    return {
        "left": left,
        "max_left": int(max_left) if max_left.is_integer() else round(max_left, 2),
        "contributors": contributors,
        "reset_after": reset_after,
        "reset_at": compact_reset_at(None, nearest_reset),
        "reset_epoch": nearest_reset,
    }


def account_quota_detail_line(
    accounts: list[dict[str, Any]],
    current: int | str | None,
    key: str,
    width: int,
    merged_quota_mode: str = DEFAULT_MERGED_QUOTA_MODE,
) -> str:
    sortable: list[tuple[float, str]] = []
    for account in valid_quota_accounts(accounts):
        index = account_index(account)
        account_id = str(index) if index is not None else "-"
        left = window_info(account, key).get("left")
        if not isinstance(left, (int, float)):
            continue
        value = float(left)
        text = percent_text(left)
        if merged_quota_mode == "actual":
            value = value / 100.0 * account_quota_multiplier(account)
            text = quota_multiple_text(value)
        sortable.append((value, paint(f"{account_id}({text})", percent_color(left))))
    entries = [text for _left, text in sorted(sortable, key=lambda item: item[0], reverse=True)]
    return ansi_ellipsis(", ".join(entries), width)


def merged_quota_detail_reset_line(
    accounts: list[dict[str, Any]],
    current: int | str | None,
    key: str,
    info: dict[str, Any],
    width: int,
    merged_quota_mode: str = DEFAULT_MERGED_QUOTA_MODE,
) -> str:
    reset_text = f"于 {countdown(info['reset_after'])} 后在 {info['reset_at']} 重置"
    reset = paint_style(reset_text, quota.reset_after_style(info["reset_after"]))
    reset_width = min(visible_width(reset), width)
    detail_width = max(0, width - reset_width - 1)
    if detail_width <= 0:
        return right_ansi(reset, width)
    detail = account_quota_detail_line(
        accounts,
        current,
        key,
        detail_width,
        merged_quota_mode,
    )
    return fit_ansi(detail, detail_width) + " " + right_ansi(reset, reset_width)


def merged_quota_rows(
    accounts: list[dict[str, Any]],
    current: int | str | None,
    width: int,
    *,
    compact: bool = False,
    curve_mode: str = DEFAULT_CURVE_MODE,
    window_scope: str = DEFAULT_WINDOW_SCOPE,
    merged_quota_mode: str = DEFAULT_MERGED_QUOTA_MODE,
) -> list[str]:
    rows: list[str] = []
    keys = window_keys(window_scope)
    infos = {
        key: merged_window_info(accounts, key, merged_quota_mode)
        for key in keys
    }
    primary_color = len(keys) == 1 or sum(
        isinstance(info.get("left"), (int, float)) for info in infos.values()
    ) == 1
    for key in keys:
        info = infos[key]
        left = info["left"]
        max_left = info["max_left"] if isinstance(info["max_left"], (int, float)) else 100
        label_text = window_marker_label(key, curve_mode, primary=primary_color)
        label = paint(label_text, bold=True)
        pct_raw = (
            quota_multiple_text(left)
            if merged_quota_mode == "actual"
            else percent_sum_text(left)
        )
        pct_width = max(4, visible_width(pct_raw))
        ratio = merged_ratio_percent(left, max_left)
        pct = paint(pct_raw.rjust(pct_width), percent_color(ratio))
        bar_width = max(8, width - visible_width(label_text) - 1 - 1 - pct_width)
        rows.append(f"{label} {progress_bar(left, bar_width, float(max_left))} {pct}")
        rows.append(
            merged_quota_detail_reset_line(
                accounts,
                current,
                key,
                info,
                width,
                merged_quota_mode,
            )
        )
        if key != keys[-1] and not compact:
            rows.append("")
    return rows
