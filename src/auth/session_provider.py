"""Update Codex session provider metadata and its SQLite index together."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import UUID


def session_selector(value: str) -> tuple[str | None, float | None]:
    value = value.strip().lower()
    if value == "all":
        return None, None
    match = re.fullmatch(r"([1-9]\d*)([hd])", value)
    if match:
        return None, time.time() - int(match[1]) * (3600 if match[2] == "h" else 86400)
    try:
        return str(UUID(value)), None
    except ValueError as exc:
        raise ValueError("Expected a session UUID, all, Nh, or Nd (e.g. 1h, 7d)") from exc


class SessionProviderUpdate:
    def __init__(self, root: Path, selector: str, source: str | None = None) -> None:
        session_id, cutoff = session_selector(selector)
        self.root = root.resolve()
        self.databases: list[Path] = []
        self.rows: dict[Path, list[tuple[str, str]]] = {}
        candidates: dict[Path, tuple[str, float]] = {}
        indexed_paths: set[Path] = set()
        databases = sorted(root.glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]), reverse=True)
        for database in databases:
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
                if not {"id", "model_provider", "rollout_path", "updated_at"} <= columns:
                    continue
                rows = connection.execute(
                    "SELECT id, model_provider, rollout_path, updated_at FROM threads"
                ).fetchall()
            self.databases.append(database)
            self.rows[database] = []
            for ident, provider, path, updated_at in rows:
                indexed_paths.add(Path(path).resolve())
                if session_id and ident != session_id:
                    continue
                if cutoff is not None and updated_at < cutoff:
                    continue
                if source is not None and provider != source:
                    continue
                resolved = Path(path).resolve()
                if not resolved.is_relative_to(self.root):
                    raise ValueError(f"Session rollout outside Codex home: {ident}")
                if not resolved.is_file():
                    raise ValueError(f"Missing session rollout: {ident}")
                candidates[resolved] = (ident, updated_at)
                self.rows[database].append((ident, provider))
            break  # Codex uses the newest supported state database.

        # Legacy sessions may have no SQLite row yet, including archived rollouts.
        for directory in (root / "sessions", root / "archived_sessions"):
            for path in directory.rglob("rollout-*.jsonl"):
                resolved = path.resolve()
                if resolved in indexed_paths or not resolved.is_relative_to(self.root):
                    continue
                if cutoff is not None and path.stat().st_mtime < cutoff:
                    continue
                if session_id and session_id not in path.name:
                    continue
                candidates[resolved] = ("", path.stat().st_mtime)

        self.files: dict[Path, dict] = {}
        for path, (indexed_id, updated_at) in candidates.items():
            with path.open("rb") as handle:
                first = handle.readline()
            record = json.loads(first)
            payload = record.get("payload", {})
            ident = payload.get("id") or payload.get("session_id")
            if record.get("type") != "session_meta" or not ident:
                raise ValueError(f"Invalid session metadata: {path}")
            if indexed_id and ident != indexed_id:
                raise ValueError(f"Session index/rollout ID mismatch: {indexed_id}")
            if session_id and ident != session_id:
                continue
            if source is not None and payload.get("model_provider") != source:
                # An index disagreement must not silently update only one copy.
                if indexed_id:
                    raise ValueError(f"Session provider index/rollout mismatch: {ident}")
                continue
            self.files[path] = record
        if session_id and not self.files:
            raise ValueError(f"Session not found: {session_id}")

    def apply(self, provider: str) -> dict:
        changed = {path: record for path, record in self.files.items()
                   if record["payload"].get("model_provider") != provider}
        if not changed:
            return {"updated": 0, "provider": provider, "backup": None}
        backup = self.root / "codextop" / "session_provider_backups"
        backup.mkdir(parents=True, exist_ok=True)
        manifest = backup / f"{datetime.now():%Y%m%dT%H%M%S}-{time.time_ns()}.json"
        manifest.write_text(json.dumps({
            "target_provider": provider,
            "sessions": [{"path": str(path), "session_meta": record} for path, record in changed.items()],
            "databases": {str(path): rows for path, rows in self.rows.items()},
        }, ensure_ascii=False, indent=2) + "\n")
        manifest.chmod(0o600)
        connections: list[sqlite3.Connection] = []
        replaced: list[tuple[Path, Path]] = []
        with tempfile.TemporaryDirectory(dir=backup) as temporary:
            staged = Path(temporary)
            try:
                for database in self.databases:
                    connection = sqlite3.connect(database, timeout=5)
                    connections.append(connection)
                    connection.execute("BEGIN IMMEDIATE")
                    for ident, old_provider in self.rows[database]:
                        connection.execute(
                            "UPDATE threads SET model_provider = ? WHERE id = ? AND model_provider = ?",
                            (provider, ident, old_provider),
                        )
                for i, (path, record) in enumerate(changed.items()):
                    original = staged / f"{i}.original"
                    shutil.copy2(path, original)
                    replacement = staged / f"{i}.new"
                    updated = dict(record, payload=dict(record["payload"], model_provider=provider))
                    with path.open("rb") as source, replacement.open("wb") as target:
                        source.readline()
                        target.write(json.dumps(updated, ensure_ascii=False).encode() + b"\n")
                        shutil.copyfileobj(source, target)
                    shutil.copystat(path, replacement)
                    os.replace(replacement, path)
                    replaced.append((path, original))
                for connection in connections:
                    connection.commit()
            except Exception:
                for connection in connections:
                    connection.rollback()
                for path, original in reversed(replaced):
                    os.replace(original, path)
                raise
            finally:
                for connection in connections:
                    connection.close()
        return {"updated": len(changed), "provider": provider, "backup": str(manifest)}
