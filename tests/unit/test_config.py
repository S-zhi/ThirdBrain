"""config.py 单元测试。"""
import os

import pytest

import config as cfg


class TestLoadConfig:
    def test_load_default(self, isolated_config):
        c = cfg.load_config(isolated_config)
        assert c.embedder.type == "local"
        assert c.embedder.local.dimension == 384
        assert c.embedder.bailian.dimension == 2048  # 模板给的
        assert c.zvec.default_collection == "unit_test"
        assert "{name} {namespace} " in c.api_name.strip_pattern

    def test_singleton(self, isolated_config):
        a = cfg.get_config(isolated_config)
        b = cfg.get_config()
        assert a is b

    def test_reset(self, isolated_config):
        a = cfg.get_config(isolated_config)
        cfg.reset_config()
        b = cfg.get_config(isolated_config)
        assert a is not b
        # 但内容一致
        assert a.zvec.default_collection == b.zvec.default_collection

    def test_missing_file(self, tmp_path):
        cfg.reset_config()
        with pytest.raises(cfg.ConfigError, match="不存在"):
            cfg.load_config(tmp_path / "nope.yaml")

    def test_invalid_yaml_format(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("- this\n- is\n- a list\n", encoding="utf-8")
        cfg.reset_config()
        with pytest.raises(cfg.ConfigError, match="顶层必须是 dict"):
            cfg.load_config(p)


class TestDashScopeAPIKey:
    def test_missing_env_raises(self, monkeypatch):
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        with pytest.raises(cfg.ConfigError, match="DASHSCOPE_API_KEY"):
            cfg.get_dashscope_api_key()

    def test_set_env_returns(self, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-123")
        assert cfg.get_dashscope_api_key() == "sk-test-123"

    def test_constant_matches_env_name(self):
        assert cfg.ENV_DASHSCOPE_API_KEY == "DASHSCOPE_API_KEY"


class TestFrozenDataclass:
    def test_config_is_frozen(self, isolated_config):
        c = cfg.get_config(isolated_config)
        with pytest.raises(Exception):  # FrozenInstanceError
            c.embedder.type = "bailian"  # type: ignore
