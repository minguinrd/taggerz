import os
import re
import time

import pytest

import app.db as db
from app import beets_adapter, create_app, worker
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
    # generic error row
    p3 = make_track(cfg.music_dir, "u3.flac")
    r3 = db.enqueue(conn, path=str(p3), mtime=1.0, size=2, kind="needs-tags",
                    current_tags={})
    db.update_status(conn, r3.id, "error", error="disk exploded")
    conn.close()

    resp = client.get("/")
    assert resp.status_code == 200
    assert b"needs tags" in resp.data or b"needs-tags" in resp.data
    assert b"unidentified" in resp.data
    assert b"destination exists" not in resp.data   # badge is gone


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
                               "fingerprinted", "errors", "message",
                               "last_scan", "activity"}


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

def _seed_proposal(cfg, make_track, filename="a.flac", kind="needs-tags",
                   status="awaiting-review", candidates=None, mtime=None,
                   size=None, current_tags=None, error=None):
    p = make_track(cfg.music_dir, filename, title="Old")
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    if mtime is None:
        mtime = p.stat().st_mtime
    if size is None:
        size = p.stat().st_size
    if current_tags is None:
        current_tags = {"title": "Old"}
    row = db.enqueue(conn, path=str(p), mtime=mtime, size=size, kind=kind,
                     current_tags=current_tags)
    if candidates is not None:
        db.set_candidates(conn, row.id, candidates, kind=kind if kind else None)
    if status != "awaiting-review":
        db.update_status(conn, row.id, status, error=error)
    conn.close()
    return p, row


def test_approve_candidate_writes_and_marks_done(cfg, make_track, client, monkeypatch):
    cand = {f: f"V-{f}" for f in beets_adapter.CANDIDATE_FIELDS}
    cand.update({"track": 1, "disc": 1, "year": 2000, "score": 0.9,
                 "recommendation": "strong"})
    p, row = _seed_proposal(cfg, make_track, candidates=[cand])
    calls = []

    def fake(path, fields, move_only=False, expect=None, beets_item_id=None):
        calls.append({"fields": fields, "move_only": move_only, "expect": expect})
        return (cfg.music_dir / "A" / "B" / "01 - New.flac", 42)

    monkeypatch.setattr(beets_adapter, "apply_approved", fake)
    resp = client.post(f"/proposal/{row.id}/approve",
                       data={"action": "candidate"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"tagged and moved" in resp.data
    assert len(calls) == 1
    assert calls[0]["move_only"] is False
    assert calls[0]["fields"] == {f: cand.get(f) for f in beets_adapter.CANDIDATE_FIELDS}
    assert calls[0]["expect"] == (p.stat().st_mtime, p.stat().st_size)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    prop = db.get(conn, row.id)
    assert prop.status == "done"
    assert prop.beets_item_id == 42
    conn.close()


def test_approve_edits_coerces_types(cfg, make_track, client, monkeypatch):
    p, row = _seed_proposal(cfg, make_track, candidates=[])
    calls = []

    def fake(path, fields, move_only=False, expect=None, beets_item_id=None):
        calls.append(fields)
        return (cfg.music_dir / "x.flac", 7)

    monkeypatch.setattr(beets_adapter, "apply_approved", fake)
    resp = client.post(f"/proposal/{row.id}/approve",
                       data={"action": "edits", "track": "3", "year": ""},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert calls and calls[0] == {"track": 3, "year": None}


def test_approve_unidentified_without_fields_rejected(cfg, make_track, client, monkeypatch):
    p, row = _seed_proposal(cfg, make_track, kind="unidentified", candidates=[])
    called = {"n": 0}

    def fake(*a, **k):
        called["n"] += 1
        return (cfg.music_dir / "x.flac", 1)

    monkeypatch.setattr(beets_adapter, "apply_approved", fake)
    resp = client.post(f"/proposal/{row.id}/approve",
                       data={"action": "candidate"}, follow_redirects=True)
    assert resp.status_code == 200
    assert called["n"] == 0
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    prop = db.get(conn, row.id)
    assert prop.status != "done"
    conn.close()


def test_approve_with_changed_mtime_errors(cfg, make_track, client):
    cand = {f: "X" for f in beets_adapter.CANDIDATE_FIELDS}
    cand.update({"track": 1, "disc": 1, "year": 2000})
    p, row = _seed_proposal(cfg, make_track, candidates=[cand],
                            mtime=1.0, size=2)
    real_before = p.stat()
    resp = client.post(f"/proposal/{row.id}/approve",
                       data={"action": "candidate"}, follow_redirects=True)
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    prop = db.get(conn, row.id)
    assert prop.status == "error"
    assert prop.error and "changed" in prop.error.lower()
    conn.close()
    real_after = p.stat()
    assert (real_after.st_mtime, real_after.st_size) == (
        real_before.st_mtime, real_before.st_size)  # file untouched


def test_error_status_reapprove_passes_no_expect(cfg, make_track, client,
                                                 monkeypatch):
    # our own earlier write changed the stat, so a re-approve after an
    # error must not run the mtime/size guard (it would always trip)
    cand = {f: "V" for f in beets_adapter.CANDIDATE_FIELDS}
    cand.update({"track": 1, "disc": 1, "year": 2000})
    p, row = _seed_proposal(cfg, make_track, candidates=[cand])
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.update_status(conn, row.id, "error", error="failed to move file")
    conn.close()
    captured = {}

    def fake_apply(path, fields, *, move_only, expect, beets_item_id):
        captured["expect"] = expect
        return (path, 1)

    monkeypatch.setattr(beets_adapter, "apply_approved", fake_apply)
    resp = client.post(f"/proposal/{row.id}/approve",
                       data={"action": "candidate"}, follow_redirects=True)
    assert resp.status_code == 200
    assert captured["expect"] is None
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    assert db.get(conn, row.id).status == "done"
    conn.close()


def test_approve_collision_overwrites_destination(cfg, make_track, client):
    p = make_track(cfg.music_dir, "src.flac", title="T", artist="A", album="B",
                   albumartist="A", track=1)
    cand = {f: None for f in beets_adapter.CANDIDATE_FIELDS}
    cand.update({"title": "T", "artist": "A", "album": "B", "albumartist": "A",
                 "track": 1, "disc": 1, "year": 2000})
    cand = {f: cand.get(f) for f in beets_adapter.CANDIDATE_FIELDS}
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    row = db.enqueue(conn, path=str(p), mtime=p.stat().st_mtime,
                     size=p.stat().st_size, kind="needs-tags",
                     current_tags={"title": "T"})
    db.set_candidates(conn, row.id, [cand])
    conn.close()

    dest = cfg.music_dir / "A" / "B" / "01 - T.flac"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"EXISTING")

    resp = client.post(f"/proposal/{row.id}/approve",
                       data={"action": "candidate"}, follow_redirects=True)
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    prop = db.get(conn, row.id)
    assert prop.status == "done"                    # was: error
    conn.close()
    import mediafile
    assert dest.exists() and dest.read_bytes() != b"EXISTING"
    assert mediafile.MediaFile(str(dest)).title == "T"

    resp2 = client.get("/")
    assert b"destination exists" not in resp2.data


def test_skip_and_reset_roundtrip(cfg, make_track, client, monkeypatch):
    # reset with candidates starts a job (its apply phase would race the
    # status assertion below), so record the start instead of running it
    started = []
    monkeypatch.setattr(worker, "start_job",
                        lambda cfg, kind="scan": started.append(kind) or True)
    cand = [{f: "X" for f in beets_adapter.CANDIDATE_FIELDS}]
    p, row = _seed_proposal(cfg, make_track, candidates=cand)
    resp = client.post(f"/proposal/{row.id}/skip", follow_redirects=True)
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    assert db.get(conn, row.id).status == "skipped"
    conn.close()

    resp = client.post(f"/proposal/{row.id}/reset", follow_redirects=True)
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    assert db.get(conn, row.id).status == "awaiting-review"
    conn.close()
    assert started == ["fingerprint"]


def test_retry_without_candidates_requeues(cfg, make_track, client, monkeypatch):
    p, row = _seed_proposal(cfg, make_track, status="error", candidates=[])
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.update_status(conn, row.id, "error", error="boom")
    conn.execute("UPDATE proposals SET candidates='[]' WHERE id=?", (row.id,))
    conn.commit()
    conn.close()

    started = {"n": 0}

    def fake_start(cfg, kind="scan"):
        started["n"] += 1
        return True

    monkeypatch.setattr(worker, "start_job", fake_start)
    resp = client.post(f"/proposal/{row.id}/retry", follow_redirects=True)
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    prop = db.get(conn, row.id)
    assert prop.status == "pending"
    assert prop.candidates == []
    conn.close()
    assert started["n"] == 1


def test_identify_clears_candidates(cfg, make_track, client, monkeypatch):
    cand = [{f: "X" for f in beets_adapter.CANDIDATE_FIELDS}]
    p, row = _seed_proposal(cfg, make_track, candidates=cand)
    monkeypatch.setattr(worker, "start_job", lambda cfg, kind="scan": True)
    resp = client.post(f"/proposal/{row.id}/identify", follow_redirects=True)
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    prop = db.get(conn, row.id)
    assert prop.status == "pending"
    assert prop.candidates == []
    conn.close()


def test_bulk_approve_collision_still_approves_everything(cfg, make_track, client):
    p1 = make_track(cfg.music_dir, "c1.flac", title="T1", artist="A", album="B",
                    albumartist="A", track=1)
    p2 = make_track(cfg.music_dir, "c2.flac", title="T2", artist="A", album="B",
                    albumartist="A", track=2)
    p3 = make_track(cfg.music_dir, "c3.flac", title="T3", artist="A", album="B",
                    albumartist="A", track=3)

    def make_cand(title, track):
        c = {f: None for f in beets_adapter.CANDIDATE_FIELDS}
        c.update({"title": title, "artist": "A", "album": "B",
                  "albumartist": "A", "track": track, "disc": 1, "year": 2000})
        return c

    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    for p, title, track in ((p1, "T1", 1), (p2, "T2", 2), (p3, "T3", 3)):
        row = db.enqueue(conn, path=str(p), mtime=p.stat().st_mtime,
                         size=p.stat().st_size, kind="needs-tags",
                         current_tags={"title": title})
        db.set_candidates(conn, row.id, [make_cand(title, track)])
    conn.close()

    dest = cfg.music_dir / "A" / "B" / "01 - T1.flac"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"EXISTING")

    resp = client.post("/albums/approve", data={"album_key": "A | B"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b"approved 3" in resp.data               # was: approved 2

    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    done = [r for r in db.list_all(conn) if r.status == "done"]
    err = [r for r in db.list_all(conn) if r.status == "error"]
    assert len(done) == 3 and err == []
    conn.close()
    import mediafile
    assert mediafile.MediaFile(str(dest)).title == "T1"   # replaced


def test_bulk_approve_ignores_unidentified(cfg, make_track, client, monkeypatch):
    p = make_track(cfg.music_dir, "u.flac", title="U", artist="A", album="B",
                   albumartist="A", track=1)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    row = db.enqueue(conn, path=str(p), mtime=p.stat().st_mtime,
                     size=p.stat().st_size, kind="unidentified",
                     current_tags={"title": "U"})
    db.set_candidates(conn, row.id, [], kind="unidentified")
    conn.close()

    called = {"n": 0}

    def fake(*a, **k):
        called["n"] += 1
        return (cfg.music_dir / "x.flac", 1)

    monkeypatch.setattr(beets_adapter, "apply_approved", fake)
    resp = client.post("/albums/approve", data={"album_key": "A | B"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert called["n"] == 0
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    prop = db.get(conn, row.id)
    assert prop.status == "awaiting-review"
    conn.close()


def test_settings_page_shows_configuration(cfg, client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert str(cfg.music_dir).encode() in resp.data
    assert b"$albumartist/$album/$track - $title" in resp.data
    assert b"beets default" in resp.data          # no ACOUSTID_APP_KEY in test env


def test_open_path_existing_row_clears_candidates(cfg, make_track, client,
                                                  monkeypatch):
    monkeypatch.setattr(worker, "start_job", lambda c, *a, **k: True)
    cand = {f: "V" for f in beets_adapter.CANDIDATE_FIELDS}
    p, row = _seed_proposal(cfg, make_track, candidates=[cand])
    resp = client.post("/open-path", data={"path": str(p)},
                       follow_redirects=False)
    assert resp.status_code == 302
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    prop = db.get(conn, row.id)
    assert prop.status == "pending"
    assert prop.kind == "manual"
    assert prop.candidates == []   # stale candidates cleared, no status force
    conn.close()


def test_create_app_fails_fast_on_readonly_data_dir(cfg):
    # wrong-owner data volume must fail at boot with a clear message,
    # not as a cryptic sqlite error on the first scan click
    os.chmod(cfg.data_dir, 0o555)
    try:
        with pytest.raises(RuntimeError, match="not writable"):
            create_app(cfg)
    finally:
        os.chmod(cfg.data_dir, 0o755)


def test_create_app_fails_fast_on_readonly_queue_db(cfg):
    # dir writable, DB file itself not (partial chown): same clear failure
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    conn.close()
    os.chmod(cfg.db_path, 0o444)
    try:
        with pytest.raises(RuntimeError, match="not writable"):
            create_app(cfg)
    finally:
        os.chmod(cfg.db_path, 0o644)


def test_queue_bulk_approve_button_renders_decodable_key(cfg, make_track,
                                                          client):
    # bulk-approve UI: hidden input must carry a single-encoded character
    # reference (browser decodes it back to "A | B" on submit) and the raw
    # group key must still appear exactly once on the page
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    for name, track in (("g1.flac", 1), ("g2.flac", 2)):
        p = make_track(cfg.music_dir, name, title=f"T{track}", artist="A",
                       album="B", albumartist="A", track=track)
        r = db.enqueue(conn, path=str(p), mtime=1.0, size=2,
                       kind="organize-only",
                       current_tags={"albumartist": "A", "album": "B"})
        db.set_candidates(conn, r.id,
                          [{"albumartist": "A", "album": "B"}])
    conn.close()
    resp = client.get("/")
    assert b'name="album_key" value="A &#124; B"' in resp.data
    assert b"Approve 2" in resp.data
    assert resp.data.count(b"A | B") == 1


def test_base_renders_confirm_dialog_and_toast_region(cfg, client):
    resp = client.get("/")
    assert b"<dialog" in resp.data          # custom confirm popup markup
    assert b'id="toasts"' in resp.data      # toast container (flashes land here)


def test_queue_renders_status_tabs_with_counts(cfg, make_track, client):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    seeds = [("awaiting", "needs-tags"), ("skipped", "needs-tags"),
             ("done", "needs-tags"), ("error", "needs-tags")]
    for i, (state, kind) in enumerate(seeds):
        p = make_track(cfg.music_dir, f"tab{i}.flac")
        r = db.enqueue(conn, path=str(p), mtime=1.0, size=1, kind=kind,
                       current_tags={})
        if state == "awaiting":
            db.set_candidates(conn, r.id, [{"title": "x"}])
        else:
            db.update_status(conn, r.id, state,
                             error="boom" if state == "error" else None)
    conn.close()
    resp = client.get("/")
    html = resp.data
    assert b'id="queue-tabs"' in html
    for t in (b"open", b"errors", b"skipped", b"done"):
        assert b'data-tab="' + t + b'"' in html
    assert b'aria-current="page"' in html
    # counts: open = awaiting + error = 2; errors/skipped/done = 1 each
    assert b'data-count="2"' in html
    assert html.count(b'data-count="1"') == 3
    # active tab is Open on the default view
    assert b'data-tab="open" data-count="2" aria-current="page"' in html


def test_queue_has_search_and_group_toggles(cfg, make_track, client):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    for name, track in (("s1.flac", 1), ("s2.flac", 2)):
        p = make_track(cfg.music_dir, name, title=f"S{track}", artist="A",
                       album="B", albumartist="A", track=track)
        r = db.enqueue(conn, path=str(p), mtime=1.0, size=2,
                       kind="organize-only",
                       current_tags={"albumartist": "A", "album": "B"})
        db.set_candidates(conn, r.id, [{"albumartist": "A", "album": "B"}])
    conn.close()
    resp = client.get("/")
    html = resp.data
    assert b'id="queue-search"' in html
    assert b'id="toggle-all"' in html
    assert b'class="group-toggle"' in html
    assert b'aria-expanded="true"' in html
    # data-group carries the entity-encoded key (raw key stays single-occurrence)
    assert b'data-group="A &#124; B"' in html
    assert html.count(b"A | B") == 1


def test_queue_paginates_by_rows(cfg, make_track, client, monkeypatch):
    import app.web as webmod
    monkeypatch.setattr(webmod, "QUEUE_PAGE_SIZE", 10)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    for aa, al in (("A", "B"), ("C", "D"), ("E", "F")):
        for t in range(5):
            p = make_track(cfg.music_dir, f"{aa}{t}.flac", title=f"T{t}",
                           artist=aa, album=al, albumartist=aa, track=t + 1)
            r = db.enqueue(conn, path=str(p), mtime=1.0, size=1,
                           kind="organize-only",
                           current_tags={"albumartist": aa, "album": al})
            db.set_candidates(conn, r.id, [{"albumartist": aa, "album": al}])
    conn.close()

    resp = client.get("/")
    html = resp.data.decode()
    assert 'class="pagination"' in html
    assert 'data-page="2"' in html
    assert "of 15 files" in html
    assert html.count('class="path"') == 10          # page 1 = 10 rows
    assert "A | B" in html and "C | D" in html
    assert "E | F" not in html                        # third group on page 2

    # out-of-range clamps to the last page
    h2 = client.get("/?page=99").data.decode()
    assert "E | F" in h2 and "A | B" not in h2
    assert h2.count('class="path"') == 5
    assert 'pag-link current' in h2

    # non-integer page falls back to page 1
    assert client.get("/?page=abc").status_code == 200


def test_queue_splits_oversized_group_with_full_counts(cfg, make_track,
                                                       client, monkeypatch):
    import app.web as webmod
    monkeypatch.setattr(webmod, "QUEUE_PAGE_SIZE", 4)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    for t in range(6):
        p = make_track(cfg.music_dir, f"big{t}.flac", title=f"B{t}",
                       artist="A", album="B", albumartist="A", track=t + 1)
        r = db.enqueue(conn, path=str(p), mtime=1.0, size=1,
                       kind="organize-only",
                       current_tags={"albumartist": "A", "album": "B"})
        db.set_candidates(conn, r.id, [{"albumartist": "A", "album": "B"}])
    conn.close()

    h1 = client.get("/").data.decode()
    assert h1.count('class="path"') == 4
    assert h1.count("A | B") == 1
    assert "Approve 6" in h1 and "6 files" in h1      # full-group counts

    h2 = client.get("/?page=2").data.decode()
    assert h2.count('class="path"') == 2
    assert h2.count("A | B") == 1                     # header repeated per chunk
    assert "Approve 6" in h2


def test_queue_search_is_server_side(cfg, make_track, client):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    for name, aa, al, t in (("a0.flac", "Alpha", "One", 1),
                            ("a1.flac", "Alpha", "One", 2),
                            ("z0.flac", "Zeta", "Yellow", 1)):
        p = make_track(cfg.music_dir, name, title=name, artist=aa, album=al,
                       albumartist=aa, track=t)
        r = db.enqueue(conn, path=str(p), mtime=1.0, size=1,
                       kind="organize-only",
                       current_tags={"albumartist": aa, "album": al})
        db.set_candidates(conn, r.id, [{"albumartist": aa, "album": al}])
    conn.close()

    # matches the group key (album name), not just paths
    h = client.get("/?q=yellow").data.decode()
    assert "Zeta | Yellow" in h and "Alpha | One" not in h
    assert "1 file match" in h
    assert 'autofocus' in h                           # focus restored after reload

    # no-results state, never a blank page
    h2 = client.get("/?q=zzz-nothing").data.decode()
    assert "No matching files" in h2


# --- job progress section -------------------------------------------------


def _job_status(**over):
    base = {"running": False, "phase": "idle", "seen": 0, "queued": 0,
            "fingerprinted": 0, "errors": 0, "message": None,
            "last_scan": None,
            "activity": {"current": None, "recent": []}}
    base.update(over)
    return base


def _seed_scan(cfg, **fields):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    sid = db.start_scan(conn, kind="scan")
    db.update_scan(conn, sid, **fields)
    conn.close()
    return sid


_SCAN_EPOCH = time.mktime((2026, 11, 23, 14, 5, 0, 0, 0, -1))  # local 14:05


def test_queue_job_section_hidden_without_scans(cfg, client, monkeypatch):
    monkeypatch.setattr(worker, "status", _job_status)
    html = client.get("/").data.decode()
    assert re.search(r'<section id="job-progress"[^>]*\shidden', html)
    assert 'data-state="hidden"' in html


def test_queue_job_section_idle_renders_last_scan_from_db(cfg, client, monkeypatch):
    # worker.status() claims no last_scan (global DB path unset); the DB row wins
    monkeypatch.setattr(worker, "status", _job_status)
    _seed_scan(cfg, phase="done", status="done", seen=1204, queued=300,
               fingerprinted=300, errors=12, started_at=_SCAN_EPOCH)
    html = client.get("/").data.decode()
    assert 'data-state="idle"' in html
    assert "Last scan: 14:05 · 1,204 files · 12 errors" in html


def test_queue_job_section_running_renders_counts_now_and_progress(
        cfg, client, monkeypatch):
    monkeypatch.setattr(worker, "status", lambda: _job_status(
        running=True, phase="fingerprinting", fingerprinted=1240,
        queued=3100, errors=12,
        activity={"current": {"op": "fingerprint",
                              "path": "/music/Album/04 - Track.flac",
                              "duration": 1.8},
                  "recent": []}))
    html = client.get("/").data.decode()
    assert 'data-state="running"' in html
    assert "1,240 / 3,100 files · 12 errors" in html
    assert 'aria-valuenow="40"' in html
    assert "04 - Track.flac" in html
    assert "(1.8s)" in html
    # single live region only (the dock's); no second announcement channel
    assert html.count("aria-live") == 1


def test_queue_job_section_running_without_path_does_not_500(cfg, client, monkeypatch):
    # scanning phase: activity.start("scan") carries path=None — the now-line
    # must not call .split() on it (review critical: 500 during main scan flow)
    monkeypatch.setattr(worker, "status", lambda: _job_status(
        running=True, phase="scanning", seen=420,
        activity={"current": {"op": "scan", "path": None, "duration": 0.4},
                  "recent": []}))
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert 'data-state="running"' in html
    assert "Now:" not in html


def test_queue_job_section_error_shows_message(cfg, client, monkeypatch):
    monkeypatch.setattr(worker, "status", lambda: _job_status(
        phase="error", message="AcoustID HTTP 503"))
    html = client.get("/").data.decode()
    assert 'data-state="error"' in html
    assert "Job failed: AcoustID HTTP 503" in html


def test_format_scan_time_returns_local_hhmm():
    from app import web
    assert web.format_scan_time(_SCAN_EPOCH) == "14:05"


def test_step_classes_applying_marks_apply_step_active():
    from app import web
    steps = web._step_classes("applying")
    assert steps == {"scan": "is-done", "fingerprint": "is-done",
                     "apply": "is-active", "done": "is-todo"}
    assert set(web._step_classes("scanning")) == set(steps)
    assert web._step_classes("scanning")["scan"] == "is-active"
    assert web._step_classes("fingerprinting")["fingerprint"] == "is-active"
    assert web._step_classes("done")["done"] == "is-active"


def test_queue_job_section_applying_renders(cfg, client, monkeypatch):
    monkeypatch.setattr(worker, "status", lambda: _job_status(
        running=True, phase="applying", fingerprinted=9, queued=10, errors=0,
        activity={"current": {"op": "move",
                              "path": "/music/A/B/01 - X.flac",
                              "duration": 0.9},
                  "recent": []}))
    html = client.get("/").data.decode()
    assert 'data-state="running"' in html
    assert re.search(r'<span class="job-step is-active">[^<]*Applying', html)
    assert 'aria-valuenow="90"' in html
    # no fake "fingerprinting" counts while applying
    assert '<p id="job-counts" class="job-counts"></p>' in html
    assert "01 - X.flac" in html                    # now-line shows the apply


def test_retry_with_candidates_starts_job(cfg, make_track, client,
                                          monkeypatch):
    p, row = _seed_proposal(cfg, make_track, candidates=[{"title": "X"}])
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.update_status(conn, row.id, "error", error="boom")  # candidates kept
    conn.close()
    started = {"n": 0}
    monkeypatch.setattr(worker, "start_job",
                        lambda cfg, kind="scan": started.__setitem__(
                            "n", started["n"] + 1) or True)
    resp = client.post(f"/proposal/{row.id}/retry", follow_redirects=True)
    assert resp.status_code == 200
    assert started["n"] == 1
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    prop = db.get(conn, row.id)
    assert prop.status == "awaiting-review"   # phase 2 will pick it up
    conn.close()
