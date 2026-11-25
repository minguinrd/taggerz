"""The only module that imports beets/beetsplug.

All beets interaction (setup, tag reading, path templating, candidate
generation, apply) lives here so the rest of the app stays beets-free.
"""

import json
import os
from pathlib import Path

import yaml
from beets.autotag.match import tag_item
from beets.library import Item

from app.config import Config

DEFAULT_TEMPLATE = "$albumartist/$album/$track - $title"

CANDIDATE_FIELDS = (
    "title", "artist", "album", "albumartist", "track", "disc", "year",
    "mb_trackid", "mb_albumid",
)


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


def path_signature() -> str:
    """Stable string of everything that decides a file's expected path.

    The scan's settled cache is only valid for the signature it was built
    under: a template or library-directory change must re-evaluate files.
    """
    from beets import config as beets_config

    paths = {key: beets_config["paths"][key].as_str()
             for key in ("default", "singleton", "comp")}
    return json.dumps({"directory": beets_config["directory"].as_str(),
                       "paths": paths}, sort_keys=True)


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


def default_acoustid_key() -> str:
    """Return beets' embedded AcoustID application key."""
    import beetsplug.chroma

    return beetsplug.chroma.API_KEY


def candidates_for(path: Path, ids: list[str]) -> list[dict]:
    """Generate candidate tag dicts for ``path`` using AcoustID recording ids.

    ``ids`` may be empty/None-ish for text-only search. When an ID search
    yields nothing, retry once with text search. Returns ``[]`` if no match.
    """
    from beets.autotag import Source
    from beets.library import Item

    item = Item.from_path(path)
    source = Source.from_item(item)

    proposal = None
    if ids:
        proposal = tag_item(source, search_ids=ids)
        if not (proposal and proposal.candidates):
            # Fall back to text search once.
            proposal = tag_item(source)
    else:
        proposal = tag_item(source)

    if not proposal or not proposal.candidates:
        return []

    out: list[dict] = []
    for match in proposal.candidates:
        info = getattr(match, "info", None)
        item_data = getattr(info, "item_data", {}) if info is not None else {}
        if not isinstance(item_data, dict):
            item_data = dict(item_data) if item_data is not None else {}
        entry: dict = {
            field: item_data.get(field) for field in CANDIDATE_FIELDS
        }
        dist = getattr(match, "distance", None)
        distance_val = getattr(dist, "distance", None)
        if callable(distance_val):
            distance_val = distance_val()
        try:
            entry["score"] = round(1 - float(distance_val), 3)
        except (TypeError, ValueError):
            entry["score"] = None
        rec = getattr(proposal, "recommendation", None)
        entry["recommendation"] = getattr(rec, "name", str(rec)) if rec is not None else None
        out.append(entry)
    return out


def _remove_library_row_at(path: Path) -> None:
    """Remove beets library rows pointing at ``path`` (files untouched)."""
    for it in list(lib().items()):
        raw = it.path
        it_path = Path(os.fsdecode(bytes(raw))) if isinstance(
            raw, (bytes, bytearray)) else Path(raw)
        if it_path == path:
            it.remove(delete=False)


def apply_approved(path: Path, fields: dict | None, *, move_only: bool,
                  expect: tuple[float, int] | None, beets_item_id: int | None) -> tuple[Path, int | None]:
    """Collision-safe write + organize. Returns (dest, item_id)."""
    from beets import config as beets_config
    from beets.util.pathformats import get_path_formats

    path = Path(path)

    # 1. Expect check.
    if expect is not None:
        try:
            st = path.stat()
        except FileNotFoundError:
            raise ApplyFailed("file no longer exists")
        if (st.st_mtime, st.st_size) != expect:
            raise FileChanged()

    # 2. Detached item + precompute destination BEFORE any library insert.
    # Apply approved fields in-memory first so the destination reflects the
    # approved (new) tags rather than the on-disk originals.
    item = Item.from_path(path)
    if (not move_only) and fields:
        item.update(fields)
    item._db = lib()
    item.id = 1
    dest = item.destination(
        basedir=lib().directory,
        path_formats=get_path_formats(beets_config["paths"]),
    )
    if isinstance(dest, (bytes, bytearray)):
        dest = Path(os.fsdecode(bytes(dest)))
    else:
        dest = Path(dest)

    # 3. (Former collision check removed: an existing destination is now
    # replaced at move time — step 6 — never before the tag write.)

    # 4. Library sync.
    reused = None
    if beets_item_id is not None:
        existing = lib().get_item(beets_item_id)
        if existing is not None:
            try:
                existing_path = Path(os.fsdecode(bytes(existing.path))) if isinstance(existing.path, (bytes, bytearray)) else Path(existing.path)
            except Exception:
                existing_path = Path(existing.path)
            if existing_path == path:
                reused = existing
    if reused is not None:
        item = reused
    else:
        item = Item.from_path(path)
        if (not move_only) and fields:
            item.update(fields)
        item._db = lib()
        item.id = 1
        lib().add(item)

    # 5. Tag write (unless move-only).
    if (not move_only) and fields:
        item.update(fields)
        if not item.try_write():
            raise ApplyFailed("failed to write tags", item_id=item.id)

    # 6. Move. An existing destination is replaced (spec: overwrite).
    # Destruction happens only here, after the tag write succeeded; with the
    # path free, item.move() lands exactly at dest instead of beets'
    # uniquified "dest.1".
    if dest != path:
        if dest.exists():
            _remove_library_row_at(dest)
            try:
                dest.unlink()
            except OSError:
                raise ApplyFailed("failed to replace destination",
                                  item_id=item.id)
        try:
            item.move()
        except OSError:
            raise ApplyFailed("failed to move file", item_id=item.id)

    # 7. Return.
    return (dest, getattr(item, "id", None))
