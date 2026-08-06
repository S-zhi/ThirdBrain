"""All-duplicate / idempotent ingest must not be marked failed (#10)."""

from src.script.ingest import IngestRunRecord, _finalize_status


def test_all_skipped_is_skipped_not_failed():
    record = IngestRunRecord(
        source="fixture",
        sub_directory="Sub",
        collection="col",
        dry_run=False,
        parsed_count=3,
        skipped_count=3,
        indexed_count=0,
        errors=[],
    )
    _finalize_status(record)
    assert record.status == "skipped"
    assert record.status != "failed"


def test_zero_parsed_is_failed():
    record = IngestRunRecord(
        source="fixture",
        sub_directory="Sub",
        collection="col",
        dry_run=False,
        parsed_count=0,
        skipped_count=0,
        indexed_count=0,
        errors=[],
    )
    _finalize_status(record)
    assert record.status == "failed"


def test_indexed_is_succeeded():
    record = IngestRunRecord(
        source="fixture",
        sub_directory="Sub",
        collection="col",
        dry_run=False,
        parsed_count=2,
        skipped_count=0,
        indexed_count=2,
        errors=[],
    )
    _finalize_status(record)
    assert record.status == "succeeded"
