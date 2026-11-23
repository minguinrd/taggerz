"""The only module that imports beets/beetsplug.

All beets interaction (setup, tag reading, path templating, candidate
generation, apply) lives here so the rest of the app stays beets-free.
"""

import os
from pathlib import Path

import yaml

from app.config import Config

DEFAULT_TEMPLATE = "$albumartist/$album/$track - $title"


class DestinationExists(Exception):
    """Raised when an approve-time destination already exists."""


class FileChanged(Exception):
    """Raised when the source file changed on disk since enqueueing."""


class ApplyFailed(Exception):
    """Raised when a tag write or move fails. ``item_id`` is optional."""

    def __init__(self, message: str, item_id: int | None = None) -> None:
        super().__init__(message)
        self.item_id = item_id


# Process-global state.
_config_loaded: bool = False
_lib = None


def _reset() -> None:
    """Test-only: clear the singleton and the config-loaded flag."""
    global _config_loaded, _lib
    _config_loaded = False
    _lib = None


def ensure_setup(cfg: Config) -> None:
    """Idempotent first-boot setup against ``cfg``'s tmp/real dirs."""
    global _config_loaded, _lib

    if _lib is not None:
        return

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    config_path = cfg.data_dir / "config.yaml"
    if not config_path.exists():
        config_path.write_text(
            f"directory: {cfg.music_dir}\n"
            f"library: {cfg.data_dir / 'beets.db'}\n"
            f"paths:\n"
            f"  default: {DEFAULT_TEMPLATE}\n"
            f"  singleton: {DEFAULT_TEMPLATE}\n"
            f"  comp: {DEFAULT_TEMPLATE}\n"
        )

    import beets
    from beets import config as beets_config

    if not _config_loaded:
        beets_config.read(user=False, defaults=True)
        _config_loaded = True

    data = yaml.safe_load(config_path.read_text()) or {}
    beets_config["directory"] = data.get("directory", str(cfg.music_dir))
    beets_config["library"] = data.get("library", str(cfg.data_dir / "beets.db"))
    paths = data.get("paths") or {}
    for key in ("default", "singleton", "comp"):
        beets_config["paths"][key] = paths.get(key, DEFAULT_TEMPLATE)

    import beets.plugins

    beets.plugins.load_plugins()

    from beets.library.library import Library

    _lib = Library(cfg.data_dir / "beets.db", directory=cfg.music_dir)


def lib():
    """The singleton Library (raises if ``ensure_setup`` not called)."""
    if _lib is None:
        raise RuntimeError("beets_adapter.ensure_setup() has not been called")
    return _lib


def read_fields(path: Path) -> dict:
    """Read the seven core fields from the file at ``path``."""
    from beets.library import Item

    item = Item.from_path(path)
    result: dict = {}
    for key in ("title", "artist", "album", "albumartist", "track", "disc", "year"):
        val = item.get(key)
        if isinstance(val, str):
            val = val.strip() or None
        elif key in ("track", "disc", "year") and val == 0:
            val = None
        result[key] = val
    return result


def expected_path(path: Path) -> Path:
    """Where beets would put ``path`` under the configured template."""
    from beets import config as beets_config
    from beets.library import Item
    from beets.util.pathformats import get_path_formats

    item = Item.from_path(path)
    # destination() always reads self.db.replacements, and the Model.db
    # property runs _check_db(need_id=True) — so a detached item needs both
    # the attached library and a truthy id, even with basedir/path_formats
    # passed explicitly. Harmless: lib().add() reassigns id on insert.
    item._db = lib()
    item.id = 1

    dest = item.destination(
        basedir=lib().directory,
        path_formats=get_path_formats(beets_config["paths"]),
    )
    if isinstance(dest, (bytes, bytearray)):
        return Path(os.fsdecode(bytes(dest)))
    return Path(dest)
