import app.db as db
from app.models import STATUSES


def _seed(cfg, make_track):
    import pathlib
    p1 = make_track(cfg.music_dir, "a.flac", title="T", artist="A", album="B",
                    albumartist="A", track=1)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    row = db.enqueue(conn, path=str(p1), mtime=p1.stat().st_mtime,
                      size=p1.stat().st_size, kind="needs-tags",
                      current_tags={"title": "T"})
    # make it awaiting-review with candidates? leave pending for badge? We'll create separate
    conn.close()
    return p1, row


def test_queue_lists_proposals_with_badges(cfg, make_track, client):
    p = make_track(cfg.music_dir, "u.flac")
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    # needs-tags row (awaiting review so its kind badge shows)
    r1 = db.enqueue(conn, path=str(p), mtime=1.0, size=2, kind="needs-tags",
                   current_tags={})
    db.set_candidates(conn, r1.id, [{"title": "T"}])
    # unidentified row
    p2 = make_track(cfg.music_dir, "u2.flac")
    r2 = db.enqueue(conn, path=str(p2), mtime=1.0, size=2, kind="unidentified",
                    current_tags={})
    db.set_candidates(conn, r2.id, [], kind="unidentified")
    # error destination exists
    p3 = make_track(cfg.music_dir, "u3.flac")
    r3 = db.enqueue(conn, path=str(p3), mtime=1.0, size=2, kind="needs-tags",
                    current_tags={})
    db.update_status(conn, r3.id, "error", error="destination exists: /x")
    conn.close()

    resp = client.get("/")
    assert resp.status_code == 200
    assert b"needs tags" in resp.data or b"needs-tags" in resp.data
    assert b"unidentified" in resp.data
    assert b"destination exists" in resp.data


def test_queue_groups_by_album(cfg, make_track, client):
    p1 = make_track(cfg.music_dir, "t1.flac", title="S1", artist="A", album="B",
                    albumartist="A", track=1)
    p2 = make_track(cfg.music_dir, "t2.flac", title="S2", artist="A", album="B",
                    albumartist="A", track=2)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    for p in (p1, p2):
        r = db.enqueue(conn, path=str(p), mtime=1.0, size=2, kind="organize-only",
                       current_tags={"albumartist": "A", "album": "B"})
        db.set_candidates(conn, r.id, [{"albumartist": "A", "album": "B"}])
    conn.close()
    resp = client.get("/")
    assert resp.status_code == 200
    # one group header
    assert resp.data.count(b"A | B") == 1


def test_queue_status_filter(cfg, make_track, client):
    p = make_track(cfg.music_dir, "s.flac")
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    r = db.enqueue(conn, path=str(p), mtime=1.0, size=2, kind="needs-tags",
                   current_tags={})
    db.update_status(conn, r.id, "skipped")
    conn.close()
    resp = client.get("/?status=skipped")
    assert resp.status_code == 200
    assert str(p).encode() in resp.data
    resp2 = client.get("/")
    assert str(p).encode() not in resp2.data


def test_queue_invalid_status_falls_back(cfg, client):
    resp = client.get("/?status=bogus")
    assert resp.status_code == 200


def test_status_endpoint_shape(cfg, client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data.keys()) == {"running", "phase", "seen", "queued",
                               "fingerprinted", "errors", "message", "last_scan"}


def test_scan_post_starts_job(cfg, client, monkeypatch):
    import app.worker as worker
    monkeypatch.setattr(worker, "start_job", lambda c, *a, **k: True)
    resp = client.post("/scan", follow_redirects=True)
    assert resp.status_code == 200
    assert b"scan started" in resp.data


def test_open_path_valid_file(cfg, make_track, client):
    p = make_track(cfg.music_dir, "o.flac", title="T")
    resp = client.post("/open-path", data={"path": str(p)}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/proposal/" in resp.headers["Location"]
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    row = db.get_by_path(conn, str(p.resolve()))
    assert row is not None
    assert row.status == "pending" and row.kind == "manual"
    conn.close()


def test_open_path_outside_library_rejected(cfg, client):
    resp = client.post("/open-path", data={"path": "/etc/passwd"},
                       follow_redirects=True)
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    assert db.list_all(conn) == []
    conn.close()


def test_open_path_non_audio_rejected(cfg, make_track, client):
    p = make_track(cfg.music_dir, "n.txt")
    # make a txt file
    (cfg.music_dir / "n.txt").write_text("x")
    resp = client.post("/open-path", data={"path": str(cfg.music_dir / "n.txt")},
                       follow_redirects=True)
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    assert db.list_all(conn) == []
    conn.close()


def test_static_assets_served(client):
    resp = client.get("/static/app.js")
    assert resp.status_code == 200