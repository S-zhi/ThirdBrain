"""src.dao.emb.indexer 单测。

覆盖：
- collection_path 路径拼接
- _attach_vectors：默认文本拼接 / 显式文本 / 空文本报错 / embedder 异常包成 EmbedderError
- insert_batch / delete_batch 错误统计
- open_collection 不存在时抛 CollectionNotFoundError
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import zvec

from src.dao.emb.exceptions import (
    CollectionNotFoundError,
    EmbedderError,
)
from src.dao.emb.indexer import (
    _attach_vectors,
    collection_path,
    delete_batch,
    delete_doc,
    open_collection,
    open_or_create_collection,
    insert_batch,
    insert_doc,
)
from src.dao.emb.schema import (
    FIELD_DENSE_EMBEDDING,
    FIELD_SPARSE_EMBEDDING,
)


# ---------------------------------------------------------------------------
# collection_path
# ---------------------------------------------------------------------------

class TestCollectionPath:
    def test_default_uses_config(self, use_tmp_config):
        # use_tmp_config 已经把单例切到 tmp_path/zvec
        p = collection_path()
        assert p.endswith("unit_test")
        assert "zvec" in p
        # 应该是绝对路径
        assert Path(p).is_absolute()

    def test_custom_collection_name(self, use_tmp_config):
        p = collection_path("custom")
        assert p.endswith("custom")

    def test_resolves_user_path(self, use_tmp_config, tmp_path, monkeypatch):
        from dataclasses import replace
        import config
        # collection_path 包含 "~" → expanduser
        monkeypatch.setenv("HOME", str(tmp_path))
        new_zvec = replace(config._config.zvec, collection_path="~/my_zvec")
        config._config = replace(config._config, zvec=new_zvec)
        p = collection_path("foo")
        assert p.startswith(str(tmp_path))
        assert p.endswith("foo")


# ---------------------------------------------------------------------------
# _attach_vectors
# ---------------------------------------------------------------------------

class FakeEmbedder:
    def __init__(self, dense=None, sparse=None, dense_exc=None, sparse_exc=None):
        self._dense = dense or [0.1, 0.2, 0.3]
        self._sparse = sparse or {1: 0.5}
        self._dense_exc = dense_exc
        self._sparse_exc = sparse_exc
        self.dense_calls: list[tuple[str, str]] = []
        self.sparse_calls: list[tuple[str, str]] = []

    def embed_dense(self, text, mode="document"):
        self.dense_calls.append((text, mode))
        if self._dense_exc:
            raise self._dense_exc
        return self._dense

    def embed_sparse(self, text, mode="document"):
        self.sparse_calls.append((text, mode))
        if self._sparse_exc:
            raise self._sparse_exc
        return self._sparse


class TestAttachVectors:
    def test_default_text_from_fields(self, make_zvec_doc):
        doc = make_zvec_doc(
            doc_id="ns.op.test",
            description="desc-text",
            api_name="api-name-text",
            api_id="api-id-text",
            signature="sig-text",
        )
        emb = FakeEmbedder()
        out = _attach_vectors(doc, emb)

        # dense 文本 = description + api_name + name
        dense_call_text = emb.dense_calls[0][0]
        assert "desc-text" in dense_call_text
        assert "api-name-text" in dense_call_text
        assert "Test" in dense_call_text
        # sparse 文本 = api_id + signature + name
        sparse_call_text = emb.sparse_calls[0][0]
        assert "api-id-text" in sparse_call_text
        assert "sig-text" in sparse_call_text
        assert "Test" in sparse_call_text
        # mode 是 document
        assert emb.dense_calls[0][1] == "document"
        assert emb.sparse_calls[0][1] == "document"
        # vectors 填上了
        assert FIELD_DENSE_EMBEDDING in out.vectors
        assert FIELD_SPARSE_EMBEDDING in out.vectors
        # fields 透传
        assert out.fields == doc.fields
        # id 不变
        assert out.id == doc.id

    def test_explicit_dense_text(self, make_zvec_doc):
        doc = make_zvec_doc()
        emb = FakeEmbedder()
        _attach_vectors(doc, emb, dense_text="my custom dense text")
        assert emb.dense_calls[0][0] == "my custom dense text"
        # sparse 走默认
        assert emb.sparse_calls[0][0] != "my custom dense text"

    def test_explicit_sparse_text(self, make_zvec_doc):
        doc = make_zvec_doc()
        emb = FakeEmbedder()
        _attach_vectors(doc, emb, sparse_text="my custom sparse text")
        assert emb.sparse_calls[0][0] == "my custom sparse text"
        assert emb.dense_calls[0][0] != "my custom sparse text"

    def test_empty_dense_text_raises(self, make_zvec_doc):
        doc = make_zvec_doc(description="", api_name="", name="")
        # 全空 → 默认拼接结果为空
        emb = FakeEmbedder()
        with pytest.raises(EmbedderError) as exc:
            _attach_vectors(doc, emb)
        assert "dense_text 为空" in str(exc.value)
        assert "ns.op.test" in str(exc.value)

    def test_empty_sparse_text_raises(self, make_zvec_doc):
        doc = make_zvec_doc(
            api_id="",
            signature="",
            name="",
        )
        emb = FakeEmbedder()
        with pytest.raises(EmbedderError) as exc:
            _attach_vectors(doc, emb)
        assert "sparse_text 为空" in str(exc.value)

    def test_embedder_error_propagates(self, make_zvec_doc):
        doc = make_zvec_doc()
        emb = FakeEmbedder(dense_exc=EmbedderError("upstream failed"))
        with pytest.raises(EmbedderError) as exc:
            _attach_vectors(doc, emb)
        assert "upstream failed" in str(exc.value)

    def test_unexpected_embedder_exception_wrapped(self, make_zvec_doc):
        doc = make_zvec_doc()
        emb = FakeEmbedder(dense_exc=RuntimeError("net"))
        with pytest.raises(EmbedderError) as exc:
            _attach_vectors(doc, emb)
        assert "embedder 调用失败" in str(exc.value)
        assert "net" in str(exc.value)


# ---------------------------------------------------------------------------
# insert_doc / insert_batch
# ---------------------------------------------------------------------------

class FakeCollection:
    """最小 zvec.Collection 替身：upsert / delete 都记下来。"""

    def __init__(self, fail_ids: set[str] | None = None):
        self.upserted: list = []
        self.deleted: list[str] = []
        self._fail = fail_ids or set()

    def upsert(self, doc):
        if doc.id in self._fail:
            raise RuntimeError(f"upsert failed: {doc.id}")
        self.upserted.append(doc)

    def delete(self, ids):
        if isinstance(ids, str):
            ids = [ids]
        for i in ids:
            if i in self._fail:
                raise RuntimeError(f"delete failed: {i}")
            self.deleted.append(i)


class TestInsertDoc:
    def test_success(self, make_zvec_doc):
        coll = FakeCollection()
        emb = FakeEmbedder()
        doc = make_zvec_doc(doc_id="ns.op.1")
        insert_doc(coll, doc, emb)
        assert len(coll.upserted) == 1
        assert coll.upserted[0].id == "ns.op.1"
        assert FIELD_DENSE_EMBEDDING in coll.upserted[0].vectors

    def test_raises_on_embed_error(self, make_zvec_doc):
        coll = FakeCollection()
        emb = FakeEmbedder(dense_exc=EmbedderError("boom"))
        doc = make_zvec_doc()
        with pytest.raises(EmbedderError):
            insert_doc(coll, doc, emb)
        assert coll.upserted == []


class TestInsertBatch:
    def test_all_success(self, make_zvec_doc):
        coll = FakeCollection()
        emb = FakeEmbedder()
        docs = [make_zvec_doc(doc_id=f"ns.op.{i}") for i in range(3)]
        result = insert_batch(coll, docs, emb)
        assert result == {"ok": 3, "fail": 0, "errors": []}
        assert len(coll.upserted) == 3

    def test_partial_failure(self, make_zvec_doc):
        # 让第 2 条的 embed 失败
        emb = FakeEmbedder()
        original_embed = emb.embed_dense
        call_count = {"n": 0}

        def flaky_embed(text, mode="document"):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise EmbedderError("2nd fails")
            return original_embed(text, mode)

        emb.embed_dense = flaky_embed
        coll = FakeCollection()
        docs = [make_zvec_doc(doc_id=f"ns.op.{i}") for i in range(3)]
        result = insert_batch(coll, docs, emb)
        assert result["ok"] == 2
        assert result["fail"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0][0] == "ns.op.1"  # 失败的 doc id
        assert "2nd fails" in result["errors"][0][1]
        # 成功的两条都进 coll
        assert len(coll.upserted) == 2
        assert {d.id for d in coll.upserted} == {"ns.op.0", "ns.op.2"}

    def test_materializes_iterator(self, make_zvec_doc):
        # 传 generator 也能跑（内部 list() 一次）
        coll = FakeCollection()
        emb = FakeEmbedder()
        docs = (make_zvec_doc(doc_id=f"ns.op.{i}") for i in range(2))
        result = insert_batch(coll, docs, emb)
        assert result["ok"] == 2

    def test_insert_runtime_error_counted(self, make_zvec_doc):
        coll = FakeCollection(fail_ids={"ns.op.1"})
        emb = FakeEmbedder()
        docs = [make_zvec_doc(doc_id=f"ns.op.{i}") for i in range(3)]
        result = insert_batch(coll, docs, emb)
        assert result["ok"] == 2
        assert result["fail"] == 1
        assert result["errors"][0][0] == "ns.op.1"


# ---------------------------------------------------------------------------
# delete_doc / delete_batch
# ---------------------------------------------------------------------------

class TestDelete:
    def test_single(self):
        coll = FakeCollection()
        delete_doc(coll, "ns.op.1")
        assert coll.deleted == ["ns.op.1"]

    def test_batch_success(self):
        coll = FakeCollection()
        result = delete_batch(coll, ["a", "b", "c"])
        assert result == {"ok": 3, "fail": 0, "errors": []}
        assert coll.deleted == ["a", "b", "c"]

    def test_batch_partial_failure(self):
        coll = FakeCollection(fail_ids={"b"})
        result = delete_batch(coll, ["a", "b", "c"])
        assert result["ok"] == 2
        assert result["fail"] == 1
        assert result["errors"][0][0] == "b"
        assert "delete failed: b" in result["errors"][0][1]


# ---------------------------------------------------------------------------
# open_collection / open_or_create_collection
# ---------------------------------------------------------------------------

class TestOpenCollection:
    def test_missing_path_raises(self, use_tmp_config):
        # tmp_config 指向的路径还不存在
        with pytest.raises(CollectionNotFoundError) as exc:
            open_collection()
        assert "unit_test" in str(exc.value)


class TestOpenOrCreate:
    def test_existing_path_opens(self, use_tmp_config, tmp_path, monkeypatch):
        from dataclasses import replace
        import config
        # 模拟已有 collection 目录
        existing = Path(collection_path())
        existing.mkdir(parents=True, exist_ok=True)
        # 让 zvec.open 返回一个假对象
        fake_coll = MagicMock()
        from src.dao.emb.schema import get_collection_schema
        fake_coll.schema = get_collection_schema()
        with patch("src.dao.emb.indexer.zvec.open", return_value=fake_coll) as mock_open:
            coll = open_or_create_collection()
        assert coll is fake_coll
        mock_open.assert_called_once()
        # 显式断言：传的是我们那个 path
        kwargs = mock_open.call_args.kwargs
        assert kwargs["path"].endswith("unit_test")

    def test_existing_path_but_open_fails(self, use_tmp_config, tmp_path):
        from dataclasses import replace
        import config
        existing = Path(collection_path())
        existing.mkdir(parents=True, exist_ok=True)
        with patch("src.dao.emb.indexer.zvec.open", side_effect=RuntimeError("corrupted")):
            with pytest.raises(CollectionNotFoundError) as exc:
                open_or_create_collection()
            assert "corrupted" in str(exc.value)

    def test_missing_path_creates(self, use_tmp_config):
        fake_coll = MagicMock()
        with patch("src.dao.emb.indexer.zvec.create_and_open", return_value=fake_coll) as mock_create:
            coll = open_or_create_collection()
        assert coll is fake_coll
        mock_create.assert_called_once()
