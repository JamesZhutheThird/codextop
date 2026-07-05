"""Data-driven percent color schemes for CodexTOP."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


COLOR_SCHEME_FILE_ENV = "CODEXTOP_COLOR_SCHEME_FILE"
DEFAULT_COLOR_SCHEME_FILE = Path(__file__).with_name("color_schemes.json")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

_ACTIVE_SCHEME_KEY: str | None = None
_ACTIVE_SCHEME_FILE: Path | None = None
_SCHEME_CACHE: dict[Path, dict[str, Any]] = {}


def color_scheme_file(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    env_path = os.environ.get(COLOR_SCHEME_FILE_ENV)
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_COLOR_SCHEME_FILE


def _read_payload(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if resolved not in _SCHEME_CACHE:
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"color scheme file not found: {resolved}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"color scheme file is not valid JSON: {resolved}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"color scheme file root must be an object: {resolved}")
        _validate_payload(payload, resolved)
        _prepare_payload(payload)
        _SCHEME_CACHE[resolved] = payload
    return _SCHEME_CACHE[resolved]


def _validate_payload(payload: dict[str, Any], path: Path) -> None:
    if not payload:
        raise RuntimeError(f"color scheme file must contain non-empty color styles: {path}")
    for key, scheme in payload.items():
        if not isinstance(key, str) or not key:
            raise RuntimeError(f"color scheme key must be a non-empty string: {path}")
        if not isinstance(scheme, dict):
            raise RuntimeError(f"color scheme {key!r} must be an object: {path}")
        percent = scheme.get("percent")
        if not isinstance(percent, dict):
            raise RuntimeError(f"color scheme {key!r} must contain a percent object: {path}")
        if not percent:
            raise RuntimeError(f"color scheme {key!r} percent object must not be empty: {path}")
        seen_values: set[int] = set()
        for raw_value, color in percent.items():
            try:
                value = int(raw_value)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"color scheme {key!r} percent key must be an integer 0-100: {raw_value}") from exc
            if str(value) != str(raw_value):
                raise RuntimeError(f"color scheme {key!r} percent key must be an integer 0-100: {raw_value}")
            if not 0 <= value <= 100:
                raise RuntimeError(f"color scheme {key!r} percent key out of range 0-100: {raw_value}")
            if value in seen_values:
                raise RuntimeError(f"color scheme {key!r} duplicate percent key: {raw_value}")
            seen_values.add(value)
            if not isinstance(color, str) or not HEX_COLOR_RE.match(color):
                raise RuntimeError(f"color scheme {key!r} percent {value} must be #rrggbb: {path}")


def _prepare_payload(payload: dict[str, Any]) -> None:
    for scheme in payload.values():
        percent = scheme["percent"]
        scheme["_percent_stops"] = sorted((int(value), color.lower()) for value, color in percent.items())


def color_scheme_choices(path: Path | str | None = None) -> list[tuple[str, str]]:
    payload = _read_payload(color_scheme_file(path))
    choices: list[tuple[str, str]] = []
    for key, scheme in payload.items():
        label = scheme.get("label") if isinstance(scheme, dict) else None
        choices.append((str(label or key), key))
    return choices


def default_color_scheme_key(path: Path | str | None = None) -> str:
    payload = _read_payload(color_scheme_file(path))
    if "classic" in payload:
        return "classic"
    return next(iter(payload))


def color_scheme_exists(key: str, path: Path | str | None = None) -> bool:
    payload = _read_payload(color_scheme_file(path))
    return key in payload


def set_active_color_scheme(key: str | None = None, path: Path | str | None = None) -> str:
    global _ACTIVE_SCHEME_FILE, _ACTIVE_SCHEME_KEY
    scheme_file = color_scheme_file(path).resolve()
    payload = _read_payload(scheme_file)
    selected = key or default_color_scheme_key(scheme_file)
    if selected not in payload:
        raise RuntimeError(f"color scheme not found: {selected}")
    _ACTIVE_SCHEME_FILE = scheme_file
    _ACTIVE_SCHEME_KEY = selected
    return selected


def active_color_scheme_key() -> str:
    if _ACTIVE_SCHEME_KEY is None:
        return set_active_color_scheme()
    return _ACTIVE_SCHEME_KEY


def _active_scheme(path: Path | str | None = None, key: str | None = None) -> dict[str, Any]:
    scheme_file = color_scheme_file(path).resolve()
    if key is None:
        if _ACTIVE_SCHEME_KEY is None or _ACTIVE_SCHEME_FILE != scheme_file:
            key = set_active_color_scheme(path=scheme_file)
        else:
            key = _ACTIVE_SCHEME_KEY
    payload = _read_payload(scheme_file)
    if key not in payload:
        raise RuntimeError(f"color scheme not found: {key}")
    return payload[key]


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _rgb_to_hex(red: int, green: int, blue: int) -> str:
    return f"#{red:02x}{green:02x}{blue:02x}"


def _mix_hex(left: str, right: str, ratio: float) -> str:
    left_rgb = _hex_to_rgb(left)
    right_rgb = _hex_to_rgb(right)
    mixed = [
        round(left_part + (right_part - left_part) * ratio)
        for left_part, right_part in zip(left_rgb, right_rgb)
    ]
    return _rgb_to_hex(mixed[0], mixed[1], mixed[2])


def _interpolated_percent_color(stops: list[tuple[int, str]], percent: float) -> str:
    if percent <= stops[0][0]:
        return stops[0][1]
    if percent >= stops[-1][0]:
        return stops[-1][1]
    for index in range(1, len(stops)):
        right_percent, right_color = stops[index]
        if percent > right_percent:
            continue
        left_percent, left_color = stops[index - 1]
        if right_percent == left_percent:
            return right_color
        ratio = (percent - left_percent) / (right_percent - left_percent)
        return _mix_hex(left_color, right_color, ratio)
    return stops[-1][1]


def percent_gradient_style(value: Any, *, key: str | None = None, path: Path | str | None = None) -> str:
    if not isinstance(value, (int, float)):
        return "dim"
    percent = max(0.0, min(100.0, float(value)))
    return _interpolated_percent_color(_active_scheme(path, key)["_percent_stops"], percent)
