import app.activity as activity
import app.db as db
import app.worker as worker


def _seed_pending(cfg, path="/m/a.flac"):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    row = db.enqueue(conn, path=path, mtime=1.0, size=1, kind="needs-tags",
                     current_tags={})
    conn.close()
    return row


def test_fingerprint_pass_records_activity(cfg, monkeypatch):
    activity.reset()
    _seed_pending(cfg)
    monkeypatch.setattr(worker, "recording_ids", lambda p, k: ["rid"])
    monkeypatch.setattr(worker, "candidates_for", lambda p, ids: [{"title": "T"}])
    worker.run_fingerprint_sync(cfg)
    snap = worker.status()["activity"]
    assert snap["current"] is None
    ev = [e for e in snap["recent"] if e["op"] == "fingerprint"]
    assert ev, snap
    assert ev[0]["path"] == "/m/a.flac"
    assert ev[0]["ok"] is True
    assert "duration" in ev[0]


def test_fingerprint_failure_recorded_with_detail(cfg, monkeypatch):
    from app.fingerprint import AcoustidFailure

    activity.reset()
    _seed_pending(cfg, "/m/bad.flac")
    monkeypatch.setattr(
        worker, "recording_ids",
        lambda p, k: (_ for _ in ()).throw(AcoustidFailure(f"no fp: {p}")))
    worker.run_fingerprint_sync(cfg)
    snap = worker.status()["activity"]
    ev = [e for e in snap["recent"] if e["op"] == "fingerprint"]
    assert ev and ev[0]["ok"] is False
    assert "no fp" in ev[0]["detail"]


def test_approve_records_activity(cfg, make_track, client, monkeypatch):
    activity.reset()
    cand = {f: "V" for f in __import__("app.beets_adapter", fromlist=["x"]).CANDIDATE_FIELDS}
    cand.update({"track": 1, "disc": 1, "year": 2000})
    from tests.test_web import _seed_proposal  # reuse the web helper

    p, row = _seed_proposal(cfg, make_track, candidates=[cand])
    monkeypatch.setattr("app.beets_adapter.apply_approved",
                        lambda path, fields, **kw: (path, 1))
    resp = client.post(f"/proposal/{row.id}/approve",
                       data={"action": "candidate"}, follow_redirects=True)
    assert resp.status_code == 200
    snap = client.get("/api/status").get_json()["activity"]
    ev = [e for e in snap["recent"] if e["op"] == "approve"]
    assert ev, snap
    assert ev[0]["ok"] is True
    assert ev[0]["path"] == str(p)


def test_ring_buffer_caps_and_newest_first():
    activity.reset()
    for i in range(activity.MAX_EVENTS + 20):
        activity.record("fingerprint", f"/m/{i}.flac")
    snap = activity.snapshot()
    assert len(snap["recent"]) == activity.MAX_EVENTS
    assert snap["recent"][0]["path"] == f"/m/{activity.MAX_EVENTS + 19}.flac"
    activity.reset()


def test_current_op_visible_while_running():
    activity.reset()
    activity.start("fingerprint", "/m/x.flac")
    snap = activity.snapshot()
    assert snap["current"]["op"] == "fingerprint"
    assert snap["current"]["path"] == "/m/x.flac"
    assert snap["current"]["duration"] >= 0
    activity.finish(ok=True)
    snap = activity.snapshot()
    assert snap["current"] is None
    assert snap["recent"][0]["path"] == "/m/x.flac"
    activity.reset()
