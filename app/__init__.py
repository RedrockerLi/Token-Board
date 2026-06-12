"""DeepSeek Dashboard application package."""

from pathlib import Path

from flask import Flask

from app.data_loader import DataStore


def create_app():
    """Flask application factory.

    Explicitly sets template_folder and static_folder because Flask
    defaults to looking under the package directory (app/), but we
    keep them at the project root for clean separation.
    """
    root = Path(__file__).resolve().parent.parent  # project root

    app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
    )
    app.config["DATA_STORE"] = DataStore(root / "data")

    from app.routes import bp  # noqa: E402 (deferred import to avoid circularity)
    app.register_blueprint(bp)

    return app
