from pathlib import Path

import mediafile
import pytest

from app import beets_adapter


def test_ensure_setup_writes_config_yaml(cfg):
    beets_adapter.ensure_setup(cfg)
    text = (cfg.data_dir / "config.yaml").read_text()
    assert "directory:" in text and beets_adapter.DEFAULT_TEMPLATE in text


def test_ensure_setup_is_idempotent(cfg):
    beets_adapter.ensure_setup(cfg); first = beets_adapter.lib()
    beets_adapter.ensure_setup(cfg)
    assert beets_adapter.lib() is first


def test_read_fields_returns_tag_values(cfg, make_track):
    p = make_track(cfg.music_dir, "t.flac", title="Hello", artist="A", album="B",
                   albumartist="A", track=1, disc=1, year=2001)
    assert beets_adapter.read_fields(p) == {
        "title": "Hello", "artist": "A", "album": "B", "albumartist": "A",
        "track": 1, "disc": 1, "year": 2001}


def test_read_fields_untagged_is_all_none(cfg, make_track):
    p = make_track(cfg.music_dir, "u.flac")
    assert all(v is None for v in beets_adapter.read_fields(p).values())


def test_expected_path_follows_template(cfg, make_track):
    p = make_track(cfg.music_dir, "x.flac", title="Test Title", artist="Test Artist",
                   album="Test Album", albumartist="Test Artist", track=1)
    assert beets_adapter.expected_path(p) == (
        cfg.music_dir / "Test Artist" / "Test Album" / "01 - Test Title.flac")
    # zero-padded track: template's $track renders "01"


def test_candidates_for_flattens_track_match(cfg, make_track, monkeypatch):
    from types import SimpleNamespace
    import beets.autotag as beets_autotag

    p = make_track(cfg.music_dir, "c.flac")
    match = SimpleNamespace(
        info=SimpleNamespace(item_data={"title": "T", "artist": "A", "album": "B",
                                       "albumartist": "A", "track": 3, "disc": 1,
                                       "year": 1999, "mb_trackid": "mt",
                                       "mb_albumid": "ma"}),
        distance=SimpleNamespace(distance=0.06))
    proposal = SimpleNamespace(candidates=[match],
                               recommendation=beets_autotag.Recommendation.strong)
    monkeypatch.setattr(beets_adapter, "tag_item", lambda *a, **k: proposal)
    out = beets_adapter.candidates_for(p, ["rid"])
    assert out == [{**{f: match.info.item_data[f] for f in beets_adapter.CANDIDATE_FIELDS},
                    "score": 0.94, "recommendation": "strong"}]


def test_candidates_for_falls_back_to_text_search(cfg, make_track, monkeypatch):
    from types import SimpleNamespace
    import beets.autotag as beets_autotag

    p = make_track(cfg.music_dir, "c2.flac")
    match = SimpleNamespace(
        info=SimpleNamespace(item_data={"title": "T"}),
        distance=SimpleNamespace(distance=0.1))
    proposal = SimpleNamespace(candidates=[match],
                               recommendation=beets_autotag.Recommendation.strong)

    calls = {"n": 0}

    def fake_tag_item(*a, **k):
        calls["n"] += 1
        if "search_ids" in k and k.get("search_ids"):
            return SimpleNamespace(candidates=[], recommendation=beets_autotag.Recommendation.none)
        return proposal

    monkeypatch.setattr(beets_adapter, "tag_item", fake_tag_item)
    out = beets_adapter.candidates_for(p, ["rid"])
    assert len(out) == 1
    assert calls["n"] == 2


def test_candidates_for_empty_when_no_match(cfg, make_track, monkeypatch):
    from types import SimpleNamespace
    import beets.autotag as beets_autotag

    p = make_track(cfg.music_dir, "c3.flac")
    empty = SimpleNamespace(candidates=[], recommendation=beets_autotag.Recommendation.none)
    monkeypatch.setattr(beets_adapter, "tag_item", lambda *a, **k: empty)
    assert beets_adapter.candidates_for(p, ["rid"]) == []


def test_real_trackinfo_item_data_has_candidate_fields():
    from beets.autotag.hooks import TrackInfo
    ti = TrackInfo(title="T", index=3, track_id="mt", medium=1, year=1999,
                   artist="A", album="B", albumartist="A")
    data = ti.item_data
    # flattening reads every CANDIDATE_FIELD with .get — must never raise
    for field in beets_adapter.CANDIDATE_FIELDS:
        data.get(field)
    # every field this TrackInfo sets must actually be present (mb_albumid
    # is the one field left unset here and legitimately absent)
    for field in set(beets_adapter.CANDIDATE_FIELDS) - {"mb_albumid"}:
        assert field in data, field
    assert data["mb_trackid"] == "mt"
    assert data["track"] == 3
    assert data["disc"] == 1
    assert data["year"] == 1999


def test_happy_path_writes_tags_and_moves(cfg, make_track):
    p = make_track(cfg.music_dir, "old.flac", title="Old")
    dest, item_id = beets_adapter.apply_approved(
        p, {"title": "New", "artist": "A", "album": "B", "albumartist": "A",
            "track": 1, "disc": 1, "year": 2000},
        move_only=False, expect=None, beets_item_id=None)
    assert dest == cfg.music_dir / "A" / "B" / "01 - New.flac" and dest.exists()
    assert mediafile.MediaFile(str(dest)).title == "New"
    assert len(list(beets_adapter.lib().items())) == 1


def test_collision_destination_overwritten(cfg, make_track):
    p = make_track(cfg.music_dir, "a.flac", title="T", artist="A", album="B",
                   albumartist="A", track=1)
    dest = cfg.music_dir / "A" / "B" / "01 - T.flac"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"EXISTING")
    got, item_id = beets_adapter.apply_approved(
        p, {"title": "T"}, move_only=False, expect=None, beets_item_id=None)
    assert got == dest
    assert dest.exists() and not p.exists()          # moved, not uniquified
    assert dest.read_bytes() != b"EXISTING"
    assert mediafile.MediaFile(str(dest)).title == "T"
    assert not (dest.parent / "01 - T.1.flac").exists()  # beets unique_path trap
    assert len(list(beets_adapter.lib().items())) == 1


def test_overwrite_removes_displaced_beets_row(cfg, make_track):
    import os
    p = make_track(cfg.music_dir, "a.flac", title="T", artist="A", album="B",
                   albumartist="A", track=1)
    dest = make_track(cfg.music_dir, "A/B/01 - T.flac", title="T", artist="A",
                      album="B", albumartist="A", track=1)
    displaced = beets_adapter.Item.from_path(dest)
    displaced._db = beets_adapter.lib()
    beets_adapter.lib().add(displaced)               # beets auto-assigns the id
    assert len(list(beets_adapter.lib().items())) == 1
    got, _ = beets_adapter.apply_approved(
        p, None, move_only=True, expect=None, beets_item_id=None)
    assert got == dest and dest.exists() and not p.exists()
    items = list(beets_adapter.lib().items())
    assert len(items) == 1                            # displaced row removed
    assert Path(os.fsdecode(bytes(items[0].path))) == dest
    assert mediafile.MediaFile(str(dest)).title == "T"  # source content won


def test_write_failure_leaves_existing_destination_intact(cfg, make_track,
                                                          monkeypatch):
    p = make_track(cfg.music_dir, "a.flac", title="T", artist="A", album="B",
                   albumartist="A", track=1)
    dest = make_track(cfg.music_dir, "A/B/01 - T.flac", title="T", artist="A",
                      album="B", albumartist="A", track=1)
    monkeypatch.setattr(beets_adapter.Item, "try_write",
                        lambda self, *a, **k: False)
    with pytest.raises(beets_adapter.ApplyFailed):
        beets_adapter.apply_approved(
            p, {"title": "T"}, move_only=False, expect=None, beets_item_id=None)
    # destruction is deferred to move time: a failed write never ate dest
    assert dest.exists()
    assert mediafile.MediaFile(str(dest)).title == "T"
    assert p.exists()


def test_file_changed_rejected(cfg, make_track):
    p = make_track(cfg.music_dir, "c.flac", title="T")
    with pytest.raises(beets_adapter.FileChanged):
        beets_adapter.apply_approved(p, {"title": "X"}, move_only=False,
                                     expect=(1.0, 2), beets_item_id=None)
    assert p.exists()
    assert mediafile.MediaFile(str(p)).title == "T"  # rejected before any write


def test_write_failure_keeps_original_and_retry_succeeds(cfg, make_track, monkeypatch):
    p = make_track(cfg.music_dir, "w.flac")
    monkeypatch.setattr(beets_adapter.Item, "try_write", lambda self, *a, **k: False)
    with pytest.raises(beets_adapter.ApplyFailed) as exc:
        beets_adapter.apply_approved(p, {"title": "New"}, move_only=False,
                                     expect=None, beets_item_id=None)
    assert exc.value.item_id is not None
    assert p.exists() and mediafile.MediaFile(str(p)).title is None
    monkeypatch.undo()
    dest, item_id = beets_adapter.apply_approved(p, {"title": "New"}, move_only=False,
                                                 expect=None, beets_item_id=exc.value.item_id)
    assert dest.exists() and len(list(beets_adapter.lib().items())) == 1


def test_move_failure_retry_single_row(cfg, make_track, monkeypatch):
    p = make_track(cfg.music_dir, "mv.flac", title="Keep", artist="A", album="B",
                   albumartist="A", track=1)
    calls = {"n": 0}

    original_move = beets_adapter.Item.move

    def fake_move(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("boom")
        return original_move(self, *a, **k)

    monkeypatch.setattr(beets_adapter.Item, "move", fake_move)
    fields = {"title": "New", "artist": "A", "album": "B", "albumartist": "A", "track": 1}
    with pytest.raises(beets_adapter.ApplyFailed) as exc:
        beets_adapter.apply_approved(p, fields, move_only=False,
                                     expect=None, beets_item_id=None)
    assert exc.value.item_id is not None
    assert p.exists()
    assert mediafile.MediaFile(str(p)).title == "New"
    dest, item_id = beets_adapter.apply_approved(p, fields, move_only=False,
                                                 expect=None, beets_item_id=exc.value.item_id)
    assert dest.exists()
    assert len(list(beets_adapter.lib().items())) == 1


def test_move_only_leaves_tags_untouched(cfg, make_track):
    p = make_track(cfg.music_dir, "m.flac", title="Keep", artist="A", album="B",
                   albumartist="A", track=1)
    dest, _ = beets_adapter.apply_approved(p, None, move_only=True,
                                           expect=None, beets_item_id=None)
    assert dest.exists() and mediafile.MediaFile(str(dest)).title == "Keep"


def test_expect_match_proceeds(cfg, make_track):
    p = make_track(cfg.music_dir, "e.flac", title="T", artist="A", album="B",
                   albumartist="A", track=1)
    st = p.stat()
    dest, _ = beets_adapter.apply_approved(p, None, move_only=True,
                                           expect=(st.st_mtime, st.st_size),
                                           beets_item_id=None)
    assert dest.exists()
