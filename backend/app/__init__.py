from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from .routes import api
from .storage import initialize


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=os.environ.get(
            "RECONCILIATION_DATABASE",
            str(Path(__file__).resolve().parents[1] / "instance" / "reconciliation.sqlite3"),
        ),
        COMPANIES_CONFIG=os.environ.get("COMPANIES_CONFIG", ""),
        DEMO_MODE=os.environ.get("RECONCILIATION_DEMO_MODE", "true").lower() == "true",
    )
    if test_config:
        app.config.update(test_config)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    initialize(app.config["DATABASE"])
    app.register_blueprint(api, url_prefix="/api")
    return app
