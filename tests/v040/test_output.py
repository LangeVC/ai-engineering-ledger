from export.living.output import build_changelog, build_living_export

def test_v04_output_wraps_v03_without_mutating_it():
    study={"schema_version":"0.3","content_hash":"a"*64,"active_filter":{"start":"2026-04-26T00:00:00Z","end_exclusive":"2026-11-01T00:00:00Z"},"scope":{"dimensions":{}}}
    snapshot={"status":"finalized","snapshot_id":"snap_a","content_hash":"b"*64,"transformation_version":"0.4.0"}
    health=[{"source_alias":"deepseek","state":"healthy"}]
    first=build_living_export(study_v03=study,snapshot=snapshot,source_health=health)
    second=build_living_export(study_v03=study,snapshot=snapshot,source_health=list(reversed(health)))
    assert first==second
    assert first["study"]==study
    assert first["compatibility"]["source_contract"]=="ael.study.data-export@0.3"

def test_changelog_is_data_only():
    result=build_changelog({"changes":[{"domain":"claims","change_type":"added","reason_code":"record_added"}]})
    assert result==[{"domain":"claims","change_type":"added","reason_code":"record_added","count":1}]

