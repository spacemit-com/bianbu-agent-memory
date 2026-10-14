"""Testing helpers.

This module deliberately does not expose identity-forging helpers. Tests must launch
the daemon with its explicit ``--development-identity`` switch or install a runtime
registration that matches the real test process.
"""

from __future__ import annotations

import os


def development_confirm_token() -> str:
    """Return the explicitly configured test token without supplying a default."""

    token = os.environ.get("BIANBU_MEMORY_DEV_CONFIRM_TOKEN")
    if not token:
        raise RuntimeError("BIANBU_MEMORY_DEV_CONFIRM_TOKEN is not configured")
    return token
