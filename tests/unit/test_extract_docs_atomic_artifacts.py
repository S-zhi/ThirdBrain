"""Tests for write_v21_debug_artifacts atomic writing behavior in extract_docs.py."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.script.extract_docs import write_v21_debug_artifacts
from src.script.markdown_yaml_v21 import PipelineResult


@pytest.fixture
def dummy_pipeline_result() -> PipelineResult:
    """Provides a dummy PipelineResult with required structure."""
    result = MagicMock(spec=PipelineResult)
    result.document = {
        "source": {
            "source_markdown": "source markdown text",
            "preprocess_markdown": "preprocessed markdown text",
        }
    }
    result.evidence = {"test_evidence": "value"}
    result.image_prompts = [{"image_id": 1}]
    result.image_responses = [{"response": "ok"}]
    result.ai_prompt = "ai prompt text"
    result.ai_response = "ai response text"
    return result


def test_write_v21_debug_artifacts_success(
    tmp_path: Path, dummy_pipeline_result: PipelineResult
) -> None:
    """Verifies successful atomic writing of exactly 8 expected files."""
    root = tmp_path / "debug_out"
    source_path = Path("test_doc.md")
    yaml_text = "yaml text content"

    target = write_v21_debug_artifacts(
        root=root,
        source_path=source_path,
        result=dummy_pipeline_result,
        yaml_text=yaml_text,
    )

    assert target.exists()
    assert target.is_dir()

    # The temporary target directory must not exist.
    tmp_target = target.with_suffix(target.suffix + ".tmp")
    assert not tmp_target.exists()

    expected_files = {
        "01_source.md",
        "02_preprocessed.md",
        "03_slot_evidence.yaml",
        "04_image_prompts.yaml",
        "05_image_responses.yaml",
        "06_ai_prompt.txt",
        "07_ai_response.txt",
        "08_result.yaml",
    }
    files = {f.name for f in target.iterdir()}
    assert files == expected_files

    # Assert content of one of the files
    assert (target / "01_source.md").read_text(encoding="utf-8") == "source markdown text"
    assert (target / "08_result.yaml").read_text(encoding="utf-8") == "yaml text content"


def test_write_v21_debug_artifacts_failure_and_cleanup(
    tmp_path: Path, dummy_pipeline_result: PipelineResult
) -> None:
    """Verifies that if writing a file fails, the tmp_target is deleted, target is not created."""
    root = tmp_path / "debug_out"
    source_path = Path("test_doc.md")
    yaml_text = "yaml text content"

    # We mock write_text of Path. We want it to raise an error when writing the second file.
    original_write_text = Path.write_text
    write_count = 0

    def mock_write_text(self: Path, data: str, *args, **kwargs):
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("Mock disk failure")
        return original_write_text(self, data, *args, **kwargs)

    with (
        patch.object(Path, "write_text", mock_write_text),
        pytest.raises(OSError, match="Mock disk failure"),
    ):
        write_v21_debug_artifacts(
            root=root,
            source_path=source_path,
            result=dummy_pipeline_result,
            yaml_text=yaml_text,
        )

    suffix = "5b80a5fe" # first 8 chars of hashlib.sha256("test_doc.md".encode("utf-8")).hexdigest()
    target = root / f"test_doc-{suffix}"
    tmp_target = target.with_suffix(target.suffix + ".tmp")

    # Assert target was not created, and tmp_target is cleaned up
    assert not target.exists()
    assert not tmp_target.exists()


def test_write_v21_debug_artifacts_overwrite_existing(
    tmp_path: Path, dummy_pipeline_result: PipelineResult
) -> None:
    """Verifies that an existing target is cleanly replaced."""
    root = tmp_path / "debug_out"
    source_path = Path("test_doc.md")
    yaml_text = "yaml text content"

    # Pre-create the target directory with a dummy file
    suffix = "5b80a5fe"
    target = root / f"test_doc-{suffix}"
    target.mkdir(parents=True, exist_ok=True)
    dummy_file = target / "dummy.txt"
    dummy_file.write_text("dummy", encoding="utf-8")

    # Execute and verify successful replacement
    target = write_v21_debug_artifacts(
        root=root,
        source_path=source_path,
        result=dummy_pipeline_result,
        yaml_text=yaml_text,
    )

    assert target.exists()
    assert not (target / "dummy.txt").exists()
    assert (target / "01_source.md").exists()
