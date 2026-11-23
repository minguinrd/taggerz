"""Application configuration: env-var driven, pure, no filesystem access."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    music_dir: Path
    data_dir: Path
    db_path: Path
    acoustid_app_key: str | None
    host: str
    port: int


def load_config(environ: Mapping[str, str] = os.environ) -> Config:
    music_dir = Path(environ.get("MUSIC_DIR", "/music"))
    data_dir = Path(environ.get("DATA_DIR", "/data"))
    port = int(environ.get("PORT", "8080"))  # ValueError propagates
    host = environ.get("HOST", "0.0.0.0")
    acoustid_app_key = environ.get("ACOUSTID_APP_KEY") or None
    return Config(
        music_dir=music_dir,
        data_dir=data_dir,
        db_path=data_dir / "queue.db",
        acoustid_app_key=acoustid_app_key,
        host=host,
        port=port,
    )
