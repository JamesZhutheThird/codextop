from __future__ import annotations

import ast
import json
import os
import string
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

import sys

sys.path.insert(0, str(SRC))

from core.i18n import DEFAULT_LANGUAGE, LOCALES_DIR, detect_language, normalize_language, t
from core.state import (
    read_codextop_state,
    save_codextop_state,
    saved_dashboard_density,
    saved_hide_account_info,
    saved_language,
)
from ui.app import run_tui
from ui.color_schemes import color_scheme_choices
from ui.models import MonitorState
from ui.settings import apply_setting_value, handle_setting_key, render_sidebar, setting_items
from ui.terminal_text import strip_ansi
from ui.token_charts import token_label


class TranslationTests(unittest.TestCase):
    def test_locale_selection_uses_supported_system_language_or_english(self) -> None:
        self.assertEqual(detect_language({"LANG": "zh_CN.UTF-8"}), "zh_CN")
        self.assertEqual(detect_language({"LANG": "zh.UTF-8"}), "zh_CN")
        self.assertEqual(detect_language({"CODEXTOP_LANG": "zh-CN"}), "zh_CN")
        self.assertEqual(detect_language({"LANGUAGE": "fr:zh_CN"}), "zh_CN")
        self.assertEqual(detect_language({"LANG": "fr_FR.UTF-8"}), DEFAULT_LANGUAGE)
        self.assertEqual(detect_language({"LANG": "zh_TW.UTF-8"}), DEFAULT_LANGUAGE)
        self.assertEqual(normalize_language("C.UTF-8"), DEFAULT_LANGUAGE)

    def test_language_files_have_matching_keys(self) -> None:
        catalogs = {
            path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(LOCALES_DIR.glob("*.json"))
        }
        self.assertEqual(set(catalogs), {"en", "zh_CN"})
        self.assertEqual(set(catalogs["en"]), set(catalogs["zh_CN"]))
        self.assertTrue(all(catalogs[language][key] for language in catalogs for key in catalogs[language]))
        formatter = string.Formatter()
        for key in catalogs["en"]:
            placeholders = {
                language: {
                    field
                    for _text, field, _spec, _conversion in formatter.parse(
                        catalogs[language][key]
                    )
                    if field
                }
                for language in catalogs
            }
            self.assertEqual(placeholders["en"], placeholders["zh_CN"], key)

    def test_literal_translation_keys_exist(self) -> None:
        catalog = json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8"))
        used: set[str] = set()
        for path in SRC.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                function = node.func
                if isinstance(function, ast.Name):
                    name = function.id
                elif isinstance(function, ast.Attribute):
                    name = function.attr
                else:
                    name = ""
                key = node.args[0]
                if name == "t" and isinstance(key, ast.Constant) and isinstance(key.value, str):
                    used.add(key.value)
        used.update(f"color_scheme.{path.stem}" for path in (SRC / "ui" / "styles").glob("*.json"))
        used.update(f"language.{language}" for language in ("en", "zh_CN"))
        self.assertEqual(used - set(catalog), set())

    def test_translation_and_named_placeholders(self) -> None:
        self.assertEqual(t("app.account_info", _language="en"), "Account information")
        self.assertEqual(t("app.account_info", _language="zh-CN"), "账号信息")
        self.assertEqual(t("install.language", _language="en", language="English"), "Language: English")
        values = {"window": "5h", "percent": "100%", "multiple": "5x", "countdown": "4h"}
        self.assertEqual(
            t("statistics.current_quota_line", _language="zh_CN", **values),
            "5h：100% · 5x · 最近于 4h 后重置",
        )
        self.assertEqual(
            t("statistics.current_quota_line", _language="en", **values),
            "5h: 100% · 5x · next reset in 4h",
        )

    def test_custom_color_scheme_keeps_its_own_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "classic.json"
            path.write_text(
                json.dumps({"label": "My classic", "percent": {"0": "#000000"}}),
                encoding="utf-8",
            )
            self.assertEqual(color_scheme_choices(path.parent), [("My classic", "classic")])

    def test_language_setting_updates_in_process_without_reloading_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"CODEXTOP_LANG": "en"}):
            root = Path(temp_dir)
            history_cache = object()
            token_monitor = object()
            records = [{"t": 1, "a": []}]
            state = MonitorState(
                period="5h",
                interval=60,
                tz="Asia/Shanghai",
                log_path=root / "quota.jsonl",
                restore_interval=60,
                state_path=root / "state.json",
                curve_mode="connected",
                display_scope="list",
                window_scope="both",
                color_scheme="classic",
                language="en",
                records=records,
                history_cache=history_cache,
                token_monitor=token_monitor,
                main_cache_key=("cached",),
                statistics_cache_key=("cached",),
            )
            choices = next(choices for key, _title, choices in setting_items(state) if key == "language")
            self.assertEqual(choices, [("English (en)", "en"), ("简体中文 (zh_CN)", "zh_CN")])
            handle_setting_key(state, "f9")
            handle_setting_key(state, "enter")
            sidebar = "\n".join(strip_ansi(line) for line in render_sidebar(state, 21, 29, 100, []))
            self.assertIn(t("setting.language"), sidebar)
            self.assertIn("English (en)", sidebar)

            apply_setting_value(state, "language", "zh_CN")
            self.assertEqual(state.language, "zh_CN")
            self.assertEqual(os.environ["CODEXTOP_LANG"], "zh_CN")
            self.assertEqual(state.status, "语言已切换")
            self.assertIs(state.records, records)
            self.assertIs(state.history_cache, history_cache)
            self.assertIs(state.token_monitor, token_monitor)
            self.assertIsNone(state.main_cache_key)
            self.assertIsNone(state.statistics_cache_key)
            translated_items = setting_items(state)
            self.assertIn(("看板模式", "list"), next(choices for key, _title, choices in translated_items if key == "display_scope"))
            self.assertEqual(token_label("input"), "输入")
            save_codextop_state(state)
            saved = read_codextop_state(state.state_path)
            self.assertEqual(saved_language(saved), "zh_CN")
            self.assertEqual(saved_dashboard_density(saved), "default")
            self.assertFalse(saved_hide_account_info(saved))
            self.assertIsNone(saved_language({"language": "invalid"}))

    def test_saved_language_is_activated_before_command_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "state.json").write_text('{"language":"zh_CN"}\n', encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "CODEXTOP_LANG": "en",
                    "CODEXTOP_CODEX_DIR": str(root / "codex"),
                    "PYTHONPATH": str(SRC),
                }
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SRC / "codextop.py"),
                    "--log-dir",
                    str(root),
                    "--state-file",
                    "state.json",
                    "--help",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(t("app.arg.description", _language="zh_CN"), result.stdout)
            self.assertNotIn(t("app.arg.description", _language="en"), result.stdout)

    def test_auth_commands_follow_saved_language_and_fall_back_without_valid_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "codextop" / "log" / "codextop_state.json"
            state_path.parent.mkdir(parents=True)
            cases = [
                ('{"language":"zh_CN"}', "en", "zh_CN"),
                ('{"language":"en"}', "zh_CN", "en"),
                ('{"language":"invalid"}', "zh_CN", "zh_CN"),
                ('{broken', "en", "en"),
                (None, "zh_CN", "zh_CN"),
            ]
            for content, inherited, expected in cases:
                with self.subTest(content=content):
                    if content is None:
                        state_path.unlink(missing_ok=True)
                    else:
                        state_path.write_text(content, encoding="utf-8")
                    env = dict(os.environ, CODEXTOP_CODEX_DIR=str(root), CODEXTOP_LANG=inherited)
                    for args, exit_code, translation in (
                        (["--help"], 0, "auth.arg.description"),
                        (["--api"], 1, "auth.api_requires_new"),
                    ):
                        result = subprocess.run(
                            [sys.executable, str(SRC / "auth" / "codex_auth.py"), *args],
                            cwd=ROOT, env=env, capture_output=True, text=True, check=False,
                        )
                        self.assertEqual(result.returncode, exit_code, result.stderr)
                        self.assertIn(t(translation, _language=expected), result.stdout + result.stderr)

class InstallerLanguageTests(unittest.TestCase):
    def run_installer(self, system_language: str) -> tuple[str, str, str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            bin_dir = root / "bin"
            codex_dir = root / "codex"
            zshrc = home / ".zshrc"
            env = os.environ.copy()
            for key in ("CODEXTOP_LANG", "LC_ALL", "LC_MESSAGES", "LANGUAGE"):
                env.pop(key, None)
            env.update(
                {
                    "HOME": str(home),
                    "LANG": system_language,
                    "ZSHRC": str(zshrc),
                    "CODEXTOP_BIN_DIR": str(bin_dir),
                    "CODEXTOP_CODEX_DIR": str(codex_dir),
                    "PYTHON_BIN": "/bin/true",
                }
            )

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "install_zshrc.sh")],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            return (
                zshrc.read_text(encoding="utf-8"),
                (bin_dir / "CODEXTOP").read_text(encoding="utf-8"),
                result.stdout,
            )

    def test_installer_persists_detected_system_language(self) -> None:
        zshrc, wrapper, output = self.run_installer("zh_CN.UTF-8")
        language_export = 'export CODEXTOP_LANG="${CODEXTOP_LANG:-zh_CN}"'
        self.assertIn(language_export, zshrc)
        self.assertIn(language_export, wrapper)
        self.assertIn("语言：简体中文", output)

    def test_installer_defaults_unsupported_system_language_to_english(self) -> None:
        zshrc, wrapper, output = self.run_installer("fr_FR.UTF-8")
        language_export = 'export CODEXTOP_LANG="${CODEXTOP_LANG:-en}"'
        self.assertIn(language_export, zshrc)
        self.assertIn(language_export, wrapper)
        self.assertIn("Language: English", output)


if __name__ == "__main__":
    unittest.main()
