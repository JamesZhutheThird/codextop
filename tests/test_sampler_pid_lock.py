"""Tests for the CodexTOP sampler process lock."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = ROOT / "scripts" / "start_codextop_backend.sh"
STOP_SCRIPT = ROOT / "scripts" / "stop_codextop_backend.sh"
sys.path.insert(0, str(ROOT / "src"))

from quota import codex_quota_sampler as sampler


class SamplerPidLockTests(unittest.TestCase):
    def test_start_script_replaces_live_unrelated_pid(self) -> None:
        unrelated = subprocess.Popen(["sleep", "30"])
        sampler_pid: int | None = None
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                codex_dir = Path(tmp_dir) / "codex"
                log_dir = codex_dir / "codextop" / "log"
                log_dir.mkdir(parents=True)
                pid_path = log_dir / "sampler.pid"
                pid_path.write_text(f"{unrelated.pid}\n", encoding="utf-8")
                env = os.environ.copy()
                env["CODEXTOP_CODEX_DIR"] = str(codex_dir)

                subprocess.run([str(START_SCRIPT)], check=True, env=env)
                for _ in range(40):
                    try:
                        sampler_pid = int(pid_path.read_text(encoding="utf-8").strip())
                    except (FileNotFoundError, ValueError):
                        sampler_pid = None
                    if sampler_pid and sampler_pid != unrelated.pid:
                        break
                    time.sleep(0.05)

                self.assertIsNotNone(sampler_pid)
                self.assertNotEqual(sampler_pid, unrelated.pid)
                cmdline = Path(f"/proc/{sampler_pid}/cmdline").read_bytes()
                self.assertIn(b"codex_quota_sampler.py", cmdline)
                self.assertIsNone(unrelated.poll())

                subprocess.run([str(STOP_SCRIPT)], check=True, env=env)
                sampler_pid = None
                self.assertIsNone(unrelated.poll())
        finally:
            if sampler_pid is not None:
                try:
                    os.kill(sampler_pid, 15)
                except ProcessLookupError:
                    pass
            unrelated.terminate()
            unrelated.wait()

    def test_live_unrelated_stale_pid_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_path = Path(tmp_dir) / "sampler.pid"
            pid_path.write_text(f"{os.getppid()}\n", encoding="utf-8")

            sampler.acquire_pid_lock(pid_path)

            self.assertEqual(pid_path.read_text(encoding="utf-8"), f"{os.getpid()}\n")

    def test_held_sampler_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_path = Path(tmp_dir) / "sampler.pid"
            with pid_path.open("w+", encoding="utf-8") as owner:
                owner.write("4321\n")
                owner.flush()
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                with self.assertRaisesRegex(RuntimeError, "sampler already running with pid 4321"):
                    sampler.acquire_pid_lock(pid_path)


if __name__ == "__main__":
    unittest.main()
