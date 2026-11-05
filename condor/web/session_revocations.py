"""Durable logout revocations shared by HTTP, WebSockets and MCP processes."""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import closing

from condor.paths import data_dir

log = logging.getLogger(__name__)


def _path():
    return data_dir() / "session_revocations.sqlite3"


def is_revoked(jti: str) -> bool:
    path = _path()
    if not path.exists():
        return False
    try:
        # Read-only: a missing or corrupt store must not silently become empty.
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as db:
            return (
                db.execute(
                    "SELECT 1 FROM revocations WHERE jti = ? AND expires > ?",
                    (jti, time.time()),
                ).fetchone()
                is not None
            )
    except sqlite3.Error:
        log.exception("Cannot read session revocations; refusing authentication")
        return True


def revoke(jti: str, expires: int) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create privately before SQLite opens the file, including under a loose umask.
    import os

    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS revocations "
            "(jti TEXT PRIMARY KEY, expires INTEGER NOT NULL)"
        )
        db.execute("DELETE FROM revocations WHERE expires <= ?", (time.time(),))
        db.execute(
            "INSERT OR REPLACE INTO revocations(jti, expires) VALUES (?, ?)",
            (jti, expires),
        )
