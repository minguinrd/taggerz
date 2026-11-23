"""Application factory."""

import secrets

from flask import Flask

from app import beets_adapter, db
from app.config import Config, load_config


def create_app(cfg: Config | None = None) -> Flask:
    if cfg is None:
        cfg = load_config()

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
    return app
