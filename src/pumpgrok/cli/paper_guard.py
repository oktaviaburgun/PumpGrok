"""Refuse desk actions while the operator paper policy is engaged.

The refusal is a config error. It does not clear a halt, approve a ticket,
start a cycle, or report the desk operational.
"""

from __future__ import annotations

from pumpgrok.config.settings import ConfigError, Settings


def refuse_engaged(settings: Settings, action: str) -> None:
    """Raise ``ConfigError`` when the operator policy is engaged.

    Callers do not catch it. ``main`` maps ``ConfigError`` to exit 1. A
    disengaged policy returns without writing.
    """
    if not settings.paper_policy.engaged:
        return
    raise ConfigError(
        [f"paper policy engaged: refusing {action}; entries stay blocked and no desk cycle starts"]
    )
