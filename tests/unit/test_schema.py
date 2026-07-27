"""schema.py 单元测试（构造 + 字段定义）。"""
import pytest

import zvec

import config as cfg
from src.dao.emb.schema import (
    FIELD_API_ID,
    FIELD_API_NAME,
    FIELD_DENSE_EMBEDDING,
    FIELD_DEPRECATED,
    FIELD_DEPRECATION_NOTE,
    FIELD_DESCRIPTION,
    FIELD_EXAMPLES,
    FIELD_INGESTED_AT,
    FIELD_KIND,
    FIELD_LANGUAGE,
    FIELD_NAME,
    FIELD_NAMESPACE,
    FIELD_PARAMETERS_MD,
    FIELD_RETURNS_JSON,
    FIELD_SIGNATURE,
    FIELD_SOURCE_MARKDOWN,
    FIELD_SPARSE_EMBEDDING,
    FIELD_VERSION,
    FIELD_VERSION_SUPPORT,
    get_collection_schema,
)


# 17 字段常量必须全在
ALL_FIELDS = {
    FIELD_NAMESPACE, FIELD_API_ID, FIELD_NAME, FIELD_API_NAME, FIELD_VERSION,
    FIELD_KIND, FIELD_LANGUAGE, FIELD_VERSION_SUPPORT, FIELD_DEPRECATED,
    FIELD_INGESTED_AT,
    FIELD_DESCRIPTION, FIELD_SIGNATURE, FIELD_PARAMETERS_MD, FIELD_RETURNS_JSON,
    FIELD_EXAMPLES, FIELD_SOURCE_MARKDOWN, FIELD_DEPRECATION_NOTE,
}


class TestFieldConstants:
    def test_all_17_fields_defined(self):
        # 防止漏字段或拼错
        assert len(ALL_FIELDS) == 17

    def test_no_duplicates(self):
        assert len(ALL_FIELDS) == len(set(ALL_FIELDS))

    def test_field_names_are_lowercase_snake_case(self):
        import re
        for name in ALL_FIELDS:
            assert re.match(r"^[a-z][a-z0-9_]*$", name), f"bad name: {name}"


class TestSchemaConstruction:
    def test_default_collection_name(self, isolated_config):
        s = get_collection_schema()
        assert s.name == "unit_test"

    def test_custom_collection_name(self, isolated_config):
        s = get_collection_schema("my_custom_coll")
        assert s.name == "my_custom_coll"

    def test_field_count(self, isolated_config):
        s = get_collection_schema()
        assert len(s.fields) == 17

    def test_vector_count_and_types(self, isolated_config):
        s = get_collection_schema()
        assert len(s.vectors) == 2
        dense = next(v for v in s.vectors if v.name == FIELD_DENSE_EMBEDDING)
        sparse = next(v for v in s.vectors if v.name == FIELD_SPARSE_EMBEDDING)
        assert dense.data_type == zvec.DataType.VECTOR_FP32
        assert sparse.data_type == zvec.DataType.SPARSE_VECTOR_FP32
        # 维度：local embedder → 384
        assert dense.dimension == 384
        # sparse 没固定维度
        assert sparse.dimension == 0

    def test_metrics(self, isolated_config):
        s = get_collection_schema()
        dense = next(v for v in s.vectors if v.name == FIELD_DENSE_EMBEDDING)
        sparse = next(v for v in s.vectors if v.name == FIELD_SPARSE_EMBEDDING)
        assert dense.index_param.metric_type == zvec.MetricType.COSINE
        assert sparse.index_param.metric_type == zvec.MetricType.IP

    def test_indexed_fields_have_invert_index(self, isolated_config):
        s = get_collection_schema()
        # 元信息字段都应该有 InvertIndexParam
        indexed_names = {
            FIELD_NAMESPACE, FIELD_API_ID, FIELD_NAME, FIELD_VERSION, FIELD_KIND,
            FIELD_LANGUAGE, FIELD_DEPRECATED, FIELD_INGESTED_AT,
        }
        for f in s.fields:
            if f.name in indexed_names:
                assert f.index_param is not None, f"{f.name} should be indexed"

    def test_payload_fields_unindexed(self, isolated_config):
        s = get_collection_schema()
        # 载荷字段不应该建索引
        unindexed_names = {
            FIELD_API_NAME, FIELD_DESCRIPTION, FIELD_SIGNATURE,
            FIELD_PARAMETERS_MD, FIELD_RETURNS_JSON, FIELD_EXAMPLES,
            FIELD_SOURCE_MARKDOWN, FIELD_DEPRECATION_NOTE,
        }
        for f in s.fields:
            if f.name in unindexed_names:
                assert f.index_param is None, f"{f.name} should NOT be indexed"

    def test_language_is_single_value_string(self, isolated_config):
        # language 应该是 STRING 不是 ARRAY_STRING（按设计）
        s = get_collection_schema()
        lang = next(f for f in s.fields if f.name == FIELD_LANGUAGE)
        assert lang.data_type == zvec.DataType.STRING

    def test_version_support_is_array(self, isolated_config):
        s = get_collection_schema()
        vs = next(f for f in s.fields if f.name == FIELD_VERSION_SUPPORT)
        assert vs.data_type == zvec.DataType.ARRAY_STRING
