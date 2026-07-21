from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

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


if __name__ == "__main__":
    unittest.main()
