"""Identify Codex quota windows from their server-reported duration."""

from __future__ import annotations

from typing import Any


WINDOW_SECONDS = {
    "5h": 5 * 3600,
    "7d": 7 * 86400,
}


def window_key_for_seconds(value: Any, fallback: str | None = None) -> str | None:
    """Return the supported window closest to ``value`` seconds.

    Codex may expose the weekly limit as ``primary_window`` when the 5-hour
    limit is disabled, so the API field name cannot be used as the window
    identity. Older payloads without a duration retain their positional
    fallback for backward compatibility.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return min(WINDOW_SECONDS, key=lambda key: abs(float(value) - WINDOW_SECONDS[key]))
    return fallback if fallback in WINDOW_SECONDS else None
