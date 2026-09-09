from __future__ import annotations

import base64
import io
import json
import stat
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auth import codex_auth
from core.i18n import t


def jwt_with_claims(claims: dict[str, object]) -> str:
    def encode(payload: dict[str, object]) -> str:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    return f"{encode({'alg': 'none'})}.{encode(claims)}."


def auth_payload(email: str, marker: str) -> dict[str, object]:
    return {
        "tokens": {
            "id_token": jwt_with_claims({"email": email, "jti": marker}),
            "access_token": jwt_with_claims(
                {
                    "https://api.openai.com/profile": {"email": email},
                    "jti": marker,
                }
            ),
            "refresh_token": f"refresh-{marker}",
            "account_id": f"account-{marker}",
        }
    }


def path_globals(paths: object) -> dict[str, object]:
    return {
        "PATHS": paths,
        "CODEX_DIR": paths.codex_dir,
        "CONFIG_FILE": paths.config_file,
        "ACTIVE_AUTH_FILE": paths.active_auth_file,
        "AUTH_LIST_DIR": paths.auth_list_dir,
        "CURRENT_PROVIDER_FILE": paths.current_provider_file,
        "REGISTRY_FILE": paths.registry_file,
        "AUTH_BACKUP_DIR": paths.auth_backup_dir,
    }


class FakeProcEntry:
    def __init__(
        self,
        name: str,
        uid: int,
        *,
        stat_error: bool = False,
    ) -> None:
        self.name = name
        self._uid = uid
        self._stat_error = stat_error

    def stat(self) -> SimpleNamespace:
        if self._stat_error:
            raise OSError("process disappeared")
        return SimpleNamespace(st_uid=self._uid)


class CodexAuthProcessTests(unittest.TestCase):
    def test_running_codex_processes_only_checks_current_user(self) -> None:
        entries = [
            FakeProcEntry("101", 1000),
            FakeProcEntry("102", 2000),
            FakeProcEntry("103", 1000),
            FakeProcEntry("104", 1000, stat_error=True),
            FakeProcEntry("not-a-pid", 1000),
        ]

        commands = {
            101: ["codex", "--yolo"],
            103: ["bash"],
        }

        with (
            patch.object(codex_auth.Path, "exists", return_value=True),
            patch.object(codex_auth.Path, "iterdir", return_value=entries),
            patch.object(codex_auth.os, "getpid", return_value=999),
            patch.object(codex_auth.os, "getuid", return_value=1000),
            patch.object(
                codex_auth,
                "command_for_pid",
                side_effect=lambda pid: commands[pid],
            ) as command_for_pid,
        ):
            processes = codex_auth.running_codex_processes()

        self.assertEqual(
            processes,
            [{"pid": 101, "cmd": "codex --yolo"}],
        )
        self.assertEqual(
            command_for_pid.call_args_list,
            [call(101), call(103)],
        )


class CodexAuthNewProviderTests(unittest.TestCase):
    def test_new_target_normalizes_numbers_and_keeps_names(self) -> None:
        self.assertEqual(codex_auth.normalize_new_auth_target("4"), "openai-4")
        self.assertEqual(codex_auth.normalize_new_auth_target("openai-04"), "openai-4")
        self.assertEqual(codex_auth.normalize_new_auth_target("name_of_provider"), "name_of_provider")

    def test_missing_target_suggests_new_command(self) -> None:
        with patch.object(codex_auth, "auth_file_map", return_value={}):
            with self.assertRaisesRegex(codex_auth.AuthError, r"CODEXAUTH --new 4"):
                codex_auth.resolve_auth_target("4")
            with self.assertRaisesRegex(codex_auth.AuthError, r"CODEXAUTH --new work"):
                codex_auth.resolve_auth_target("work")

    def test_device_login_streams_child_output_and_checks_status(self) -> None:
        process = Mock()
        process.wait.return_value = 0
        status = CompletedProcess([], 0, "Logged in using ChatGPT\n", "")

        with (
            patch.object(codex_auth.subprocess, "Popen", return_value=process) as popen,
            patch.object(codex_auth.subprocess, "run", return_value=status) as run,
        ):
            result = codex_auth.run_device_login(Path("/tmp/login-home"))

        self.assertEqual(result, "Logged in using ChatGPT")
        command, = popen.call_args.args
        self.assertEqual(command[:3], ["codex", "login", "--device-auth"])
        self.assertNotIn("stdout", popen.call_args.kwargs)
        self.assertNotIn("stderr", popen.call_args.kwargs)
        self.assertEqual(popen.call_args.kwargs["env"]["CODEX_HOME"], "/tmp/login-home")
        self.assertEqual(run.call_args.args[0][-1], "status")

    def test_new_provider_is_validated_then_saved_switched_and_registered(self) -> None:
        old_payload = {
            "tokens": {
                "access_token": "old-token",
                "refresh_token": "old-refresh",
                "account_id": "old-account",
            }
        }
        new_payload = {
            "tokens": {
                "access_token": "new-token",
                "refresh_token": "new-refresh",
                "account_id": "new-account",
            }
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = codex_auth.ensure_runtime_layout(
                codex_auth.default_paths(Path(temp_dir) / ".codex"),
                migrate=False,
            )
            paths.active_auth_file.write_text(json.dumps(old_payload), encoding="utf-8")
            old_slot = paths.auth_list_dir / "auth-openai-1.json"
            old_slot.write_text(json.dumps(old_payload), encoding="utf-8")
            paths.config_file.write_text('model_provider = "openai"\n', encoding="utf-8")
            codex_auth.atomic_write_json(
                paths.current_provider_file,
                codex_auth.provider_config_payload("openai", "openai-1"),
            )

            def login(login_home: Path) -> str:
                self.assertTrue((login_home / "config.toml").exists())
                (login_home / "auth.json").write_text(json.dumps(new_payload), encoding="utf-8")
                return "Logged in using ChatGPT"

            path_globals = {
                "PATHS": paths,
                "CODEX_DIR": paths.codex_dir,
                "CONFIG_FILE": paths.config_file,
                "ACTIVE_AUTH_FILE": paths.active_auth_file,
                "AUTH_LIST_DIR": paths.auth_list_dir,
                "CURRENT_PROVIDER_FILE": paths.current_provider_file,
                "REGISTRY_FILE": paths.registry_file,
                "AUTH_BACKUP_DIR": paths.auth_backup_dir,
            }
            output = io.StringIO()
            with (
                patch.multiple(codex_auth, **path_globals),
                patch.object(codex_auth, "assert_no_running_codex_processes"),
                patch.object(codex_auth, "run_device_login", side_effect=login),
                patch.object(
                    codex_auth,
                    "validate_new_auth",
                    return_value={"email": "new@example.com", "plan_type": "plus", "allowed": True},
                ),
                patch.object(codex_auth, "request_sampler_refresh") as refresh,
                redirect_stdout(output),
            ):
                result = codex_auth.command_new("4", False)

            new_slot = paths.auth_list_dir / "auth-openai-4.json"
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(new_slot.read_text(encoding="utf-8")), new_payload)
            self.assertEqual(json.loads(paths.active_auth_file.read_text(encoding="utf-8")), new_payload)
            self.assertEqual(stat.S_IMODE(new_slot.stat().st_mode), 0o600)
            current = json.loads(paths.current_provider_file.read_text(encoding="utf-8"))
            self.assertEqual(current["auth_keyword"], "openai-4")
            registry = json.loads(paths.registry_file.read_text(encoding="utf-8"))
            self.assertEqual(
                [account["auth_keyword"] for account in registry["accounts"]],
                ["openai-1", "openai-4"],
            )
            self.assertIn(t("auth.login_intro", provider="openai-4").strip(), output.getvalue())
            refresh.assert_called_once_with()

    def test_main_routes_new_option(self) -> None:
        with patch.object(codex_auth, "command_new", return_value=0) as command_new:
            self.assertEqual(codex_auth.main(["--new", "4"]), 0)
        command_new.assert_called_once_with("4", False)


class CodexAuthProviderMutationTests(unittest.TestCase):
    def test_refresh_validates_matching_email_before_replacing_current_auth(self) -> None:
        old_payload = auth_payload("same@example.com", "old")
        new_payload = auth_payload("same@example.com", "new")

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = codex_auth.ensure_runtime_layout(
                codex_auth.default_paths(Path(temp_dir) / ".codex"),
                migrate=False,
            )
            paths.active_auth_file.write_text(json.dumps(old_payload), encoding="utf-8")
            slot = paths.auth_list_dir / "auth-openai-1.json"
            slot.write_text(json.dumps(old_payload), encoding="utf-8")
            paths.config_file.write_text('model_provider = "openai"\n', encoding="utf-8")
            codex_auth.atomic_write_json(
                paths.current_provider_file,
                codex_auth.provider_config_payload("openai", "openai-1"),
            )

            def login(login_home: Path) -> str:
                (login_home / "auth.json").write_text(json.dumps(new_payload), encoding="utf-8")
                return "Logged in using ChatGPT"

            output = io.StringIO()
            with (
                patch.multiple(codex_auth, **path_globals(paths)),
                patch.object(codex_auth, "assert_no_running_codex_processes"),
                patch.object(codex_auth, "run_device_login", side_effect=login),
                patch.object(
                    codex_auth,
                    "validate_new_auth",
                    return_value={"email": "same@example.com", "allowed": True},
                ),
                patch.object(codex_auth, "request_sampler_refresh") as refresh,
                redirect_stdout(output),
            ):
                result = codex_auth.command_refresh(None, False)

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(slot.read_text(encoding="utf-8")), new_payload)
            self.assertEqual(json.loads(paths.active_auth_file.read_text(encoding="utf-8")), new_payload)
            backups = list((paths.auth_backup_dir / "openai-1").glob("auth-*.backup.*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), old_payload)
            self.assertIn(t("auth.provider_refreshed", provider="openai-1"), output.getvalue())
            refresh.assert_called_once_with()

    def test_refresh_email_mismatch_does_not_overwrite_any_auth(self) -> None:
        old_payload = auth_payload("owner@example.com", "old")
        new_payload = auth_payload("other@example.com", "new")

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = codex_auth.ensure_runtime_layout(
                codex_auth.default_paths(Path(temp_dir) / ".codex"),
                migrate=False,
            )
            paths.active_auth_file.write_text(json.dumps(old_payload), encoding="utf-8")
            slot = paths.auth_list_dir / "auth-openai-1.json"
            slot.write_text(json.dumps(old_payload), encoding="utf-8")
            paths.config_file.write_text('model_provider = "openai"\n', encoding="utf-8")
            codex_auth.atomic_write_json(
                paths.current_provider_file,
                codex_auth.provider_config_payload("openai", "openai-1"),
            )

            def login(login_home: Path) -> str:
                (login_home / "auth.json").write_text(json.dumps(new_payload), encoding="utf-8")
                return "Logged in using ChatGPT"

            with (
                patch.multiple(codex_auth, **path_globals(paths)),
                patch.object(codex_auth, "assert_no_running_codex_processes"),
                patch.object(codex_auth, "run_device_login", side_effect=login),
                patch.object(
                    codex_auth,
                    "validate_new_auth",
                    return_value={"email": "other@example.com", "allowed": True},
                ),
                patch.object(codex_auth, "request_sampler_refresh") as refresh,
                redirect_stdout(io.StringIO()),
                self.assertRaises(codex_auth.AuthError),
            ):
                codex_auth.command_refresh("openai-1", False)

            self.assertEqual(json.loads(slot.read_text(encoding="utf-8")), old_payload)
            self.assertEqual(json.loads(paths.active_auth_file.read_text(encoding="utf-8")), old_payload)
            self.assertFalse((paths.auth_backup_dir / "openai-1").exists())
            refresh.assert_not_called()

    def test_refresh_failed_online_validation_does_not_overwrite_any_auth(self) -> None:
        old_payload = auth_payload("owner@example.com", "old")
        new_payload = auth_payload("owner@example.com", "new")

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = codex_auth.ensure_runtime_layout(
                codex_auth.default_paths(Path(temp_dir) / ".codex"),
                migrate=False,
            )
            paths.active_auth_file.write_text(json.dumps(old_payload), encoding="utf-8")
            slot = paths.auth_list_dir / "auth-openai-1.json"
            slot.write_text(json.dumps(old_payload), encoding="utf-8")
            paths.config_file.write_text('model_provider = "openai"\n', encoding="utf-8")
            codex_auth.atomic_write_json(
                paths.current_provider_file,
                codex_auth.provider_config_payload("openai", "openai-1"),
            )

            def login(login_home: Path) -> str:
                (login_home / "auth.json").write_text(json.dumps(new_payload), encoding="utf-8")
                return "Logged in using ChatGPT"

            with (
                patch.multiple(codex_auth, **path_globals(paths)),
                patch.object(codex_auth, "assert_no_running_codex_processes"),
                patch.object(codex_auth, "run_device_login", side_effect=login),
                patch.object(
                    codex_auth,
                    "validate_new_auth",
                    side_effect=codex_auth.AuthError("unusable auth"),
                ),
                patch.object(codex_auth, "request_sampler_refresh") as refresh,
                redirect_stdout(io.StringIO()),
                self.assertRaises(codex_auth.AuthError),
            ):
                codex_auth.command_refresh("openai-1", False)

            self.assertEqual(json.loads(slot.read_text(encoding="utf-8")), old_payload)
            self.assertEqual(json.loads(paths.active_auth_file.read_text(encoding="utf-8")), old_payload)
            self.assertFalse((paths.auth_backup_dir / "openai-1").exists())
            refresh.assert_not_called()

    def test_delete_requires_exact_full_id_and_keeps_files_on_mismatch(self) -> None:
        payload = auth_payload("owner@example.com", "old")

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = codex_auth.ensure_runtime_layout(
                codex_auth.default_paths(Path(temp_dir) / ".codex"),
                migrate=False,
            )
            paths.active_auth_file.write_text(json.dumps(payload), encoding="utf-8")
            slot = paths.auth_list_dir / "auth-openai-1.json"
            slot.write_text(json.dumps(payload), encoding="utf-8")
            paths.config_file.write_text('model_provider = "openai"\n', encoding="utf-8")
            codex_auth.atomic_write_json(
                paths.current_provider_file,
                codex_auth.provider_config_payload("openai", "openai-1"),
            )

            with (
                patch.multiple(codex_auth, **path_globals(paths)),
                patch.object(codex_auth, "assert_no_running_codex_processes"),
                patch.object(codex_auth.sys, "stdin", io.StringIO("1\n")),
                patch.object(codex_auth, "request_sampler_refresh") as refresh,
                redirect_stderr(io.StringIO()),
                self.assertRaises(codex_auth.AuthError),
            ):
                codex_auth.command_delete("openai-1", False)

            self.assertTrue(slot.exists())
            self.assertTrue(paths.active_auth_file.exists())
            refresh.assert_not_called()

    def test_delete_current_provider_removes_backups_and_selects_replacement(self) -> None:
        first_payload = auth_payload("first@example.com", "first")
        second_payload = auth_payload("second@example.com", "second")

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = codex_auth.ensure_runtime_layout(
                codex_auth.default_paths(Path(temp_dir) / ".codex"),
                migrate=False,
            )
            paths.active_auth_file.write_text(json.dumps(first_payload), encoding="utf-8")
            first_slot = paths.auth_list_dir / "auth-openai-1.json"
            second_slot = paths.auth_list_dir / "auth-openai-2.json"
            first_slot.write_text(json.dumps(first_payload), encoding="utf-8")
            second_slot.write_text(json.dumps(second_payload), encoding="utf-8")
            backup_dir = paths.auth_backup_dir / "openai-1"
            backup_dir.mkdir()
            (backup_dir / "auth-openai-1.backup.old.json").write_text(
                json.dumps(first_payload),
                encoding="utf-8",
            )
            paths.config_file.write_text('model_provider = "openai"\n', encoding="utf-8")
            codex_auth.atomic_write_json(
                paths.current_provider_file,
                codex_auth.provider_config_payload("openai", "openai-1"),
            )

            output = io.StringIO()
            with (
                patch.multiple(codex_auth, **path_globals(paths)),
                patch.object(codex_auth, "assert_no_running_codex_processes"),
                patch.object(codex_auth.sys, "stdin", io.StringIO("openai-1\n")),
                patch.object(codex_auth, "request_sampler_refresh") as refresh,
                redirect_stderr(io.StringIO()),
                redirect_stdout(output),
            ):
                result = codex_auth.command_delete(None, False)

            self.assertEqual(result, 0)
            self.assertFalse(first_slot.exists())
            self.assertFalse(backup_dir.exists())
            self.assertEqual(json.loads(paths.active_auth_file.read_text(encoding="utf-8")), second_payload)
            current = json.loads(paths.current_provider_file.read_text(encoding="utf-8"))
            self.assertEqual(current["auth_keyword"], "openai-2")
            registry = json.loads(paths.registry_file.read_text(encoding="utf-8"))
            self.assertEqual(
                [account["auth_keyword"] for account in registry["accounts"]],
                ["openai-2"],
            )
            self.assertIn(t("auth.provider_deleted", provider="openai-1"), output.getvalue())
            refresh.assert_called_once_with()

    def test_main_routes_refresh_and_delete_options(self) -> None:
        with patch.object(codex_auth, "command_refresh", return_value=0) as refresh:
            self.assertEqual(codex_auth.main(["--refresh"]), 0)
        refresh.assert_called_once_with(None, False)

        with patch.object(codex_auth, "command_delete", return_value=0) as delete:
            self.assertEqual(codex_auth.main(["--json", "--delete", "openai-2"]), 0)
        delete.assert_called_once_with("openai-2", True)


class CodexAuthApiAndRenameTests(unittest.TestCase):
    def setUp(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.enter_context = stack.enter_context
        temp = self.enter_context(tempfile.TemporaryDirectory())
        self.paths = codex_auth.ensure_runtime_layout(codex_auth.default_paths(Path(temp)), migrate=False)
        self.enter_context(patch.multiple(codex_auth, **path_globals(self.paths)))
        self.enter_context(patch.object(codex_auth, "assert_no_running_codex_processes"))
        self.enter_context(patch.object(codex_auth, "request_sampler_refresh"))
        self.output = self.enter_context(redirect_stdout(io.StringIO()))
        self.enter_context(redirect_stderr(io.StringIO()))
        self.chatgpt = auth_payload("owner@example.com", "original")
        codex_auth.atomic_write_json(codex_auth.auth_file("openai-1"), self.chatgpt)
        codex_auth.atomic_write_json(self.paths.active_auth_file, self.chatgpt)
        self.original_config = (
            '# keep this comment\nmodel_provider = \'openai\' # active\n'
            'model = "example-model"\n[profiles.work]\nmodel_provider = "other"\n'
        )
        self.paths.config_file.write_text(self.original_config)
        codex_auth.write_provider_config("openai", "openai-1")

    def add_api(self, name: str = "my-api", *, key_env: str | None = None) -> dict:
        payload = {"auth_mode": "apikey", "base_url": "https://example.com/v1", "wire_api": "responses"}
        if key_env:
            payload["env_key"] = key_env
        else:
            payload["api_key"] = "test-secret-key"
        with patch.object(codex_auth, "prompt_api_source", return_value=payload):
            self.assertEqual(codex_auth.main(["--new", name, "--api", "--json"]), 0)
        return payload

    def test_api_round_trip_preserves_source_chatgpt_and_config_and_redacts_key(self) -> None:
        payload = self.add_api()
        config = codex_auth.parse_config(self.paths.config_file.read_text())
        self.assertEqual(config["model_provider"], "codexauth-third-party-api")
        self.assertEqual(config["profiles"]["work"]["model_provider"], "other")
        self.assertEqual(config["model"], "example-model")
        self.assertTrue(config["model_providers"]["codexauth-third-party-api"]["requires_openai_auth"])
        self.assertEqual(config["cli_auth_credentials_store"], "file")
        registry = codex_auth.sync_registry()
        self.assertEqual(registry["current_service"], "my-api")
        self.assertTrue(next(a for a in registry["accounts"] if a["label"] == "my-api")["is_active_provider"])
        for path in (self.paths.config_file, self.paths.current_provider_file):
            self.assertNotIn("test-secret-key", path.read_text())
        self.assertNotIn("test-secret-key", self.output.getvalue())
        for path in (self.paths.active_auth_file, self.paths.registry_file):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(codex_auth.main(["switch", "openai"]), 0)
        self.assertEqual(codex_auth.read_auth_payload(self.paths.active_auth_file), self.chatgpt)
        self.assertEqual(codex_auth.read_auth_account("my-api")["meta"], payload)
        self.assertEqual(codex_auth.main(["switch", "my-api"]), 0)
        self.assertEqual(codex_auth.read_auth_payload(codex_auth.auth_file("openai-1")), self.chatgpt)
        self.assertEqual(self.paths.config_file.read_text().count('[model_providers."codexauth-third-party-api"]'), 1)

    def test_api_registry_is_authoritative_and_inactive_rename_keeps_active_config(self) -> None:
        self.add_api("first-api")
        self.add_api("second-api")
        before = self.paths.config_file.read_bytes()
        codex_auth.command_rename("first-api", "renamed-api", False)
        self.assertEqual(self.paths.config_file.read_bytes(), before)
        registry = codex_auth.sync_registry()
        self.assertEqual([a["label"] for a in registry["accounts"] if a["is_active_provider"]], ["second-api"])
        self.assertFalse(codex_auth.auth_file("second-api").exists())
        with patch.object(codex_auth, "confirm_provider_deletion"):
            codex_auth.command_delete("renamed-api", False)
        self.assertEqual(self.paths.config_file.read_bytes(), before)
        old_file = codex_auth.auth_file("old-api")
        codex_auth.atomic_write_json(old_file, {"auth_mode": "apikey", "OPENAI_API_KEY": "old-secret",
                                               "codextop_api": {"base_url": "https://example.com/v1"}})
        self.assertNotIn("old-api", codex_auth.numbered_accounts())
        with self.assertRaises(codex_auth.AuthError):
            codex_auth.switch_auth("old-api")
        self.assertTrue(old_file.exists())

    def test_only_current_managed_api_provider_is_kept(self) -> None:
        manual = '[model_providers.manual]\nname = "manual"\nbase_url = "https://manual.example/v1"\n'
        self.paths.config_file.write_text(self.original_config + manual)
        self.add_api("first-api")
        self.add_api("second-api")
        config = codex_auth.parse_config(self.paths.config_file.read_text())
        self.assertEqual(set(config["model_providers"]), {"manual", "codexauth-third-party-api"})
        self.assertTrue(self.paths.config_file.read_text().endswith("# END CODEXAUTH codexauth-third-party-api\n"))
        codex_auth.command_switch("first-api", False)
        config = codex_auth.parse_config(self.paths.config_file.read_text())
        self.assertEqual(set(config["model_providers"]), {"manual", "codexauth-third-party-api"})
        codex_auth.command_switch("openai", False)
        config = codex_auth.parse_config(self.paths.config_file.read_text())
        self.assertEqual(set(config["model_providers"]), {"manual"})
        self.assertIn(manual, self.paths.config_file.read_text())
        self.assertFalse(codex_auth.auth_file("first-api").exists())
        self.assertFalse(codex_auth.auth_file("second-api").exists())
        self.assertEqual(len(codex_auth.registry_api_accounts()), 2)

    def test_registry_api_numbers_switch_and_preserve_secrets(self) -> None:
        for number in range(2, 5):
            codex_auth.atomic_write_json(codex_auth.auth_file(f"openai-{number}"), self.chatgpt)
        accounts = []
        for name, credentials in (("xlance-api", {"api_key": "registry-secret"}),
                                  ("xi-ai", {"env_key": "REGISTRY_TEST_KEY"})):
            accounts.append({"auth_keyword": name, "label": name, "auth_type": "api",
                             "provider": codex_auth.api_provider_id(name), "path": None,
                             "meta": {"base_url": "https://example.com/v1", "wire_api": "responses",
                                      **credentials}})
        codex_auth.atomic_write_json(self.paths.registry_file, {"accounts": accounts})
        self.assertEqual(codex_auth.main(["5", "--json"]), 0)
        self.assertEqual(codex_auth.read_current_provider(), "xlance-api")
        self.assertEqual(codex_auth.read_auth_payload(self.paths.active_auth_file)["OPENAI_API_KEY"],
                         "registry-secret")
        codex_auth.save_active_auth("xlance-api")
        self.assertFalse(codex_auth.auth_file("xlance-api").exists())
        self.assertFalse(codex_auth.backup_dir_for("xlance-api").exists())
        self.assertEqual(codex_auth.sync_registry()["current_provider_number"], 5)
        with patch.dict(codex_auth.os.environ, {"REGISTRY_TEST_KEY": "env-secret"}):
            self.assertEqual(codex_auth.main(["6"]), 0)
        self.assertEqual(codex_auth.read_auth_payload(self.paths.active_auth_file)["OPENAI_API_KEY"],
                         "env-secret")
        self.assertEqual(codex_auth.main(["openai"]), 0)
        self.assertEqual(codex_auth.read_model_provider(), "openai")
        self.assertEqual(codex_auth.resolve_auth_target("5"), "xlance-api")
        registry = codex_auth.read_auth_payload(self.paths.registry_file)
        self.assertEqual(registry["accounts"][4]["meta"]["api_key"], "registry-secret")
        self.assertNotIn("registry-secret", self.output.getvalue())
        self.assertNotIn("env-secret", self.output.getvalue())
        before = self.paths.active_auth_file.read_bytes()
        with patch.dict(codex_auth.os.environ, {}, clear=True):
            self.assertEqual(codex_auth.main(["6"]), 1)
        self.assertEqual(self.paths.active_auth_file.read_bytes(), before)

    def test_env_source_is_reread_and_failure_keeps_active_credentials(self) -> None:
        env = self.enter_context(patch.dict(codex_auth.os.environ, {"CODEXAUTH_TEST_KEY": "first-key"}))
        payload = self.add_api(key_env="CODEXAUTH_TEST_KEY")
        self.assertNotIn("OPENAI_API_KEY", payload)
        codex_auth.command_switch("1", False)
        env["CODEXAUTH_TEST_KEY"] = "rotated-key"
        codex_auth.command_switch("my-api", False)
        self.assertEqual(codex_auth.read_auth_payload(self.paths.active_auth_file)["OPENAI_API_KEY"], "rotated-key")
        self.assertEqual(codex_auth.read_auth_account("my-api")["meta"], payload)
        codex_auth.command_switch("1", False)
        before = self.paths.config_file.read_text()
        del env["CODEXAUTH_TEST_KEY"]
        with self.assertRaises(codex_auth.AuthError):
            codex_auth.command_switch("my-api", False)
        self.assertEqual(self.paths.config_file.read_text(), before)
        self.assertEqual(codex_auth.read_auth_payload(self.paths.active_auth_file), self.chatgpt)
        self.assertEqual(codex_auth.read_current_provider(), "openai-1")
        for path in (self.paths.registry_file, self.paths.config_file):
            self.assertNotIn("rotated-key", path.read_text())

    def test_switch_to_current_chatgpt_keeps_refreshed_tokens(self) -> None:
        refreshed = auth_payload("owner@example.com", "refreshed")
        codex_auth.atomic_write_json(self.paths.active_auth_file, refreshed)
        codex_auth.command_switch("1", False)
        self.assertEqual(codex_auth.read_auth_payload(self.paths.active_auth_file), refreshed)
        self.assertEqual(codex_auth.read_auth_payload(codex_auth.auth_file("openai-1")), refreshed)

    def test_rename_current_chatgpt_moves_backups_and_rejects_collision(self) -> None:
        codex_auth.save_active_auth("openai-1")
        self.assertEqual(codex_auth.main(["--rename", "1", "Work"]), 0)
        self.assertFalse(codex_auth.auth_file("openai-1").exists())
        self.assertEqual(codex_auth.read_auth_payload(codex_auth.auth_file("Work")), self.chatgpt)
        self.assertEqual(codex_auth.read_current_provider(), "Work")
        self.assertEqual(codex_auth.backup_count("Work"), 1)
        self.assertEqual(codex_auth.main(["--new", "work", "--api"]), 1)
        self.assertEqual(codex_auth.main(["--rename", "Work", "../bad"]), 1)
        self.assertEqual(codex_auth.read_current_provider(), "Work")

    def test_rename_active_and_inactive_api_then_delete(self) -> None:
        self.add_api()
        self.assertEqual(codex_auth.main(["--rename", "my-api", "New.API"]), 0)
        self.assertEqual(codex_auth.read_model_provider(), "codexauth-third-party-api")
        self.assertEqual(codex_auth.read_current_provider(), "New.API")
        self.assertNotIn("# BEGIN CODEXAUTH my-api", self.paths.config_file.read_text())
        codex_auth.command_switch("1", False)
        codex_auth.command_rename("New.API", "final-api", False)
        self.assertEqual(codex_auth.read_model_provider(), "openai")
        codex_auth.command_switch("final-api", False)
        with patch.object(codex_auth, "confirm_provider_deletion"):
            codex_auth.command_delete(None, False)
        self.assertEqual(codex_auth.read_current_provider(), "openai-1")
        self.assertEqual(codex_auth.read_auth_payload(self.paths.active_auth_file), self.chatgpt)
        self.assertNotIn("codexauth-third-party-api", self.paths.config_file.read_text())

    def test_delete_chatgpt_selects_api_and_delete_last_does_not_bootstrap_it(self) -> None:
        self.add_api()
        codex_auth.command_switch("1", False)
        with patch.object(codex_auth, "confirm_provider_deletion"):
            codex_auth.command_delete(None, False)
            self.assertEqual(codex_auth.read_model_provider(), "codexauth-third-party-api")
            codex_auth.command_delete(None, False)
        self.assertFalse(self.paths.active_auth_file.exists())
        self.assertEqual(codex_auth.discover_auth_files(), [])
        self.assertEqual(codex_auth.read_model_provider(), "openai")

    def test_import_config_collision_and_write_failure_roll_back(self) -> None:
        self.paths.config_file.write_text(self.original_config + '[model_providers.codexauth-third-party-api]\nname="custom"\n')
        before = self.paths.config_file.read_text()
        payload = {"auth_mode": "apikey", "api_key": "secret"}
        with patch.object(codex_auth, "prompt_api_source", return_value=payload):
            self.assertEqual(codex_auth.main(["--new", "my-api", "--api"]), 1)
        self.assertEqual(self.paths.config_file.read_text(), before)
        self.assertFalse(codex_auth.auth_file("my-api").exists())
        self.assertEqual(codex_auth.registry_api_accounts(), [])
        self.paths.config_file.write_text(self.original_config)
        with (
            patch.object(codex_auth, "prompt_api_source", return_value=payload),
            patch.object(codex_auth, "write_provider_config", side_effect=OSError("write failed")),
        ):
            self.assertEqual(codex_auth.main(["--new", "my-api", "--api"]), 1)
        self.assertFalse(codex_auth.auth_file("my-api").exists())
        self.assertEqual(codex_auth.read_auth_payload(self.paths.active_auth_file), self.chatgpt)
        self.assertEqual(self.paths.config_file.read_text(), self.original_config)

    def test_prompt_manual_default_url_path_and_cancellation(self) -> None:
        from rich.prompt import Prompt

        with (
            patch.object(codex_auth.sys.stdin, "isatty", return_value=True),
            redirect_stderr(io.StringIO()) as manual_output,
            patch.object(Prompt, "ask", side_effect=["1", "sk-abcdefghijz", ""]) as ask,
        ):
            payload = codex_auth.prompt_api_source("demo")
        self.assertEqual(payload["base_url"], codex_auth.DEFAULT_API_BASE_URL)
        self.assertTrue(ask.call_args_list[1].kwargs["password"])
        self.assertIn("sk-a******z", manual_output.getvalue())
        self.assertNotIn("sk-abcdefghijz", manual_output.getvalue())
        with (
            patch.object(codex_auth.sys.stdin, "isatty", return_value=True),
            patch.dict(codex_auth.os.environ, {"OEPNAI_API_KEY": "sk-abcdefghijz"}),
            redirect_stderr(io.StringIO()) as output,
            patch.object(Prompt, "ask", side_effect=["2", " OEPNAI_API_KEY ", "https://example.com/v1/"]),
        ):
            payload = codex_auth.prompt_api_source("demo")
        self.assertEqual(payload["env_key"], "OEPNAI_API_KEY")
        self.assertEqual(payload["base_url"], "https://example.com/v1")
        self.assertNotIn("OPENAI_API_KEY", payload)
        self.assertIn("sk-a******z", output.getvalue())
        self.assertNotIn("sk-abcdefghijz", output.getvalue())
        self.assertFalse(any(c in output.getvalue() for c in "╭╮╰╯│─┌┐└┘"))
        with (
            patch.object(codex_auth.sys.stdin, "isatty", return_value=True),
            patch.object(Prompt, "ask", side_effect=KeyboardInterrupt),
        ):
            self.assertEqual(codex_auth.main(["--new", "demo", "--api"]), 1)
        self.assertFalse(codex_auth.auth_file("demo").exists())

    def test_env_prompt_rejects_missing_empty_invalid_keys_and_masks_short_keys(self) -> None:
        from rich.prompt import Prompt

        for value in (None, "", "  ", "key\nheader", "short"):
            with (
                self.subTest(value=value),
                patch.dict(codex_auth.os.environ, {} if value is None else {"CODEXAUTH_TEST_KEY": value}, clear=True),
                patch.object(codex_auth.sys.stdin, "isatty", return_value=True),
                patch.object(Prompt, "ask", side_effect=["2", "CODEXAUTH_TEST_KEY", "" if value == "short" else KeyboardInterrupt]),
                redirect_stderr(io.StringIO()) as output,
            ):
                result = codex_auth.main(["--new", "demo", "--api"])
                if value == "short":
                    self.assertEqual(result, 0)
                    self.assertIn("******", output.getvalue())
                    self.assertNotIn("short", output.getvalue())
                else:
                    self.assertEqual(result, 1)
                    self.assertFalse(codex_auth.auth_file("demo").exists())
                    self.assertEqual(self.paths.config_file.read_text(), self.original_config)
                    self.assertEqual(codex_auth.read_auth_payload(self.paths.active_auth_file), self.chatgpt)
                    if value is None or not value.strip():
                        self.assertIn(
                            t("auth.api_env_missing", name="CODEXAUTH_TEST_KEY"),
                            " ".join(output.getvalue().split()),
                        )

    def test_env_prompt_retries_until_a_nonempty_variable_is_entered(self) -> None:
        from rich.prompt import Prompt

        with (
            patch.dict(codex_auth.os.environ, {"EMPTY": "", "BLANK": "  ", "VALID": "sk-abcdefghijz"}, clear=True),
            patch.object(codex_auth.sys.stdin, "isatty", return_value=True),
            patch.object(Prompt, "ask", side_effect=["2", "MISSING", "EMPTY", "BLANK", "VALID", ""]) as ask,
            redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(codex_auth.main(["--new", "demo", "--api"]), 0)
        for name in ("MISSING", "EMPTY", "BLANK"):
            self.assertIn(t("auth.api_env_missing", name=name), " ".join(output.getvalue().split()))
        self.assertEqual([c.args[0] for c in ask.call_args_list[1:5]], [t("auth.api_key_env")] * 4)
        self.assertIn("sk-a******z", output.getvalue())
        self.assertNotIn("sk-abcdefghijz", output.getvalue())
        payload = codex_auth.read_auth_account("demo")["meta"]
        self.assertEqual(payload["env_key"], "VALID")
        self.assertEqual(codex_auth.read_current_provider(), "demo")

    def test_api_credentials_never_reach_chatgpt_quota_or_device_login(self) -> None:
        from quota import check_codex_quota as quota

        self.add_api()
        with patch.object(quota, "get_json") as get_json:
            bundle = quota.collect_accounts([(name, path) for name, path in codex_auth.discover_auth_files() if name == "my-api"], "my-api", "UTC")
        get_json.assert_not_called()
        self.assertNotIn("my-api", [a.get("label") for a in bundle["accounts"]])
        with patch.object(codex_auth, "run_device_login") as login:
            self.assertEqual(codex_auth.main(["--refresh"]), 1)
        login.assert_not_called()

    def test_invalid_flags_and_input_do_not_change_state(self) -> None:
        for args in (["--api"], ["--refresh", "--api"], ["--new", "x", "--rename", "1", "y"]):
            self.assertEqual(codex_auth.main(args), 1)
        for key in ("", " \n ", "key\nheader", "key\x00", None):
            with self.assertRaises(codex_auth.AuthError):
                codex_auth.validate_api_key(key)
        for url in ("file:///tmp/key", "https://user:pass@example.com", "https://example.com/?key=secret", "https://"):
            with self.assertRaises(codex_auth.AuthError):
                codex_auth.validate_api_base_url(url)
        self.assertEqual(self.paths.config_file.read_text(), self.original_config)
        for name in ("", "1KEY", "$OPENAI_API_KEY", "/tmp/key", None):
            with self.assertRaises(codex_auth.AuthError):
                codex_auth.api_auth_payload({"env_key": name})


if __name__ == "__main__":
    unittest.main()
