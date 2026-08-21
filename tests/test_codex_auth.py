from __future__ import annotations

import io
import json
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auth import codex_auth


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
            self.assertIn(
                "请按照下方登录信息在网页版完成登录；等待期间可按 Ctrl-C 取消。\n\n",
                output.getvalue(),
            )
            refresh.assert_called_once_with()

    def test_main_routes_new_option(self) -> None:
        with patch.object(codex_auth, "command_new", return_value=0) as command_new:
            self.assertEqual(codex_auth.main(["--new", "4"]), 0)
        command_new.assert_called_once_with("4", False)


if __name__ == "__main__":
    unittest.main()
