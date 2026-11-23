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
