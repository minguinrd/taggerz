"""Background worker: scan + fingerprint jobs with progress.

DB connections are opened inside each function and never shared across
threads. A single module-level lock serialises jobs.
"""

import threading
import traceback
from pathlib import Path

from app import activity, db, scan
from app.beets_adapter import (
    ApplyFailed,
    FileChanged,
    candidates_for,
    default_acoustid_key,
)
from app.config import Config
from app.fingerprint import AcoustidFailure, recording_ids
from app.models import (
    KIND_MANUAL,
    KIND_ORGANIZE_ONLY,
    KIND_UNIDENTIFIED,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PENDING,
)

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


def _finish_apply(conn, row, dest: Path, item_id: int | None) -> None:
    """Mark an auto-applied row done and settle its destination.

    The row's mtime/size are refreshed from the destination so its expect
    guard stays truthful; ``file_state`` is recorded for ``dest`` so the
    next scan skips it, and the source entry is dropped when the path moved.
    A stat failure is swallowed: the apply already succeeded, and the next
    scan simply re-reads the file.
    """
    try:
        st = Path(dest).stat()
    except OSError:
        st = None
    if st is not None:
        db.update_status(conn, row.id, STATUS_DONE,
                         mtime=float(st.st_mtime), size=int(st.st_size))
    else:
        db.update_status(conn, row.id, STATUS_DONE)
    if item_id is not None:
        db.set_beets_item_id(conn, row.id, item_id)
    if st is not None:
        db.file_state_set(conn, str(dest), float(st.st_mtime),
                          int(st.st_size))
    if str(row.path) != str(dest):
        db.file_state_drop(conn, str(row.path))


def _fail_apply(conn, row, exc: Exception) -> str:
    """Park a row whose auto-apply failed; return the stored message."""
    if isinstance(exc, FileChanged):
        msg = str(exc) or "file changed"
    elif isinstance(exc, ApplyFailed):
        msg = str(exc)
    else:
        msg = f"{type(exc).__name__}: {exc}"
    db.update_status(conn, row.id, STATUS_ERROR, error=msg)
    if getattr(exc, "item_id", None) is not None:
        db.set_beets_item_id(conn, row.id, exc.item_id)
    return msg


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
    _phase = "applying"
    _message = None

    try:
        # Phase 1: pure moves. Correctly tagged files at the wrong path are
        # organized without fingerprinting (no match is needed to move).
        if scan_id is not None:
            db.update_scan(conn, scan_id, phase="applying",
                           fingerprinted=_fingerprinted, errors=_errors)
        for prop in list(db.list_by_status(conn, (STATUS_PENDING,))):
            if prop.kind != KIND_ORGANIZE_ONLY:
                continue
            activity.start("move", prop.path)
            try:
                dest, item_id = beets_adapter.apply_approved(
                    Path(prop.path), None, move_only=True,
                    expect=(prop.mtime, prop.size),
                    beets_item_id=prop.beets_item_id,
                )
            except Exception as exc:
                # Per-file catch-all: one broken file never aborts the job.
                if not isinstance(exc, (ApplyFailed, FileChanged)):
                    traceback.print_exc()
                msg = _fail_apply(conn, prop, exc)
                activity.finish(ok=False, detail=msg)
                _errors += 1
            else:
                _finish_apply(conn, prop, dest, item_id)
                activity.finish(ok=True, detail=f"→ {dest}")
                _fingerprinted += 1
            if scan_id is not None:
                db.update_scan(conn, scan_id, phase="applying",
                               fingerprinted=_fingerprinted, errors=_errors)

        _phase = "fingerprinting"
        acoustid_key = cfg.acoustid_app_key or default_acoustid_key()
        if scan_id is not None:
            db.update_scan(conn, scan_id, phase="fingerprinting",
                           fingerprinted=_fingerprinted, errors=_errors)
        pending = list(db.list_by_status(conn, (STATUS_PENDING,)))
        for prop in pending:
            if prop.kind == KIND_ORGANIZE_ONLY:
                continue  # phase 1 already consumed these
            activity.start("fingerprint", prop.path)
            try:
                ids = recording_ids(Path(prop.path), acoustid_key)
                cands = candidates_for(Path(prop.path), ids)
                if not cands:
                    if prop.kind == KIND_MANUAL:
                        db.set_candidates(conn, prop.id, [], kind=None)
                    else:
                        db.set_candidates(conn, prop.id, [],
                                          kind=KIND_UNIDENTIFIED)
                else:
                    db.set_candidates(conn, prop.id, cands)
            except AcoustidFailure as exc:
                activity.finish(ok=False, detail=str(exc))
                db.update_status(conn, prop.id, STATUS_ERROR, error=str(exc))
                _errors += 1
                if scan_id is not None:
                    db.update_scan(conn, scan_id, phase="fingerprinting",
                                   fingerprinted=_fingerprinted, errors=_errors)
                continue
            except Exception as exc:
                # One broken file must not abort the whole pass: park it as
                # an error and keep going (mirrors scan's per-file handling).
                traceback.print_exc()
                activity.finish(ok=False,
                                detail=f"{type(exc).__name__}: {exc}")
                db.update_status(conn, prop.id, STATUS_ERROR,
                                 error=f"{type(exc).__name__}: {exc}")
                _errors += 1
                if scan_id is not None:
                    db.update_scan(conn, scan_id, phase="fingerprinting",
                                   fingerprinted=_fingerprinted, errors=_errors)
                continue

            activity.finish(ok=True)
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

    activity.start("scan")
    try:
        stats = scan.run(cfg)
        activity.finish(ok=True, detail=f"{stats.get('seen', 0)} files seen")
        _seen = stats.get("seen", 0)
        _queued = stats.get("queued", 0)
        _errors = stats.get("errors", 0)
        # fingerprint phase
        run_fingerprint_sync(cfg)
        _phase = "done"
        return status()
    except Exception as exc:
        activity.finish(ok=False, detail=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        _running = False


def start_job(cfg: Config, kind: str = "scan") -> bool:
    """Spawn a daemon worker thread. Returns False if already busy."""
    _set_db_path(cfg)
    acquired = _lock.acquire(blocking=False)
    if not acquired:
        return False

    def _target() -> None:
        global _phase, _message
        try:
            if kind == "fingerprint":
                run_fingerprint_sync(cfg)
            else:
                run_scan_sync(cfg)
        except Exception as exc:
            # A crashed job must not leave status() stuck on a live phase.
            _phase = "error"
            _message = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            _lock.release()

    try:
        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
    except BaseException:
        # Thread never ran, so _target's finally cannot release the lock.
        _lock.release()
        raise
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
        "activity": activity.snapshot(),
    }
