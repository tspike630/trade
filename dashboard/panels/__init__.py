"""Dashboard panels.

Phase 1 registers four read-only sections. A later ops panel (manual trading,
not part of this package) should be appended in ``registry.py`` only.
"""

from dashboard.panels.registry import PANELS, PanelSpec

__all__ = ["PANELS", "PanelSpec"]
