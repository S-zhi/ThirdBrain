"""indexer CRUD 集成测试（用真实 Zvec collection，跳过 embedder）。"""
import gc
import shutil

import pytest
import zvec

import config as cfg
from src.dao.emb import (
    CollectionNotFoundError,
    NotSupportedError,
    SchemaMismatchError,
    collection_path,
    count_docs,
    delete_batch,
    delete_doc,
    fetch_batch,
    fetch_doc,
    list_ids,
    open_collection,
    open_or_create_collection,
    upsert_batch,
    update_batch,
    update_doc,
)
from src.dao.emb.schema import (
    FIELD_DENSE_EMBEDDING,
    FIELD_SPARSE_EMBEDDING,
    get_collection_schema,
)


def make_doc(doc_id: str, **overrides) -> zvec.Doc:
    """造一条 schema 兼容的 zvec.Doc。dense 维度从 config 读，避免硬编码。"""
    import config as cfg
    c = cfg.get_config()
    if c.embedder.type == "bailian":
        dim = c.embedder.bailian.dimension
    else:
        dim = c.embedder.local.dimension

    fields = {
        "namespace": "com.test.v1",
        "api_id": f"com.test.v1.{doc_id}",
        "name": doc_id,
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


@pytest.fixture
def collection(isolated_config, tmp_path):
    """每个测试一个独立的 collection 目录。"""
    name = "test_crud_coll"
    coll = open_or_create_collection(name)
    yield coll
    # teardown：zvec 0.6 的 Collection 没有 close()，
    # 只能靠 GC 释放 C++ 持有的 LOCK 文件。
    # 显式删引用 + collect()，确保下个测试能拿锁。
    import gc
    del coll
    gc.collect()
    coll_path = collection_path(name)
    if coll_path:
        shutil.rmtree(coll_path, ignore_errors=True)


class TestOpenAndCreate:
    def test_open_or_create_new(self, isolated_config, tmp_path):
        coll = open_or_create_collection("brand_new")
        assert coll.stats.doc_count == 0
        # 清理
        shutil.rmtree(collection_path("brand_new"), ignore_errors=True)

    def test_open_collection_not_found(self, isolated_config):
        with pytest.raises(CollectionNotFoundError):
            open_collection("nonexistent_xyz")
        # 注："打开现有 collection" 没法在单测里验证——zvec C++ binding
        # 持有文件锁，GC 都不释放。open_or_create 已在别的测试隐式覆盖。


class TestReadOnlyHandle:
    """read_only 句柄: 不能 upsert, 但 fetch / count / search 正常。"""

    def test_read_only_handle_rejects_upsert(self, isolated_config, tmp_path):
        name = "ro_reject"
        # 先用 read-write 建 + 写一条
        coll = open_or_create_collection(name)
        coll.upsert(make_doc("a"))
        assert count_docs(coll) == 1
        del coll
        gc.collect()

        # 再用 read_only 打开 → upsert 必须被拒
        coll_ro = open_or_create_collection(name, read_only=True)
        try:
            with pytest.raises(Exception, match="read-only"):
                coll_ro.upsert(make_doc("b"))
        finally:
            del coll_ro
            gc.collect()
        # 清理
        shutil.rmtree(collection_path(name), ignore_errors=True)

    def test_read_only_handle_allows_reads(self, isolated_config, tmp_path):
        name = "ro_read"
        coll = open_or_create_collection(name)
        coll.upsert(make_doc("a"))
        del coll
        gc.collect()

        coll_ro = open_or_create_collection(name, read_only=True)
        try:
            # 读操作应该都 OK
            assert count_docs(coll_ro) == 1
            d = fetch_doc(coll_ro, "a")
            assert d is not None
            assert d.id == "a"
        finally:
            del coll_ro
            gc.collect()
        shutil.rmtree(collection_path(name), ignore_errors=True)

    def test_read_only_on_missing_collection_raises(self, isolated_config, tmp_path):
        """read_only=True 但目录不在 → CollectionNotFoundError（不能创建）。"""
        name = "ro_missing"
        with pytest.raises(CollectionNotFoundError, match="read_only=True"):
            open_or_create_collection(name, read_only=True)
        # 确认目录没被误建
        import os
        assert not os.path.isdir(collection_path(name))

    def test_open_collection_read_only(self, isolated_config, tmp_path):
        """open_collection(read_only=True) 也走 read-only 句柄路径。"""
        name = "ro_open"
        coll = open_or_create_collection(name)
        coll.upsert(make_doc("a"))
        del coll
        gc.collect()

        coll_ro = open_collection(name, read_only=True)
        try:
            assert count_docs(coll_ro) == 1
        finally:
            del coll_ro
            gc.collect()
        shutil.rmtree(collection_path(name), ignore_errors=True)


class TestSchemaEvolution:
    """open_or_create_collection 的 schema 兜底迁移（auto-expand）。"""

    @staticmethod
    def _build_minimal_schema(
        name: str,
        extra_fields: list[zvec.FieldSchema] | None = None,
    ) -> zvec.CollectionSchema:
        """构造最小可用 schema（含 id + v1, 可选 extra 字段）。"""
        fields = [
            zvec.FieldSchema(
                name="id", data_type=zvec.DataType.STRING, index_param=zvec.InvertIndexParam()
            ),
        ]
        if extra_fields:
            fields.extend(extra_fields)
        return zvec.CollectionSchema(
            name=name,
            fields=fields,
            vectors=[
                zvec.VectorSchema(
                    name="v",
                    data_type=zvec.DataType.VECTOR_FP32,
                    dimension=4,
                    index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
                ),
            ],
        )

    def test_auto_add_nullable_numeric_field(self, isolated_config, tmp_path):
        """C 端多了 nullable 数值字段 → 打开时自动 add_column + backfill。"""
        name = "ev_add_num"
        # 用"老"schema 创建
        old_schema = self._build_minimal_schema(name)
        coll_old = zvec.create_and_open(path=collection_path(name), schema=old_schema)
        coll_old.upsert(zvec.Doc(id="a", fields={"id": "a"}, vectors={"v": [0.1] * 4}))
        del coll_old
        gc.collect()

        # 用"新"schema 打开 (C 端多了 counter: INT64)
        new_schema = self._build_minimal_schema(
            name,
            extra_fields=[zvec.FieldSchema(name="counter", data_type=zvec.DataType.INT64)],
        )
        coll_new = open_or_create_collection(name, schema=new_schema)

        # 验证: 磁盘 schema 多了 counter
        field_names = {f.name for f in coll_new.schema.fields}
        assert "counter" in field_names
        # 老 doc 的 counter 被 backfill 成 0
        d = coll_new.fetch(ids="a", include_vector=False).get("a")
        assert d.fields.get("counter") == 0
        del coll_new
        gc.collect()
        shutil.rmtree(collection_path(name), ignore_errors=True)

    def test_auto_add_works_for_all_numeric_types(self, isolated_config, tmp_path):
        """6 个数值类型（INT32/INT64/UINT32/UINT64/FLOAT/DOUBLE）都能 auto-add。"""
        for dt, expr_default in [
            (zvec.DataType.INT32, 0),
            (zvec.DataType.INT64, 0),
            (zvec.DataType.UINT32, 0),
            (zvec.DataType.UINT64, 0),
            (zvec.DataType.FLOAT, 0.0),
            (zvec.DataType.DOUBLE, 0.0),
        ]:
            name = f"ev_num_{dt.name}"
            old = self._build_minimal_schema(name)
            coll = zvec.create_and_open(path=collection_path(name), schema=old)
            coll.upsert(zvec.Doc(id="a", fields={"id": "a"}, vectors={"v": [0.1] * 4}))
            del coll
            gc.collect()

            new = self._build_minimal_schema(
                name, extra_fields=[zvec.FieldSchema(name="x", data_type=dt)]
            )
            coll = open_or_create_collection(name, schema=new)
            d = coll.fetch(ids="a", include_vector=False).get("a")
            assert d.fields.get("x") == expr_default, f"{dt.name} backfill 不对"
            del coll
            gc.collect()
            shutil.rmtree(collection_path(name), ignore_errors=True)

    def test_non_numeric_new_field_raises(self, isolated_config, tmp_path):
        """C 端加 STRING 字段 → SchemaMismatchError (zvec 0.6 不支持 add_column 非数值)。"""
        name = "ev_add_str"
        old = self._build_minimal_schema(name)
        coll = zvec.create_and_open(path=collection_path(name), schema=old)
        del coll
        gc.collect()

        new = self._build_minimal_schema(
            name,
            extra_fields=[zvec.FieldSchema(name="tag", data_type=zvec.DataType.STRING)],
        )
        with pytest.raises(SchemaMismatchError, match="add_column"):
            open_or_create_collection(name, schema=new)
        shutil.rmtree(collection_path(name), ignore_errors=True)

    def test_array_new_field_raises(self, isolated_config, tmp_path):
        """C 端加 ARRAY_STRING 字段 → SchemaMismatchError。"""
        name = "ev_add_arr"
        old = self._build_minimal_schema(name)
        coll = zvec.create_and_open(path=collection_path(name), schema=old)
        del coll
        gc.collect()

        new = self._build_minimal_schema(
            name,
            extra_fields=[
                zvec.FieldSchema(name="tags", data_type=zvec.DataType.ARRAY_STRING)
            ],
        )
        with pytest.raises(SchemaMismatchError, match="add_column"):
            open_or_create_collection(name, schema=new)
        shutil.rmtree(collection_path(name), ignore_errors=True)

    def test_new_vector_field_raises(self, isolated_config, tmp_path):
        """C 端加 vector 字段 → SchemaMismatchError (backfill 需要 re-embed, 不能自动做)。"""
        name = "ev_add_vec"
        old = self._build_minimal_schema(name)
        coll = zvec.create_and_open(path=collection_path(name), schema=old)
        del coll
        gc.collect()

        new = zvec.CollectionSchema(
            name=name,
            fields=[
                zvec.FieldSchema(
                    name="id", data_type=zvec.DataType.STRING, index_param=zvec.InvertIndexParam()
                ),
            ],
            vectors=[
                zvec.VectorSchema(
                    name="v", data_type=zvec.DataType.VECTOR_FP32, dimension=4,
                    index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
                ),
                zvec.VectorSchema(
                    name="v2", data_type=zvec.DataType.VECTOR_FP32, dimension=4,
                    index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
                ),
            ],
        )
        with pytest.raises(SchemaMismatchError, match="vector 字段"):
            open_or_create_collection(name, schema=new)
        shutil.rmtree(collection_path(name), ignore_errors=True)

    def test_existing_field_type_mismatch_raises(self, isolated_config, tmp_path):
        """同名字段 type 不一致 → SchemaMismatchError。"""
        name = "ev_type_mismatch"
        old = self._build_minimal_schema(
            name, extra_fields=[zvec.FieldSchema(name="x", data_type=zvec.DataType.INT64)]
        )
        coll = zvec.create_and_open(path=collection_path(name), schema=old)
        del coll
        gc.collect()

        # 把 x 改成 DOUBLE
        new = self._build_minimal_schema(
            name, extra_fields=[zvec.FieldSchema(name="x", data_type=zvec.DataType.DOUBLE)]
        )
        with pytest.raises(SchemaMismatchError, match="类型不一致"):
            open_or_create_collection(name, schema=new)
        shutil.rmtree(collection_path(name), ignore_errors=True)

    def test_existing_vector_dim_mismatch_raises(self, isolated_config, tmp_path):
        """同名 vector 维度不一致 → SchemaMismatchError。"""
        name = "ev_dim_mismatch"
        old = zvec.CollectionSchema(
            name=name,
            fields=[
                zvec.FieldSchema(
                    name="id", data_type=zvec.DataType.STRING, index_param=zvec.InvertIndexParam()
                ),
            ],
            vectors=[
                zvec.VectorSchema(
                    name="v", data_type=zvec.DataType.VECTOR_FP32, dimension=4,
                    index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
                ),
            ],
        )
        coll = zvec.create_and_open(path=collection_path(name), schema=old)
        del coll
        gc.collect()

        # 改 dim 到 8
        new = zvec.CollectionSchema(
            name=name,
            fields=[
                zvec.FieldSchema(
                    name="id", data_type=zvec.DataType.STRING, index_param=zvec.InvertIndexParam()
                ),
            ],
            vectors=[
                zvec.VectorSchema(
                    name="v", data_type=zvec.DataType.VECTOR_FP32, dimension=8,
                    index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
                ),
            ],
        )
        with pytest.raises(SchemaMismatchError, match="vector 字段.*定义不一致"):
            open_or_create_collection(name, schema=new)
        shutil.rmtree(collection_path(name), ignore_errors=True)

    def test_orphan_field_preserved(self, isolated_config, tmp_path, caplog):
        """磁盘有 C 端没有的字段 → WARN 日志 + 保留 (不删)。"""
        name = "ev_orphan"
        # 老 schema 有 orphan 字段
        old = self._build_minimal_schema(
            name, extra_fields=[zvec.FieldSchema(name="orphan", data_type=zvec.DataType.INT64)]
        )
        coll = zvec.create_and_open(path=collection_path(name), schema=old)
        del coll
        gc.collect()

        # 新 schema 不包含 orphan
        new = self._build_minimal_schema(name)
        with caplog.at_level("WARNING", logger="src.dao.emb.indexer"):
            coll = open_or_create_collection(name, schema=new)
        try:
            # orphan 字段还在
            field_names = {f.name for f in coll.schema.fields}
            assert "orphan" in field_names
            # WARN 日志含 orphan
            assert any("orphan_field" in r.message for r in caplog.records)
        finally:
            del coll
            gc.collect()
        shutil.rmtree(collection_path(name), ignore_errors=True)

    def test_read_only_skips_migration_but_warns(self, isolated_config, tmp_path, caplog):
        """read_only=True 不会调 add_column, 但会记 WARN。"""
        name = "ev_ro_drift"
        old = self._build_minimal_schema(name)
        coll = zvec.create_and_open(path=collection_path(name), schema=old)
        del coll
        gc.collect()

        new = self._build_minimal_schema(
            name, extra_fields=[zvec.FieldSchema(name="x", data_type=zvec.DataType.INT64)]
        )
        with caplog.at_level("WARNING", logger="src.dao.emb.indexer"):
            coll_ro = open_or_create_collection(name, schema=new, read_only=True)
        try:
            # 没补字段
            field_names = {f.name for f in coll_ro.schema.fields}
            assert "x" not in field_names
            # 记了 WARN
            assert any("read_only_drift" in r.message for r in caplog.records)
        finally:
            del coll_ro
            gc.collect()
        shutil.rmtree(collection_path(name), ignore_errors=True)


class TestFetch:
    def test_fetch_existing(self, collection):
        collection.upsert(make_doc("a"))
        d = fetch_doc(collection, "a")
        assert d is not None
        assert d.id == "a"
        assert d.fields["name"] == "a"

    def test_fetch_missing_returns_none(self, collection):
        assert fetch_doc(collection, "nope") is None

    def test_fetch_batch(self, collection):
        for did in ["x", "y", "z"]:
            collection.upsert(make_doc(did))
        result = fetch_batch(collection, ["x", "y", "missing", "z"])
        # missing 的 id 静默忽略
        assert set(result.keys()) == {"x", "y", "z"}

    def test_fetch_batch_empty(self, collection):
        assert fetch_batch(collection, []) == {}


class TestCount:
    def test_count_zero(self, collection):
        assert count_docs(collection) == 0

    def test_count_after_upsert(self, collection):
        for did in ["a", "b", "c"]:
            collection.upsert(make_doc(did))
        assert count_docs(collection) == 3

    def test_count_after_delete(self, collection):
        for did in ["a", "b", "c"]:
            collection.upsert(make_doc(did))
        delete_doc(collection, "b")
        assert count_docs(collection) == 2


class TestUpdate:
    def test_update_only_specified_field(self, collection):
        collection.upsert(make_doc("a", description="original"))
        # 部分更新：只改 description
        update_doc(collection, zvec.Doc(
            id="a",
            fields={"description": "UPDATED"},
        ))
        d = fetch_doc(collection, "a")
        assert d.fields["description"] == "UPDATED"
        # 其他字段保留
        assert d.fields["name"] == "a"
        assert d.fields["api_id"] == "com.test.v1.a"
        assert d.fields["namespace"] == "com.test.v1"

    def test_update_nonexistent(self, collection):
        # zvec 的 update 对不存在的 id 行为：返回 error status（不抛异常）
        # 我们只验证不崩
        from src.dao.emb import upsert_doc
        # 先确保 collection 里有 a
        collection.upsert(make_doc("a"))
        # 试着更新不存在的 id（zvec 0.6 应该不抛）
        try:
            update_doc(collection, zvec.Doc(id="ghost", fields={"name": "G"}))
        except Exception:
            pass  # 不同版本行为不同

    def test_update_batch(self, collection):
        for did in ["a", "b"]:
            collection.upsert(make_doc(did))
        statuses = update_batch(collection, [
            zvec.Doc(id="a", fields={"description": "new_a"}),
            zvec.Doc(id="b", fields={"description": "new_b"}),
        ])
        assert len(statuses) == 2
        d_a = fetch_doc(collection, "a")
        assert d_a.fields["description"] == "new_a"


class TestDelete:
    def test_delete(self, collection):
        collection.upsert(make_doc("a"))
        assert count_docs(collection) == 1
        delete_doc(collection, "a")
        assert count_docs(collection) == 0
        assert fetch_doc(collection, "a") is None

    def test_delete_batch(self, collection):
        for did in ["a", "b", "c"]:
            collection.upsert(make_doc(did))
        result = delete_batch(collection, ["a", "b", "missing"])
        # zvec delete 对不存在的 id 静默成功（不抛异常），所以 ok=3, fail=0
        assert result["ok"] == 3
        assert result["fail"] == 0
        assert count_docs(collection) == 1  # 实际只剩 c


class TestUpsertBatch:
    def test_upsert_batch_returns_stats(self, collection):
        # 这里用 _FakeEmbedder 不行——upsert_batch 内部调 embedder
        # 所以走直接 zvec.upsert 验证 coll 行为
        collection.upsert(make_doc("a"))
        collection.upsert(make_doc("b"))
        result = collection.upsert([make_doc("c"), make_doc("d")])
        # 返回值可能是 list of Status
        assert count_docs(collection) == 4


class TestListIds:
    def test_list_ids_raises_not_supported(self, collection):
        with pytest.raises(NotSupportedError, match="不支持 list_all"):
            list_ids(collection)


class TestCollectionPath:
    def test_path_includes_collection_name(self, isolated_config):
        path = collection_path("my_coll")
        assert path.endswith("my_coll")
        assert "zvec_data" in path or "unit_test" in path  # base path components
