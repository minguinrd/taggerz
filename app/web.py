"""Web routes: queue, scan control, status, open-by-path."""

from pathlib import Path

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

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


def _reopen(conn, proposal, cfg) -> None:
    """Shared reset/retry logic: reopen a proposal for reprocessing."""
    if proposal is None:
        return
    if proposal.candidates:
        db.set_candidates(conn, proposal.id, proposal.candidates)
    else:
        db.update_status(conn, proposal.id, "pending")
        worker.start_job(cfg, "fingerprint")


@bp.route("/proposal/<int:pid>")
def proposal_detail(pid):
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    proposal = db.get(conn, pid)
    conn.close()
    if proposal is None:
        abort(404)
    from app import beets_adapter

    beets_adapter.ensure_setup(cfg)
    return render_template(
        "proposal.html",
        proposal=proposal,
        candidate_fields=beets_adapter.CANDIDATE_FIELDS,
    )


@bp.route("/proposal/<int:pid>/approve", methods=["POST"])
def approve(pid):
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    proposal = db.get(conn, pid)
    if proposal is None:
        conn.close()
        abort(404)

    from app import beets_adapter

    beets_adapter.ensure_setup(cfg)

    action = request.form.get("action", "")
    kind = proposal.kind

    # reject unidentified without edits
    if kind == "unidentified" and action == "candidate":
        conn.close()
        flash("must edit first")
        return redirect(url_for("web.proposal_detail", pid=pid))

    # move requires organize-only
    if action == "move" and kind != "organize-only":
        conn.close()
        flash("move only allowed for organize-only proposals")
        return redirect(url_for("web.proposal_detail", pid=pid))

    expect = (proposal.mtime, proposal.size) if proposal.status == "awaiting-review" else None

    move_only = False
    fields = None
    if action == "candidate":
        idx_raw = request.form.get("candidate", "0")
        try:
            idx = int(idx_raw)
        except (TypeError, ValueError):
            idx = 0
        cands = proposal.candidates or []
        if not cands:
            conn.close()
            flash("no candidate selected")
            return redirect(url_for("web.proposal_detail", pid=pid))
        if idx < 0 or idx >= len(cands):
            idx = 0
        cand = cands[idx]
        fields = {f: cand.get(f) for f in beets_adapter.CANDIDATE_FIELDS}
    elif action == "edits":
        fields = {}
        for key in request.form:
            if key in beets_adapter.CANDIDATE_FIELDS:
                val = request.form.get(key, "")
                if key in ("track", "disc", "year"):
                    val = (val or "").strip()
                    fields[key] = int(val) if val != "" else None
                else:
                    fields[key] = val
    elif action == "move":
        move_only = True
        fields = None
    else:
        conn.close()
        flash("unknown action")
        return redirect(url_for("web.proposal_detail", pid=pid))

    try:
        dest, item_id = beets_adapter.apply_approved(
            Path(proposal.path), fields, move_only=move_only,
            expect=expect, beets_item_id=proposal.beets_item_id,
        )
    except (beets_adapter.DestinationExists, beets_adapter.FileChanged,
            beets_adapter.ApplyFailed) as exc:
        if isinstance(exc, beets_adapter.DestinationExists):
            msg = str(exc) or "destination exists"
        elif isinstance(exc, beets_adapter.FileChanged):
            msg = str(exc) or "file changed"
        else:
            msg = str(exc)
        db.update_status(conn, pid, "error", error=msg)
        if getattr(exc, "item_id", None) is not None:
            db.set_beets_item_id(conn, pid, exc.item_id)
        conn.close()
        flash(msg)
        return redirect(url_for("web.queue_view"))

    db.update_status(conn, pid, "done")
    if item_id is not None:
        db.set_beets_item_id(conn, pid, item_id)
    conn.close()
    flash(f"tagged and moved to {dest}")
    return redirect(url_for("web.queue_view"))


@bp.route("/proposal/<int:pid>/skip", methods=["POST"])
def skip(pid):
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.update_status(conn, pid, "skipped")
    conn.close()
    return redirect(url_for("web.queue_view"))


@bp.route("/proposal/<int:pid>/reset", methods=["POST"])
def reset(pid):
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    proposal = db.get(conn, pid)
    _reopen(conn, proposal, cfg)
    conn.close()
    return redirect(url_for("web.queue_view"))


@bp.route("/proposal/<int:pid>/retry", methods=["POST"])
def retry(pid):
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    proposal = db.get(conn, pid)
    _reopen(conn, proposal, cfg)
    conn.close()
    return redirect(url_for("web.queue_view"))


@bp.route("/proposal/<int:pid>/identify", methods=["POST"])
def identify(pid):
    cfg = _cfg()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.set_candidates(conn, pid, [])
    db.update_status(conn, pid, "pending")
    conn.close()
    worker.start_job(cfg, "fingerprint")
    return redirect(url_for("web.queue_view"))


@bp.route("/albums/approve", methods=["POST"])
def bulk_approve():
    cfg = _cfg()
    album_key = request.form.get("album_key", "")
    conn = db.connect(cfg.db_path)
    db.init_db(conn)

    from app import beets_adapter

    beets_adapter.ensure_setup(cfg)

    proposals = db.list_by_status(conn, ("awaiting-review",))
    group_props = [p for p in proposals if group_key(p) == album_key]

    approved = 0
    failed = 0
    for p in group_props:
        if p.kind == "unidentified":
            continue
        if p.kind == "organize-only":
            fields = None
            move_only = True
        else:
            if not p.candidates:
                continue
            cand = p.candidates[0]
            fields = {f: cand.get(f) for f in beets_adapter.CANDIDATE_FIELDS}
            move_only = False

        expect = (p.mtime, p.size) if p.status == "awaiting-review" else None
        try:
            dest, item_id = beets_adapter.apply_approved(
                Path(p.path), fields, move_only=move_only,
                expect=expect, beets_item_id=p.beets_item_id,
            )
            db.update_status(conn, p.id, "done")
            if item_id is not None:
                db.set_beets_item_id(conn, p.id, item_id)
            approved += 1
        except Exception as exc:
            if isinstance(exc, beets_adapter.DestinationExists):
                msg = str(exc) or "destination exists"
            elif isinstance(exc, beets_adapter.FileChanged):
                msg = str(exc) or "file changed"
            else:
                msg = str(exc)
            db.update_status(conn, p.id, "error", error=msg)
            if getattr(exc, "item_id", None) is not None:
                db.set_beets_item_id(conn, p.id, exc.item_id)
            failed += 1

    conn.close()
    flash(f"approved {approved}, failed {failed}")
    return redirect(url_for("web.queue_view"))
