"""Automatic local and downloaded-database schema upgrades.

The proxy process only runs V1 databases.  This package is the boundary that
may read V0 files: it creates checked shadow copies, upgrades same-major
minors, runs the V0->V1 transformer and atomically publishes the result.
"""

from .coordinator import (UpgradeResult, ensure_local_databases,
                          upgrade_downloaded_artifact, upgrade_shadow,
                          verify_current_database)

__all__ = ["UpgradeResult", "ensure_local_databases",
           "upgrade_downloaded_artifact", "upgrade_shadow",
           "verify_current_database"]
