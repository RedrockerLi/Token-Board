"""Stable DashboardDatabase façade."""

from app.db.dashboard.reader import DashboardReaderMixin
from app.db.dashboard.writer import DashboardWriterMixin


class DashboardDatabase(DashboardWriterMixin, DashboardReaderMixin):
    pass


__all__ = ["DashboardDatabase"]
