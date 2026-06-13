"""AI API Usage Dashboard application package."""

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

    flask_app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
    )
    flask_app.config["DATA_STORE"] = DataStore(root / "data")

    # Ensure adapters are imported so they self-register.
    # Use __import__ to avoid binding 'app' and shadowing the local
    # flask_app variable (import app.adapters.deepseek would override
    # any local named 'app' with the module reference).
    __import__("app.adapters.deepseek")
    __import__("app.adapters.mimo")

    from app.routes import bp  # noqa: E402
    flask_app.register_blueprint(bp)

    return flask_app
