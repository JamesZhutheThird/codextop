"""Shared filesystem layout for CodexTOP runtime data."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CODEX_DIR_ENV = "CODEXTOP_CODEX_DIR"
CODEX_HOME_ENV = "CODEX_HOME"
RUNTIME_DIR_NAME = "codextop"
AUTH_FILE_RE = re.compile(r"auth-([A-Za-z0-9][A-Za-z0-9_.-]*)\.json$")
LEGACY_AUTH_FILE_RE = re.compile(r"auth-plus-(\d+)\.json$")
MONTHLY_LOG_RE = re.compile(r"^(?P<stem>.+)_(?P<month>\d{4}-\d{2})(?P<suffix>\.[^.]+)?$")


@dataclass(frozen=True)
class CodexTopPaths:
    codex_dir: Path
    runtime_dir: Path
    auth_list_dir: Path
    auth_backup_dir: Path
    log_dir: Path
    settings_dir: Path
    active_auth_file: Path
    config_file: Path
    current_provider_file: Path
    registry_file: Path

    @property
    def legacy_auth_list_dir(self) -> Path:
        return self.codex_dir / "auth_list"

    @property
    def legacy_log_dir(self) -> Path:
        return self.codex_dir / "logs" / RUNTIME_DIR_NAME


def codex_dir_from_env() -> Path:
    raw = os.environ.get(CODEX_DIR_ENV) or os.environ.get(CODEX_HOME_ENV) or "~/.codex"
    return Path(raw).expanduser()


def default_paths(codex_dir: Path | None = None) -> CodexTopPaths:
    root = (codex_dir or codex_dir_from_env()).expanduser()
    runtime_dir = root / RUNTIME_DIR_NAME
    auth_list_dir = runtime_dir / "auth_list"
    settings_dir = runtime_dir / "settings"
    return CodexTopPaths(
        codex_dir=root,
        runtime_dir=runtime_dir,
        auth_list_dir=auth_list_dir,
        auth_backup_dir=auth_list_dir / "backup",
        log_dir=runtime_dir / "log",
        settings_dir=settings_dir,
        active_auth_file=root / "auth.json",
        config_file=root / "config.toml",
        current_provider_file=settings_dir / "current_provider.json",
        registry_file=settings_dir / "auth_registry.json",
    )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp_name, mode & 0o777)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", mode)


def timezone_for_name(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def monthly_log_path(base_log_path: Path, timestamp: int | float | None, tz_name: str) -> Path:
    base_log_path = base_log_path.expanduser()
    tz = timezone_for_name(tz_name)
    observed = datetime.fromtimestamp(timestamp, tz) if isinstance(timestamp, (int, float)) else datetime.now(tz)
    suffix = base_log_path.suffix or ".jsonl"
    return base_log_path.with_name(f"{base_log_path.stem}_{observed:%Y-%m}{suffix}")


def recent_month_keys(tz_name: str, count: int = 2) -> set[str]:
    tz = timezone_for_name(tz_name)
    observed = datetime.now(tz)
    year = observed.year
    month = observed.month
    keys: set[str] = set()
    for _ in range(max(1, count)):
        keys.add(f"{year:04d}-{month:02d}")
        month -= 1
        if month <= 0:
            month = 12
            year -= 1
    return keys


def iter_snapshot_log_paths(base_log_path: Path, months: set[str] | None = None) -> list[Path]:
    base_log_path = base_log_path.expanduser()
    parent = base_log_path.parent
    suffix = base_log_path.suffix or ".jsonl"
    pattern = f"{base_log_path.stem}_????-??{suffix}"
    paths: list[Path] = []
    if parent.exists():
        for candidate in sorted(parent.glob(pattern)):
            match = MONTHLY_LOG_RE.fullmatch(candidate.name)
            if (
                match
                and match.group("stem") == base_log_path.stem
                and (match.group("suffix") or "") == suffix
                and (months is None or match.group("month") in months)
            ):
                paths.append(candidate)
    return paths


def auth_keyword_from_path(path: Path) -> str | None:
    match = AUTH_FILE_RE.fullmatch(path.name)
    return match.group(1) if match else None


def auth_keyword_number(keyword: str | int | None) -> int | None:
    if isinstance(keyword, int):
        return keyword if keyword > 0 else None
    if not isinstance(keyword, str):
        return None
    if keyword.isdigit():
        value = int(keyword)
        return value if value > 0 else None
    match = re.fullmatch(r"openai-(\d+)", keyword)
    if not match:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def normalize_auth_keyword(value: str | int | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return f"openai-{value}" if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("auth-") and text.endswith(".json"):
        text = text[5:-5]
    return text


def auth_sort_key(keyword: str) -> tuple[int, int, str]:
    number = auth_keyword_number(keyword)
    if keyword.isdigit() and number is not None:
        return (0, number, keyword)
    if keyword.startswith("openai-") and number is not None:
        return (1, number, keyword)
    return (2, number or 0, keyword)


def provider_config_payload(provider: str, auth_keyword: str | int | None) -> dict[str, Any]:
    provider = provider.strip() or "unknown"
    keyword = normalize_auth_keyword(auth_keyword)
    number = auth_keyword_number(keyword)
    if provider == "openai" and keyword is not None:
        service = keyword
    else:
        service = provider
    return {
        "updated_at": now_iso(),
        "provider": provider,
        "auth_keyword": keyword,
        "openai_provider_number": number,
        "service": service,
    }


def read_legacy_current_provider(paths: CodexTopPaths) -> int | None:
    for current_path in (
        paths.legacy_auth_list_dir / "current_provider.txt",
        paths.auth_list_dir / "current_provider.txt",
    ):
        try:
            raw = current_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def auth_list_has_configs(auth_list_dir: Path) -> bool:
    return any(AUTH_FILE_RE.fullmatch(path.name) for path in auth_list_dir.glob("auth-*.json"))


def migrate_legacy_file(source: Path, paths: CodexTopPaths) -> bool:
    match = LEGACY_AUTH_FILE_RE.fullmatch(source.name)
    if not match:
        return False
    target = paths.auth_list_dir / f"auth-openai-{match.group(1)}.json"
    if not target.exists():
        shutil.copy2(source, target)
    if source.parent == paths.auth_list_dir and source.exists():
        legacy_backup_dir = paths.auth_backup_dir / "legacy"
        legacy_backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = legacy_backup_dir / f"{source.stem}.{timestamp}{source.suffix}"
        suffix = 1
        while backup.exists():
            backup = legacy_backup_dir / f"{source.stem}.{timestamp}-{suffix:02d}{source.suffix}"
            suffix += 1
        shutil.move(str(source), str(backup))
    return True


def migrate_legacy_auth_list(paths: CodexTopPaths) -> bool:
    legacy_dir = paths.legacy_auth_list_dir

    copied = False
    paths.auth_list_dir.mkdir(parents=True, exist_ok=True)
    if legacy_dir != paths.auth_list_dir and legacy_dir.exists():
        for source in sorted(legacy_dir.glob("auth-plus-*.json")):
            copied = migrate_legacy_file(source, paths) or copied

    for source in sorted(paths.auth_list_dir.glob("auth-plus-*.json")):
        copied = migrate_legacy_file(source, paths) or copied

    if not paths.current_provider_file.exists():
        current = read_legacy_current_provider(paths)
        if current is not None:
            atomic_write_json(paths.current_provider_file, provider_config_payload("openai", f"openai-{current}"))
            copied = True
    return copied


def bootstrap_auth_list_from_active_auth(paths: CodexTopPaths) -> bool:
    if auth_list_has_configs(paths.auth_list_dir):
        return False
    if not paths.active_auth_file.exists():
        return False

    target = paths.auth_list_dir / "auth-openai-1.json"
    shutil.copy2(paths.active_auth_file, target)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    if not paths.current_provider_file.exists():
        atomic_write_json(paths.current_provider_file, provider_config_payload("openai", "openai-1"))
    return True


def ensure_runtime_layout(paths: CodexTopPaths | None = None, *, migrate: bool = True) -> CodexTopPaths:
    paths = paths or default_paths()
    for directory in (paths.runtime_dir, paths.auth_list_dir, paths.auth_backup_dir, paths.log_dir, paths.settings_dir):
        directory.mkdir(parents=True, exist_ok=True)
    if migrate:
        migrate_legacy_auth_list(paths)
        bootstrap_auth_list_from_active_auth(paths)
    return paths


def current_provider_file_for_auth_list(auth_list_dir: Path) -> Path:
    return auth_list_dir.expanduser().parent / "settings" / "current_provider.json"
