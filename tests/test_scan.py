import os
from pathlib import Path

import pytest

import app.db as db
import app.scan as scan


def _conn(cfg):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    return conn


def _seed_tree(cfg, make_track):
    make_track(cfg.music_dir, "untagged.flac")  # no tags
    make_track(cfg.music_dir, "Artist/Album/01 - Song.flac",
               title="Song", artist="Artist", album="Album", albumartist="Artist", track=1)
    make_track(cfg.music_dir, "loose.flac",
               title="Song2", artist="Artist2", album="Album2", albumartist="Artist2", track=2)
    (cfg.music_dir / "notes.txt").write_text("x")


def test_audio_exts_exact():
    assert ".flac" in scan.AUDIO_EXTS
    assert ".txt" not in scan.AUDIO_EXTS


def test_missing_core():
    assert scan.missing_core({"artist": None, "album": "A", "title": "T", "track": 1}) is True
    assert scan.missing_core({"artist": "A", "album": "A", "title": "T", "track": 0}) is True
    assert scan.missing_core({"artist": "A", "album": "A", "title": "T", "track": 1}) is False
    assert scan.missing_core({"artist": "  ", "album": "A", "title": "T", "track": 1}) is True


def test_classify():
    p = Path("/m/a.flac")
    assert scan.classify({"artist": None, "album": None, "title": None, "track": None},
                        p, None) == "needs-tags"
    exp = Path("/m/Artist/Album/01 - T.flac")
    assert scan.classify({"artist": "A", "album": "B", "title": "T", "track": 1}, p, exp) == "organize-only"
    same = p
    assert scan.classify({"artist": "A", "album": "B", "title": "T", "track": 1}, p, p) is None


def test_walk_skips_dotdirs_and_nonaudio(cfg, make_track):
    _seed_tree(cfg, make_track)
    (cfg.music_dir / ".hidden").mkdir()
    make_track(cfg.music_dir / ".hidden", "secret.flac")
    paths = list(scan.walk(cfg.music_dir))
    names = [p.name for p in paths]
    assert "secret.flac" not in names
    assert "notes.txt" not in names
    assert len(paths) == 3


def test_scan_classifies_and_enqueues(cfg, make_track):
    _seed_tree(cfg, make_track)
    stats = scan.run(cfg)
    assert stats["seen"] == 3 and stats["queued"] == 2
    conn = _conn(cfg)
    untagged = str(cfg.music_dir / "untagged.flac")
    loose = str(cfg.music_dir / "loose.flac")
    assert db.get_by_path(conn, untagged).kind == "needs-tags"
    assert db.get_by_path(conn, loose).kind == "organize-only"
    good = str(cfg.music_dir / "Artist" / "Album" / "01 - Song.flac")
    assert db.get_by_path(conn, good) is None
    assert db.get_by_path(conn, str(cfg.music_dir / "notes.txt")) is None
    conn.close()


def test_rescan_preserves_decisions(cfg, make_track):
    _seed_tree(cfg, make_track)
    scan.run(cfg)
    conn = _conn(cfg)
    untagged = str(cfg.music_dir / "untagged.flac")
    loose = str(cfg.music_dir / "loose.flac")
    # enqueue a third pending row by marking loose pending stays pending
    p_untagged = db.get_by_path(conn, untagged)
    p_loose = db.get_by_path(conn, loose)
    # skip one
    db.update_status(conn, p_untagged.id, "skipped")
    # error one with message
    db.update_status(conn, p_loose.id, "error", error="boom")
    conn.close()

    # re-run scan
    scan.run(cfg)
    conn = _conn(cfg)
    assert db.get_by_path(conn, untagged).status == "skipped"
    err_row = db.get_by_path(conn, loose)
    assert err_row.status == "error"
    assert err_row.error == "boom"
    conn.close()


def test_rescan_resets_done_when_file_changed(cfg, make_track):
    _seed_tree(cfg, make_track)
    scan.run(cfg)
    conn = _conn(cfg)
    loose = str(cfg.music_dir / "loose.flac")
    row = db.get_by_path(conn, loose)
    # mark done
    db.update_status(conn, row.id, "done")
    conn.close()

    p = cfg.music_dir / "loose.flac"
    import mediafile
    mf = mediafile.MediaFile(str(p))
    mf.title = None
    mf.artist = None
    mf.album = None
    mf.albumartist = None
    mf.track = 0
    mf.save()
    new_mtime = p.stat().st_mtime + 10
    os.utime(p, (new_mtime, new_mtime))

    scan.run(cfg)
    conn = _conn(cfg)
    new = db.get_by_path(conn, loose)
    assert new.status == "pending"
    assert new.kind == "needs-tags"
    conn.close()


def test_rescan_leaves_done_unchanged_alone(cfg, make_track):
    _seed_tree(cfg, make_track)
    scan.run(cfg)
    conn = _conn(cfg)
    loose = str(cfg.music_dir / "loose.flac")
    row = db.get_by_path(conn, loose)
    db.update_status(conn, row.id, "done")
    conn.close()

    scan.run(cfg)
    conn = _conn(cfg)
    assert db.get_by_path(conn, loose).status == "done"
    conn.close()


def test_open_row_missing_file_becomes_error(cfg, make_track):
    _seed_tree(cfg, make_track)
    scan.run(cfg)
    conn = _conn(cfg)
    loose = str(cfg.music_dir / "loose.flac")
    row = db.get_by_path(conn, loose)
    # make it pending
    db.update_status(conn, row.id, "pending")
    conn.close()

    # delete file
    (cfg.music_dir / "loose.flac").unlink()

    scan.run(cfg)
    conn = _conn(cfg)
    r = db.get_by_path(conn, loose)
    assert r.status == "error"
    assert r.error == "file no longer exists"
    conn.close()


def test_scan_does_not_modify_files(cfg, make_track):
    _seed_tree(cfg, make_track)
    before = {p: (p.stat().st_mtime, p.stat().st_size) for p in cfg.music_dir.rglob("*") if p.is_file()}
    scan.run(cfg)
    after = {p: (p.stat().st_mtime, p.stat().st_size) for p in cfg.music_dir.rglob("*") if p.is_file()}
    assert before == after
