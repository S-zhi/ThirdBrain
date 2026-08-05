"""Tests for CLI graph export atomic write functionality."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.cli.graph import _write_atomic_text


def test_write_atomic_text_success(tmp_path: Path) -> None:
    """Test that _write_atomic_text writes successfully to the file."""
    output_file = tmp_path / "sub" / "output.txt"
    content = "hello world"

    # Write for the first time
    _write_atomic_text(output_file, content)
    assert output_file.exists()
    assert output_file.read_text(encoding="utf-8") == content

    # Overwrite
    new_content = "new text content"
    _write_atomic_text(output_file, new_content)
    assert output_file.read_text(encoding="utf-8") == new_content


def test_write_atomic_text_failure_cleans_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that when an exception occurs during replace, the tmp file is cleaned up."""
    output_file = tmp_path / "failed_output.txt"
    content = "should not be written"

    def mock_replace(src: Path | str, dst: Path | str) -> None:
        raise OSError("Simulated disk error or permission failure")

    monkeypatch.setattr(os, "replace", mock_replace)

    with pytest.raises(OSError, match="Simulated disk error"):
        _write_atomic_text(output_file, content)

    # The main output file should not have been created
    assert not output_file.exists()

    # The tmp file should have been cleaned up
    tmp_files = list(tmp_path.glob(".*.tmp"))
    assert len(tmp_files) == 0
