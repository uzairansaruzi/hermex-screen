"""hermex-screen: its routes live in dashboard/plugin_api.py, which the web server mounts."""

from __future__ import annotations


def register(ctx) -> None:
    """Plugin entry point. There are no hooks or tools: this plugin only serves routes."""
