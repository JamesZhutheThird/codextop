"""Data-driven percent color schemes for CodexTOP."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


COLOR_SCHEME_DIR_ENV = "CODEXTOP_COLOR_SCHEME_DIR"
COLOR_SCHEME_FILE_ENV = "CODEXTOP_COLOR_SCHEME_FILE"
DEFAULT_COLOR_SCHEME_DIR = Path(__file__).with_name("styles")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# TODO: Remove the redblue -> icefire compatibility alias when the minimum supported version is >= 2.5.0.
COLOR_SCHEME_ALIASES = {"redblue": "icefire"}

_ACTIVE_SCHEME_KEY: str | None = None
_ACTIVE_SCHEME_SOURCE: Path | None = None
_ACTIVE_SCHEME: dict[str, Any] | None = None
_ACTIVE_IMPLICIT_SOURCE: tuple[str | None, str | None] | None = None
_SCHEME_CACHE: dict[Path, dict[str, Any]] = {}


def color_scheme_source(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    env_dir = os.environ.get(COLOR_SCHEME_DIR_ENV)
    if env_dir:
        return Path(env_dir).expanduser()
    env_path = os.environ.get(COLOR_SCHEME_FILE_ENV)
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_COLOR_SCHEME_DIR


def _implicit_source_signature() -> tuple[str | None, str | None]:
    return os.environ.get(COLOR_SCHEME_DIR_ENV), os.environ.get(COLOR_SCHEME_FILE_ENV)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"color style file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"color style file is not valid JSON: {path}") from exc


def _read_payload(source: Path) -> dict[str, Any]:
    resolved = source.resolve()
    if resolved not in _SCHEME_CACHE:
        if resolved.is_dir():
            payload = {}
            scheme_files = sorted(resolved.glob("*.json"), key=lambda path: (path.stem != "classic", path.stem))
            for scheme_file in scheme_files:
                scheme = _read_json(scheme_file)
                if not isinstance(scheme, dict):
                    raise RuntimeError(f"color style root must be an object: {scheme_file}")
                payload[scheme_file.stem] = scheme
        else:
            payload = _read_json(resolved)
            if not isinstance(payload, dict):
                raise RuntimeError(f"color style source root must be an object: {resolved}")
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
    payload = _read_payload(color_scheme_source(path))
    choices: list[tuple[str, str]] = []
    for key, scheme in payload.items():
        label = scheme.get("label") if isinstance(scheme, dict) else None
        choices.append((str(label or key), key))
    return choices


def default_color_scheme_key(path: Path | str | None = None) -> str:
    payload = _read_payload(color_scheme_source(path))
    if "classic" in payload:
        return "classic"
    return next(iter(payload))


def _resolve_color_scheme_key(key: str, payload: dict[str, Any]) -> str:
    if key in payload:
        return key
    alias = COLOR_SCHEME_ALIASES.get(key)
    return alias if alias in payload else key


def color_scheme_exists(key: str, path: Path | str | None = None) -> bool:
    payload = _read_payload(color_scheme_source(path))
    return _resolve_color_scheme_key(key, payload) in payload


def set_active_color_scheme(key: str | None = None, path: Path | str | None = None) -> str:
    global _ACTIVE_IMPLICIT_SOURCE, _ACTIVE_SCHEME, _ACTIVE_SCHEME_SOURCE, _ACTIVE_SCHEME_KEY
    scheme_source = color_scheme_source(path).resolve()
    payload = _read_payload(scheme_source)
    selected = _resolve_color_scheme_key(key or default_color_scheme_key(scheme_source), payload)
    if selected not in payload:
        raise RuntimeError(f"color scheme not found: {selected}")
    _ACTIVE_SCHEME_SOURCE = scheme_source
    _ACTIVE_SCHEME_KEY = selected
    _ACTIVE_SCHEME = payload[selected]
    _ACTIVE_IMPLICIT_SOURCE = _implicit_source_signature() if path is None else None
    return selected


def active_color_scheme_key() -> str:
    if _ACTIVE_SCHEME_KEY is None:
        return set_active_color_scheme()
    return _ACTIVE_SCHEME_KEY


def _active_scheme(path: Path | str | None = None, key: str | None = None) -> dict[str, Any]:
    global _ACTIVE_IMPLICIT_SOURCE, _ACTIVE_SCHEME, _ACTIVE_SCHEME_SOURCE, _ACTIVE_SCHEME_KEY
    implicit_active_request = path is None and key is None
    if (
        implicit_active_request
        and _ACTIVE_SCHEME is not None
        and _ACTIVE_IMPLICIT_SOURCE == _implicit_source_signature()
    ):
        return _ACTIVE_SCHEME
    scheme_source = color_scheme_source(path).resolve()
    if key is None:
        if _ACTIVE_SCHEME_KEY is None or _ACTIVE_SCHEME_SOURCE != scheme_source:
            key = set_active_color_scheme(path=path)
        else:
            key = _ACTIVE_SCHEME_KEY
    payload = _read_payload(scheme_source)
    key = _resolve_color_scheme_key(key, payload)
    if key not in payload:
        raise RuntimeError(f"color scheme not found: {key}")
    scheme = payload[key]
    if implicit_active_request:
        _ACTIVE_SCHEME_SOURCE = scheme_source
        _ACTIVE_SCHEME_KEY = key
        _ACTIVE_SCHEME = scheme
        _ACTIVE_IMPLICIT_SOURCE = _implicit_source_signature()
    return scheme


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


def token_series_colors(*, key: str | None = None, path: Path | str | None = None) -> dict[str, str]:
    """Return four fixed colors sampled from the selected scheme."""
    stops = {
        "input": 15,
        "cached": 40,
        "output": 70,
        "total": 100,
    }
    return {
        label: percent_gradient_style(percent, key=key, path=path)
        for label, percent in stops.items()
    }
