"""
Startup database guard.

Originally intended to verify that all DB usage goes through service.db.
This lightweight stub keeps the hook optional and non-blocking.
"""

import logging

logger = logging.getLogger("service.hooks.startup_check")


def check_database_usage() -> None:
    """
    Placeholder for DB usage checks.
    Kept non-fatal to avoid blocking bot startup if the optional hook is absent.
    """
    logger.debug("Startup DB check stub executed (no-op).")
