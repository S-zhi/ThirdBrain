"""独立 document_sync.yaml 配置加载测试。"""

from pathlib import Path

import pytest

from src.doc_sync.adapters import AdapterFactory
from src.doc_sync.config import load_document_sync_config
from src.doc_sync.errors import SyncConfigError


def _write_config(path: Path, extra: str = "") -> Path:
    """写入最小可用的同步 YAML。"""
    path.write_text(
        f"""
schema_version: "1.0"
workspace_root: .
runtime:
  root_directory: ./runtime
sources:
  - id: source-a
    target_directory: ./documents
    adapter:
      type: test
      options: {{}}
{extra}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def test_config_paths_do_not_depend_on_cwd(tmp_path: Path, monkeypatch) -> None:
    """workspace_root 应相对于 YAML 而不是当前工作目录解析。"""
    config_path = _write_config(tmp_path / "document_sync.yaml")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    config = load_document_sync_config(config_path)
    assert config.workspace_root == tmp_path.resolve()
    assert config.runtime_root == (tmp_path / "runtime").resolve()
    assert config.source_target(config.sources[0]) == (tmp_path / "documents").resolve()


def test_config_rejects_duplicate_source_ids(tmp_path: Path) -> None:
    """重复 source id 必须在启动前失败。"""
    path = tmp_path / "document_sync.yaml"
    path.write_text(
        """
schema_version: "1.0"
workspace_root: .
sources:
  - id: duplicate
    target_directory: ./a
    adapter: {type: test, options: {}}
  - id: duplicate
    target_directory: ./b
    adapter: {type: test, options: {}}
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(SyncConfigError, match="id 必须唯一"):
        load_document_sync_config(path)


def test_config_rejects_unknown_fields(tmp_path: Path) -> None:
    """配置拼写错误不能被静默忽略。"""
    path = _write_config(tmp_path / "document_sync.yaml", "unknown_field: true")
    with pytest.raises(SyncConfigError, match="unknown_field"):
        load_document_sync_config(path)


def test_config_rejects_inline_secret(tmp_path: Path) -> None:
    """Adapter options 中的明文 Token 字段必须失败。"""
    path = tmp_path / "document_sync.yaml"
    path.write_text(
        """
schema_version: "1.0"
workspace_root: .
sources:
  - id: secret-source
    target_directory: ./documents
    adapter:
      type: test
      options:
        token: do-not-store-here
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(SyncConfigError, match="明文凭证"):
        load_document_sync_config(path)


def test_config_rejects_compound_inline_secret_key(tmp_path: Path) -> None:
    """access_token 等复合凭证字段也不能绕过明文检查。"""
    path = tmp_path / "document_sync.yaml"
    path.write_text(
        """
schema_version: "1.0"
workspace_root: .
sources:
  - id: secret-source
    target_directory: ./documents
    adapter:
      type: test
      options:
        access_token: do-not-store-here
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(SyncConfigError, match="access_token"):
        load_document_sync_config(path)


def test_config_allows_environment_variable_name(tmp_path: Path) -> None:
    """credential_env 只保存变量名，应允许加载。"""
    path = tmp_path / "document_sync.yaml"
    path.write_text(
        """
schema_version: "1.0"
workspace_root: .
sources:
  - id: env-source
    target_directory: ./documents
    adapter:
      type: test
      options:
        credential_env: DOC_TOKEN
""".strip(),
        encoding="utf-8",
    )
    config = load_document_sync_config(path)
    assert config.sources[0].adapter.options["credential_env"] == "DOC_TOKEN"


def test_repository_initial_config_loads_with_registered_adapter() -> None:
    """仓库提供的初始化 YAML 应能完成通用与 Adapter 专属校验。"""
    project_root = Path(__file__).resolve().parents[2]
    config = load_document_sync_config(project_root / "configs" / "document_sync.yaml")
    adapter = AdapterFactory.create(config.sources[0])
    assert config.workspace_root == project_root
    assert adapter.adapter_type == "hiascend"
