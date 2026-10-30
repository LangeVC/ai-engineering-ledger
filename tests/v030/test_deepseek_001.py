from __future__ import annotations

from collectors.deepseek.collector import DeepSeekCollector


ROWS = [
    {
        "date": "2026-10-02",
        "model": "deepseek-chat",
        "requests": 2,
        "prompt_tokens": 100,
        "prompt_cache_hit_tokens": 60,
        "prompt_cache_miss_tokens": 40,
        "completion_tokens": 20,
        "reasoning_tokens": 0,
        "cost": "0.02",
    },
    {
        "date": "2026-10-01",
        "model": "deepseek-reasoner",
        "requests": 1,
        "prompt_tokens": 70,
        "prompt_cache_hit_tokens": 20,
        "prompt_cache_miss_tokens": 50,
        "completion_tokens": 30,
        "reasoning_tokens": 10,
        "cost": "0.03",
    },
]


def test_historical_import_is_deterministic_and_unchanged_rerun_is_noop() -> None:
    collector = DeepSeekCollector()
    checkpoint: dict[str, str] = {}

    first, report = collector.parse_export_data(ROWS, existing_keys=checkpoint)
    second, rerun = collector.parse_export_data(list(reversed(ROWS)), existing_keys=checkpoint)

    assert [(record.date, record.model) for record in first] == sorted(
        (record.date, record.model) for record in first
    )
    assert report.accepted == 2 and report.corrected == 0
    assert second == []
    assert rerun.accepted == 0 and rerun.existing == 2 and rerun.corrected == 0
    assert rerun.verify_disjoint_counts()


def test_changed_native_slice_is_a_correction_not_a_duplicate_charge() -> None:
    collector = DeepSeekCollector()
    checkpoint: dict[str, str] = {}
    collector.parse_export_data([ROWS[0]], existing_keys=checkpoint)
    corrected = {**ROWS[0], "requests": 3, "cost": "0.04"}

    records, report = collector.parse_export_data([corrected], existing_keys=checkpoint)

    assert len(records) == 1
    assert report.accepted == 0 and report.existing == 0 and report.corrected == 1
    assert report.verify_disjoint_counts()
