"""SQLite proposal queue. Every function takes an open sqlite3.Connection."""

import json
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path

from .models import (
    KINDS,
    STATUS_AWAITING_REVIEW,
    STATUS_PENDING,
    STATUSES,
    Proposal,
)

_SCAN_COLUMNS = frozenset({
    "kind", "phase", "seen", "queued", "fingerprinted", "errors", "status",
    "message", "started_at", "finished_at",
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    mtime REAL,
    size INTEGER,
    kind TEXT,
    status TEXT NOT NULL,
    current_tags TEXT NOT NULL DEFAULT '{}',
    candidates TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    beets_item_id INTEGER
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    phase TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0,
    queued INTEGER NOT NULL DEFAULT 0,
    fingerprinted INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    message TEXT,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file_state (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=row["id"],
        path=row["path"],
        mtime=row["mtime"],
        size=row["size"],
        kind=row["kind"],
        status=row["status"],
        current_tags=json.loads(row["current_tags"]),
        candidates=json.loads(row["candidates"]),
        error=row["error"],
        beets_item_id=row["beets_item_id"],
    )


def enqueue(conn: sqlite3.Connection, *, path: str, mtime: float, size: int,
            kind: str, current_tags: dict) -> Proposal:
    """Insert a pending proposal; an existing row is returned untouched.

    ON CONFLICT(path) DO NOTHING keeps sticky decisions: status, mtime, size
    and stored tags of an already-queued path are never overwritten.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    conn.execute(
        """INSERT INTO proposals
               (path, mtime, size, kind, status, current_tags, candidates)
           VALUES (?, ?, ?, ?, ?, ?, '[]')
           ON CONFLICT(path) DO NOTHING""",
        (path, mtime, size, kind, STATUS_PENDING, json.dumps(current_tags)),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM proposals WHERE path = ?", (path,)).fetchone()
    if row is None:  # pragma: no cover - only reachable if the row vanishes mid-call
        raise RuntimeError(f"proposal row missing after enqueue: {path}")
    return _proposal(row)


def get(conn: sqlite3.Connection, pid: int) -> Proposal | None:
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    return _proposal(row) if row is not None else None


def get_by_path(conn: sqlite3.Connection, path: str) -> Proposal | None:
    row = conn.execute("SELECT * FROM proposals WHERE path = ?", (path,)).fetchone()
    return _proposal(row) if row is not None else None


def list_by_status(conn: sqlite3.Connection,
                   statuses: Sequence[str]) -> list[Proposal]:
    statuses = tuple(statuses)
    for status in statuses:
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status!r}")
    if not statuses:
        return []
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT * FROM proposals WHERE status IN ({marks}) ORDER BY id",
        statuses,
    ).fetchall()
    return [_proposal(row) for row in rows]


def list_all(conn: sqlite3.Connection) -> list[Proposal]:
    rows = conn.execute("SELECT * FROM proposals ORDER BY id").fetchall()
    return [_proposal(row) for row in rows]


def update_status(conn: sqlite3.Connection, pid: int, status: str, *,
                  error: str | None = None, mtime: float | None = None,
                  size: int | None = None, kind: str | None = None,
                  current_tags: dict | None = None) -> None:
    """Move a proposal to `status`; only the given optional fields change.

    The error column always follows the `error` argument: passing a message
    stores it, passing nothing clears it (an error text is only meaningful
    while the row sits in `error`). `current_tags` is the opposite: None
    leaves the stored tags untouched, a dict replaces them (Task 4's rescan
    refreshes tags only when a changed file is re-queued).
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status!r}")
    if kind is not None and kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    sets = ["status = ?", "error = ?"]
    args: list = [status, error]
    if mtime is not None:
        sets.append("mtime = ?")
        args.append(mtime)
    if size is not None:
        sets.append("size = ?")
        args.append(size)
    if kind is not None:
        sets.append("kind = ?")
        args.append(kind)
    if current_tags is not None:
        sets.append("current_tags = ?")
        args.append(json.dumps(current_tags))
    args.append(pid)
    conn.execute(f"UPDATE proposals SET {', '.join(sets)} WHERE id = ?", args)
    conn.commit()


def set_candidates(conn: sqlite3.Connection, pid: int,
                   candidates: list[dict], *, kind: str | None = None) -> None:
    """Store candidates and move the proposal to `awaiting-review`."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    sets = [
        "candidates = ?",
        f"status = '{STATUS_AWAITING_REVIEW}'",
        "error = NULL",
    ]
    args: list = [json.dumps(candidates)]
    if kind is not None:
        sets.append("kind = ?")
        args.append(kind)
    args.append(pid)
    conn.execute(f"UPDATE proposals SET {', '.join(sets)} WHERE id = ?", args)
    conn.commit()


def clear_candidates(conn: sqlite3.Connection, pid: int) -> None:
    """Empty a proposal's candidate list without touching status or error.

    The caller decides the resulting status (open-path re-queues the row as
    ``pending`` after clearing); unlike ``set_candidates`` this never forces
    ``awaiting-review``.
    """
    conn.execute("UPDATE proposals SET candidates = '[]' WHERE id = ?", (pid,))
    conn.commit()


def set_beets_item_id(conn: sqlite3.Connection, pid: int, item_id: int) -> None:
    conn.execute("UPDATE proposals SET beets_item_id = ? WHERE id = ?",
                 (item_id, pid))
    conn.commit()


# -- file_state: settled-scan cache (path -> last fully-read stat) ---------

def file_state_get(conn: sqlite3.Connection,
                   path: str) -> tuple[float, int] | None:
    row = conn.execute("SELECT mtime, size FROM file_state WHERE path = ?",
                       (path,)).fetchone()
    return None if row is None else (row["mtime"], row["size"])


def file_state_set(conn: sqlite3.Connection, path: str, mtime: float,
                   size: int) -> None:
    conn.execute(
        "INSERT INTO file_state (path, mtime, size) VALUES (?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime, "
        "size = excluded.size",
        (path, mtime, size))
    conn.commit()


def file_state_drop(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM file_state WHERE path = ?", (path,))
    conn.commit()


def file_state_clear(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM file_state")
    conn.commit()


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?",
                       (key,)).fetchone()
    return None if row is None else row["value"]


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
    conn.commit()


def file_state_sweep(conn: sqlite3.Connection, keep: set[str]) -> int:
    """Delete every entry whose path is not in ``keep``; return the count."""
    stale = [r["path"] for r in conn.execute("SELECT path FROM file_state")
             if r["path"] not in keep]
    for path in stale:
        conn.execute("DELETE FROM file_state WHERE path = ?", (path,))
    conn.commit()
    return len(stale)


def start_scan(conn: sqlite3.Connection, *, kind: str) -> int:
    phase = "scanning" if kind == "scan" else "fingerprinting"
    cur = conn.execute(
        """INSERT INTO scans (kind, phase, status, started_at)
           VALUES (?, ?, 'running', ?)""",
        (kind, phase, time.time()),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def update_scan(conn: sqlite3.Connection, scan_id: int, **fields) -> None:
    if not fields:
        return
    unknown = set(fields) - _SCAN_COLUMNS
    if unknown:
        raise ValueError(f"unknown scan fields: {sorted(unknown)}")
    sets = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(f"UPDATE scans SET {sets} WHERE id = ?",
                 [*fields.values(), scan_id])
    conn.commit()


def latest_scan(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row is not None else None
