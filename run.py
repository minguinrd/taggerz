"""Entrypoint for the taggerz web service."""

from app import create_app
from app.config import load_config
import waitress

cfg = load_config()
app = create_app(cfg)

if __name__ == "__main__":
    waitress.serve(app, host=cfg.host, port=cfg.port)
