"""Deferred-release GPU resource lifetime registry (exercise package).

The public contract lives in :mod:`lifetime.registry`; every function there
is an unimplemented stub that raises :class:`NotImplementedError`.
"""

from . import registry
from .registry import (
    LEAK_AFTER_FRAMES,
    RELEASE_DELAY_FRAMES,
    SLOT_BITS,
    SLOT_MASK,
    create,
    cycle_detect,
    leak_report,
    release,
    retain,
    reuse_conflict,
    tick,
    validate_registry,
)

__all__ = [
    "LEAK_AFTER_FRAMES",
    "RELEASE_DELAY_FRAMES",
    "SLOT_BITS",
    "SLOT_MASK",
    "create",
    "cycle_detect",
    "leak_report",
    "registry",
    "release",
    "retain",
    "reuse_conflict",
    "tick",
    "validate_registry",
]
