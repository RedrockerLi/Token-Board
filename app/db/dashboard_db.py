"""Stable DashboardDatabase façade."""

from app.db.dashboard.reader import DashboardReaderMixin
from app.db.dashboard.writer import DashboardWriterMixin
from app.db.dashboard.reconcile import reconcile_accounts


class DashboardDatabase(DashboardWriterMixin, DashboardReaderMixin):
    pass


__all__ = ["DashboardDatabase", "reconcile_accounts"]
