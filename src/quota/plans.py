"""Normalize Codex account plans and their quota multipliers."""

from __future__ import annotations

from enum import Enum
from typing import Any


class AccountPlan(Enum):
    """Supported wire types, Plus-relative quotas, labels, and legacy aliases.

    None means no verified fixed multiplier; consumers retain the 1x fallback.
    """

    PLUS = ("plus", 1, "Plus", ("chatgptplus",))
    PROLITE = ("prolite", 5, "Pro(5x)", ("pro-5x", "pro_5x", "pro5x", "chatgptprolite"))
    PRO = ("pro", 10, "Pro(10x)", ("chatgptpro",))
    PROMAX = ("promax", 25, "ProMax(25x)", ())
    LEGACY_PRO_20X = ("pro-20x", 20, "Pro(20x)", ("pro_20x", "pro20x"))
    FREE = ("free", None, "Free", ())
    GO = ("go", None, "Go", ())
    TEAM = ("team", None, "Team", ())
    SELF_SERVE_BUSINESS_PROLITE = ("self_serve_business_prolite", None, "Business ProLite", ())
    SELF_SERVE_BUSINESS_USAGE_BASED = ("self_serve_business_usage_based", None, "Business Usage Based", ())
    BUSINESS = ("business", None, "Business", ())
    ENT26 = ("ent26", None, "Enterprise (ent26)", ())
    ENTERPRISE_CBP_AUTOMATION = ("enterprise_cbp_automation", None, "Enterprise Automation", ())
    ENTERPRISE_CBP_USAGE_BASED = ("enterprise_cbp_usage_based", None, "Enterprise Usage Based", ())
    ENTERPRISE = ("enterprise", None, "Enterprise", ())
    EDU = ("edu", None, "Edu", ())
    EDU_PLUS = ("edu_plus", None, "Edu Plus", ())
    EDU_PRO = ("edu_pro", None, "Edu Pro", ())
    UNKNOWN = ("unknown", None, "Unknown", ())

    def __init__(self, key: str, multiplier: int | None, label: str, aliases: tuple[str, ...]) -> None:
        self.key = key
        self.multiplier = multiplier
        self.label = label
        self.aliases = aliases

    @classmethod
    def from_key(cls, key: str) -> AccountPlan | None:
        return next((plan for plan in cls if key == plan.key or key in plan.aliases), None)


def normalize_plan_type(plan_type: Any) -> str | None:
    raw = plan_type.strip().lower() if isinstance(plan_type, str) and plan_type.strip() else None
    plan = AccountPlan.from_key(raw) if raw else None
    return plan.key if plan else raw


def account_plan_key(account: dict[str, Any]) -> str:
    plan = account.get("plan_type") or account.get("plan")
    return normalize_plan_type(plan) or ""


def account_quota_multiplier(account: dict[str, Any]) -> float:
    plan = AccountPlan.from_key(account_plan_key(account))
    return float(plan.multiplier) if plan and plan.multiplier is not None else 1.0


def recorded_quota_multiplier(account: dict[str, Any]) -> float | None:
    """Return a snapshot's exact multiplier, or None when current-plan fallback is required."""
    plan = account_plan_key(account)
    if account.get("_plan_inherited") or not plan or plan == "pro":
        return None
    return account_quota_multiplier(account)
