"""Format, delimiter, decimal separator, and timezone inspection heuristics for files.
Standard library only.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def detect_delimiter(sample_text: str, default: str = ",") -> str:
    """Detect CSV delimiter from sample text.
    Prefers csv.Sniffer if valid, otherwise falls back to common candidates: ',', ';', '\t', '|'.
    """
    if not sample_text.strip():
        return default

    # First attempt using standard Sniffer on lines
    first_few_lines = "\n".join(sample_text.strip().splitlines()[:5])
    try:
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(first_few_lines, delimiters=[",", ";", "\t", "|"])
        return dialect.delimiter
    except Exception:
        pass

    # Frequency counting heuristic
    candidates = [",", ";", "\t", "|"]
    counts = {c: 0 for c in candidates}
    for line in first_few_lines.splitlines():
        for c in candidates:
            counts[c] += line.count(c)

    best_char = max(candidates, key=lambda c: counts[c])
    return best_char if counts[best_char] > 0 else default


def detect_decimal_separator(sample_text: str, delimiter: str = ",") -> str:
    """Detect decimal separator ('.' vs ',') in sample text.
    If the delimiter is ',', numbers with decimals typically use '.' or quoted ','.
    If delimiter is ';', numbers frequently use ','.
    Looks at numeric patterns like 12.34 or 12,34.
    """
    # Look at non-delimiter tokens that resemble numbers
    dot_count = len(re.findall(r"\b\d+\.\d+\b", sample_text))
    # If delimiter is ',', comma between digits might be a column boundary unless quoted or isolated
    if delimiter == ",":
        comma_count = len(re.findall(r'"\d+,\d+"', sample_text))
    else:
        comma_count = len(re.findall(r"\b\d+,\d+\b", sample_text))

    if comma_count > dot_count:
        return ","
    return "."


def resolve_timezone_name(tz_input: Optional[str]) -> str:
    """Validate or resolve timezone identifier to valid IANA or fixed offset.
    Defaults to 'UTC'.
    """
    if not tz_input or not tz_input.strip():
        return "UTC"

    tz_str = tz_input.strip()
    if tz_str.upper() in ("UTC", "Z", "+00:00", "-00:00"):
        return "UTC"

    # Test fixed offset (+HH:MM, -HH:MM)
    if (tz_str.startswith("+") or tz_str.startswith("-")) and (":" in tz_str or len(tz_str) in (3, 5)):
        return tz_str

    try:
        ZoneInfo(tz_str)
        return tz_str
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise ValueError(f"Invalid timezone '{tz_input}': {err}")


def inspect_tabular_file(
    content: str,
    format_type: str = "csv",
    delimiter: Optional[str] = None,
    decimal_separator: Optional[str] = None,
    timezone_str: Optional[str] = None,
) -> Dict[str, Any]:
    """Inspects text content and infers or verifies format options."""
    detected_tz = resolve_timezone_name(timezone_str)

    if format_type == "csv":
        actual_delim = delimiter or detect_delimiter(content)
        actual_dec = decimal_separator or detect_decimal_separator(content, delimiter=actual_delim)
        return {
            "format": "csv",
            "delimiter": actual_delim,
            "decimal_separator": actual_dec,
            "timezone": detected_tz,
        }
    elif format_type in ("json", "jsonl"):
        actual_dec = decimal_separator or "."
        return {
            "format": format_type,
            "delimiter": None,
            "decimal_separator": actual_dec,
            "timezone": detected_tz,
        }
    else:
        raise ValueError(f"Unsupported format: {format_type}")


def detect_format_options(
    filename_or_content: str,
    explicit_format: Optional[str] = None,
    explicit_delimiter: Optional[str] = None,
    explicit_decimal_sep: Optional[str] = None,
    explicit_timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """Helper to detect or apply explicit format options."""
    fmt = explicit_format
    if not fmt:
        lower = filename_or_content.lower()
        if lower.endswith(".jsonl"):
            fmt = "jsonl"
        elif lower.endswith(".json"):
            fmt = "json"
        elif lower.endswith(".csv"):
            fmt = "csv"
        else:
            # Check content
            trimmed = filename_or_content.strip()
            if trimmed.startswith("[") or (trimmed.startswith("{") and "\n" not in trimmed):
                fmt = "json"
            elif trimmed.startswith("{") and "\n" in trimmed:
                fmt = "jsonl"
            else:
                fmt = "csv"

    return inspect_tabular_file(
        content=filename_or_content,
        format_type=fmt,
        delimiter=explicit_delimiter,
        decimal_separator=explicit_decimal_sep,
        timezone_str=explicit_timezone,
    )
