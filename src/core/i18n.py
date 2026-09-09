"""Load CodexTOP translations from one JSON file per language."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Mapping


DEFAULT_LANGUAGE = "en"
LANGUAGE_ENV = "CODEXTOP_LANG"
LOCALES_DIR = Path(__file__).with_name("locales")


@lru_cache(maxsize=1)
def available_languages() -> tuple[str, ...]:
    languages = tuple(path.stem for path in sorted(LOCALES_DIR.glob("*.json")))
    return languages if DEFAULT_LANGUAGE in languages else (DEFAULT_LANGUAGE, *languages)


def _match_language(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.split(".", 1)[0].split("@", 1)[0]
    normalized = raw.strip().replace("-", "_").lower()
    if not normalized or normalized in {"c", "posix"}:
        return None

    languages = available_languages()
    by_normalized = {language.lower(): language for language in languages}
    if normalized in by_normalized:
        return by_normalized[normalized]

    base = normalized.split("_", 1)[0]
    if base in by_normalized:
        return by_normalized[base]
    if "_" in normalized:
        return None
    regional = [language for language in languages if language.lower().startswith(f"{base}_")]
    return regional[0] if len(regional) == 1 else None


def normalize_language(value: str | None) -> str:
    """Return a supported language code, falling back to English."""
    for candidate in (value or "").split(":"):
        matched = _match_language(candidate)
        if matched is not None:
            return matched
    return DEFAULT_LANGUAGE


def detect_language(environ: Mapping[str, str] | None = None) -> str:
    """Select the app override or the process locale, with English fallback."""
    env = os.environ if environ is None else environ
    explicit = env.get(LANGUAGE_ENV)
    if explicit:
        return normalize_language(explicit)
    system_locale = (
        env.get("LC_ALL")
        or env.get("LC_MESSAGES")
        or env.get("LANGUAGE")
        or env.get("LANG")
    )
    return normalize_language(system_locale)


def set_language(language: str | None) -> str:
    """Activate a supported language for subsequent translations."""
    selected = normalize_language(language)
    os.environ[LANGUAGE_ENV] = selected
    return selected


def activate_saved_language(state_path: Path) -> None:
    """Apply the saved UI language before building translated command output."""
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    language = payload.get("language") if isinstance(payload, dict) else None
    if language in available_languages():
        set_language(language)


@lru_cache(maxsize=None)
def _catalog(language: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{normalize_language(language)}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load translation file: {path}") from exc
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        raise RuntimeError(f"translation file must contain a string map: {path}")
    return payload


def t(
    key: str,
    *,
    _language: str | None = None,
    _default: str | None = None,
    **values: object,
) -> str:
    """Translate a key and interpolate named placeholders."""
    selected = normalize_language(_language) if _language is not None else detect_language()
    text = _catalog(selected).get(key)
    if text is None and selected != DEFAULT_LANGUAGE:
        text = _catalog(DEFAULT_LANGUAGE).get(key)
    if text is None:
        text = _default if _default is not None else key
    return text.format(**values) if values else text


def language_name(language: str | None = None) -> str:
    selected = normalize_language(language) if language is not None else detect_language()
    return t(f"language.{selected}", _language=selected, _default=selected)
