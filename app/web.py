"""Web routes: queue, scan control, status, open-by-path."""

from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from app import db, scan
from app.beets_adapter import read_fields
from app.config import Config
from app.models import STATUSES
from app import worker

bp = Blueprint("web", __name__)


def _cfg() -> Config:
    return current_app.config["CFG"]


def group_key(proposal) -> str:
    src = proposal.candidates[0] if proposal.candidates else proposal.current_tags
    if not isinstance(src, dict):
        src = {}
    aa = (src.get("albumartist") or "").strip() if isinstance(src.get("albumartist"), str) else (src.get("albumartist") or "")
    al = (src.get("album") or "").strip() if isinstance(src.get("album"), str) else (src.get("album") or "")
    aa = str(aa).strip() if aa is not None else ""
    al = str(al).strip() if al is not None else ""
    if not aa and not al:
        return "(no album)"
    return f"{aa} | {al}"


def _badge(proposal) -> str:
    if proposal.status == "pending":
        return "fingerprinting…"
    if proposal.status == "error":
        if proposal.error and proposal.error.startswith("destination exists"):
            return "destination exists"
        return "error"
    kind = proposal.kind
    if kind == "needs-tags":
        return "needs tags"
    if kind == "organize-only":
        return "organize only"
    if kind == "unidentified":
        return "unidentified"
    if kind == "manual":
        return "manual"
    return kind


@bp.route("/")
def queue_view():
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    requested = request.args.get("status")
    if requested in STATUSES:
        proposals = db.list_by_status(conn, (requested,))
    else:
        proposals = db.list_by_status(conn, ("pending", "awaiting-review", "error"))
    conn.close()

    groups: dict[str, list] = {}
    for p in proposals:
        groups.setdefault(group_key(p), []).append(p)

    return render_template(
        "queue.html",
        groups=list(groups.items()),
        status_filter=requested if requested in STATUSES else "",
    )


@bp.route("/api/status")
def api_status():
    return worker.status()


@bp.route("/scan", methods=["POST"])
def scan_post():
    cfg = _cfg()
    if worker.start_job(cfg, "scan"):
        flash("scan started")
    else:
        flash("scan already running")
    return redirect(url_for("web.queue_view"))


@bp.route("/open-path", methods=["POST"])
def open_path():
    cfg = _cfg()
    raw = request.form.get("path", "").strip()
    if not raw:
        flash("path is required")
        return redirect(url_for("web.queue_view"))
    music_dir = cfg.music_dir.resolve()
    candidate = Path(raw).expanduser()
    if not candidate.exists():
        flash("file does not exist")
        return redirect(url_for("web.queue_view"))
    try:
        resolved = candidate.resolve()
    except Exception:
        flash("invalid path")
        return redirect(url_for("web.queue_view"))
    if music_dir not in resolved.parents and resolved != music_dir:
        flash("path must be inside the music library")
        return redirect(url_for("web.queue_view"))
    if resolved.suffix.lower() not in scan.AUDIO_EXTS:
        flash("not an audio file")
        return redirect(url_for("web.queue_view"))
    if not resolved.is_file():
        flash("not a file")
        return redirect(url_for("web.queue_view"))

    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    from app import beets_adapter

    beets_adapter.ensure_setup(cfg)
    tags = beets_adapter.read_fields(resolved)
    stat = resolved.stat()
    existing = db.get_by_path(conn, str(resolved))
    if existing is not None:
        db.update_status(
            conn, existing.id, "pending", kind="manual",
            current_tags=tags, mtime=stat.st_mtime, size=stat.st_size,
        )
        # clear stale candidates
        conn.execute("UPDATE proposals SET candidates = '[]' WHERE id = ?", (existing.id,))
        conn.commit()
        pid = existing.id
    else:
        prop = db.enqueue(
            conn, path=str(resolved), mtime=stat.st_mtime, size=stat.st_size,
            kind="manual", current_tags=tags,
        )
        pid = prop.id
    conn.close()

    if not worker.start_job(cfg, "fingerprint"):
        flash("queued; will be identified by the running job")
    return redirect(url_for("web.proposal_detail", pid=pid))


# placeholder for task 9 route name compatibility
@bp.route("/proposal/<int:pid>")
def proposal_detail(pid):
    # Task 9 implements detail; for now 404-ish placeholder to avoid build errors
    return ("", 404)
