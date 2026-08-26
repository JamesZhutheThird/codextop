#!/usr/bin/env python3
"""Compatibility wrapper for the CodexTOP terminal application."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.i18n import LANGUAGE_ENV, available_languages
from core.paths import default_paths


def _activate_saved_language() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--log-dir", type=Path, default=default_paths().log_dir)
    parser.add_argument("--state-file", default="codextop_state.json")
    args, _unknown = parser.parse_known_args()
    try:
        payload = json.loads((args.log_dir.expanduser() / args.state_file).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    language = payload.get("language") if isinstance(payload, dict) else None
    if language in available_languages():
        os.environ[LANGUAGE_ENV] = language


_activate_saved_language()

from ui.app import main


if __name__ == "__main__":
    raise SystemExit(main())
