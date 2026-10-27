"""Kilo Adapter Package.

Parses Kilo Code session JSON exports and aggregate statistics into normalized,
audit-safe records with strict compliance to US-10 (US-10.a, US-10.b, US-10.c, US-10.d).
"""

from .adapter import (
    KiloAdapter,
    KiloParseError,
    KiloValidationError,
    KiloSessionRecord,
    KiloRequestRecord,
    KiloAggregateRecord,
    parse_kilo_export,
    parse_kilo_stats,
)

__all__ = [
    "KiloAdapter",
    "KiloParseError",
    "KiloValidationError",
    "KiloSessionRecord",
    "KiloRequestRecord",
    "KiloAggregateRecord",
    "parse_kilo_export",
    "parse_kilo_stats",
]
