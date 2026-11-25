"""Application factory."""

import os
import secrets
import sqlite3

from flask import Flask

from app import beets_adapter, db
from app.config import Config, load_config


def _ensure_data_writable(cfg: Config) -> None:
    """Fail fast with an actionable message when the data dir can't be written.

    A wrong-owner data volume (Docker ``user:`` changed after the volume
    existed) otherwise boots fine — ``init_db`` is a no-op on an existing
    schema — and only surfaces as a cryptic sqlite "attempt to write a
    readonly database" on the first scan.
    """
    uid = os.getuid()
    probe = cfg.data_dir / ".write-probe"
    try:
        probe.write_text("")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"data directory missing or not writable by uid {uid}: "
            f"{cfg.data_dir} ({exc.strerror or exc}). If the container runs "
            "with a `user:` directive, chown the data volume to that uid/gid: "
            "docker run --rm -v <project>_taggerz-data:/data alpine "
            f"chown -R {uid}:{uid} /data"
        ) from exc

    conn = sqlite3.connect(cfg.db_path)
    try:
        # Real write required: BEGIN alone doesn't touch the file. At least
        # one of these two statements writes on any given boot (table absent
        # -> CREATE writes; table present -> DROP writes), so a read-only
        # file is caught either way.
        conn.execute("CREATE TABLE IF NOT EXISTS _write_probe (x INTEGER)")
        conn.execute("DROP TABLE IF EXISTS _write_probe")
    except sqlite3.OperationalError as exc:
        if "readonly" in str(exc).lower():
            raise RuntimeError(
                f"queue database not writable by uid {uid}: {cfg.db_path} "
                f"({exc}). Same fix: chown the data volume to the "
                "container's uid/gid."
            ) from exc
        raise
    finally:
        conn.close()


def create_app(cfg: Config | None = None) -> Flask:
    if cfg is None:
        cfg = load_config()

    _ensure_data_writable(cfg)

    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)
    app.config["CFG"] = cfg

    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    conn.close()

    beets_adapter.ensure_setup(cfg)

    from app import web

    app.register_blueprint(web.bp)
    app.jinja_env.globals["group_key"] = web.group_key
    app.jinja_env.globals["badge"] = web._badge
    app.jinja_env.globals["album_key_value"] = web.album_key_value
    app.jinja_env.globals["format_scan_time"] = web.format_scan_time
    return app
