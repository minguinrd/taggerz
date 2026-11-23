import pathlib
import shutil

import pytest

from app import beets_adapter
from app.config import Config


@pytest.fixture(autouse=True)
def _reset_beets():
    beets_adapter._reset()
    yield


@pytest.fixture
def cfg(tmp_path, _reset_beets):
    music = tmp_path / "music"
    data = tmp_path / "data"
    music.mkdir()
    data.mkdir()
    c = Config(
        music_dir=music,
        data_dir=data,
        db_path=data / "queue.db",
        acoustid_app_key=None,
        host="127.0.0.1",
        port=8080,
    )
    beets_adapter.ensure_setup(c)
    return c


@pytest.fixture
def make_track():
    fixture = pathlib.Path(__file__).parent / "fixtures" / "silence.flac"

    def _make(directory, filename, **tags):
        directory = pathlib.Path(directory)
        dest = directory / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(fixture, dest)
        if tags:
            import mediafile

            mf = mediafile.MediaFile(str(dest))
            for key, value in tags.items():
                setattr(mf, key, value)
            mf.save()
        return dest

    return _make