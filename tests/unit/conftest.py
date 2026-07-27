"""共用 fixtures。"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def project_root():
    return ROOT


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
    dimension: 2560
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
