"""Application configuration and constants."""
import logging
from pathlib import Path

# Suppress Flask/Werkzeug startup noise
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)
cli = logging.getLogger("flask.app")
cli.setLevel(logging.ERROR)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
