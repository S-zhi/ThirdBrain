"""异常体系：ConfigError 跨模块同一类，继承关系。"""
import pytest

import config as cfg
from src.dao.emb.exceptions import (
    CollectionNotFoundError,
    ConfigError,
    DocBuildError,
    EmbedderError,
    EmbError,
    NotSupportedError,
    SchemaMismatchError,
    SearchError,
)


class TestConfigErrorIdentity:
    """ConfigError 在 config.py 和 src.dao.emb.exceptions 必须指向同一个类。"""

    def test_same_class(self):
        assert cfg.ConfigError is ConfigError
        # import 路径不影响身份
        assert issubclass(ConfigError, cfg.ConfigError)

    def test_except_catches_across_modules(self):
        # 在 config.py 抛的异常，emb 里能 catch
        try:
            raise cfg.ConfigError("test")
        except ConfigError as e:
            assert str(e) == "test"
        # 反向
        try:
            raise ConfigError("test2")
        except cfg.ConfigError:
            pass

    def test_exported_from_emb_package(self):
        from src.dao.emb import ConfigError as Exported
        assert Exported is ConfigError


class TestExceptionHierarchy:
    def test_emb_error_is_base(self):
        assert issubclass(EmbedderError, EmbError)
        assert issubclass(SchemaMismatchError, EmbError)
        assert issubclass(CollectionNotFoundError, EmbError)
        assert issubclass(DocBuildError, EmbError)
        assert issubclass(SearchError, EmbError)
        assert issubclass(NotSupportedError, EmbError)

    def test_catch_all_via_emb_error(self):
        for exc_cls in (EmbedderError, SchemaMismatchError,
                        CollectionNotFoundError, DocBuildError,
                        SearchError, NotSupportedError):
            try:
                raise exc_cls("test")
            except EmbError as e:
                assert str(e) == "test"

    def test_config_error_is_not_emb_error(self):
        # ConfigError 是顶层 Exception，不继承 EmbError
        # 因为配置异常可能在任何地方抛
        assert not issubclass(ConfigError, EmbError)
        assert issubclass(ConfigError, Exception)
