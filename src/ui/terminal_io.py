"""Terminal session management, mouse parsing, and key parsing."""

from __future__ import annotations

import os
import re
import select
import sys
import termios
import tty
from typing import Any


MOUSE_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
CSI_RE = re.compile(rb"\x1b\[[0-9;?<>]*[A-Za-z~]")
KEY_PATTERNS = [
    (re.compile(rb"\x1b\[20(?:;[0-9]+)?~"), "f9"),
    (re.compile(rb"\x1b\[21(?:;[0-9]+)?~"), "f10"),
    (re.compile(rb"\x1b\[23(?:;[0-9]+)?~"), "f11"),
    (re.compile(rb"\x1b\[(?:1;[0-9]+)?A"), "up"),
    (re.compile(rb"\x1b\[(?:1;[0-9]+)?B"), "down"),
    (re.compile(rb"\x1bOA"), "up"),
    (re.compile(rb"\x1bOB"), "down"),
]

class TerminalSession:
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.old: list[Any] | None = None
        self.buffer = b""

    def __enter__(self) -> "TerminalSession":
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h\x1b[2J\x1b[H")
        sys.stdout.flush()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if self.old is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def read(self) -> bytes:
        chunks = []
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if len(chunk) < 4096:
                break
        return b"".join(chunks)


def parse_input(session: TerminalSession) -> tuple[bool, list[tuple[int, int]], list[str]]:
    session.buffer += session.read()
    data = session.buffer
    keep_running = True
    clicks: list[tuple[int, int]] = []
    keys: list[str] = []
    trailing = b""
    pos = 0

    while pos < len(data):
        byte = data[pos:pos + 1]
        if byte == b"\x03":
            keep_running = False
            pos += 1
            continue
        if byte in {b"q", b"Q"}:
            keep_running = False
            pos += 1
            continue
        if byte in {b"\r", b"\n"}:
            keys.append("enter")
            pos += 1
            continue
        if byte != b"\x1b":
            pos += 1
            continue

        mouse = MOUSE_RE.match(data, pos)
        if mouse:
            button = int(mouse.group(1))
            x = int(mouse.group(2))
            y = int(mouse.group(3))
            event = mouse.group(4)
            if event == b"M" and button & 3 == 0:
                clicks.append((x, y))
            pos = mouse.end()
            continue

        matched_key = False
        for pattern, key in KEY_PATTERNS:
            match = pattern.match(data, pos)
            if not match:
                continue
            if key == "f10":
                keep_running = False
            else:
                keys.append(key)
            pos = match.end()
            matched_key = True
            break
        if matched_key:
            continue

        if pos == len(data) - 1:
            keys.append("esc")
            pos += 1
            continue

        csi = CSI_RE.match(data, pos)
        if csi:
            pos = csi.end()
            continue
        if data[pos:pos + 2] == b"\x1b[":
            trailing = data[pos:]
            break
        keys.append("esc")
        pos += 1

    session.buffer = trailing
    return keep_running, clicks, keys
