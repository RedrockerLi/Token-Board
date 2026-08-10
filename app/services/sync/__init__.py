"""Stable synchronization-service package façade."""

from app.services.sync import settings as _settings
from app.services.sync import webdav as _webdav
from app.services.sync import dashboard_sync as _dashboard_sync
from app.services.sync import snapshot as _snapshot
from app.services.sync import config_merge as _config_merge
from app.services.sync import config_sync as _config_sync

_modules = [_settings, _webdav, _dashboard_sync, _snapshot, _config_merge, _config_sync]
for _module in _modules:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__") and _name not in globals():
            globals()[_name] = _value

# Legacy functions resolve sibling helpers through module globals.  Populate
# those globals only after all modules have loaded, avoiding import cycles.
_shared = {name: value for name, value in globals().items()
           if not name.startswith("__")}
for _module in _modules:
    _module.__dict__.update(_shared)

__all__ = [name for name in globals() if not name.startswith("_")]
