import sqlite3

import pytest

import app.db as db
from app.models import KINDS, STATUSES


def _open(tmp_path):
    conn = db.connect(tmp_path / "q.db")
    db.init_db(conn)
    return conn


def _enqueue(conn, path="/m/a.flac"):
    return db.enqueue(conn, path=path, mtime=1.0, size=2, kind="needs-tags",
                      current_tags={"title": None})


def test_status_and_kind_constants_exact():
    assert STATUSES == ("pending", "awaiting-review", "done", "skipped", "error")
    assert KINDS == ("needs-tags", "organize-only", "unidentified", "manual")


def test_connect_sets_row_factory_and_foreign_keys(tmp_path):
    conn = db.connect(tmp_path / "q.db")
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_init_db_idempotent(tmp_path):
    conn = _open(tmp_path)
    db.init_db(conn)  # second call must not raise
    conn.close()


def test_enqueue_creates_pending(tmp_path):
    conn = _open(tmp_path)
    p = db.enqueue(conn, path="/m/a.flac", mtime=1.0, size=2, kind="needs-tags",
                   current_tags={"title": None})
    assert p.status == "pending" and p.kind == "needs-tags"
    assert p.id > 0 and p.error is None and p.candidates == []
    assert p.mtime == 1.0 and p.size == 2
    assert db.get_by_path(conn, "/m/a.flac") == p


def test_enqueue_same_path_preserves_decision(tmp_path):
    conn = _open(tmp_path)
    p = _enqueue(conn)
    db.update_status(conn, p.id, "skipped")
    again = db.enqueue(conn, path="/m/a.flac", mtime=9.0, size=9, kind="needs-tags",
                       current_tags={})
    # decision sticks; mtime/size/tags NOT overwritten
    assert again.id == p.id and again.status == "skipped"
    assert again.mtime == 1.0 and again.size == 2
    assert again.current_tags == {"title": None}


def test_enqueue_rejects_unknown_kind(tmp_path):
    conn = _open(tmp_path)
    with pytest.raises(ValueError):
        db.enqueue(conn, path="/m/a.flac", mtime=1.0, size=1, kind="bogus",
                   current_tags={})


def test_set_candidates_moves_to_awaiting_review(tmp_path):
    conn = _open(tmp_path)
    p = _enqueue(conn)
    db.update_status(conn, p.id, "error", error="boom")
    cands = [{"title": "T", "score": 0.9}]
    db.set_candidates(conn, p.id, cands)
    got = db.get(conn, p.id)
    assert got.status == "awaiting-review"
    assert got.error is None
    assert got.candidates == cands
    assert got.kind == "needs-tags"  # kind unchanged unless overridden
    # kind override (worker marks no-candidate results unidentified)
    db.set_candidates(conn, p.id, [], kind="unidentified")
    got = db.get(conn, p.id)
    assert got.status == "awaiting-review"
    assert got.kind == "unidentified" and got.candidates == []


def test_update_status_records_error_text(tmp_path):
    conn = _open(tmp_path)
    p = _enqueue(conn)
    db.update_status(conn, p.id, "error",
                     error="destination exists: /music/A/B/01 - T.flac")
    got = db.get(conn, p.id)
    assert got.status == "error"
    assert got.error == "destination exists: /music/A/B/01 - T.flac"
    # an error transition clears a stale message when none is given
    db.update_status(conn, p.id, "error")
    assert db.get(conn, p.id).error is None
    # leaving error status clears the message
    db.update_status(conn, p.id, "error", error="boom")
    db.update_status(conn, p.id, "pending")
    got = db.get(conn, p.id)
    assert got.status == "pending" and got.error is None


def test_update_status_rejects_unknown_status(tmp_path):
    conn = _open(tmp_path)
    p = _enqueue(conn)
    with pytest.raises(ValueError):
        db.update_status(conn, p.id, "approved")


def test_update_status_updates_stat_and_kind_when_given(tmp_path):
    conn = _open(tmp_path)
    p = _enqueue(conn)  # mtime=1.0, size=2, kind=needs-tags
    db.update_status(conn, p.id, "pending", mtime=5.0, size=6, kind="organize-only")
    got = db.get(conn, p.id)
    assert (got.mtime, got.size, got.kind) == (5.0, 6, "organize-only")
    # omitted fields are left untouched
    db.update_status(conn, p.id, "done")
    got = db.get(conn, p.id)
    assert got.status == "done"
    assert (got.mtime, got.size, got.kind) == (5.0, 6, "organize-only")


def test_list_by_status_filters(tmp_path):
    conn = _open(tmp_path)
    a = _enqueue(conn, "/m/a.flac")
    b = _enqueue(conn, "/m/b.flac")
    c = _enqueue(conn, "/m/c.flac")
    db.update_status(conn, a.id, "skipped")
    db.update_status(conn, c.id, "error", error="nope")
    assert [x.id for x in db.list_by_status(conn, ["pending"])] == [b.id]
    assert ([x.id for x in db.list_by_status(conn, ["skipped", "error"])]
            == [a.id, c.id])
    assert [x.id for x in db.list_by_status(conn, list(STATUSES))] == [a.id, b.id, c.id]
    assert db.list_by_status(conn, []) == []
    assert [x.id for x in db.list_all(conn)] == [a.id, b.id, c.id]


def test_get_and_get_by_path_missing_returns_none(tmp_path):
    conn = _open(tmp_path)
    p = _enqueue(conn)
    assert db.get(conn, p.id) == p
    assert db.get(conn, 999) is None
    assert db.get_by_path(conn, "/m/other.flac") is None
    assert db.get_by_path(conn, "/m/a.flac").id == p.id


def test_json_roundtrip_of_tags_and_candidates(tmp_path):
    conn = _open(tmp_path)
    tags = {"title": None, "track": 3, "nested": {"list": [1, 2, 3]}}
    cands = [{"title": "T", "score": 0.94, "mb_trackid": None, "tags": ["a", "b"]}]
    p = db.enqueue(conn, path="/m/a.flac", mtime=1.0, size=2, kind="needs-tags",
                   current_tags=tags)
    db.set_candidates(conn, p.id, cands)
    got = db.get(conn, p.id)
    assert got.current_tags == tags
    assert got.candidates == cands


def test_set_beets_item_id(tmp_path):
    conn = _open(tmp_path)
    p = _enqueue(conn)
    assert db.get(conn, p.id).beets_item_id is None
    db.set_beets_item_id(conn, p.id, 42)
    assert db.get(conn, p.id).beets_item_id == 42


def test_scans_roundtrip(tmp_path):
    conn = _open(tmp_path)
    scan_id = db.start_scan(conn, kind="scan")
    assert scan_id > 0
    latest = db.latest_scan(conn)
    assert latest["kind"] == "scan" and latest["status"] == "running"
    assert (latest["seen"], latest["queued"], latest["fingerprinted"],
            latest["errors"]) == (0, 0, 0, 0)
    assert latest["finished_at"] is None and latest["message"] is None
    db.update_scan(conn, scan_id, phase="fingerprinting", seen=3, queued=2,
                   fingerprinted=1, message="halfway")
    latest = db.latest_scan(conn)
    assert latest["phase"] == "fingerprinting"
    assert (latest["seen"], latest["queued"], latest["fingerprinted"]) == (3, 2, 1)
    assert latest["message"] == "halfway"
    db.update_scan(conn, scan_id, phase="done", status="done", message=None,
                   finished_at=123.0)
    latest = db.latest_scan(conn)
    assert latest["status"] == "done"
    assert latest["finished_at"] == 123.0 and latest["message"] is None
    # a newer scan wins
    db.start_scan(conn, kind="fingerprint")
    assert db.latest_scan(conn)["kind"] == "fingerprint"


def test_latest_scan_none_when_empty(tmp_path):
    conn = _open(tmp_path)
    assert db.latest_scan(conn) is None


def test_update_scan_rejects_unknown_field(tmp_path):
    conn = _open(tmp_path)
    scan_id = db.start_scan(conn, kind="scan")
    with pytest.raises(ValueError):
        db.update_scan(conn, scan_id, bogus=1)


def test_data_persists_across_connections(tmp_path):
    # the queue must survive restarts (open → write → close → reopen)
    path = tmp_path / "q.db"
    conn = db.connect(path)
    db.init_db(conn)
    p = _enqueue(conn)
    db.update_status(conn, p.id, "error", error="boom")
    conn.close()
    reopened = db.connect(path)
    try:
        got = db.get_by_path(reopened, "/m/a.flac")
        assert got is not None
        assert got.status == "error" and got.error == "boom"
    finally:
        reopened.close()
