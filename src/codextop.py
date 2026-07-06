#!/usr/bin/env python3
"""Compatibility wrapper for the CodexTOP terminal application."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ui.app import main
else:
    from ui.app import main


if __name__ == "__main__":
    raise SystemExit(main())
