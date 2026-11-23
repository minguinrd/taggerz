import os
from pathlib import Path

import pytest

import app.db as db
import app.scan as scan
import app.worker as worker
from app import beets_adapter


def _conn(cfg):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    return conn


def test_scan_sync_produces_reviewable_proposals(cfg, make_track, monkeypatch):
    make_track(cfg.music_dir, "untagged.flac")
    monkeypatch.setattr(worker, "recording_ids", lambda p, key: ["rid"])
    monkeypatch.setattr(worker, "candidates_for", lambda p, ids: [{"title": "T"}])
    out = worker.run_scan_sync(cfg)
    conn = _conn(cfg)
    p = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert p.status == "awaiting-review"
    assert p.candidates[0]["title"] == "T"
    assert out["fingerprinted"] == 1
    conn.close()


def test_no_candidates_marks_unidentified(cfg, make_track, monkeypatch):
    make_track(cfg.music_dir, "untagged.flac")
    monkeypatch.setattr(worker, "recording_ids", lambda p, key: ["rid"])
    monkeypatch.setattr(worker, "candidates_for", lambda p, ids: [])
    worker.run_scan_sync(cfg)
    conn = _conn(cfg)
    p = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert p.status == "awaiting-review"
    assert p.kind == "unidentified"
    conn.close()


def test_manual_kind_survives_no_candidates(cfg, make_track, monkeypatch):
    # manually insert a manual pending row
    conn = _conn(cfg)
    p0 = db.enqueue(conn, path=str(cfg.music_dir / "m.flac"), mtime=1.0, size=1,
                    kind="manual", current_tags={})
    db.update_status(conn, p0.id, "pending", kind="manual")
    conn.close()
    make_track(cfg.music_dir, "m.flac")
    monkeypatch.setattr(worker, "recording_ids", lambda p, key: [])
    monkeypatch.setattr(worker, "candidates_for", lambda p, ids: [])
    worker.run_fingerprint_sync(cfg)
    conn = _conn(cfg)
    p = db.get_by_path(conn, str(cfg.music_dir / "m.flac"))
    assert p.kind == "manual"
    assert p.status == "awaiting-review"
    conn.close()


def test_fingerprint_error_parks_proposal_and_continues(cfg, make_track, monkeypatch):
    make_track(cfg.music_dir, "a.flac")
    make_track(cfg.music_dir, "b.flac")
    from app.fingerprint import AcoustidFailure

    def rec(p, key):
        if "a.flac" in str(p):
            raise AcoustidFailure(f"{p}: boom")
        return ["rid"]
    monkeypatch.setattr(worker, "recording_ids", rec)
    monkeypatch.setattr(worker, "candidates_for", lambda p, ids: [{"title": "T"}])
    # need pending rows; run scan first? Instead manually enqueue two pending
    conn = _conn(cfg)
    # ensure scan creates rows? Use scan.run for organize? Simpler: manually enqueue
    for name in ("a.flac", "b.flac"):
        path = str(cfg.music_dir / name)
        st = Path(path).stat()
        # remove existing
    conn.close()
    # run full scan to enqueue needs-tags
    scan.run(cfg)
    worker.run_fingerprint_sync(cfg)
    conn = _conn(cfg)
    pa = db.get_by_path(conn, str(cfg.music_dir / "a.flac"))
    pb = db.get_by_path(conn, str(cfg.music_dir / "b.flac"))
    assert pa.status == "error"
    assert "boom" in (pa.error or "")
    assert pb.status == "awaiting-review"
    conn.close()


def test_start_job_rejects_when_busy(cfg, monkeypatch):
    assert worker._lock.acquire(blocking=False)
    try:
        assert worker.start_job(cfg) is False
    finally:
        worker._lock.release()


def test_pending_rows_are_resumed(cfg, make_track, monkeypatch):
    make_track(cfg.music_dir, "untagged.flac")
    # manually insert pending row as if previous run died
    conn = _conn(cfg)
    st = (cfg.music_dir / "untagged.flac").stat()
    db.enqueue(conn, path=str(cfg.music_dir / "untagged.flac"), mtime=float(st.st_mtime),
               size=int(st.st_size), kind="needs-tags", current_tags={})
    conn.close()
    monkeypatch.setattr(worker, "recording_ids", lambda p, key: ["rid"])
    monkeypatch.setattr(worker, "candidates_for", lambda p, ids: [{"title": "T"}])
    worker.run_fingerprint_sync(cfg)
    conn = _conn(cfg)
    p = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert p.status == "awaiting-review"
    conn.close()


def test_status_shape(cfg):
    s = worker.status()
    assert set(s.keys()) == {"running", "phase", "seen", "queued", "fingerprinted",
                            "errors", "message", "last_scan"}


def test_fingerprint_only_does_not_rescan(cfg, make_track, monkeypatch):
    # fingerprint job should not walk library / create new proposals beyond existing
    make_track(cfg.music_dir, "untagged.flac")
    monkeypatch.setattr(worker, "recording_ids", lambda p, key: ["rid"])
    monkeypatch.setattr(worker, "candidates_for", lambda p, ids: [{"title": "T"}])
    # no prior scan; manually no rows. fingerprint sync should process none
    out = worker.run_fingerprint_sync(cfg)
    conn = _conn(cfg)
    # no proposal created
    assert db.list_all(conn) == []
    conn.close()
    assert out["fingerprinted"] == 0
