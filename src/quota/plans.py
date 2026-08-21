"""Normalize Codex account plans and their quota multipliers."""

from __future__ import annotations

from typing import Any


def account_plan_key(account: dict[str, Any]) -> str:
    plan = account.get("plan_type") or account.get("plan")
    return plan.strip().lower() if isinstance(plan, str) and plan.strip() else ""


def account_quota_multiplier(account: dict[str, Any]) -> float:
    plan = account_plan_key(account)
    if plan in {"pro", "pro-20x", "pro_20x", "pro20x", "chatgptpro"}:
        return 20.0
    if plan in {"pro-5x", "pro_5x", "pro5x", "chatgptprolite"}:
        return 5.0
    return 1.0


def recorded_quota_multiplier(account: dict[str, Any]) -> float | None:
    """Return a snapshot's exact multiplier, or None when current-plan fallback is required."""
    plan = account_plan_key(account)
    if account.get("_plan_inherited") or not plan or plan == "pro":
        return None
    return account_quota_multiplier(account)
