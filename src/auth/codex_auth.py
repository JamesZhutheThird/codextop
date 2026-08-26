#!/usr/bin/env python3
"""Manage CodexTOP auth slots, provider state, and provider switching."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.paths import (
    CodexTopPaths,
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
from core.i18n import t


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
MODEL_PROVIDER_RE = re.compile(r'(?m)^(\s*model_provider\s*=\s*)"([^"]+)"(\s*)$')
KNOWN_NON_OPENAI_PROVIDERS = {"third-party-api","api"}
FILE_AUTH_CONFIG = 'cli_auth_credentials_store="file"'


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
        if keyword is not None:
            configs.append((keyword, path))
    return sorted(configs, key=lambda item: auth_sort_key(item[0]))


def auth_file_map() -> dict[str, Path]:
    return {keyword: path for keyword, path in discover_auth_files()}


def new_auth_hint(target: str) -> str:
    return t("auth.new_hint", target=target)


def resolve_auth_target(target: str) -> str:
    value = normalize_auth_keyword(target)
    if value is None:
        raise AuthError(t("auth.missing_target"))
    configs = auth_file_map()
    if value.isdigit():
        for keyword in (value, f"openai-{value}"):
            if keyword in configs:
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
    if value.lower() in {"openai", *KNOWN_NON_OPENAI_PROVIDERS}:
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
    if current_provider is None or not auth_file(current_provider).exists():
        raise AuthError(t("auth.current_provider_missing"))
    return current_provider


def write_provider_config(provider: str, auth_keyword: str | None = None) -> dict[str, Any]:
    provider = "third-party-api" if provider == "api" else provider
    payload = provider_config_payload(provider, auth_keyword)
    payload["codex_dir"] = str(CODEX_DIR)
    payload["auth_list_dir"] = str(AUTH_LIST_DIR)
    payload["config_path"] = str(CONFIG_FILE)
    atomic_write_json(CURRENT_PROVIDER_FILE, payload, 0o600)
    return payload


def read_model_provider() -> str:
    if not CONFIG_FILE.exists():
        return "unset"
    content = CONFIG_FILE.read_text(encoding="utf-8")
    match = MODEL_PROVIDER_RE.search(content)
    if not match:
        return "unknown"
    return match.group(2).strip() or "unknown"


def set_model_provider(provider: str) -> None:
    if CONFIG_FILE.exists():
        old_mode = CONFIG_FILE.stat().st_mode
        content = CONFIG_FILE.read_text(encoding="utf-8")
    else:
        old_mode = 0o600
        content = ""

    line = f'model_provider = "{provider}"'
    if MODEL_PROVIDER_RE.search(content):
        content = MODEL_PROVIDER_RE.sub(rf'\1"{provider}"\3', content, count=1)
    elif content:
        content = line + "\n" + content
    else:
        content = line + "\n"

    atomic_write_text(CONFIG_FILE, content, old_mode)


def normalize_provider_from_config(model_provider: str, current_provider: str | None) -> dict[str, Any]:
    raw_config = read_provider_config()
    provider = raw_config.get("provider")
    if model_provider not in {"unset", "unknown"}:
        provider = model_provider
    if not isinstance(provider, str) or not provider:
        provider = "openai" if current_provider is not None else model_provider
    provider = "third-party-api" if provider == "api" else provider
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
    if not ACTIVE_AUTH_FILE.exists():
        raise AuthError(t("auth.active_file_missing", path=ACTIVE_AUTH_FILE))

    current_auth_file = auth_file(current_provider)
    if current_auth_file.exists():
        shutil.copy2(current_auth_file, backup_file(current_provider))
        prune_backups(current_provider)
    else:
        current_auth_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ACTIVE_AUTH_FILE, current_auth_file)


def switch_openai_auth(target_provider: str) -> None:
    target_auth_file = auth_file(target_provider)
    if not target_auth_file.exists():
        raise AuthError(
            t("auth.target_path_not_found", path=target_auth_file)
            + new_auth_hint(target_provider)
        )

    current_provider = read_current_provider()
    if current_provider is not None and auth_file(current_provider).exists():
        save_active_auth(current_provider)

    shutil.copy2(target_auth_file, ACTIVE_AUTH_FILE)
    set_model_provider("openai")
    write_provider_config("openai", target_provider)


def safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


def sanitized_auth_meta(payload: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "tokens":
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
    is_active_provider = current_config.get("provider") == "openai" and is_current
    provider_number = auth_keyword_number(keyword)
    return {
        "provider": "openai",
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
    accounts = [
        account_meta(keyword, path, current_provider, current_config)
        for keyword, path in configs
    ]
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
        "current_provider_number": auth_keyword_number(current_provider),
        "current_service": current_config.get("service") or provider_display(model_provider, current_provider),
        "accounts": accounts,
    }


def sync_registry() -> dict[str, Any]:
    registry = build_registry()
    atomic_write_json(REGISTRY_FILE, registry, 0o600)
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
                label=account["label"],
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
    rendered = "; ".join(f"pid={item['pid']} {item['cmd']}" for item in processes[:5])
    if len(processes) > 5:
        rendered += t("auth.more_processes", count=len(processes) - 5)
    raise AuthError(t("auth.switch_refused_running", processes=rendered))


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


def command_new(target: str, as_json: bool) -> int:
    keyword = normalize_new_auth_target(target)
    target_auth_file = auth_file(keyword)
    if target_auth_file.exists():
        raise AuthError(t("auth.provider_exists", provider=keyword))
    assert_no_running_codex_processes()

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

    switch_openai_auth(keyword)
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


def command_refresh(target: str | None, as_json: bool) -> int:
    keyword = resolve_existing_auth_target(target)
    target_auth_file = auth_file(keyword)
    existing_email = auth_email(target_auth_file)
    assert_no_running_codex_processes()

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
    if read_current_provider() == keyword:
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


def command_delete(target: str | None, as_json: bool) -> int:
    keyword = resolve_existing_auth_target(target)
    target_auth_file = auth_file(keyword)
    assert_no_running_codex_processes()
    confirm_provider_deletion(keyword)

    current_provider = read_current_provider()
    if current_provider == keyword:
        replacements = [item for item in discover_auth_files() if item[0] != keyword]
        replacement = replacements[0][0] if replacements else None
        current_config = read_provider_config()
        configured_provider = current_config.get("provider")
        if not isinstance(configured_provider, str) or configured_provider in {"", "unset", "unknown"}:
            configured_provider = read_model_provider()
        if configured_provider in {"unset", "unknown"}:
            configured_provider = "openai"
        if replacement is None:
            ACTIVE_AUTH_FILE.unlink(missing_ok=True)
        else:
            atomic_write_json(ACTIVE_AUTH_FILE, read_auth_payload(auth_file(replacement)), 0o600)
        write_provider_config(configured_provider, replacement)

    provider_backup_dir = backup_dir_for(keyword)
    if provider_backup_dir.exists():
        shutil.rmtree(provider_backup_dir)
    target_auth_file.unlink()

    registry = sync_registry()
    request_sampler_refresh()
    if as_json:
        print(json.dumps(registry, ensure_ascii=False, indent=2))
    else:
        print(t("auth.provider_deleted", provider=keyword))
        print(t("auth.current_service", service=registry["current_service"]))
        print(t("auth.registry_path", path=registry["registry_path"]))
    return 0


def command_switch(target: str, as_json: bool) -> int:
    value = target.strip().lower()
    target_provider = None
    if value not in {"0", "openai", *KNOWN_NON_OPENAI_PROVIDERS}:
        target_provider = resolve_auth_target(value)
    assert_no_running_codex_processes()
    before = sync_registry()
    current_provider = read_current_provider()

    if value in {"0", *KNOWN_NON_OPENAI_PROVIDERS}:
        if current_provider is not None and ACTIVE_AUTH_FILE.exists() and auth_file(current_provider).exists():
            save_active_auth(current_provider)
        set_model_provider("third-party-api")
        write_provider_config("third-party-api", current_provider)
    elif value == "openai":
        if current_provider is not None and auth_file(current_provider).exists():
            switch_openai_auth(current_provider)
        else:
            set_model_provider("openai")
            write_provider_config("openai", current_provider)
    else:
        switch_openai_auth(target_provider or value)

    after = sync_registry()
    if as_json:
        print(json.dumps(after, ensure_ascii=False, indent=2))
    else:
        print_provider_change(before, after)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=t("auth.arg.description"))
    parser.add_argument("--json", action="store_true", help=t("auth.arg.json"))
    parser.add_argument("--new", metavar="TARGET", help=t("auth.arg.new"))
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        action_count = sum(
            option is not None
            for option in (args.new, args.refresh, args.delete_target)
        )
        if action_count > 1:
            raise AuthError(t("auth.action_conflict"))
        if action_count and (args.command is not None or args.target is not None):
            raise AuthError(t("auth.action_conflict"))
        if args.new is not None:
            return command_new(args.new, args.json)
        if args.refresh is not None:
            return command_refresh(args.refresh or None, args.json)
        if args.delete_target is not None:
            return command_delete(args.delete_target or None, args.json)
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
            return command_switch(args.target, args.json)

        if args.target is not None:
            raise AuthError(t("auth.unexpected_argument", argument=args.target))
        return command_switch(args.command, args.json)
    except AuthError as exc:
        print(t("common.error_message", error=exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
