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
