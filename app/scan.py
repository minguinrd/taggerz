"""Library scan: classify files, enqueue proposals, apply rescan stickiness.

Read-only against the library: only reads tags and stats; never writes audio.
"""

import os
import time
from collections.abc import Iterator
from pathlib import Path

from app import beets_adapter, db
from app.config import Config
from app.models import (
    KIND_NEEDS_TAGS,
    KIND_ORGANIZE_ONLY,
    STATUS_AWAITING_REVIEW,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_SKIPPED,
)


AUDIO_EXTS: set[str] = {
    ".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".wma",
    ".aiff", ".aif", ".ape", ".wv",
}


def missing_core(fields: dict) -> bool:
    for key in ("artist", "album", "title"):
        val = fields.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            return True
    track = fields.get("track")
    if track is None or track == 0:
        return True
    return False


def classify(fields: dict, actual: Path, expected: Path | None) -> str | None:
    if missing_core(fields):
        return KIND_NEEDS_TAGS
    if expected != actual:
        return KIND_ORGANIZE_ONLY
    return None


def walk(music_dir: Path) -> Iterator[Path]:
    for root, dirs, files in os.walk(music_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            p = Path(root) / name
            if p.suffix.lower() in AUDIO_EXTS:
                yield p


def run(cfg: Config) -> dict:
    beets_adapter.ensure_setup(cfg)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    scan_id = db.start_scan(conn, kind="scan")
    db.update_scan(conn, scan_id, phase="scanning")

    seen = 0
    queued = 0
    errors = 0
    missing_files = 0

    audio_paths = list(walk(cfg.music_dir))
    seen = len(audio_paths)
    audio_strs = {str(p) for p in audio_paths}
    db.file_state_sweep(conn, audio_strs)
    # The settled cache is only valid under the path config it was built
    # with: a changed template must re-evaluate (and reorganize) files.
    sig = beets_adapter.path_signature()
    if db.meta_get(conn, "file_state_signature") != sig:
        db.file_state_clear(conn)
        db.meta_set(conn, "file_state_signature", sig)

    # Park open rows whose file has vanished.
    for row in db.list_all(conn):
        if row.status in (STATUS_PENDING, STATUS_AWAITING_REVIEW):
            if str(row.path) not in audio_strs and not Path(row.path).exists():
                db.update_status(conn, row.id, STATUS_ERROR,
                                 error="file no longer exists")
                missing_files += 1

    for p in audio_paths:
        try:
            st = p.stat()
        except OSError:
            errors += 1                    # vanished mid-walk: counted, never
            continue                       # crashes the scan
        mtime = float(st.st_mtime)
        size = int(st.st_size)

        # Decided rows stay parked (skipped/error stay; pending/awaiting
        # stay as-is unless vanished, handled above) — no tag read needed.
        row = db.get_by_path(conn, str(p))
        if row is not None and row.status in (
                STATUS_SKIPPED, STATUS_ERROR, STATUS_PENDING,
                STATUS_AWAITING_REVIEW):
            continue
        # Settled: unchanged since the last fully successful read.
        if db.file_state_get(conn, str(p)) == (mtime, size):
            continue

        try:
            fields = beets_adapter.read_fields(p)
        except Exception:
            errors += 1
            continue                       # not recorded -> retried next scan
        try:
            expected = None if missing_core(fields) else beets_adapter.expected_path(p)
        except Exception:
            errors += 1
            continue
        kind = classify(fields, p, expected)
        db.file_state_set(conn, str(p), mtime, size)  # only after full success

        if row is None:
            if kind is None:
                continue
            db.enqueue(conn, path=str(p), mtime=mtime, size=size, kind=kind,
                       current_tags=fields)
            queued += 1
            continue

        if row.status == STATUS_DONE:      # the only status reaching here
            if (row.mtime, row.size) == (mtime, size):
                continue
            if kind is None:
                db.update_status(conn, row.id, STATUS_DONE, mtime=mtime,
                                 size=size, current_tags=fields)
            else:
                db.update_status(conn, row.id, STATUS_PENDING, kind=kind,
                                 mtime=mtime, size=size, current_tags=fields)
                queued += 1

    db.update_scan(conn, scan_id, phase="done", status="done", seen=seen,
                   queued=queued, errors=errors, fingerprinted=0,
                   finished_at=time.time())
    conn.close()
    return {"seen": seen, "queued": queued, "errors": errors,
            "missing_files": missing_files}
