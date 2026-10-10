from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auth import codex_auth
from auth.session_provider import SessionProviderUpdate, session_selector


class SessionProviderTests(unittest.TestCase):
    def test_transfer_parameters_and_validation(self) -> None:
        ident = str(uuid4())
        for argv, duration, session_id in (
            (["work", "-f", "--transfer"], None, None),
            (["work", "-f", "--transfer", "-d", "7d"], "7d", None),
            (["-f", "--transfer", "-s", ident], None, ident),
        ):
            with self.subTest(argv=argv):
                args = codex_auth.build_parser().parse_args(argv)
                self.assertTrue(args.transfer)
                self.assertTrue(args.force)
                self.assertEqual(args.duration, duration)
                self.assertEqual(args.session_id, session_id)
        for argv in (["-d", "1h"], ["-s", str(uuid4())],
                     ["--transfer", "-s", "all"], ["--transfer", "-d", str(uuid4())]):
            with self.subTest(argv=argv), patch.object(codex_auth, "dispatch") as dispatch:
                self.assertEqual(codex_auth.main(argv), 1)
                dispatch.assert_not_called()
        with self.assertRaises(SystemExit):
            codex_auth.build_parser().parse_args(["--transfer", "-d", "1h", "-s", str(uuid4())])

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "state_5.sqlite"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, rollout_path TEXT, updated_at INTEGER)")

    def session(self, provider: str = "api", age: int = 0, indexed: bool = True, archived: bool = False) -> tuple[str, Path]:
        ident = str(uuid4())
        path = self.root / ("archived_sessions" if archived else "sessions") / f"rollout-{ident}.jsonl"
        path.parent.mkdir(exist_ok=True)
        metadata = {"type": "session_meta", "payload": {"id": ident, "model_provider": provider, "history_mode": "paginated"}}
        path.write_bytes(json.dumps(metadata).encode() + b'\n{"type":"event_msg","payload":{"unchanged":true}}\n')
        updated = int(time.time()) - age
        os.utime(path, (updated, updated))
        if indexed:
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute("INSERT INTO threads VALUES (?, ?, ?, ?)", (ident, provider, str(path), updated))
        return ident, path

    def provider(self, ident: str) -> str:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            return connection.execute("SELECT model_provider FROM threads WHERE id=?", (ident,)).fetchone()[0]

    def test_default_range_updates_metadata_and_database_and_preserves_transcript(self) -> None:
        ident, path = self.session()
        old_id, old_path = self.session(age=7200)
        other_id, other_path = self.session(provider="other")
        before = path.read_bytes().split(b"\n", 1)[1]
        mtime = path.stat().st_mtime_ns
        result = SessionProviderUpdate(self.root, "1h", source="api").apply("openai")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.provider(ident), "openai")
        self.assertEqual(json.loads(path.read_bytes().split(b"\n")[0])["payload"]["model_provider"], "openai")
        self.assertEqual(path.read_bytes().split(b"\n", 1)[1], before)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.provider(old_id), "api")
        self.assertEqual(self.provider(other_id), "other")
        backup = json.loads(Path(result["backup"]).read_text())
        self.assertEqual(backup["sessions"][0]["session_meta"]["payload"]["model_provider"], "api")

    def test_database_activity_time_wins_over_rollout_mtime(self) -> None:
        ident, path = self.session(age=7200)
        os.utime(path, None)
        self.assertEqual(SessionProviderUpdate(self.root, "1h").apply("openai")["updated"], 0)
        self.assertEqual(self.provider(ident), "api")

    def test_id_days_all_and_legacy_archives(self) -> None:
        ident, path = self.session(age=172800)
        legacy_id, legacy = self.session(indexed=False, archived=True, age=172800)
        self.assertEqual(SessionProviderUpdate(self.root, "1d").apply("openai")["updated"], 0)
        self.assertEqual(SessionProviderUpdate(self.root, ident).apply("openai")["updated"], 1)
        self.assertEqual(SessionProviderUpdate(self.root, "all").apply("openai")["updated"], 1)
        self.assertEqual(json.loads(legacy.read_text().splitlines()[0])["payload"]["model_provider"], "openai")

    def test_write_failure_rolls_back_files_and_database(self) -> None:
        ident, path = self.session()
        second_id, second = self.session()
        originals = {p: p.read_bytes() for p in (path, second)}
        real_replace = os.replace
        calls = 0

        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("write failed")
            return real_replace(source, destination)

        update = SessionProviderUpdate(self.root, "all")
        with patch("auth.session_provider.os.replace", side_effect=fail_second), self.assertRaises(OSError):
            update.apply("openai")
        for p, data in originals.items():
            self.assertEqual(p.read_bytes(), data)
        self.assertEqual(self.provider(ident), "api")
        self.assertEqual(self.provider(second_id), "api")

    def test_invalid_selectors_and_missing_id_fail_before_writes(self) -> None:
        for selector in ("0h", "-1d", "1m", "garbage"):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                session_selector(selector)
        with self.assertRaises(ValueError):
            SessionProviderUpdate(self.root, str(uuid4()))

    def test_standalone_cli_defaults_to_one_hour_and_reports_json(self) -> None:
        ident, path = self.session()
        paths = codex_auth.ensure_runtime_layout(codex_auth.default_paths(self.root), migrate=False)
        paths.config_file.write_text('model_provider = "openai"\n')
        globals = {"PATHS": paths, "CODEX_DIR": self.root, "CONFIG_FILE": paths.config_file,
                   "ACTIVE_AUTH_FILE": paths.active_auth_file, "CURRENT_PROVIDER_FILE": paths.current_provider_file,
                   "REGISTRY_FILE": paths.registry_file}
        with patch.multiple(codex_auth, **globals), patch.object(codex_auth, "assert_no_running_codex_processes"), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(codex_auth.main(["--transfer", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["session_provider_update"]["updated"], 1)
        self.assertEqual(self.provider(ident), "openai")

    def test_attached_migration_only_runs_when_internal_provider_changes(self) -> None:
        ident, path = self.session()
        paths = codex_auth.ensure_runtime_layout(codex_auth.default_paths(self.root), migrate=False)
        paths.config_file.write_text('model_provider = "api"\n')
        globals = {"PATHS": paths, "CODEX_DIR": self.root, "CONFIG_FILE": paths.config_file,
                   "ACTIVE_AUTH_FILE": paths.active_auth_file, "CURRENT_PROVIDER_FILE": paths.current_provider_file,
                   "REGISTRY_FILE": paths.registry_file}

        def switch(_args):
            paths.config_file.write_text('model_provider = "openai"\n')
            print(json.dumps({"model_provider": "openai"}))
            return 0

        with patch.multiple(codex_auth, **globals), patch.object(codex_auth, "assert_no_running_codex_processes"), patch.object(codex_auth, "dispatch", side_effect=switch), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(codex_auth.main(["work", "--transfer", "--json"]), 0)
        self.assertEqual(self.provider(ident), "openai")
        self.assertEqual(json.loads(output.getvalue())["session_provider_update"]["updated"], 1)

    def test_failed_migration_restores_provider_config(self) -> None:
        self.session()
        paths = codex_auth.ensure_runtime_layout(codex_auth.default_paths(self.root), migrate=False)
        original = 'model_provider = "api"\n'
        paths.config_file.write_text(original)
        globals = {"PATHS": paths, "CODEX_DIR": self.root, "CONFIG_FILE": paths.config_file,
                   "ACTIVE_AUTH_FILE": paths.active_auth_file, "CURRENT_PROVIDER_FILE": paths.current_provider_file,
                   "REGISTRY_FILE": paths.registry_file}

        def switch(_args):
            paths.config_file.write_text('model_provider = "openai"\n')
            return 0

        with (
            patch.multiple(codex_auth, **globals),
            patch.object(codex_auth, "assert_no_running_codex_processes"),
            patch.object(codex_auth, "dispatch", side_effect=switch),
            patch.object(SessionProviderUpdate, "apply", side_effect=OSError("write failed")),
        ):
            self.assertEqual(codex_auth.main(["work", "--transfer"]), 1)
        self.assertEqual(paths.config_file.read_text(), original)

    def test_batch_same_provider_is_noop_and_explicit_id_can_be_repaired(self) -> None:
        ident, path = self.session(provider="other")
        paths = codex_auth.ensure_runtime_layout(codex_auth.default_paths(self.root), migrate=False)
        paths.config_file.write_text('model_provider = "openai"\n')
        globals = {"PATHS": paths, "CODEX_DIR": self.root, "CONFIG_FILE": paths.config_file,
                   "ACTIVE_AUTH_FILE": paths.active_auth_file, "CURRENT_PROVIDER_FILE": paths.current_provider_file,
                   "REGISTRY_FILE": paths.registry_file}
        with (
            patch.multiple(codex_auth, **globals),
            patch.object(codex_auth, "assert_no_running_codex_processes"),
            patch.object(codex_auth, "dispatch", return_value=0),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(codex_auth.main(["work", "--transfer"]), 0)
            self.assertEqual(self.provider(ident), "other")
            self.assertEqual(codex_auth.main(["work", "--transfer", "-s", ident]), 0)
        self.assertEqual(self.provider(ident), "openai")


if __name__ == "__main__":
    unittest.main()
