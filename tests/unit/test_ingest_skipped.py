"""Tests for the skipped status logic in ingest script."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.script import ingest
from src.script.ingest import IngestRunRecord, _finalize_status, main, run_ingest


def test_finalize_status_skipped() -> None:
    """Test _finalize_status correctly determines the skipped status."""
    # Scenario 1: All documents are skipped
    record = IngestRunRecord(
        source="dummy",
        sub_directory="Sub",
        collection="dummy_coll",
        dry_run=False,
    )
    record.parsed_count = 5
    record.skipped_count = 5
    record.indexed_count = 0
    _finalize_status(record)
    assert record.status == "skipped"

    # Scenario 2: Some documents are indexed (not skipped)
    record = IngestRunRecord(
        source="dummy",
        sub_directory="Sub",
        collection="dummy_coll",
        dry_run=False,
    )
    record.parsed_count = 5
    record.skipped_count = 0
    record.indexed_count = 5
    _finalize_status(record)
    assert record.status == "succeeded"

    # Scenario 3: dry_run on duplicates
    record = IngestRunRecord(
        source="dummy",
        sub_directory="Sub",
        collection="dummy_coll",
        dry_run=True,
    )
    record.parsed_count = 5
    record.skipped_count = 5
    record.indexed_count = 0
    _finalize_status(record)
    assert record.status == "dry_run"

    # Scenario 4: parsed_count == 0 (failed)
    record = IngestRunRecord(
        source="dummy",
        sub_directory="Sub",
        collection="dummy_coll",
        dry_run=False,
    )
    record.parsed_count = 0
    record.skipped_count = 0
    record.indexed_count = 0
    _finalize_status(record)
    assert record.status == "failed"


def test_run_ingest_all_skipped(tmp_path: Path, monkeypatch) -> None:
    """Mock the ingest pipeline to return all records skipped and verify state is 'skipped'."""
    # Mocking discover_yaml_sources to return 1 path
    monkeypatch.setattr(ingest, "discover_yaml_sources", lambda src, sub: ["/fake/path.yaml"])
    # Mocking read_yaml_source to return dummy string
    monkeypatch.setattr(ingest, "read_yaml_source", lambda src: "dummy yaml content")

    # Mock parse_yaml_documents to return a single dummy record
    dummy_record = ingest.ApiDocumentRecord(
        chunk_id="test_chunk_id",
        name="test_name",
        namespace="test_namespace",
    )
    monkeypatch.setattr(ingest, "parse_yaml_documents", lambda content, src: [dummy_record])

    # Mock reject_duplicate_documents to reject everything (return empty accepted list, and dummy error)
    monkeypatch.setattr(
        ingest,
        "reject_duplicate_documents",
        lambda records: ([], [ingest.IngestError(source="test_chunk_id", stage="parse", message="duplicate")]),
    )

    result = run_ingest(
        source="/fake",
        record_directory=tmp_path,
        dry_run=False,
    )

    assert result.record.status == "skipped"
    assert result.record.parsed_count == 1
    assert result.record.skipped_count == 1
    assert result.record.indexed_count == 0


def test_main_skipped_exit_code(monkeypatch) -> None:
    """Verify that main() returns 0 when the record status is 'skipped'."""
    fake_result = MagicMock()
    fake_result.record.status = "skipped"
    fake_result.model_dump_json.return_value = "{}"

    # Mock run_ingest to return our fake_result
    monkeypatch.setattr(ingest, "run_ingest", lambda *args, **kwargs: fake_result)

    # Mock build_parser so we can invoke main without CLI arguments
    mock_parser = MagicMock()
    mock_parser.parse_args.return_value = MagicMock(
        source="dummy",
        sub_dir="Sub",
        collection=None,
        record_dir=Path("data/ingest_records"),
        dry_run=False,
        preview_limit=1,
    )
    monkeypatch.setattr(ingest, "build_parser", lambda: mock_parser)

    # Run main() and assert exit code is 0
    exit_code = main()
    assert exit_code == 0
