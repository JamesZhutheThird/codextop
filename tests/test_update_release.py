from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.update_check import (
    DailyUpdateCheck,
    read_daily_update_check,
    start_daily_update_check,
    write_daily_update_check,
)
from core.updater import GitHubRelease, check_update, latest_github_release
from core.version import APP_VERSION
from ui.app import render_frame
from ui.models import MonitorState
from ui.settings import (
    CODEXTOP_ASCII_ART,
    _gradient_logo_rows,
    current_version_changelog,
    handle_click,
    handle_setting_key,
    handle_update_dialog_key,
    handle_version_dialog_key,
    render_version_overlay,
    render_sidebar,
    render_update_overlay,
    settings_root_entries,
)
from ui.terminal_text import strip_ansi


def monitor_state(root: Path) -> MonitorState:
    return MonitorState(
        period="1d",
        interval=60,
        tz="Asia/Shanghai",
        log_path=root / "quota_snapshots.jsonl",
        restore_interval=60,
        state_path=root / "codextop_state.json",
        curve_mode="connected",
        display_scope="list",
        window_scope="both",
        color_scheme="classic",
    )


class GitHubReleaseUpdateTests(unittest.TestCase):
    def test_forced_check_bypasses_daily_cache_and_keeps_result_overlay_open(self) -> None:
        class ImmediateThread:
            def __init__(self, *, target, **_kwargs) -> None:
                self.target = target

            def start(self) -> None:
                self.target()

        with tempfile.TemporaryDirectory() as temp_dir:
            state = monitor_state(Path(temp_dir))
            state.update_check_path = Path(temp_dir) / "update_check.json"
            state.update_dialog_open = True
            result = DailyUpdateCheck(
                checked_date="2026-08-27",
                latest_version=APP_VERSION.removeprefix("v"),
            )
            with (
                patch("core.update_check.read_daily_update_check") as read_cache,
                patch("core.update_check.run_update_check", return_value=result),
                patch("core.update_check.write_daily_update_check"),
                patch("core.update_check.threading.Thread", ImmediateThread),
            ):
                start_daily_update_check(state, force=True)

        read_cache.assert_not_called()
        self.assertFalse(state.update_checking)
        self.assertFalse(state.update_available)
        self.assertTrue(state.update_dialog_open)

    def test_latest_release_parses_version_and_notes_from_github(self) -> None:
        payload = {
            "tag_name": "v2.6.0",
            "name": "CodexTOP 2.6.0",
            "body": "- Added release notes",
            "html_url": "https://github.com/JamesZhutheThird/codextop/releases/tag/v2.6.0",
            "draft": False,
            "prerelease": False,
        }
        with patch("core.updater.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as mocked:
            release = latest_github_release("JamesZhutheThird/codextop")

        self.assertEqual(release, GitHubRelease("v2.6.0", "2.6.0", "CodexTOP 2.6.0", "- Added release notes"))
        self.assertEqual(mocked.call_args.args[0].full_url, "https://api.github.com/repos/JamesZhutheThird/codextop/releases/latest")

    def test_remote_branch_difference_alone_is_not_an_update(self) -> None:
        release = GitHubRelease("v2.5.0", "2.5.0", "v2.5.0", "notes")
        with (
            patch("core.updater.discover_repo_root", return_value=Path("/repo")),
            patch("core.updater.resolve_remote", return_value=("origin", "https://github.com/JamesZhutheThird/codextop.git")),
            patch("core.updater.local_revision", return_value="local"),
            patch("core.updater.remote_branch_revision", return_value="remote"),
            patch("core.updater.latest_github_release", return_value=release),
            patch("core.updater.has_dirty_worktree", return_value=False),
            patch("core.updater.PACKAGE_VERSION", "2.5.0"),
            patch("core.updater.APP_VERSION", "v2.5.0"),
        ):
            status = check_update(branch="main")

        self.assertFalse(status.update_available)
        self.assertEqual(status.remote_revision, "remote")

    def test_newer_release_is_available_with_its_notes(self) -> None:
        release = GitHubRelease("v2.6.0", "2.6.0", "CodexTOP 2.6.0", "- New UI")
        with (
            patch("core.updater.discover_repo_root", return_value=Path("/repo")),
            patch("core.updater.resolve_remote", return_value=("origin", "https://github.com/JamesZhutheThird/codextop.git")),
            patch("core.updater.local_revision", return_value="same"),
            patch("core.updater.remote_branch_revision", return_value="same"),
            patch("core.updater.latest_github_release", return_value=release),
            patch("core.updater.has_dirty_worktree", return_value=False),
            patch("core.updater.PACKAGE_VERSION", "2.5.0"),
            patch("core.updater.APP_VERSION", "v2.5.0"),
        ):
            status = check_update(branch="main")

        self.assertTrue(status.update_available)
        self.assertEqual(status.latest_version, "2.6.0")
        self.assertEqual(status.release_notes, "- New UI")

    def test_daily_cache_rejects_old_detection_results_and_keeps_release_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "update_check.json"
            path.write_text('{"checked_date":"2026-08-27","update_available":true}\n', encoding="utf-8")
            self.assertIsNone(read_daily_update_check(path))

            expected = DailyUpdateCheck(
                checked_date="2026-08-27",
                update_available=True,
                latest_version="2.6.0",
                release_name="CodexTOP 2.6.0",
                release_notes="- New UI",
            )
            write_daily_update_check(path, expected)
            self.assertEqual(read_daily_update_check(path), expected)


class UpdateReleaseInterfaceTests(unittest.TestCase):
    def test_software_version_root_opens_readme_art_and_colored_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"CODEXTOP_LANG": "en"}):
            state = monitor_state(Path(temp_dir))
            self.assertEqual(
                [key for key, _title, _items in settings_root_entries(state)],
                ["general", "display", "appearance", "software_version"],
            )

            handle_setting_key(state, "f9")
            for _ in range(3):
                handle_setting_key(state, "down")
            sidebar_zones = []
            sidebar = render_sidebar(state, 21, 35, 100, sidebar_zones)
            version_row = next(index for index, line in enumerate(sidebar) if "Software version" in strip_ansi(line))
            self.assertIn("\x1b[33m", sidebar[version_row])

            handle_setting_key(state, "enter")
            self.assertTrue(state.version_dialog_open)
            with patch("ui.app.read_records_if_due", return_value=[]):
                lines, zones = render_frame(state, 120, 36)
            rendered = "\n".join(strip_ansi(line) for line in lines)
            for art_row in CODEXTOP_ASCII_ART:
                self.assertIn(art_row, rendered)
            notes = current_version_changelog()
            self.assertIsNotNone(notes)
            self.assertIn(APP_VERSION, rendered)
            self.assertIn(notes.splitlines()[0], rendered)
            self.assertIn("<- / Close", rendered)

            plain = [strip_ansi(line) for line in lines]
            title = next(index for index, line in enumerate(plain) if "Software version" in line)
            art = [next(index for index, line in enumerate(plain) if row in line) for row in CODEXTOP_ASCII_ART]
            author = next(index for index, line in enumerate(plain) if "@JamesZhutheThird" in line)
            version = next(index for index, line in enumerate(plain) if APP_VERSION in line)
            first_note = next(index for index, line in enumerate(plain) if notes.splitlines()[0] in line)
            self.assertEqual(art[0], title + 2)
            self.assertEqual(art, list(range(art[0], art[0] + len(CODEXTOP_ASCII_ART))))
            self.assertEqual(author, art[-1] + 2)
            self.assertEqual(version, author + 2)
            self.assertEqual(first_note, version + 1)

            check = next(zone for zone in zones if zone.kind == "version_check")
            close = next(zone for zone in zones if zone.kind == "version_close")
            panel_bottom = check.y - 2
            self.assertIn("╰", plain[panel_bottom])
            left, right = plain[panel_bottom].index("╰"), plain[panel_bottom].index("╯")
            self.assertFalse(plain[panel_bottom - 1][left : right + 1].strip(" │"))
            self.assertIn("\x1b[33m", lines[check.y - 1])
            self.assertIn("\x1b[31m", lines[close.y - 1])
            handle_click(state, zones, close.x1, close.y)
            self.assertFalse(state.version_dialog_open)

            handle_setting_key(state, "enter")
            self.assertTrue(state.version_dialog_open)
            self.assertTrue(handle_version_dialog_key(state, "esc"))
            self.assertTrue(state.version_dialog_open)
            self.assertTrue(handle_version_dialog_key(state, "backspace"))
            self.assertFalse(state.version_dialog_open)

    def test_version_check_reuses_the_automatic_update_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"CODEXTOP_LANG": "en"}):
            state = monitor_state(Path(temp_dir))
            state.update_check_path = Path(temp_dir) / "update_check.json"
            state.version_dialog_open = True
            _lines, zones = render_version_overlay([" " * 120] * 36, state, 120, 36)
            check = next(zone for zone in zones if zone.kind == "version_check")

            with patch("ui.settings.start_daily_update_check") as start_check:
                handle_click(state, zones, check.x1, check.y)
            start_check.assert_called_once_with(state, force=True)
            self.assertFalse(state.version_dialog_open)
            self.assertTrue(state.update_dialog_open)

            state.update_checking = True
            checking_lines, checking_zones = render_update_overlay(
                [" " * 120] * 36, state, 120, 36
            )
            checking_plain = [strip_ansi(line) for line in checking_lines]
            checking_title = next(index for index, line in enumerate(checking_plain) if "Check for updates" in line)
            checking_message = next(
                index for index, line in enumerate(checking_plain) if "Checking GitHub Releases" in line
            )
            checking_bottom = next(
                index for index in range(checking_message + 1, len(checking_plain)) if "╰" in checking_plain[index]
            )
            self.assertEqual(checking_message, checking_title + 2)
            self.assertEqual(checking_bottom, checking_message + 2)
            self.assertFalse(checking_plain[checking_title + 1].strip(" │"))
            self.assertFalse(checking_plain[checking_bottom - 1].strip(" │"))
            self.assertEqual([zone.kind for zone in checking_zones], ["update_close"])

            state.update_checking = False
            state.update_available = True
            state.update_latest_version = "2.6.0"
            state.update_release_name = "CodexTOP 2.6.0"
            state.update_release_notes = "- New settings layout"
            release_lines, release_zones = render_update_overlay(
                [" " * 120] * 36, state, 120, 36
            )
            self.assertIn(
                "v2.6.0 release notes",
                "\n".join(strip_ansi(line) for line in release_lines),
            )
            self.assertIn("update_install", {zone.kind for zone in release_zones})

    def test_logo_uses_selected_scheme_diagonally(self) -> None:
        rows = _gradient_logo_rows("bright")
        self.assertEqual(tuple(strip_ansi(row) for row in rows), CODEXTOP_ASCII_ART)
        rendered = "".join(rows)
        self.assertIn("\x1b[38;2;242;96;96m", rendered)
        self.assertIn("\x1b[38;2;96;242;96m", rendered)

    def test_update_button_is_below_author_version_and_blank_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"CODEXTOP_LANG": "en"}):
            state = monitor_state(Path(temp_dir))
            state.update_available = True
            state.update_latest_version = "2.6.0"
            zones = []
            lines = render_sidebar(state, 21, 36, 100, zones)
            plain = [strip_ansi(line) for line in lines]

        author = next(index for index, line in enumerate(plain) if "@JamesZhutheThird" in line)
        version = next(index for index, line in enumerate(plain) if APP_VERSION in line)
        button = next(index for index, line in enumerate(plain) if "F11 /" in line)
        self.assertEqual(version, author + 1)
        self.assertEqual(button, version + 2)
        self.assertIn("\x1b[7m", lines[button])
        self.assertIn("\x1b[33m", lines[button])
        self.assertEqual(next(zone.y for zone in zones if zone.kind == "update_action"), button + 1)

    def test_f11_opens_release_notes_and_enter_requests_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"CODEXTOP_LANG": "en"}):
            state = monitor_state(Path(temp_dir))
            state.update_available = True
            state.update_latest_version = "2.6.0"
            state.update_release_name = "CodexTOP 2.6.0"
            state.update_release_notes = "\n".join(f"- Change {index}" for index in range(1, 22))

            self.assertTrue(handle_setting_key(state, "f11"))
            self.assertTrue(state.update_dialog_open)
            lines, zones = render_update_overlay([" " * 120] * 36, state, 120, 36)
            rendered = "\n".join(strip_ansi(line) for line in lines)
            self.assertIn("v2.6.0 release notes", rendered)
            self.assertIn("Change 1", rendered)

            self.assertTrue(handle_update_dialog_key(state, "down"))
            lines, zones = render_update_overlay([" " * 120] * 36, state, 120, 36)
            rendered = "\n".join(strip_ansi(line) for line in lines)
            self.assertEqual(state.update_notes_scroll, 1)
            self.assertIn("Change 17", rendered)

            install = next(zone for zone in zones if zone.kind == "update_install")
            self.assertFalse(handle_click(state, zones, install.x1, install.y))
            self.assertTrue(state.update_requested)

            state.update_requested = False
            self.assertTrue(handle_update_dialog_key(state, "esc"))
            self.assertTrue(state.update_dialog_open)
            self.assertTrue(handle_update_dialog_key(state, "backspace"))
            self.assertFalse(state.update_dialog_open)


if __name__ == "__main__":
    unittest.main()
