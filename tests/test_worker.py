import os
import time
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
    monkeypatch.setattr(beets_adapter, "apply_approved",
                        lambda path, fields, *, move_only, expect,
                        beets_item_id: (path, None))
    out = worker.run_scan_sync(cfg)
    conn = _conn(cfg)
    p = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert p.status == "done"
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
    monkeypatch.setattr(beets_adapter, "apply_approved",
                        lambda path, fields, *, move_only, expect,
                        beets_item_id: (path, None))
    worker.run_fingerprint_sync(cfg)
    conn = _conn(cfg)
    pa = db.get_by_path(conn, str(cfg.music_dir / "a.flac"))
    pb = db.get_by_path(conn, str(cfg.music_dir / "b.flac"))
    assert pa.status == "error"
    assert "boom" in (pa.error or "")
    assert pb.status == "done"
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
    monkeypatch.setattr(beets_adapter, "apply_approved",
                        lambda path, fields, *, move_only, expect,
                        beets_item_id: (path, None))
    worker.run_fingerprint_sync(cfg)
    conn = _conn(cfg)
    p = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert p.status == "done"
    conn.close()


def test_status_shape(cfg):
    s = worker.status()
    assert set(s.keys()) == {"running", "phase", "seen", "queued", "fingerprinted",
                            "errors", "message", "last_scan", "activity"}


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


def test_start_job_releases_lock_when_thread_fails(cfg, monkeypatch):
    class ExplodingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(worker.threading, "Thread", ExplodingThread)
    with pytest.raises(RuntimeError, match="cannot start new thread"):
        worker.start_job(cfg)
    # the lock must not leak: worker still idle, next job can acquire
    assert not worker.is_running()
    assert worker._lock.acquire(blocking=False)
    worker._lock.release()


def test_crashed_job_reports_error_status(cfg, monkeypatch):
    def boom(cfg):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(worker, "run_scan_sync", boom)
    assert worker.start_job(cfg) is True
    deadline = time.monotonic() + 5
    while worker.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not worker.is_running()
    s = worker.status()
    assert s["phase"] == "error"
    assert "kaboom" in s["message"]


def test_fingerprint_persists_fingerprinting_phase(cfg, monkeypatch):
    conn = _conn(cfg)
    db.start_scan(conn, kind="scan")
    conn.close()
    calls = []
    real_update_scan = db.update_scan

    def spy(conn_, scan_id_, **fields):
        calls.append(fields)
        return real_update_scan(conn_, scan_id_, **fields)

    monkeypatch.setattr(worker.db, "update_scan", spy)
    worker.run_fingerprint_sync(cfg)
    assert any(c.get("phase") == "fingerprinting" for c in calls)
    assert any(c.get("phase") == "done" for c in calls)
    assert any(c.get("phase") == "applying" for c in calls)


def test_unexpected_error_parks_file_and_continues(cfg, monkeypatch):
    # a non-AcoustID failure (e.g. a beets bug) on one file must park THAT
    # file as error and keep processing the rest of the pass
    conn = _conn(cfg)
    db.enqueue(conn, path="/m/a.flac", mtime=1.0, size=1, kind="needs-tags",
               current_tags={})
    db.enqueue(conn, path="/m/b.flac", mtime=1.0, size=1, kind="needs-tags",
               current_tags={})
    db.start_scan(conn, kind="scan")
    conn.close()

    monkeypatch.setattr(worker, "recording_ids", lambda p, key: ["rid"])
    calls = []

    def flaky(path, ids):
        calls.append(str(path))
        if len(calls) == 1:
            raise KeyError("boom")
        return [{"title": "T"}]

    monkeypatch.setattr(worker, "candidates_for", flaky)
    monkeypatch.setattr(beets_adapter, "apply_approved",
                        lambda path, fields, *, move_only, expect,
                        beets_item_id: (path, None))
    worker.run_fingerprint_sync(cfg)

    conn = _conn(cfg)
    a = db.get_by_path(conn, "/m/a.flac")
    b = db.get_by_path(conn, "/m/b.flac")
    assert a.status == "error" and "KeyError" in a.error
    assert b.status == "done" and b.candidates[0]["title"] == "T"
    # the pass still completes: scan row not left mid-flight
    assert db.latest_scan(conn)["phase"] == "done"
    conn.close()


def test_auto_organizes_correctly_tagged_file(cfg, make_track, monkeypatch):
    p = make_track(cfg.music_dir, "loose.flac", title="Song", artist="Artist",
                   album="Album", albumartist="Artist", track=1)
    scan.run(cfg)
    fp_calls = []
    monkeypatch.setattr(worker, "recording_ids",
                        lambda path, key: fp_calls.append(str(path)) or ["rid"])
    out = worker.run_fingerprint_sync(cfg)
    dest = cfg.music_dir / "Artist" / "Album" / "01 - Song.flac"
    assert dest.exists() and not p.exists()
    assert fp_calls == []                        # organize-only never fingerprinted
    conn = _conn(cfg)
    row = db.get_by_path(conn, str(p))
    assert row.status == "done"
    st = dest.stat()
    assert db.file_state_get(conn, str(dest)) == (st.st_mtime, st.st_size)
    conn.close()
    assert out["fingerprinted"] == 1             # phase-1 applies count toward the bar


def test_auto_organize_overwrites_existing_destination(cfg, make_track,
                                                       monkeypatch):
    src = make_track(cfg.music_dir, "loose.flac", title="Song", artist="Artist",
                     album="Album", albumartist="Artist", track=1)
    make_track(cfg.music_dir, "Artist/Album/01 - Song.flac", title="Old",
               artist="Artist", album="Album", albumartist="Artist", track=1)
    scan.run(cfg)
    monkeypatch.setattr(worker, "recording_ids", lambda path, key: [])
    worker.run_fingerprint_sync(cfg)
    dest = cfg.music_dir / "Artist" / "Album" / "01 - Song.flac"
    import mediafile
    assert mediafile.MediaFile(str(dest)).title == "Song"   # replaced, not .1
    assert not src.exists()
    assert not (dest.parent / "01 - Song.1.flac").exists()


def test_phase1_apply_failure_parks_and_continues(cfg, make_track,
                                                  monkeypatch):
    make_track(cfg.music_dir, "l1.flac", title="S1", artist="A", album="B",
               albumartist="A", track=1)
    make_track(cfg.music_dir, "l2.flac", title="S2", artist="A", album="B",
               albumartist="A", track=2)
    scan.run(cfg)
    calls = {"n": 0}

    def fake_apply(path, fields, *, move_only, expect, beets_item_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return (Path(str(path) + ".done"), None)

    monkeypatch.setattr(beets_adapter, "apply_approved", fake_apply)
    worker.run_fingerprint_sync(cfg)
    conn = _conn(cfg)
    rows = db.list_all(conn)
    errs = [r for r in rows if r.status == "error"]
    dones = [r for r in rows if r.status == "done"]
    assert len(errs) == 1 and "RuntimeError" in errs[0].error
    assert len(dones) == 1                       # one broken file never aborts
    assert db.latest_scan(conn)["phase"] == "done"
    conn.close()


def test_apply_failure_parking_details(cfg, make_track, monkeypatch):
    make_track(cfg.music_dir, "l1.flac", title="S1", artist="A", album="B",
               albumartist="A", track=1)
    make_track(cfg.music_dir, "l2.flac", title="S2", artist="A", album="B",
               albumartist="A", track=2)
    scan.run(cfg)
    calls = {"n": 0}

    def fake_apply(path, fields, *, move_only, expect, beets_item_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise beets_adapter.FileChanged()        # carries no message
        raise beets_adapter.ApplyFailed("failed to move file", item_id=7)

    monkeypatch.setattr(beets_adapter, "apply_approved", fake_apply)
    worker.run_fingerprint_sync(cfg)
    conn = _conn(cfg)
    rows = db.list_all(conn)
    changed = [r for r in rows if r.error == "file changed"]
    failed = [r for r in rows if r.beets_item_id == 7]
    assert len(changed) == 1 and changed[0].status == "error"
    assert len(failed) == 1 and failed[0].error == "failed to move file"
    conn.close()


def test_apply_refreshes_row_expect_after_success(cfg, make_track,
                                                  monkeypatch):
    p = make_track(cfg.music_dir, "l.flac", title="S", artist="A", album="B",
                   albumartist="A", track=1)
    scan.run(cfg)
    monkeypatch.setattr(beets_adapter, "apply_approved",
                        lambda path, fields, *, move_only, expect,
                        beets_item_id: (path, None))
    worker.run_fingerprint_sync(cfg)
    st = p.stat()
    conn = _conn(cfg)
    row = db.get_by_path(conn, str(p))
    assert row.status == "done"
    assert (row.mtime, row.size) == (st.st_mtime, st.st_size)  # truthful expect
    conn.close()


def test_auto_approves_best_match_end_to_end(cfg, make_track, monkeypatch):
    make_track(cfg.music_dir, "untagged.flac")
    scan.run(cfg)
    monkeypatch.setattr(worker, "recording_ids", lambda path, key: ["rid"])

    def cands(path, ids):
        low = {f: None for f in beets_adapter.CANDIDATE_FIELDS}
        low.update({"title": "Worse", "artist": "A", "album": "B",
                    "albumartist": "A", "track": 1, "disc": 1, "year": 2000,
                    "score": 0.4})
        high = {f: None for f in beets_adapter.CANDIDATE_FIELDS}
        high.update({"title": "Best", "artist": "A", "album": "B",
                     "albumartist": "A", "track": 1, "disc": 1, "year": 2000,
                     "score": 0.95})
        return [low, high]

    monkeypatch.setattr(worker, "candidates_for", cands)
    worker.run_scan_sync(cfg)
    conn = _conn(cfg)
    row = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert row.status == "done"
    assert row.candidates[0]["title"] == "Worse"   # stored candidates untouched
    conn.close()
    dest = cfg.music_dir / "A" / "B" / "01 - Best.flac"
    import mediafile
    assert dest.exists()
    assert mediafile.MediaFile(str(dest)).title == "Best"


def test_auto_approve_missing_and_tied_scores_do_not_crash(cfg, make_track,
                                                           monkeypatch):
    make_track(cfg.music_dir, "untagged.flac")
    scan.run(cfg)
    monkeypatch.setattr(worker, "recording_ids", lambda path, key: ["rid"])

    def cands(path, ids):
        first = {f: None for f in beets_adapter.CANDIDATE_FIELDS}
        first.update({"title": "First", "artist": "A", "album": "B",
                      "albumartist": "A", "track": 1, "disc": 1, "year": 2000})
        # no "score" key at all — max() must not KeyError
        second = dict(first)
        second.update({"title": "Second", "score": None})  # None must not TypeError
        return [first, second]

    monkeypatch.setattr(worker, "candidates_for", cands)
    worker.run_scan_sync(cfg)
    conn = _conn(cfg)
    row = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert row.status == "done"
    conn.close()
    assert (cfg.music_dir / "A" / "B" / "01 - First.flac").exists()
    assert not (cfg.music_dir / "A" / "B" / "01 - Second.flac").exists()


def test_auto_approve_persists_beets_item_id(cfg, make_track, monkeypatch):
    make_track(cfg.music_dir, "untagged.flac")
    scan.run(cfg)
    monkeypatch.setattr(worker, "recording_ids", lambda path, key: ["rid"])
    monkeypatch.setattr(worker, "candidates_for",
                        lambda path, ids: [{"title": "T", "score": 0.9}])
    monkeypatch.setattr(beets_adapter, "apply_approved",
                        lambda path, fields, *, move_only, expect,
                        beets_item_id: (path, 42))
    worker.run_scan_sync(cfg)
    conn = _conn(cfg)
    row = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert row.status == "done" and row.beets_item_id == 42
    conn.close()


def test_auto_approve_failure_parks_with_item_id(cfg, make_track,
                                                 monkeypatch):
    make_track(cfg.music_dir, "untagged.flac")
    scan.run(cfg)
    monkeypatch.setattr(worker, "recording_ids", lambda path, key: ["rid"])
    monkeypatch.setattr(worker, "candidates_for",
                        lambda path, ids: [{"title": "T", "score": 0.9}])

    def boom(path, fields, *, move_only, expect, beets_item_id):
        raise beets_adapter.ApplyFailed("failed to move file", item_id=7)

    monkeypatch.setattr(beets_adapter, "apply_approved", boom)
    worker.run_scan_sync(cfg)
    conn = _conn(cfg)
    row = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert row.status == "error"
    assert row.error == "failed to move file" and row.beets_item_id == 7
    assert db.latest_scan(conn)["phase"] == "done"   # job still completes
    conn.close()
