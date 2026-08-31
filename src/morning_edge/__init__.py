"""Codex Screener decision-support primitives and local research operations.

Provider adapters are read-only. The command-line morning run remains an offline
planning and fixture workflow until live collection is deliberately integrated.
"""

from .config import Settings

__all__ = ["Settings"]
__version__ = "0.1.0"
