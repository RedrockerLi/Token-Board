"""Automatic local and downloaded-database schema upgrades.

Runtime services only run V2 databases. This package is the compatibility
boundary for V0/V1 files: it creates checked shadow copies, performs the
compound V1→V2 migration, and atomically publishes the result.
"""

from .coordinator import (UpgradeResult, ensure_local_databases,
                          upgrade_downloaded_artifact, upgrade_shadow,
                          verify_current_database)
from .transition_api import TransitionContext

__all__ = ["UpgradeResult", "ensure_local_databases",
           "upgrade_downloaded_artifact", "upgrade_shadow",
           "verify_current_database", "TransitionContext"]
