"""Data models and enums for generic mapping importer."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Union


class ValueStatus(str, Enum):
    """Disambiguation of value presence according to PRD R06."""
    PRESENT = "present"
    EXPLICIT_NULL = "explicit_null"
    EMPTY_STRING = "empty_string"
    MISSING = "missing"


class ImportMode(str, Enum):
    FULL = "full"
    STRICT = "strict"
    PREVIEW = "preview"


@dataclass
class ColumnMappingRule:
    """Mapping rule for a single source column or JSON property."""
    target_metric: Optional[str] = None
    target_type: str = "string"  # integer, decimal, string, boolean, iso8601_date, iso8601_timestamp, json
    unit: Optional[str] = None
    aggregation: str = "interval_total"  # absolute, delta, interval_total, gauge
    required: bool = False
    default: Optional[Any] = None
    description: str = ""
    target_dimension: Optional[str] = None  # if target_metric is None, can be mapped into dimensions.<name>
    target_field: Optional[str] = None  # event field (e.g. period_start, period_end, observed_at, model)


@dataclass
class CsvOptions:
    delimiter: str = ","
    quotechar: str = '"'
    decimal_separator: str = "."
    thousands_separator: Optional[str] = None
    has_header: bool = True
    timezone: str = "UTC"  # explicit timezone string (e.g. UTC, +02:00, Europe/Berlin)
    encoding: str = "utf-8"


@dataclass
class MappingConfig:
    """Declared mapping configuration. Guessed columns are forbidden."""
    source: str
    account_alias: str
    format: str  # csv, json, jsonl
    mapping_version: str = "0.1.0"
    collector: str = "importers.mapping"
    collector_version: str = "0.1.0"
    column_mapping: Dict[str, Union[Dict[str, Any], ColumnMappingRule]] = field(default_factory=dict)
    invariants: List[str] = field(default_factory=list)
    csv_options: CsvOptions = field(default_factory=CsvOptions)
    default_provenance_class: str = "P0"
    default_provenance_method: str = "import"
    default_confidence: float = 1.00
    default_confidence_reason: str = "Declared mapping import from raw export"
    window_type: str = "daily"  # 5h, daily, weekly, monthly, subscription, custom
    window_custom_label: Optional[str] = None


@dataclass
class QuarantineRecord:
    row_number: int
    source_record: Dict[str, Any]
    reasons: List[str]
    raw_error: Optional[str] = None


@dataclass
class RecordOutcome:
    row_number: int
    status: str  # accepted, existing, quarantined, corrected
    event: Optional[Dict[str, Any]] = None
    measurements: List[Dict[str, Any]] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)


@dataclass
class ImportReport:
    """Disjoint summary report satisfying US-05.c:
    total_records = accepted + existing + quarantined + corrected
    """
    total_records: int = 0
    accepted: int = 0
    existing: int = 0
    quarantined: int = 0
    corrected: int = 0
    run_status: str = "complete"  # complete, partial, failed
    quarantine_records: List[QuarantineRecord] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    measurements: List[Dict[str, Any]] = field(default_factory=list)
    preview_records: List[Dict[str, Any]] = field(default_factory=list)

    def verify_disjoint_sum(self) -> bool:
        return self.total_records == (self.accepted + self.existing + self.quarantined + self.corrected)
