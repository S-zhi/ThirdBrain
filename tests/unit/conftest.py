"""共用 fixtures。"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def project_root():
    return ROOT


@pytest.fixture
def use_tmp_config(isolated_config):
    """Alias for isolated_config to support existing test cases."""
    return isolated_config


@pytest.fixture
def tmp_config(isolated_config):
    """Alias for isolated_config to support existing test cases."""
    return isolated_config


@pytest.fixture
def make_zvec_doc():
    """造一条 schema 兼容的 zvec.Doc，支持 existing test cases 中的多字段覆盖。"""
    import zvec
    import config as cfg
    from src.dao.emb.schema import FIELD_DENSE_EMBEDDING, FIELD_SPARSE_EMBEDDING

    def _make(doc_id: str = "ns.op.test", **overrides) -> zvec.Doc:
        c = cfg.get_config()
        if c.embedder.type == "bailian":
            dim = c.embedder.bailian.dimension
        else:
            dim = c.embedder.local.dimension

        fields = {
            "namespace": "com.test.v1",
            "api_id": f"com.test.v1.{doc_id}",
            "name": overrides.get("name", "Test"),  # Default is 'Test' to pass text assertions
            "api_name": f"Test {doc_id}",
            "version": "v1",
            "kind": "function",
            "language": "python",
            "version_support": ["linux"],
            "deprecated": False,
            "ingested_at": 1720000000,
            "description": f"desc {doc_id}",
            "signature": f"{doc_id}()",
            "parameters_md": "",
            "returns_json": "null",
            "examples": [],
            "source_markdown": f"# {doc_id}",
            "deprecation_note": "",
        }
        fields.update(overrides)
        if "doc_id" in overrides:
            doc_id = overrides["doc_id"]
        return zvec.Doc(
            id=doc_id,
            fields=fields,
            vectors={
                FIELD_DENSE_EMBEDDING: [0.1] * dim,
                FIELD_SPARSE_EMBEDDING: {1: 0.5, 2: 0.3},
            },
        )
    return _make


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """每个测试用临时 config.yaml + 临时 collection 路径，避免污染真实配置。

    关键：fixture 末尾预加载到 singleton，让 ``get_config()`` 直接拿到测试值，
    不用每个测试调 ``get_config(yaml_path)``。
    """
    import config as cfg_mod  # 局部 import，monkeypatch.setenv 先于它

    yaml_path = tmp_path / "config.yaml"
    yaml_template = """\
embedder:
  type: local
  bailian:
    model: qwen3.7-text-embedding
    dimension: 2048
    max_retries: 3
    timeout: 30
  local:
    dense_model: sentence-transformers/all-MiniLM-L6-v2
    dimension: 384
    bm25_language: zh

zvec:
  collection_path: __COLL_PATH__
  default_collection: unit_test

api_name:
  strip_pattern: "{name} {namespace} "
"""
    yaml_path.write_text(
        yaml_template.replace("__COLL_PATH__", str(tmp_path / "zvec_data")),
        encoding="utf-8",
    )
    # DASHSCOPE_API_KEY 即使不走 Bailian 也可能被懒加载检查
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-fake-key")
    monkeypatch.setenv("PYTHONHASHSEED", "0")  # 保证 hash token_to_id 跨进程稳定

    # Reset + 预加载到 singleton
    cfg_mod.reset_config()
    cfg_mod.get_config(yaml_path)  # get_config 会 set singleton，load_config 不会
    yield yaml_path
    cfg_mod.reset_config()


@pytest.fixture
def tmp_config(isolated_config):
    return isolated_config


@pytest.fixture
def use_tmp_config(isolated_config):
    return isolated_config


@pytest.fixture
def make_zvec_doc(isolated_config):
    import zvec
    from src.dao.emb.schema import FIELD_DENSE_EMBEDDING, FIELD_SPARSE_EMBEDDING
    import config as cfg

    def _make(doc_id: str = "ns.op.test", **overrides) -> zvec.Doc:
        c = cfg.get_config()
        if c.embedder.type == "bailian":
            dim = c.embedder.bailian.dimension
        else:
            dim = c.embedder.local.dimension

        fields = {
            "namespace": "com.test.v1",
            "api_id": f"com.test.v1.{doc_id}",
            "name": "Test",
            "api_name": f"Test {doc_id}",
            "version": "v1",
            "kind": "function",
            "language": "python",
            "version_support": ["linux"],
            "deprecated": False,
            "ingested_at": 1720000000,
            "description": f"desc {doc_id}",
            "signature": f"{doc_id}()",
            "parameters_md": "",
            "returns_json": "null",
            "examples": [],
            "source_markdown": f"# {doc_id}",
            "deprecation_note": "",
        }
        fields.update(overrides)
        return zvec.Doc(
            id=doc_id,
            fields=fields,
            vectors={
                FIELD_DENSE_EMBEDDING: [0.1] * dim,
                FIELD_SPARSE_EMBEDDING: {1: 0.5, 2: 0.3},
            },
        )
    return _make
