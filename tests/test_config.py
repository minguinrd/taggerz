from pathlib import Path

import pytest

from app.config import load_config


def test_defaults(monkeypatch):
    for var in ("MUSIC_DIR", "DATA_DIR", "PORT", "HOST", "ACOUSTID_APP_KEY"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config(environ={})
    assert cfg.music_dir == Path("/music")
    assert cfg.data_dir == Path("/data")
    assert cfg.db_path == Path("/data/queue.db")
    assert cfg.acoustid_app_key is None
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 8080


def test_env_overrides():
    cfg = load_config(environ={"MUSIC_DIR": "/m", "DATA_DIR": "/d",
                               "PORT": "9000", "HOST": "127.0.0.1",
                               "ACOUSTID_APP_KEY": "abc"})
    assert cfg.music_dir == Path("/m")
    assert cfg.db_path == Path("/d/queue.db")
    assert cfg.port == 9000
    assert cfg.acoustid_app_key == "abc"


def test_bad_port_rejected():
    with pytest.raises(ValueError):
        load_config(environ={"PORT": "not-a-port"})
