#!/usr/bin/env python3
"""Manage CodexTOP auth slots, provider state, and provider switching."""

from __future__ import annotations

import argparse
import io
import base64
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.paths import (
    atomic_write_json,
    atomic_write_text,
    auth_keyword_from_path,
    auth_keyword_number,
    auth_sort_key,
    default_paths,
    ensure_runtime_layout,
    normalize_auth_keyword,
    now_iso,
    provider_config_payload,
)
from core.i18n import activate_saved_language, t


REPO_DIR = Path(__file__).resolve().parents[2]
PATHS = ensure_runtime_layout(default_paths())
CODEX_DIR = PATHS.codex_dir
CONFIG_FILE = PATHS.config_file
ACTIVE_AUTH_FILE = PATHS.active_auth_file
AUTH_LIST_DIR = PATHS.auth_list_dir
CURRENT_PROVIDER_FILE = PATHS.current_provider_file
REGISTRY_FILE = PATHS.registry_file
AUTH_BACKUP_DIR = PATHS.auth_backup_dir
BACKUPS_PER_PROVIDER = 10
AUTH_FILE_RE = re.compile(r"auth-([A-Za-z0-9][A-Za-z0-9_.-]*)\.json$")
FILE_AUTH_CONFIG = 'cli_auth_credentials_store="file"'
DEFAULT_API_BASE_URL = "https://api.openai.com/v1"


class AuthError(Exception):
    pass


def auth_file(keyword: str) -> Path:
    return AUTH_LIST_DIR / f"auth-{keyword}.json"


def discover_auth_files() -> list[tuple[str, Path]]:
    configs: list[tuple[str, Path]] = []
    if not AUTH_LIST_DIR.exists():
        return configs
    for path in AUTH_LIST_DIR.glob("auth-*.json"):
        keyword = auth_keyword_from_path(path)
        if keyword is not None and not is_api_auth(read_auth_payload(path)):
            configs.append((keyword, path))
    return sorted(configs, key=lambda item: auth_sort_key(item[0]))


def auth_file_map() -> dict[str, Path]:
    return {keyword: path for keyword, path in discover_auth_files()}


def registry_api_accounts() -> list[dict[str, Any]]:
    if not REGISTRY_FILE.exists():
        return []
    return [
        account for account in read_auth_payload(REGISTRY_FILE).get("accounts", [])
        if account.get("auth_type") == "api"
    ]


def numbered_accounts() -> dict[str, int]:
    keywords = list(auth_file_map())
    keywords.extend(a["auth_keyword"] for a in registry_api_accounts() if a["auth_keyword"] not in keywords)
    numbers = {key: auth_keyword_number(key) for key in keywords if auth_keyword_number(key) is not None}
    next_number = max(numbers.values(), default=0) + 1
    for key in keywords:
        if key not in numbers:
            numbers[key] = next_number
            next_number += 1
    return numbers


def write_api_account(keyword: str, account: dict[str, Any] | None) -> None:
    registry = read_auth_payload(REGISTRY_FILE) if REGISTRY_FILE.exists() else {}
    accounts = registry.setdefault("accounts", [])
    for index, existing in enumerate(accounts):
        if existing.get("auth_keyword") == keyword:
            if account is None:
                accounts.pop(index)
            else:
                accounts[index] = account
            break
    else:
        if account is not None:
            accounts.append(account)
    atomic_write_json(REGISTRY_FILE, registry, 0o600)


def read_auth_account(keyword: str) -> dict[str, Any]:
    for account in registry_api_accounts():
        if account["auth_keyword"] == keyword:
            return account
    payload = read_auth_payload(auth_file(keyword))
    if is_api_auth(payload):
        raise AuthError(t("auth.target_file_not_found", target=keyword) + new_auth_hint(keyword))
    return {"auth_type": "chatgpt", "meta": sanitized_auth_meta(payload)}


def new_auth_hint(target: str) -> str:
    return t("auth.new_hint", target=target)


def resolve_auth_target(target: str) -> str:
    value = normalize_auth_keyword(target)
    if value is None:
        raise AuthError(t("auth.missing_target"))
    configs = numbered_accounts()
    if value.isdigit():
        for keyword in (value, f"openai-{value}"):
            if keyword in configs:
                return keyword
        for keyword, number in configs.items():
            if number == int(value):
                return keyword
        raise AuthError(
            t("auth.target_files_not_found", target=value)
            + new_auth_hint(value)
        )
    if value in configs:
        return value
    lower_map = {keyword.lower(): keyword for keyword in configs}
    if value.lower() in lower_map:
        return lower_map[value.lower()]
    raise AuthError(t("auth.target_file_not_found", target=value) + new_auth_hint(value))


def normalize_new_auth_target(target: str) -> str:
    value = normalize_auth_keyword(target)
    if value is None:
        raise AuthError(t("auth.missing_new_target"))
    numeric = re.fullmatch(r"(?:openai-)?(\d+)", value, re.IGNORECASE)
    if numeric:
        number = int(numeric.group(1))
        if number <= 0:
            raise AuthError(t("auth.provider_number_positive"))
        value = f"openai-{number}"
    if AUTH_FILE_RE.fullmatch(f"auth-{value}.json") is None:
        raise AuthError(t("auth.invalid_new_target"))
    if value.lower() == "openai":
        raise AuthError(t("auth.reserved_provider", provider=value))
    return value


def read_auth_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AuthError(t("auth.file_not_found", path=path)) from exc
    except json.JSONDecodeError as exc:
        raise AuthError(t("auth.invalid_json", path=path)) from exc
    if not isinstance(payload, dict):
        raise AuthError(t("auth.invalid_json_root", path=path))
    return payload


def is_api_auth(payload: dict[str, Any]) -> bool:
    return payload.get("auth_mode") == "apikey" or bool(payload.get("OPENAI_API_KEY"))


def api_provider_id(keyword: str) -> str:
    return "codexauth-third-party-api"


def validate_api_key(value: Any) -> str:
    if (
        not isinstance(value, str) or not value.strip()
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value.strip())
    ):
        raise AuthError(t("auth.api_invalid_key"))
    return value.strip()


def api_auth_payload(meta: dict[str, Any]) -> dict[str, Any]:
    key = meta.get("api_key")
    if meta.get("env_key") is not None:
        name = meta["env_key"]
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
            raise AuthError(t("auth.api_invalid_env"))
        key = os.environ.get(name)
        if not key or not key.strip():
            raise AuthError(t("auth.api_env_missing", name=name))
    return {"auth_mode": "apikey", "OPENAI_API_KEY": validate_api_key(key)}


def prompt_api_source(keyword: str) -> dict[str, Any]:
    from rich.console import Console
    from rich.prompt import Prompt
    from rich.text import Text

    if not sys.stdin.isatty():
        raise AuthError(t("auth.api_terminal_required"))
    console = Console(stderr=True)
    console.print(Text(t("auth.api_intro", provider=keyword)))
    try:
        choice = Prompt.ask(t("auth.api_source"), choices=["1", "2"], default="1", console=console)
        meta: dict[str, Any] = {"auth_mode": "apikey", "wire_api": "responses"}
        while True:
            if choice == "1":
                meta["api_key"] = Prompt.ask(t("auth.api_key"), password=True, console=console).strip()
            else:
                meta["env_key"] = Prompt.ask(t("auth.api_key_env"), console=console).strip()
            try:
                key = api_auth_payload(meta)["OPENAI_API_KEY"]
                break
            except AuthError as exc:
                console.print(Text(t("common.error_message", error=exc)))
        masked_key = key[:4] + "******" + key[-1] if len(key) > 5 else "******"
        console.print(Text(t("auth.api_key_detected", masked_key=masked_key)))
        base_url = Prompt.ask(t("auth.api_base_url"), default=DEFAULT_API_BASE_URL, console=console).strip()
        meta["base_url"] = validate_api_base_url(base_url or DEFAULT_API_BASE_URL)
        return meta
    except (KeyboardInterrupt, EOFError) as exc:
        raise AuthError(t("auth.api_cancelled")) from exc


def validate_api_base_url(value: str) -> str:
    try:
        url = urlsplit(value)
        valid = url.scheme in {"http", "https"} and url.hostname and url.port != 0
        valid = valid and not (url.username or url.password or url.query or url.fragment)
    except ValueError:
        valid = False
    if not valid or any(c.isspace() for c in value):
        raise AuthError(t("auth.api_invalid_url"))
    return value.rstrip("/")


def decode_jwt_claims(token: Any) -> dict[str, Any]:
    if not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return {}
    encoded = parts[1]
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(decoded.decode("utf-8"))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def normalize_email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    email = value.strip()
    return email.casefold() if email and "@" in email else None


def auth_email(path: Path) -> str:
    payload = read_auth_payload(path)
    tokens = payload.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    id_claims = decode_jwt_claims(tokens.get("id_token"))
    access_claims = decode_jwt_claims(tokens.get("access_token"))
    profile = access_claims.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, dict) else {}
    candidates = (id_claims.get("email"), access_claims.get("email"), profile.get("email"))
    emails = {
        normalized: value.strip()
        for value in candidates
        if (normalized := normalize_email(value)) is not None and isinstance(value, str)
    }
    if len(emails) != 1:
        raise AuthError(t("auth.email_unverifiable", path=path))
    return next(iter(emails.values()))


def token_fingerprint(path: Path) -> str | None:
    try:
        payload = read_auth_payload(path)
    except AuthError:
        return None
    token = payload.get("tokens", {}).get("access_token")
    if not isinstance(token, str) or not token:
        return None
    return token


def infer_current_provider() -> str | None:
    active_token = token_fingerprint(ACTIVE_AUTH_FILE)
    if not active_token:
        return None
    for keyword, path in discover_auth_files():
        if token_fingerprint(path) == active_token:
            return keyword
    return None


def read_provider_config() -> dict[str, Any]:
    try:
        payload = json.loads(CURRENT_PROVIDER_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_legacy_current_provider() -> str | None:
    for current_path in (
        PATHS.legacy_auth_list_dir / "current_provider.txt",
        AUTH_LIST_DIR / "current_provider.txt",
    ):
        try:
            raw_value = current_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        try:
            provider_number = int(raw_value)
        except ValueError:
            continue
        if provider_number > 0:
            try:
                return resolve_auth_target(str(provider_number))
            except AuthError:
                return f"openai-{provider_number}"
    return None


def read_current_provider() -> str | None:
    payload = read_provider_config()
    keyword = normalize_auth_keyword(payload.get("auth_keyword"))
    if keyword is not None:
        return keyword
    value = payload.get("openai_provider_number")
    if isinstance(value, int) and value > 0:
        try:
            return resolve_auth_target(str(value))
        except AuthError:
            return f"openai-{value}"
    value = read_legacy_current_provider()
    if value is not None:
        return value
    return infer_current_provider()


def resolve_existing_auth_target(target: str | None) -> str:
    if target is not None and target.strip():
        return resolve_auth_target(target)
    current_provider = read_current_provider()
    if current_provider is None or current_provider not in numbered_accounts():
        raise AuthError(t("auth.current_provider_missing"))
    return current_provider


def write_provider_config(provider: str, auth_keyword: str | None = None) -> dict[str, Any]:
    payload = provider_config_payload(provider, auth_keyword)
    if auth_keyword and provider == api_provider_id(auth_keyword):
        payload["service"] = auth_keyword
    payload["codex_dir"] = str(CODEX_DIR)
    payload["auth_list_dir"] = str(AUTH_LIST_DIR)
    payload["config_path"] = str(CONFIG_FILE)
    atomic_write_json(CURRENT_PROVIDER_FILE, payload, 0o600)
    return payload


def read_model_provider() -> str:
    if not CONFIG_FILE.exists():
        return "unset"
    return str(parse_config(CONFIG_FILE.read_text(encoding="utf-8")).get("model_provider", "unknown"))


def parse_config(content: str) -> dict[str, Any]:
    try:
        return tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise AuthError(t("auth.invalid_config")) from exc


def set_root_config(content: str, key: str, value: str) -> str:
    # Only edit the root table, preserving profiles and unrelated user settings.
    table = re.search(r"(?m)^[ \t]*\[", content)
    end = table.start() if table else len(content)
    root, rest = content[:end], content[end:]
    pattern = re.compile(rf"(?m)^[ \t]*(?:{key}|\"{key}\"|'{key}')[ \t]*=.*$")
    line = f"{key} = {json.dumps(value)}"
    root = pattern.sub(lambda _: line, root, count=1) if pattern.search(root) else line + "\n" + root
    return root + rest


def set_model_provider(provider: str, api_meta: dict[str, Any] | None = None, *, remove: str | None = None) -> None:
    content = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
    config = parse_config(content)
    removed_providers = set()
    managed_blocks = list(re.finditer(
        r"(?ms)^# BEGIN CODEXAUTH ([A-Za-z0-9][A-Za-z0-9_.-]*)\n.*?^# END CODEXAUTH \1\n?", content
    ))
    managed_ids = set()
    for match in managed_blocks:
        managed_ids.update(parse_config(match.group()).get("model_providers", {}))
    if api_meta is not None and provider in config.get("model_providers", {}) and provider not in managed_ids:
        raise AuthError(t("auth.api_config_collision", provider=provider))
    for match in managed_blocks:
        ids = set(parse_config(match.group()).get("model_providers", {}))
        if api_meta is not None or provider == "openai" or provider not in ids or match[1] == remove:
            content = content.replace(match.group(), "", 1)
            removed_providers.update(ids)
    if api_meta is not None:
        keyword = provider
        base_url = validate_api_base_url(api_meta.get("base_url", DEFAULT_API_BASE_URL))
        auth_config = (
            f'env_key = {json.dumps(api_meta["env_key"])}\n'
            if api_meta.get("env_key") is not None else 'requires_openai_auth = true\n'
        )
        content = content.rstrip() + (
            f'\n\n# BEGIN CODEXAUTH {keyword}\n[model_providers.{json.dumps(provider)}]\n'
            f'name = {json.dumps(keyword)}\nbase_url = {json.dumps(base_url)}\n'
            f'wire_api = "responses"\n{auth_config}'
            f'# END CODEXAUTH {keyword}\n'
        )
    content = set_root_config(content, "model_provider", provider)
    content = set_root_config(content, "cli_auth_credentials_store", "file")
    updated = parse_config(content)
    if updated.get("model_provider") != provider or updated.get("cli_auth_credentials_store") != "file":
        raise AuthError(t("auth.invalid_config"))
    # Reject unusual TOML layouts if a textual edit changed any unrelated value.
    for parsed in (config, updated):
        parsed.pop("model_provider", None)
        parsed.pop("cli_auth_credentials_store", None)
        providers = parsed.pop("model_providers", {})
        for removed_provider in removed_providers:
            providers.pop(removed_provider, None)
        if api_meta is not None:
            providers.pop(provider, None)
        if providers:
            parsed["model_providers"] = providers
    if config != updated:
        raise AuthError(t("auth.invalid_config"))
    atomic_write_text(CONFIG_FILE, content, CONFIG_FILE.stat().st_mode if CONFIG_FILE.exists() else 0o600)


@contextmanager
def preserve_auth_state(*extra_paths: Path):
    paths = (CONFIG_FILE, ACTIVE_AUTH_FILE, CURRENT_PROVIDER_FILE, REGISTRY_FILE, *extra_paths)
    saved = {path: (path.read_text(encoding="utf-8"), path.stat().st_mode) if path.exists() else None for path in paths}
    try:
        yield
    except BaseException:
        for path, previous in saved.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, *previous)
        raise


def normalize_provider_from_config(model_provider: str, current_provider: str | None) -> dict[str, Any]:
    raw_config = read_provider_config()
    provider = raw_config.get("provider")
    if model_provider not in {"unset", "unknown"}:
        provider = model_provider
    if not isinstance(provider, str) or not provider:
        provider = "openai" if current_provider is not None else model_provider
    if provider == "openai":
        return write_provider_config("openai", current_provider)
    return write_provider_config(provider, current_provider)


def provider_display(provider: str, current_provider: str | None) -> str:
    if provider == "openai":
        return current_provider if current_provider is not None else "openai"
    if provider in {"unset", "unknown"}:
        return t("common.unset") if provider == "unset" else t("common.unknown")
    return provider


def backup_dir_for(keyword: str) -> Path:
    return AUTH_BACKUP_DIR / keyword


def backup_file(keyword: str) -> Path:
    backup_dir = backup_dir_for(keyword)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = backup_dir / f"auth-{keyword}.backup.{timestamp}.json"
    if not base.exists():
        return base
    suffix = 1
    while True:
        candidate = backup_dir / f"auth-{keyword}.backup.{timestamp}-{suffix:02d}.json"
        if not candidate.exists():
            return candidate
        suffix += 1


def prune_backups(keyword: str) -> None:
    backup_dir = backup_dir_for(keyword)
    backups = sorted(
        backup_dir.glob("auth-*.backup.*.json"),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    for path in backups[BACKUPS_PER_PROVIDER:]:
        path.unlink()


def backup_count(keyword: str) -> int:
    return len(list(backup_dir_for(keyword).glob("auth-*.backup.*.json")))


def save_active_auth(current_provider: str) -> None:
    if not current_provider:
        raise AuthError(t("auth.persist_without_keyword"))
    if current_provider in numbered_accounts() and read_auth_account(current_provider)["auth_type"] == "api":
        return
    if not ACTIVE_AUTH_FILE.exists():
        raise AuthError(t("auth.active_file_missing", path=ACTIVE_AUTH_FILE))

    current_auth_file = auth_file(current_provider)
    if is_api_auth(read_auth_payload(ACTIVE_AUTH_FILE)):
        return
    if current_auth_file.exists():
        shutil.copy2(current_auth_file, backup_file(current_provider))
        prune_backups(current_provider)
    else:
        current_auth_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ACTIVE_AUTH_FILE, current_auth_file)


def switch_auth(target_provider: str) -> None:
    target_auth_file = auth_file(target_provider)
    account = read_auth_account(target_provider)
    api = account["auth_type"] == "api"
    active_payload = api_auth_payload(account["meta"]) if api else read_auth_payload(target_auth_file)
    provider = api_provider_id(target_provider) if api else "openai"
    current_provider = read_current_provider()
    auth_type_changed = (
        is_api_auth(read_auth_payload(ACTIVE_AUTH_FILE)) != api
        if ACTIVE_AUTH_FILE.exists() else
        current_provider in numbered_accounts() and (read_auth_account(current_provider)["auth_type"] == "api") != api
    )
    with preserve_auth_state():
        if current_provider is not None and auth_file(current_provider).exists() and ACTIVE_AUTH_FILE.exists():
            save_active_auth(current_provider)
        if not api:
            # Saving the current slot may have persisted a token refreshed by Codex.
            active_payload = read_auth_payload(target_auth_file)
        set_model_provider(provider, account["meta"] if api else None)
        if api and account["meta"].get("env_key") is not None:
            ACTIVE_AUTH_FILE.unlink(missing_ok=True)
        else:
            atomic_write_json(ACTIVE_AUTH_FILE, active_payload, 0o600)
        write_provider_config(provider, target_provider)
    if auth_type_changed:
        print(f"\033[33m{t('auth.session_resume_warning')}\033[0m", file=sys.stderr)


def safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


def sanitized_auth_meta(payload: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in {"auth_mode", "last_refresh"}:
            continue
        scalar = safe_scalar(value)
        if scalar is not None:
            meta[key] = scalar
    tokens = payload.get("tokens")
    if isinstance(tokens, dict):
        meta["token_meta"] = {
            "access_token_present": isinstance(tokens.get("access_token"), str) and bool(tokens.get("access_token")),
            "refresh_token_present": isinstance(tokens.get("refresh_token"), str) and bool(tokens.get("refresh_token")),
            "id_token_present": isinstance(tokens.get("id_token"), str) and bool(tokens.get("id_token")),
            "token_keys": sorted(str(key) for key in tokens.keys()),
        }
    return meta


def account_meta(
    keyword: str,
    path: Path,
    current_provider: str | None,
    current_config: dict[str, Any],
) -> dict[str, Any]:
    stat = path.stat()
    payload = read_auth_payload(path)
    is_current = keyword == current_provider
    api = is_api_auth(payload)
    provider = api_provider_id(keyword) if api else "openai"
    is_active_provider = current_config.get("provider") == provider and is_current
    provider_number = auth_keyword_number(keyword)
    return {
        "provider": provider,
        "auth_type": "api" if api else "chatgpt",
        "auth_keyword": keyword,
        "provider_number": provider_number,
        "label": keyword,
        "path": str(path),
        "backup_dir": str(backup_dir_for(keyword)),
        "is_current": is_current,
        "is_active_provider": is_active_provider,
        "mtime": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        "size_bytes": stat.st_size,
        "backup_count": backup_count(keyword),
        "meta": sanitized_auth_meta(payload),
    }


def build_registry() -> dict[str, Any]:
    ensure_runtime_layout(PATHS)
    configs = discover_auth_files()
    current_provider = read_current_provider()
    model_provider = read_model_provider()
    current_config = normalize_provider_from_config(model_provider, current_provider)
    api_accounts = registry_api_accounts()
    registered_api = {a["auth_keyword"] for a in api_accounts}
    accounts = [
        account_meta(keyword, path, current_provider, current_config)
        for keyword, path in configs if keyword not in registered_api
    ]
    for account in api_accounts:
        keyword = account["auth_keyword"]
        account["provider"] = api_provider_id(keyword)
        account["is_current"] = keyword == current_provider
        account["is_active_provider"] = account["is_current"] and model_provider == api_provider_id(keyword)
        accounts.append(account)
    numbers = numbered_accounts()
    for account in accounts:
        account["provider_number"] = numbers[account["auth_keyword"]]
    accounts.sort(key=lambda account: account["provider_number"])
    return {
        "updated_at": now_iso(),
        "repo_dir": str(REPO_DIR),
        "codex_dir": str(CODEX_DIR),
        "runtime_dir": str(PATHS.runtime_dir),
        "registry_path": str(REGISTRY_FILE),
        "current_provider_path": str(CURRENT_PROVIDER_FILE),
        "auth_list_dir": str(AUTH_LIST_DIR),
        "auth_backup_dir": str(AUTH_BACKUP_DIR),
        "log_dir": str(PATHS.log_dir),
        "settings_dir": str(PATHS.settings_dir),
        "active_auth_path": str(ACTIVE_AUTH_FILE),
        "config_path": str(CONFIG_FILE),
        "model_provider": model_provider,
        "current_provider": current_config,
        "current_provider_keyword": current_provider,
        "current_provider_number": numbers.get(current_provider),
        "current_service": current_config.get("service") or provider_display(model_provider, current_provider),
        "accounts": accounts,
    }


def sync_registry() -> dict[str, Any]:
    registry = build_registry()
    atomic_write_json(REGISTRY_FILE, registry, 0o600)
    for account in registry["accounts"]:
        account.get("meta", {}).pop("api_key", None)
    return registry


def print_registry_summary(registry: dict[str, Any]) -> None:
    print(t("auth.current_service", service=registry["current_service"]))
    print(t("auth.provider_path", path=registry["current_provider_path"]))
    print(t("auth.registry_path", path=registry["registry_path"]))
    accounts = registry.get("accounts", [])
    if not accounts:
        print(t("auth.no_accounts"))
        return
    for account in accounts:
        marker = "*" if account.get("is_active_provider") else ("+" if account.get("is_current") else " ")
        refresh = account.get("meta", {}).get("last_refresh") or "-"
        print(
            t(
                "auth.account_summary",
                marker=marker,
                label=f'{account["provider_number"]}. {account["label"]}',
                refresh=refresh,
                backups=account.get("backup_count", 0),
            )
        )


def print_provider_change(before: dict[str, Any], after: dict[str, Any]) -> None:
    before_service = before.get("current_service", t("common.unknown"))
    after_service = after.get("current_service", t("common.unknown"))
    if before_service == after_service:
        print(t("auth.current_service", service=after_service))
    else:
        print(t("auth.service_changed", before=before_service, after=after_service))
    print(t("auth.provider_path", path=after["current_provider_path"]))
    print(t("auth.registry_path", path=after["registry_path"]))


def command_for_pid(pid: int) -> list[str]:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def is_codex_process(args: list[str]) -> bool:
    if not args:
        return False
    command = Path(args[0]).name.lower()
    if command in {"codextop", "codextop-check", "codextop-auth", "codextop-sampler"}:
        return False
    return command.startswith("codex")


def running_codex_processes() -> list[dict[str, Any]]:
    proc = Path("/proc")
    if not proc.exists():
        return []
    current_pid = os.getpid()
    current_uid = os.getuid()

    processes: list[dict[str, Any]] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue

        # Only inspect processes owned by the current Linux user.
        try:
            if entry.stat().st_uid != current_uid:
                continue
        except OSError:
            # The process may have exited during iteration.
            continue

        pid = int(entry.name)
        if pid == current_pid:
            continue
        args = command_for_pid(pid)
        if is_codex_process(args):
            processes.append({"pid": pid, "cmd": " ".join(args)})
    return sorted(processes, key=lambda item: item["pid"])


def assert_no_running_codex_processes() -> None:
    processes = running_codex_processes()
    if not processes:
        return
    rendered = "\n".join(f"【pid={item['pid']}】 {item['cmd']}" for item in processes)
    raise AuthError(t("auth.switch_refused_running", processes=rendered))


def process_terminal(pid: int) -> tuple[int, int]:
    # /proc works across sessions; tcgetpgrp rejects another pane's controlling TTY.
    fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    device = int(fields[4]) & 0xffffffff
    return os.makedev((device >> 8) & 0xfff, (device & 0xff) | ((device >> 12) & 0xfff00)), int(fields[5])


@contextmanager
def restore_codex_mouse_modes(processes: list[dict[str, Any]]):
    terminals: dict[int, tuple[int, int]] = {}
    try:
        for process in processes:
            try:
                terminal, foreground = process_terminal(process["pid"])
                if terminal == 0 or foreground != os.getpgid(process["pid"]):
                    continue
            except (OSError, ValueError, IndexError):
                continue
            if terminal in terminals:
                continue
            for number in (0, 1, 2):
                fd = None
                try:
                    path = os.readlink(f"/proc/{process['pid']}/fd/{number}")
                    if not re.fullmatch(r"/dev/(?:pts/\d+|tty\d+)", path):
                        continue
                    fd = os.open(path, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
                    if os.isatty(fd) and os.fstat(fd).st_rdev == terminal:
                        terminals[terminal] = (fd, foreground)
                        fd = None
                        break
                except OSError:
                    pass
                finally:
                    if fd is not None:
                        os.close(fd)
        yield
        foreground_groups = {}
        if terminals:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if entry.stat().st_uid == os.getuid():
                        terminal, foreground = process_terminal(int(entry.name))
                        if terminal in terminals:
                            foreground_groups[terminal] = foreground
                except (OSError, ValueError, IndexError):
                    pass
        for terminal, (fd, group) in terminals.items():
            foreground = foreground_groups.get(terminal)
            if foreground is None:
                continue
            if foreground != group:
                args = command_for_pid(foreground)
                # The shell may have reclaimed the pane; do not reset a new foreground app.
                if not args or Path(args[0]).name.lstrip("-") not in {
                    "bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "csh",
                }:
                    continue
            try:
                os.write(
                    fd,
                    b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1004l"
                    b"\x1b[?1005l\x1b[?1006l\x1b[?1015l\x1b[?1016l",
                )
            except OSError:
                # A closed pane must not prevent an otherwise successful account change.
                pass
    finally:
        for fd, _ in terminals.values():
            os.close(fd)


def kill_running_codex_processes() -> None:
    processes = running_codex_processes()
    if not processes:
        return
    with restore_codex_mouse_modes(processes):
        stop_codex_processes()


def stop_codex_processes() -> None:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(CODEX_DIR)
    command = ["codex", "app-server", "daemon", "stop"]
    try:
        supported = subprocess.run(
            [*command, "--help"], env=env, capture_output=True, text=True, timeout=5, check=False,
        )
        if supported.returncode == 0:
            subprocess.run(command, env=env, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        # Older CLIs and independent sessions still support normal OS termination.
        pass
    for process in running_codex_processes():
        try:
            os.kill(process["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while running_codex_processes():
        if time.monotonic() >= deadline:
            assert_no_running_codex_processes()
            return
        time.sleep(0.05)


def prepare_auth_change(force: bool) -> None:
    if force:
        kill_running_codex_processes()
    else:
        assert_no_running_codex_processes()


def command_kill(as_json: bool) -> int:
    kill_running_codex_processes()
    if as_json:
        print(json.dumps({"stopped": True}))
    else:
        print(t("auth.codex_stopped"))
    return 0


def run_device_login(login_home: Path) -> str:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(login_home)
    process: subprocess.Popen[Any] | None = None
    try:
        process = subprocess.Popen(
            ["codex", "login", "--device-auth", "-c", FILE_AUTH_CONFIG],
            env=env,
        )
        return_code = process.wait()
    except FileNotFoundError as exc:
        raise AuthError(t("auth.codex_not_found")) from exc
    except KeyboardInterrupt as exc:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise AuthError(t("auth.login_cancelled")) from exc
    if return_code != 0:
        raise AuthError(t("auth.login_failed", code=return_code))

    status = subprocess.run(
        ["codex", "login", "-c", FILE_AUTH_CONFIG, "status"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    status_text = (status.stdout or status.stderr).strip()
    if status.returncode != 0 or not status_text:
        raise AuthError(t("auth.verify_login_failed"))
    return status_text


def validate_new_auth(path: Path, keyword: str) -> dict[str, Any]:
    from quota.check_codex_quota import collect_account, load_auth_credentials

    try:
        _access_token, account_id = load_auth_credentials(path)
    except Exception as exc:
        raise AuthError(t("auth.verify_account_info_detail", error=exc)) from exc
    report = collect_account(keyword, path, keyword, "Asia/Shanghai")
    if report.get("error"):
        raise AuthError(t("auth.verify_account_status_detail", error=report["error"]))
    if not any((account_id, report.get("email"), report.get("plan_type"))):
        raise AuthError(t("auth.verify_account_info_failed"))
    if not isinstance(report.get("allowed"), bool):
        raise AuthError(t("auth.verify_account_status_failed"))
    return report


def request_sampler_refresh() -> None:
    from quota.codex_quota_sampler import write_control

    write_control(PATHS.log_dir / "sampler_control.json", sample_now=True, all_auth=True)


def command_current(as_json: bool) -> int:
    registry = sync_registry()
    if as_json:
        print(json.dumps(registry, ensure_ascii=False, indent=2))
    else:
        print_registry_summary(registry)
    return 0


def command_list(as_json: bool) -> int:
    registry = sync_registry()
    if as_json:
        print(json.dumps(registry.get("accounts", []), ensure_ascii=False, indent=2))
        return 0
    print_registry_summary(registry)
    return 0


def command_sync(as_json: bool) -> int:
    registry = sync_registry()
    if as_json:
        print(json.dumps(registry, ensure_ascii=False, indent=2))
    else:
        print(t("auth.provider_updated", path=registry["current_provider_path"]))
        print(t("auth.registry_updated", path=registry["registry_path"]))
    return 0


def command_new(target: str, as_json: bool, force: bool = False) -> int:
    keyword = normalize_new_auth_target(target)
    target_auth_file = auth_file(keyword)
    assert_available_target(keyword)
    prepare_auth_change(force)

    output = sys.stderr if as_json else sys.stdout
    print(
        t("auth.login_intro", provider=keyword),
        file=output,
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix=".codexauth-login-", dir=PATHS.runtime_dir) as temp_dir:
        login_home = Path(temp_dir)
        if CONFIG_FILE.exists():
            shutil.copy2(CONFIG_FILE, login_home / "config.toml")
        run_device_login(login_home)
        login_auth_file = login_home / "auth.json"
        validate_new_auth(login_auth_file, keyword)
        shutil.copy2(login_auth_file, target_auth_file)
        target_auth_file.chmod(0o600)

    switch_auth(keyword)
    registry = sync_registry()
    request_sampler_refresh()
    if as_json:
        print(json.dumps(registry, ensure_ascii=False, indent=2))
    else:
        print()
        print(t("auth.provider_added", provider=keyword))
        print(t("auth.validation_passed"))
        print(t("auth.provider_path", path=registry["current_provider_path"]))
        print(t("auth.registry_path", path=registry["registry_path"]))
    return 0


def assert_available_target(keyword: str) -> None:
    if keyword.casefold() in {name.casefold() for name in numbered_accounts()}:
        raise AuthError(t("auth.provider_exists", provider=keyword))


def command_new_api(target: str, as_json: bool, force: bool = False) -> int:
    keyword = normalize_new_auth_target(target)
    assert_available_target(keyword)
    prepare_auth_change(force)
    meta = prompt_api_source(keyword)
    api_auth_payload(meta)
    meta["base_url"] = validate_api_base_url(meta.get("base_url", DEFAULT_API_BASE_URL))
    assert_no_running_codex_processes()
    with preserve_auth_state():
        assert_available_target(keyword)
        write_api_account(keyword, {"auth_keyword": keyword, "label": keyword,
                                   "provider": api_provider_id(keyword), "auth_type": "api",
                                   "path": None, "backup_dir": None, "backup_count": 0, "meta": meta})
        switch_auth(keyword)
    registry = sync_registry()
    request_sampler_refresh()
    if as_json:
        print(json.dumps(registry, ensure_ascii=False, indent=2))
    else:
        print(t("auth.provider_added", provider=keyword))
    return 0


def command_rename(target: str, new_target: str, as_json: bool, force: bool = False) -> int:
    keyword = resolve_auth_target(target)
    new_keyword = normalize_new_auth_target(new_target)
    assert_available_target(new_keyword)
    if backup_dir_for(new_keyword).exists():
        raise AuthError(t("auth.provider_exists", provider=new_keyword))
    prepare_auth_change(force)
    old_path, new_path = auth_file(keyword), auth_file(new_keyword)
    current = read_current_provider()
    provider = read_model_provider()
    account = read_auth_account(keyword)
    api = account["auth_type"] == "api"
    with preserve_auth_state(old_path, new_path):
        if api:
            account.update(auth_keyword=new_keyword, label=new_keyword, provider=api_provider_id(new_keyword))
            write_api_account(keyword, account)
        else:
            old_path.rename(new_path)
        if api and current == keyword and provider == api_provider_id(keyword):
            set_model_provider(api_provider_id(new_keyword), account["meta"], remove=keyword)
            provider = api_provider_id(new_keyword)
        elif api:
            set_model_provider(provider, remove=keyword)
        if current == keyword:
            write_provider_config(provider, new_keyword)
        if backup_dir_for(keyword).exists():
            backup_dir_for(keyword).rename(backup_dir_for(new_keyword))
    registry = sync_registry()
    request_sampler_refresh()
    if as_json:
        print(json.dumps(registry, ensure_ascii=False, indent=2))
    else:
        print(t("auth.provider_renamed", before=keyword, after=new_keyword))
    return 0


def command_refresh(target: str | None, as_json: bool, force: bool = False) -> int:
    keyword = resolve_existing_auth_target(target)
    target_auth_file = auth_file(keyword)
    if read_auth_account(keyword)["auth_type"] == "api":
        raise AuthError(t("auth.api_refresh_unsupported"))
    existing_email = auth_email(target_auth_file)
    prepare_auth_change(force)

    output = sys.stderr if as_json else sys.stdout
    print(t("auth.refresh_intro", provider=keyword), file=output, flush=True)
    with tempfile.TemporaryDirectory(prefix=".codexauth-refresh-", dir=PATHS.runtime_dir) as temp_dir:
        login_home = Path(temp_dir)
        if CONFIG_FILE.exists():
            shutil.copy2(CONFIG_FILE, login_home / "config.toml")
        run_device_login(login_home)
        login_auth_file = login_home / "auth.json"
        report = validate_new_auth(login_auth_file, keyword)
        reported_email = normalize_email(report.get("email"))
        token_email = normalize_email(auth_email(login_auth_file))
        if reported_email is None or reported_email != token_email:
            raise AuthError(t("auth.refresh_email_unverified", provider=keyword))
        if normalize_email(existing_email) != reported_email:
            raise AuthError(
                t(
                    "auth.refresh_email_mismatch",
                    provider=keyword,
                    expected=existing_email,
                    actual=report.get("email"),
                )
            )
        refreshed_payload = read_auth_payload(login_auth_file)

    shutil.copy2(target_auth_file, backup_file(keyword))
    prune_backups(keyword)
    atomic_write_json(target_auth_file, refreshed_payload, 0o600)
    if read_current_provider() == keyword and read_model_provider() == "openai":
        atomic_write_json(ACTIVE_AUTH_FILE, refreshed_payload, 0o600)

    registry = sync_registry()
    request_sampler_refresh()
    if as_json:
        print(json.dumps(registry, ensure_ascii=False, indent=2))
    else:
        print()
        print(t("auth.provider_refreshed", provider=keyword))
        print(t("auth.refresh_validation_passed"))
        print(t("auth.registry_path", path=registry["registry_path"]))
    return 0


def confirm_provider_deletion(keyword: str) -> None:
    print(t("auth.delete_confirm", provider=keyword), end="", file=sys.stderr, flush=True)
    try:
        confirmation = sys.stdin.readline()
    except KeyboardInterrupt as exc:
        raise AuthError(t("auth.delete_cancelled")) from exc
    if not confirmation:
        raise AuthError(t("auth.delete_cancelled"))
    if confirmation.strip() != keyword:
        raise AuthError(t("auth.delete_confirmation_mismatch", provider=keyword))


def command_delete(target: str | None, as_json: bool, force: bool = False) -> int:
    keyword = resolve_existing_auth_target(target)
    target_auth_file = auth_file(keyword)
    prepare_auth_change(force)
    confirm_provider_deletion(keyword)
    api = read_auth_account(keyword)["auth_type"] == "api"

    with preserve_auth_state(target_auth_file):
        current_provider = read_current_provider()
        if current_provider == keyword:
            replacement = next((name for name in numbered_accounts() if name != keyword), None)
            current_config = read_provider_config()
            configured_provider = current_config.get("provider")
            if not isinstance(configured_provider, str) or configured_provider in {"", "unset", "unknown"}:
                configured_provider = read_model_provider()
            if configured_provider in {"unset", "unknown"}:
                configured_provider = "openai"
            if replacement is None:
                ACTIVE_AUTH_FILE.unlink(missing_ok=True)
                if configured_provider == api_provider_id(keyword):
                    set_model_provider("openai", remove=keyword)
                    configured_provider = "openai"
                write_provider_config(configured_provider, None)
            else:
                switch_auth(replacement)

        if api:
            set_model_provider(read_model_provider(), remove=keyword)
            write_api_account(keyword, None)
        else:
            target_auth_file.unlink()

    provider_backup_dir = backup_dir_for(keyword)
    if provider_backup_dir.exists():
        shutil.rmtree(provider_backup_dir)

    registry = sync_registry()
    request_sampler_refresh()
    if as_json:
        print(json.dumps(registry, ensure_ascii=False, indent=2))
    else:
        print(t("auth.provider_deleted", provider=keyword))
        print(t("auth.current_service", service=registry["current_service"]))
        print(t("auth.registry_path", path=registry["registry_path"]))
    return 0


def command_switch(target: str, as_json: bool, force: bool = False) -> int:
    value = target.strip().lower()
    target_provider = None
    if value != "openai":
        target_provider = resolve_auth_target(value)
    current_provider = read_current_provider()
    same_target = current_provider is not None and (
        target_provider == current_provider
        or (value == "openai" and read_model_provider() == "openai")
    )
    if force or not same_target:
        prepare_auth_change(force)
    before = sync_registry()

    if same_target:
        if read_auth_account(current_provider)["auth_type"] == "api":
            switch_auth(current_provider)
        else:
            save_active_auth(current_provider)
    elif value == "openai":
        if current_provider in numbered_accounts() and read_auth_account(current_provider)["auth_type"] == "api":
            current_provider = next(
                (name for name, _ in discover_auth_files()
                 if read_auth_account(name)["auth_type"] == "chatgpt"), None
            )
            if current_provider is None:
                raise AuthError(t("auth.no_chatgpt_accounts"))
        if current_provider is not None and auth_file(current_provider).exists():
            switch_auth(current_provider)
        else:
            set_model_provider("openai")
            write_provider_config("openai", current_provider)
    else:
        switch_auth(target_provider or value)

    after = sync_registry()
    request_sampler_refresh()
    if as_json:
        print(json.dumps(after, ensure_ascii=False, indent=2))
    else:
        print_provider_change(before, after)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="CODEXAUTH", description=t("auth.arg.description"),
        usage="%(prog)s [--new/--refresh/--delete/--rename/...] [PROVIDER ...] [-f] [--transfer [-d DURATION | -s SESSION_ID]] [--json]",
    )
    parser.add_argument("-f", "--force", action="store_true", help=t("auth.arg.force"))
    parser.add_argument("--json", action="store_true", help=t("auth.arg.json"))
    parser.add_argument("--kill", action="store_true", help=t("auth.arg.kill"))
    parser.add_argument("--transfer", action="store_true", help=t("auth.arg.transfer"))
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("-d", "--duration", metavar="DURATION", help=t("auth.arg.duration"))
    selection.add_argument("-s", "--session", dest="session_id", metavar="SESSION_ID", help=t("auth.arg.session"))
    parser.add_argument("--new", metavar="TARGET", help=t("auth.arg.new"))
    parser.add_argument("--api", action="store_true", help=t("auth.arg.api"))
    parser.add_argument("--rename", nargs=2, metavar=("PROVIDER", "NEW_NAME"), help=t("auth.arg.rename"))
    parser.add_argument(
        "--refresh",
        nargs="?",
        const="",
        metavar="PROVIDER",
        help=t("auth.arg.refresh"),
    )
    parser.add_argument(
        "--delete",
        dest="delete_target",
        nargs="?",
        const="",
        metavar="PROVIDER",
        help=t("auth.arg.delete"),
    )
    parser.add_argument("command", nargs="?", help=t("auth.arg.command"))
    parser.add_argument("target", nargs="?", help=t("auth.arg.target"))
    return parser


def dispatch(argv: list[str] | None = None) -> int:
    activate_saved_language(PATHS.log_dir / "codextop_state.json")
    args = build_parser().parse_args(argv)

    try:
        action_count = sum(
            option is not None
            for option in (args.new, args.refresh, args.delete_target, args.rename)
        )
        action_count += int(args.kill)
        if args.force and not action_count and args.command in {None, "current", "list", "sync"}:
            raise AuthError(t("auth.force_requires_switch"))
        if action_count > 1:
            raise AuthError(t("auth.action_conflict"))
        if action_count and (args.command is not None or args.target is not None):
            raise AuthError(t("auth.action_conflict"))
        if args.api and args.new is None:
            raise AuthError(t("auth.api_requires_new"))
        if args.kill:
            return command_kill(args.json)
        if args.new is not None:
            if args.api:
                return command_new_api(args.new, args.json, args.force)
            return command_new(args.new, args.json, args.force)
        if args.rename is not None:
            return command_rename(*args.rename, args.json, args.force)
        if args.refresh is not None:
            return command_refresh(args.refresh or None, args.json, args.force)
        if args.delete_target is not None:
            return command_delete(args.delete_target or None, args.json, args.force)
        if not args.command:
            return command_current(args.json)
        if args.command == "current":
            return command_current(args.json)
        if args.command == "list":
            return command_list(args.json)
        if args.command == "sync":
            return command_sync(args.json)
        if args.command == "switch":
            if not args.target:
                raise AuthError(t("auth.missing_switch_target"))
            return command_switch(args.target, args.json, args.force)

        if args.target is not None:
            raise AuthError(t("auth.unexpected_argument", argument=args.target))
        return command_switch(args.command, args.json, args.force)
    except AuthError as exc:
        from rich.console import Console
        from rich.text import Text

        message = Text(t("common.error_message", error=exc))
        message.stylize("red", 0, len(message.plain.split("\n", 1)[0]))
        Console(stderr=True).print(message, soft_wrap=True)
        return 1
    except OSError as exc:
        print(t("common.error_message", error=t("auth.io_error", error=exc.strerror)), file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    activate_saved_language(PATHS.log_dir / "codextop_state.json")
    args = build_parser().parse_args(argv)
    if not args.transfer:
        if args.duration is not None or args.session_id is not None:
            print(t("common.error_message", error=t("auth.transfer_required")), file=sys.stderr)
            return 1
        return dispatch(argv)
    from auth.session_provider import SessionProviderUpdate, session_selector

    try:
        if args.kill or args.command in {"list", "current", "sync"}:
            raise AuthError(t("auth.sessions_conflict"))
        standalone = args.command is None and all(
            value is None for value in (args.new, args.refresh, args.delete_target, args.rename)
        )
        if standalone and (args.api or args.target):
            raise AuthError(t("auth.sessions_conflict"))
        original_provider = read_model_provider()
        selector = args.session_id or args.duration or "1h"
        session_id, _cutoff = session_selector(selector)
        if (args.session_id is not None and session_id is None) or (args.duration is not None and session_id is not None):
            raise AuthError(t("auth.transfer_selector_invalid"))
        SessionProviderUpdate(
            CODEX_DIR, selector,
            source=original_provider if not standalone and session_id is None else None,
        )
        # Same-account sync normally skips this check; metadata migration cannot.
        if not args.force:
            assert_no_running_codex_processes()
        output = io.StringIO()
        with preserve_auth_state(), redirect_stdout(output):
            if standalone:
                prepare_auth_change(args.force)
                result = 0
            else:
                result = dispatch(argv)
            if result != 0:
                return result
            provider = read_model_provider()
            if provider in {"unset", "unknown"}:
                raise AuthError(t("auth.sessions_no_provider"))
            update = SessionProviderUpdate(
                CODEX_DIR, selector,
                source=original_provider if not standalone and session_id is None else None,
            )
            summary = (
                update.apply(provider) if standalone or session_id is not None or provider != original_provider
                else {"updated": 0, "provider": provider, "backup": None}
            )
        if args.json:
            payload = json.loads(output.getvalue()) if output.getvalue().strip() else {}
            payload["session_provider_update"] = summary
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(output.getvalue(), end="")
            print(t("auth.sessions_updated", count=summary["updated"], provider=provider))
            if summary["backup"]:
                print(t("auth.sessions_backup", path=summary["backup"]))
        return 0
    except (AuthError, ValueError, OSError, sqlite3.Error) as exc:
        print(t("common.error_message", error=str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
