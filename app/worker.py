"""Background worker: scan + fingerprint jobs with progress.

DB connections are opened inside each function and never shared across
threads. A single module-level lock serialises jobs.
"""

import threading
from pathlib import Path

from app import db, scan
from app.beets_adapter import candidates_for, default_acoustid_key
from app.config import Config
from app.fingerprint import AcoustidFailure, recording_ids

_lock = threading.Lock()

_running = False
_phase = "idle"
_seen = 0
_queued = 0
_fingerprinted = 0
_errors = 0
_message = None
_db_path = None


def _set_db_path(cfg: Config) -> None:
    global _db_path
    _db_path = cfg.db_path


def run_fingerprint_sync(cfg: Config) -> dict:
    """Fingerprint every ``pending`` proposal. Returns a progress dict."""
    global _running, _phase, _seen, _queued, _fingerprinted, _errors, _message

    _set_db_path(cfg)
    from app import beets_adapter

    beets_adapter.ensure_setup(cfg)

    conn = db.connect(cfg.db_path)
    db.init_db(conn)

    scan_row = db.latest_scan(conn)
    scan_id = scan_row["id"] if scan_row is not None else None
    if scan_row is not None:
        _seen = scan_row.get("seen", 0) or 0
        _queued = scan_row.get("queued", 0) or 0
        _fingerprinted = scan_row.get("fingerprinted", 0) or 0
        _errors = scan_row.get("errors", 0) or 0
    else:
        _seen = _queued = _fingerprinted = _errors = 0

    _running = True
    _phase = "fingerprinting"
    _message = None

    try:
        acoustid_key = cfg.acoustid_app_key or default_acoustid_key()
        pending = list(db.list_by_status(conn, ("pending",)))
        for prop in pending:
            try:
                ids = recording_ids(Path(prop.path), acoustid_key)
            except AcoustidFailure as exc:
                db.update_status(conn, prop.id, "error", error=str(exc))
                _errors += 1
                if scan_id is not None:
                    db.update_scan(conn, scan_id, phase="fingerprinting",
                                   fingerprinted=_fingerprinted, errors=_errors)
                continue

            cands = candidates_for(Path(prop.path), ids)
            if not cands:
                if prop.kind == "manual":
                    db.set_candidates(conn, prop.id, [], kind=None)
                else:
                    db.set_candidates(conn, prop.id, [], kind="unidentified")
            else:
                db.set_candidates(conn, prop.id, cands)

            _fingerprinted += 1
            if scan_id is not None:
                db.update_scan(conn, scan_id, phase="fingerprinting",
                               fingerprinted=_fingerprinted, errors=_errors)

        if scan_id is not None:
            db.update_scan(conn, scan_id, phase="done", status="done",
                           fingerprinted=_fingerprinted, errors=_errors)
        _phase = "done"
        return {
            "running": _running,
            "phase": _phase,
            "seen": _seen,
            "queued": _queued,
            "fingerprinted": _fingerprinted,
            "errors": _errors,
            "message": _message,
        }
    finally:
        _running = False
        conn.close()


def run_scan_sync(cfg: Config) -> dict:
    """Run a full scan then fingerprint. Returns final status dict."""
    global _running, _phase, _seen, _queued, _fingerprinted, _errors, _message

    _set_db_path(cfg)
    from app import beets_adapter

    beets_adapter.ensure_setup(cfg)

    _running = True
    _phase = "scanning"
    _seen = _queued = _fingerprinted = _errors = 0
    _message = None

    try:
        stats = scan.run(cfg)
        _seen = stats.get("seen", 0)
        _queued = stats.get("queued", 0)
        _errors = stats.get("errors", 0)
        # fingerprint phase
        run_fingerprint_sync(cfg)
        _phase = "done"
        return status()
    finally:
        _running = False


def start_job(cfg: Config, kind: str = "scan") -> bool:
    """Spawn a daemon worker thread. Returns False if already busy."""
    _set_db_path(cfg)
    acquired = _lock.acquire(blocking=False)
    if not acquired:
        return False

    def _target() -> None:
        try:
            if kind == "fingerprint":
                run_fingerprint_sync(cfg)
            else:
                run_scan_sync(cfg)
        finally:
            _lock.release()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return True


def is_running() -> bool:
    return _lock.locked() or _running


def status() -> dict:
    global _db_path
    last_scan = None
    if _db_path is not None:
        try:
            conn = db.connect(_db_path)
            db.init_db(conn)
            last_scan = db.latest_scan(conn)
            conn.close()
        except Exception:
            pass
    return {
        "running": is_running(),
        "phase": _phase,
        "seen": _seen,
        "queued": _queued,
        "fingerprinted": _fingerprinted,
        "errors": _errors,
        "message": _message,
        "last_scan": last_scan,
    }
