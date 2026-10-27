"""Parsing functions for CSV, JSON, and JSONL inputs (stdlib only)."""

import csv
import io
import json
from typing import Any, Dict, Iterator, List, Tuple, Union
from .models import CsvOptions, ValueStatus


def parse_csv_stream(
    stream: Union[io.TextIOBase, str],
    options: CsvOptions,
) -> Iterator[Tuple[int, Dict[str, Tuple[ValueStatus, Any]]]]:
    """Parse CSV text/stream into row dictionaries.
    Each cell value is mapped to a tuple: (ValueStatus, parsed_raw_value).
    Empty string is distinguished from missing/unset.
    """
    if isinstance(stream, str):
        stream = io.StringIO(stream)

    reader = csv.reader(
        stream,
        delimiter=options.delimiter,
        quotechar=options.quotechar,
    )

    headers: List[str] = []
    row_idx = 0
    data_row_idx = 0

    for raw_row in reader:
        row_idx += 1
        # Skip empty lines
        if not raw_row or (len(raw_row) == 1 and raw_row[0].strip() == ""):
            continue

        if options.has_header and not headers:
            headers = [h.strip() for h in raw_row]
            continue

        data_row_idx += 1
        row_dict: Dict[str, Tuple[ValueStatus, Any]] = {}
        for i, h in enumerate(headers):
            if i < len(raw_row):
                val = raw_row[i]
                if val == "":
                    # Explicit empty string in CSV
                    row_dict[h] = (ValueStatus.EMPTY_STRING, "")
                else:
                    row_dict[h] = (ValueStatus.PRESENT, val)
            else:
                # Column omitted in this line
                row_dict[h] = (ValueStatus.MISSING, None)

        yield data_row_idx, row_dict


def parse_json_stream(
    text: str,
) -> List[Tuple[int, Dict[str, Tuple[ValueStatus, Any]]]]:
    """Parse JSON array of records into row dictionaries."""
    data = json.loads(text)
    if isinstance(data, dict):
        # Allow single object or top-level wrapper with "records" / "data" / "items"
        if "records" in data and isinstance(data["records"], list):
            items = data["records"]
        elif "data" in data and isinstance(data["data"], list):
            items = data["data"]
        elif "items" in data and isinstance(data["items"], list):
            items = data["items"]
        else:
            items = [data]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError(f"Top-level JSON must be an array or object containing records, got {type(data).__name__}")

    out: List[Tuple[int, Dict[str, Tuple[ValueStatus, Any]]]] = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Record at index {idx} is not a JSON object: {type(item).__name__}")
        row_dict: Dict[str, Tuple[ValueStatus, Any]] = {}
        for k, v in item.items():
            if v is None:
                row_dict[k] = (ValueStatus.EXPLICIT_NULL, None)
            elif isinstance(v, str) and v == "":
                row_dict[k] = (ValueStatus.EMPTY_STRING, "")
            else:
                row_dict[k] = (ValueStatus.PRESENT, v)
        out.append((idx, row_dict))
    return out


def parse_jsonl_stream(
    stream: Union[io.TextIOBase, str],
) -> Iterator[Tuple[int, Dict[str, Tuple[ValueStatus, Any]]]]:
    """Parse JSONL lines into row dictionaries."""
    if isinstance(stream, str):
        stream = io.StringIO(stream)

    line_idx = 0
    data_idx = 0
    for line in stream:
        line_idx += 1
        line = line.strip()
        if not line:
            continue
        data_idx += 1
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError(f"JSONL line {line_idx} is not a JSON object: {type(obj).__name__}")
        row_dict: Dict[str, Tuple[ValueStatus, Any]] = {}
        for k, v in obj.items():
            if v is None:
                row_dict[k] = (ValueStatus.EXPLICIT_NULL, None)
            elif isinstance(v, str) and v == "":
                row_dict[k] = (ValueStatus.EMPTY_STRING, "")
            else:
                row_dict[k] = (ValueStatus.PRESENT, v)
        yield data_idx, row_dict


def parse_input_records(
    content: Union[str, bytes],
    format_type: str,
    csv_options: CsvOptions,
) -> List[Tuple[int, Dict[str, Tuple[ValueStatus, Any]]]]:
    """Parse content based on format (csv, json, jsonl)."""
    if isinstance(content, bytes):
        text = content.decode(csv_options.encoding)
    else:
        text = content

    fmt = format_type.lower()
    if fmt == "csv":
        return list(parse_csv_stream(text, csv_options))
    elif fmt == "json":
        return parse_json_stream(text)
    elif fmt in ("jsonl", "ndjson"):
        return list(parse_jsonl_stream(text))
    else:
        raise ValueError(f"Unsupported format '{format_type}'. Must be csv, json, or jsonl.")
