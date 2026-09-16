"""Stable dashboard Blueprint façade."""

from app.routes.dashboard.common import bp
from app.routes.dashboard import metadata as _dashboard_metadata
from app.routes.dashboard import summary as _dashboard_summary
from app.routes.dashboard import timeseries as _dashboard_timeseries

__all__ = ['bp']
