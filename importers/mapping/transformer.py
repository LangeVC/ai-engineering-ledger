"""Strict type transformations and date/timezone normalizations (stdlib only)."""

import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import ValueStatus


def normalize_timezone(tz_str: str) -> datetime.tzinfo:
    """Parse timezone string into datetime.tzinfo.
    Supports: UTC, Z, ±HH:MM, ±HHMM, or IANA name (e.g. Europe/Berlin).
    """
    tz_norm = tz_str.strip()
    if tz_norm in ("UTC", "Z", "+00:00", "-00:00"):
        return datetime.timezone.utc
    if tz_norm.startswith("+") or tz_norm.startswith("-"):
        # Fixed offset
        sign = 1 if tz_norm[0] == "+" else -1
        part = tz_norm[1:]
        if ":" in part:
            hh, mm = part.split(":", 1)
        else:
            hh, mm = part[:2], part[2:]
        delta = datetime.timedelta(hours=int(hh), minutes=int(mm))
        return datetime.timezone(sign * delta)
    try:
        return ZoneInfo(tz_norm)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise ValueError(f"Unknown timezone '{tz_str}': {err}")


def parse_date_to_window(
    date_str: str,
    tz_str: str,
    window_type: str = "daily",
    custom_label: Optional[str] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    """Parse date/datetime string into RFC 3339 start/end window in UTC.
    For YYYY-MM-DD daily windows, window covers [start_of_day, next_day_start).
    """
    tz = normalize_timezone(tz_str)
    raw = date_str.strip()

    # Case 1: Pure date YYYY-MM-DD
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        y, m, d = int(raw[0:4]), int(raw[5:7]), int(raw[8:10])
        dt_start = datetime.datetime(y, m, d, 0, 0, 0, tzinfo=tz)
        dt_end = dt_start + datetime.timedelta(days=1)
    else:
        # Case 2: Full ISO 8601 string
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        dt_start = dt
        dt_end = dt_start + datetime.timedelta(seconds=1)

    start_utc = dt_start.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    end_utc = dt_end.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    win: Dict[str, Any] = {
        "type": window_type,
        "starts_at": start_utc,
        "ends_at": end_utc,
    }
    if window_type == "custom":
        if not custom_label:
            raise ValueError("custom window requires explicit label")
        win["label"] = custom_label

    return start_utc, end_utc, win


def convert_value(
    status: ValueStatus,
    raw_val: Any,
    target_type: str,
    decimal_sep: str = ".",
    thousands_sep: Optional[str] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Convert a value according to target_type.
    Returns: (converted_value, error_message).
    If error_message is not None, conversion failed.
    """
    if status == ValueStatus.MISSING:
        return None, "missing"
    if status == ValueStatus.EXPLICIT_NULL:
        return None, None
    if status == ValueStatus.EMPTY_STRING:
        if target_type == "string":
            return "", None
        return None, "empty_string_for_non_string"

    tt = target_type.lower()

    if tt == "string":
        if isinstance(raw_val, (dict, list)):
            return None, f"unsupported nesting for string field: {type(raw_val).__name__}"
        return str(raw_val), None

    if tt == "integer":
        if isinstance(raw_val, bool):
            return None, "boolean cannot be converted to integer"
        if isinstance(raw_val, int):
            return raw_val, None
        s = str(raw_val).strip()
        if thousands_sep:
            s = s.replace(thousands_sep, "")
        if decimal_sep != "." and decimal_sep in s:
            s = s.replace(decimal_sep, ".")
        try:
            # Must not lose fraction silently
            dec = Decimal(s)
            if not dec.is_finite():
                return None, f"non-finite integer: {raw_val}"
            if dec != dec.to_integral_value():
                return None, f"fractional value for integer: {raw_val}"
            return int(dec), None
        except (InvalidOperation, ValueError):
            return None, f"invalid integer: {raw_val}"

    if tt in ("decimal", "decimal_string", "money"):
        if isinstance(raw_val, bool):
            return None, "boolean cannot be converted to decimal"
        if isinstance(raw_val, float):
            # Float input: convert via str to preserve exact digits where possible, but warn/check finiteness
            if not str(raw_val).replace(".", "").replace("-", "").isdigit() and not ("e" in str(raw_val).lower()):
                pass
        s = str(raw_val).strip()
        if thousands_sep:
            s = s.replace(thousands_sep, "")
        if decimal_sep != "." and decimal_sep in s:
            s = s.replace(decimal_sep, ".")
        try:
            dec = Decimal(s)
            if not dec.is_finite():
                return None, f"non-finite decimal: {raw_val}"
            if dec.is_zero():
                dec = Decimal(0)
            # Schema expects decimal_string as str matching ^-?(0|[1-9][0-9]*)(\.[0-9]+)?$
            val_str = str(dec)
            if "E" in val_str or "e" in val_str:
                # Format fixed representation
                val_str = format(dec, "f")
            return val_str, None
        except (InvalidOperation, ValueError):
            return None, f"invalid decimal: {raw_val}"

    if tt == "boolean":
        if isinstance(raw_val, bool):
            return raw_val, None
        s = str(raw_val).strip().lower()
        if s in ("true", "1", "yes", "y", "t"):
            return True, None
        if s in ("false", "0", "no", "n", "f"):
            return False, None
        return None, f"invalid boolean value: {raw_val}"

    if tt in ("iso8601_date", "iso8601_timestamp"):
        s = str(raw_val).strip()
        return s, None

    if tt == "json":
        return raw_val, None

    return None, f"unknown target_type: {target_type}"
