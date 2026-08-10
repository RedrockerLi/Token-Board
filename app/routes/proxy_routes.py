"""Stable proxy management Blueprint façade."""

from app.routes.proxy.common import bp_proxy
from app.routes.proxy import accounts as _proxy_accounts
from app.routes.proxy import routing as _proxy_routing
from app.routes.proxy import billing as _proxy_billing
from app.routes.proxy import sync as _proxy_sync
from app.routes.proxy import performance as _proxy_performance

__all__ = ['bp_proxy']
