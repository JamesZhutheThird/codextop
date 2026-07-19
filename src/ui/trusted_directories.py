"""Synchronize Codex trusted projects into a user-editable directory registry."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.paths import atomic_write_json


TOKEN_USAGE_DIRECTORIES_FILE = "token_usage_directories.json"
TOKEN_USAGE_DIRECTORIES_VERSION = 1
WINDOWS_MOUNT_RE = re.compile(r"^/mnt/[a-z](?:/|$)", re.IGNORECASE)


def resolved_path(value: Path | str) -> str:
    return os.path.abspath(os.path.normpath(os.path.expanduser(os.fspath(value))))


def path_key(value: Path | str) -> str:
    path = os.path.normpath(resolved_path(value))
    if WINDOWS_MOUNT_RE.match(path):
        return path.casefold()
    return os.path.normcase(path)


def path_is_within(path: Path | str, root: Path | str) -> bool:
    candidate = path_key(path)
    parent = path_key(root)
    return path_key_is_within(candidate, parent)


def path_key_is_within(candidate: str, parent: str) -> bool:
    try:
        return os.path.commonpath([candidate, parent]) == parent
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class TrustedDirectory:
    path: str
    key: str
    disable: bool = False


def trusted_paths_from_config(config_path: Path) -> list[str]:
    try:
        payload = tomllib.loads(config_path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"cannot read Codex trusted projects: {exc}") from exc
    projects = payload.get("projects") if isinstance(payload, dict) else None
    if not isinstance(projects, dict):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for raw_path, settings in projects.items():
        if not isinstance(raw_path, str) or not isinstance(settings, dict):
            continue
        if settings.get("trust_level") != "trusted":
            continue
        display_path = resolved_path(raw_path)
        key = path_key(display_path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(display_path)
    return paths


def _registry_entries(payload: Any) -> list[dict[str, Any]]:
    entries = payload.get("directories") if isinstance(payload, dict) else None
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def read_directory_registry(path: Path) -> tuple[dict[str, bool], list[str]]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, []
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read Token directory registry: {exc}") from exc
    disabled: dict[str, bool] = {}
    listed_paths: list[str] = []
    for entry in _registry_entries(payload):
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        display_path = resolved_path(raw_path)
        key = path_key(display_path)
        if key not in disabled:
            listed_paths.append(display_path)
        disabled[key] = entry.get("disable") is True
    return disabled, listed_paths


@dataclass(slots=True)
class TrustedDirectoryRegistry:
    config_path: Path
    registry_path: Path
    directories: list[TrustedDirectory] = field(default_factory=list)
    version: int = 0
    error: str | None = None
    _config_mtime_ns: int | None = None
    _registry_mtime_ns: int | None = None
    _trusted_paths: list[str] = field(default_factory=list)

    @staticmethod
    def _mtime_ns(path: Path) -> int | None:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    def _write(self, directories: list[TrustedDirectory]) -> None:
        atomic_write_json(
            self.registry_path,
            {
                "version": TOKEN_USAGE_DIRECTORIES_VERSION,
                "source": str(self.config_path),
                "directories": [
                    {"path": entry.path, "disable": entry.disable}
                    for entry in directories
                ],
            },
        )
        self._registry_mtime_ns = self._mtime_ns(self.registry_path)

    def poll(self, force: bool = False) -> bool:
        config_mtime = self._mtime_ns(self.config_path)
        registry_mtime = self._mtime_ns(self.registry_path)
        config_changed = force or config_mtime != self._config_mtime_ns
        registry_changed = force or registry_mtime != self._registry_mtime_ns
        if not config_changed and not registry_changed:
            return False

        try:
            if config_changed:
                self._trusted_paths = trusted_paths_from_config(self.config_path)
            disabled, listed_paths = read_directory_registry(self.registry_path)
        except RuntimeError as exc:
            self.error = str(exc)
            self._config_mtime_ns = config_mtime
            self._registry_mtime_ns = registry_mtime
            return False

        directories = [
            TrustedDirectory(path, path_key(path), disabled.get(path_key(path), False))
            for path in self._trusted_paths
        ]
        expected_keys = [entry.key for entry in directories]
        listed_keys = [path_key(path) for path in listed_paths]
        if registry_mtime is None or listed_keys != expected_keys:
            self._write(directories)

        changed = directories != self.directories or self.error is not None
        self.directories = directories
        self.error = None
        self._config_mtime_ns = config_mtime
        self._registry_mtime_ns = self._mtime_ns(self.registry_path)
        if changed:
            self.version += 1
        return changed

    def owner_for(self, directory: Path | str) -> TrustedDirectory | None:
        candidate = path_key(directory)
        matches = [
            entry
            for entry in self.directories
            if path_key_is_within(candidate, entry.key)
        ]
        if not matches:
            return None
        return max(matches, key=lambda entry: len(entry.key))

    def by_key(self, key: str | None) -> TrustedDirectory | None:
        if key is None:
            return None
        return next((entry for entry in self.directories if entry.key == key), None)
