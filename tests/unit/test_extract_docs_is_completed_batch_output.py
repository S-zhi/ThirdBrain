"""Tests for is_completed_batch_output in extract_docs.py."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.script.extract_docs import ExtractionError, is_completed_batch_output


def test_is_completed_batch_output_file_not_exist() -> None:
    """If output file does not exist, should return False."""
    mock_doc = MagicMock(spec=Path)
    mock_out = MagicMock(spec=Path)
    mock_out.is_file.return_value = False
    assert is_completed_batch_output(mock_doc, mock_out) is False


def test_is_completed_batch_output_read_text_oserror() -> None:
    """If reading output file raises OSError, should return False."""
    mock_doc = MagicMock(spec=Path)
    mock_out = MagicMock(spec=Path)
    mock_out.is_file.return_value = True
    mock_out.read_text.side_effect = OSError("Disk read error")
    assert is_completed_batch_output(mock_doc, mock_out) is False


def test_is_completed_batch_output_read_text_unicodedecodeerror() -> None:
    """If reading output file raises UnicodeDecodeError, should return False."""
    mock_doc = MagicMock(spec=Path)
    mock_out = MagicMock(spec=Path)
    mock_out.is_file.return_value = True
    mock_out.read_text.side_effect = UnicodeDecodeError("utf-8", b"", 0, 1, "invalid")
    assert is_completed_batch_output(mock_doc, mock_out) is False


def test_is_completed_batch_output_invalid_yaml() -> None:
    """If safe_load raises YAMLError, should return False."""
    mock_doc = MagicMock(spec=Path)
    mock_out = MagicMock(spec=Path)
    mock_out.is_file.return_value = True
    mock_out.read_text.return_value = "unbalanced: ["
    # yaml.safe_load will raise yaml.YAMLError
    assert is_completed_batch_output(mock_doc, mock_out) is False


@patch("src.script.extract_docs.validate_v21")
def test_is_completed_batch_output_v21_validation_error(mock_validate: MagicMock) -> None:
    """If validation_v21 raises ExtractionError or other errors, should return False."""
    mock_doc = MagicMock(spec=Path)
    mock_out = MagicMock(spec=Path)
    mock_out.is_file.return_value = True
    mock_out.read_text.return_value = "schema_version: '2.1'\n"
    mock_validate.side_effect = ExtractionError("Schema 2.1 validation failed")
    assert is_completed_batch_output(mock_doc, mock_out) is False


def test_is_completed_batch_output_v21_key_error() -> None:
    """If schema is 2.1 but 'source' key is missing, should return False."""
    mock_doc = MagicMock(spec=Path)
    mock_out = MagicMock(spec=Path)
    mock_out.is_file.return_value = True
    mock_out.read_text.return_value = "schema_version: '2.1'\n"
    with patch("src.script.extract_docs.validate_v21") as mock_validate:
        mock_validate.return_value = None
        # This will raise KeyError on result["source"]
        assert is_completed_batch_output(mock_doc, mock_out) is False


def test_is_completed_batch_output_not_v21_key_error() -> None:
    """If schema is 2.0 but 'source' key is missing, should return False."""
    mock_doc = MagicMock(spec=Path)
    mock_out = MagicMock(spec=Path)
    mock_out.is_file.return_value = True
    mock_out.read_text.return_value = "schema_version: '2.0'\n"
    with (
        patch("src.script.extract_docs.validate_serialized_yaml") as mock_val_serial,
        patch("src.script.extract_docs.validate_ready_for_ingest") as mock_val_ready,
    ):
        mock_val_serial.return_value = None
        mock_val_ready.return_value = None
        # This will raise KeyError on result["source"]
        assert is_completed_batch_output(mock_doc, mock_out) is False


def test_is_completed_batch_output_not_v21_type_error() -> None:
    """If schema_load returns an invalid non-dict type (e.g. list), should catch TypeError."""
    mock_doc = MagicMock(spec=Path)
    mock_out = MagicMock(spec=Path)
    mock_out.is_file.return_value = True
    mock_out.read_text.return_value = "- item1\n- item2"
    with patch("src.script.extract_docs.validate_serialized_yaml") as mock_val_serial:
        # If validate_serialized_yaml doesn't raise, then validate_ready_for_ingest(result)
        # or result["source"] might raise TypeError.
        mock_val_serial.return_value = None
        assert is_completed_batch_output(mock_doc, mock_out) is False


@patch("src.script.extract_docs.validate_serialized_yaml")
def test_is_completed_batch_output_not_v21_validation_error(
    mock_validate_serial: MagicMock,
) -> None:
    """If validate_serialized_yaml raises ExtractionError, should return False."""
    mock_doc = MagicMock(spec=Path)
    mock_out = MagicMock(spec=Path)
    mock_out.is_file.return_value = True
    mock_out.read_text.return_value = "schema_version: '2.0'\nsource: {}\n"
    mock_validate_serial.side_effect = ExtractionError("Failed serial validation")
    assert is_completed_batch_output(mock_doc, mock_out) is False
