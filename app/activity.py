"""Activity log: what the app is doing / just did, surfaced in /api/status."""

import threading
import time

MAX_EVENTS = 40

_lock = threading.Lock()
_current: dict | None = None
_recent: list[dict] = []


def start(op: str, path: str | None = None) -> None:
    """Mark the beginning of an operation (shown as 'now doing')."""
    global _current
    with _lock:
        _current = {"op": op, "path": path, "started_at": time.time()}


def finish(*, ok: bool = True, detail: str | None = None) -> None:
    """Close the current operation into the recent log."""
    global _current
    with _lock:
        cur = _current
        _current = None
    if cur is None:
        return
    _append(cur["op"], cur["path"], ok, detail, started_at=cur["started_at"])


def record(op: str, path: str | None, *, ok: bool = True,
           detail: str | None = None) -> None:
    """Log a one-shot operation that had no start() (e.g. approve)."""
    _append(op, path, ok, detail, started_at=None)


def _append(op: str, path: str | None, ok: bool, detail: str | None,
            started_at: float | None) -> None:
    ev: dict = {"op": op, "path": path, "ok": ok, "detail": detail,
                "at": time.time()}
    if started_at is not None:
        ev["duration"] = round(ev["at"] - started_at, 2)
    with _lock:
        _recent.append(ev)
        del _recent[:-MAX_EVENTS]


def snapshot() -> dict:
    """Thread-safe copy for status(): current op + recent, newest first."""
    with _lock:
        cur = dict(_current) if _current is not None else None
        recent = [dict(e) for e in reversed(_recent)]
    if cur is not None:
        cur["duration"] = round(time.time() - cur["started_at"], 2)
    return {"current": cur, "recent": recent}


def reset() -> None:
    """Test-only: clear all state."""
    global _current
    with _lock:
        _current = None
        _recent.clear()
