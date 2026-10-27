"""File-based import pipeline for AI Engineering Ledger (US-05).

Provides:
- FileImporter: imports tabular/structured export files (CSV, JSON, JSONL).
- Inspection and preview facilities (delimiter sniffing, decimal format detection, timezone resolution).
- DeepSeek and generic mapping adapter integrations.
- Disjoint accounting: total_read == accepted + existing + quarantined + corrected.
- Detailed quarantine reporting: line reference, field, cause (US-05.d).
"""

from .pipeline import (
    FileImporter,
    FileImportReport,
    FilePreviewResult,
    FormatOptions,
    QuarantineErrorDetail,
    parse_error_field_and_cause,
)
from .detector import detect_format_options, inspect_tabular_file

__all__ = [
    "FileImporter",
    "FileImportReport",
    "FilePreviewResult",
    "FormatOptions",
    "QuarantineErrorDetail",
    "detect_format_options",
    "inspect_tabular_file",
    "parse_error_field_and_cause",
]
