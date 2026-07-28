"""高层 CRUD 入口：``DirectorDoc`` + ``CollectionSession`` + 传 collection 名的 CRUD。

设计目标：
- 把 ``zvec.Doc``、``ApiDocumentLike``、``Embedder`` 三个零散的概念
  收拢成一个 :class:`DirectorDoc`，CRUD 函数只接受它。
- 把 ``zvec.Collection`` 句柄的 open / 释放（zvec 0.6 没 close，靠 GC）
  收拢成 :class:`CollectionSession` context manager。
- 提供**传 collection 名**的高层 CRUD（``insert / insert_many / delete`` 等），
  内部自动 open collection + 转 zvec.Doc + 写库。调用方不需要管 zvec 细节。

模块边界：
- 本文件依赖 :mod:`src.dao.emb.indexer` 的低层 CRUD 与 schema 迁移。
- 真实 zvec 调用仍由 :mod:`src.dao.emb.indexer` 内的 ``_attach_vectors``、
  ``open_or_create_collection`` 等承担。
"""

from __future__ import annotations

import gc
from collections.abc import Iterable
from dataclasses import dataclass

import zvec

from src.dao.emb.doc import ApiDocumentLike, from_orm
from src.dao.emb.embedder import Embedder
from src.dao.emb.exceptions import EmbedderError, SearchError
from src.dao.emb.indexer import (
    _attach_vectors,
    open_or_create_collection,
)
from src.dao.emb.indexer import count_docs as _low_count_docs
from src.dao.emb.indexer import (
    delete_batch as _low_delete_batch,
)
from src.dao.emb.indexer import (
    delete_doc as _low_delete_doc,
)
from src.dao.emb.indexer import (
    fetch_doc as _low_fetch_doc,
)
from src.dao.emb.indexer import (
    update_batch as _low_update_batch,
)
from src.dao.emb.indexer import (
    update_doc as _low_update_doc,
)

# ---------------------------------------------------------------------------
# DirectorDoc：CRUD 唯一识别的 canonical document 对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DirectorDoc:
    """CRUD 层唯一识别的 canonical document 对象。

    封装：:class:`ApiDocumentLike` ORM 记录 + :class:`Embedder` 引用。
    真实 zvec.Doc（含向量）由 :meth:`to_zvec` 现场生成。

    约束：
    - 不可变（``frozen=True``）；构造完就只能 ``to_zvec()`` 出去。
    - 不暴露 zvec 细节；调用方不直接碰 ``zvec.Doc``。
    - 同一 record 可以多次 ``to_zvec()``（生成新的 full doc），但
      DirectorDoc 实例本身不能改 record / embedder 引用。

    Example:
        >>> ddoc = DirectorDoc(record=my_orm_record, embedder=my_embedder)
        >>> insert("api_v1", ddoc)
        >>> full = ddoc.to_zvec()  # 一般不直接用；CRUD 内部会调
    """

    record: ApiDocumentLike
    embedder: Embedder

    @property
    def doc_id(self) -> str:
        """这条 doc 的 id（来自 ``record.chunk_id``）。"""
        return self.record.chunk_id

    def to_zvec(self) -> zvec.Doc:
        """转成带 dense + sparse 向量的 :class:`zvec.Doc`。

        内部走 :func:`src.dao.emb.doc.from_orm` 拼 fields，
        再走 :func:`src.dao.emb.indexer._attach_vectors` 调 embedder。
        返回的是新 doc；不修改 self。
        """
        return _attach_vectors(from_orm(self.record), self.embedder)


# ---------------------------------------------------------------------------
# CollectionSession：collection 句柄的 context manager
# ---------------------------------------------------------------------------

class CollectionSession:
    """``zvec.Collection`` 句柄的 context manager 封装。

    解决两个痛点：
    1. **open 自动化**：with 块进入时调 :func:`open_or_create_collection`，
       退出时显式释放引用 + ``gc.collect()``。zvec 0.6 C++ binding
       不提供 ``close()``，靠引用释放 + GC 才能让下个句柄拿到 LOCK 文件。
    2. **read_only 一致性**：所有 CRUD 操作都希望走统一的 read_only
       标志；把 read_only 也下沉到 Session。

    用法：
        >>> with CollectionSession("api_v1") as coll:
        ...     count = coll.stats.doc_count
        >>> # with 块退出后，coll 句柄已释放

    Args:
        name: collection 名；用 :func:`open_or_create_collection` 解析路径。
        read_only: True → read-only + mmap 句柄（写操作会被 zvec 拒）；
            False（默认）→ 读写句柄。

    Note:
        - ``__exit__`` **不**抛异常（吞掉所有内部清理异常）；
          业务异常照常向上抛。
        - ``with`` 块退出后**不**保证句柄立即失效（zvec 引用释放靠
          Python GC 时机），但代码层不再持有 coll 引用，等于"逻辑关闭"。
    """

    def __init__(self, name: str, *, read_only: bool = False) -> None:
        self._name = name
        self._read_only = read_only
        self._coll: zvec.Collection | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def read_only(self) -> bool:
        return self._read_only

    def __enter__(self) -> zvec.Collection:
        self._coll = open_or_create_collection(
            self._name, read_only=self._read_only
        )
        return self._coll

    def __exit__(self, exc_type, exc, tb) -> None:
        # 显式释放；zvec 0.6 没 close，靠引用释放 + GC
        self._coll = None
        gc.collect()


# ---------------------------------------------------------------------------
# 高层 CRUD：传 collection 名 + DirectorDoc，内部自动 open + 转 zvec + 写库
#
# 与低层 CRUD（:mod:`src.dao.emb.indexer` 内的 ``insert_doc / insert_batch`` 等）
# 的关系：高层是 sugar，底层是核心。所有高层函数都先 ``with CollectionSession``
# 拿句柄，然后调对应的低层函数。
# ---------------------------------------------------------------------------

def insert(collection: str, ddoc: DirectorDoc) -> None:
    """单条写入指定 collection。

    Args:
        collection: collection 名（不是 path；走 :func:`open_or_create_collection`）。
        ddoc: :class:`DirectorDoc`（自带 embedder）。

    行为：
    - 同 id 已存在则覆盖（zvec upsert 语义，**不**是严格 SQL INSERT）。
    - 内部 open collection → ``ddoc.to_zvec()`` 拼向量 → ``coll.upsert``。

    Raises:
        EmbedderError: 调 embedder 失败。
        SearchError: zvec 写阶段失败。
    """
    with CollectionSession(collection) as coll:
        full = ddoc.to_zvec()
        try:
            coll.upsert(full)
        except EmbedderError:
            raise
        except Exception as e:
            raise SearchError(
                f"zvec insert failed for {ddoc.doc_id} into {collection!r}: {e}"
            ) from e


def insert_many(collection: str, ddocs: Iterable[DirectorDoc]) -> dict:
    """批量写入指定 collection。

    行为：
    - 串行 embed + insert（Zvec 写是单进程独占，串行更安全）。
    - 单条失败不影响其他 doc；embed 失败和 zvec 失败都被捕获。
    - 假设所有 ddocs 共享同一 embedder（典型情况），但代码层不强制 —
      每个 ddoc 仍用自己的 embedder（允许一条 batch 跨 embedder）。

    Args:
        collection: collection 名。
        ddocs: :class:`DirectorDoc` 列表（任意 Iterable）。

    Returns:
        ``{"ok": int, "fail": int, "errors": list[(doc_id, str)]}``
        - ``ok``: 成功数。
        - ``fail``: 失败数。
        - ``errors``: 失败列表，每条 ``(doc_id, error_message)``。
    """
    ok, fail, errors = 0, 0, []
    ddocs_list = list(ddocs)

    with CollectionSession(collection) as coll:
        for ddoc in ddocs_list:
            try:
                full = ddoc.to_zvec()
                coll.upsert(full)
                ok += 1
            except (EmbedderError, SearchError) as e:
                fail += 1
                errors.append((ddoc.doc_id, str(e)))
            except Exception as e:  # noqa: BLE001
                fail += 1
                errors.append((ddoc.doc_id, f"unexpected: {e}"))

    return {"ok": ok, "fail": fail, "errors": errors}


def delete(collection: str, doc_id: str) -> None:
    """按 id **物理**删除指定 collection 里的单条 doc。

    ⚠️ zvec 的 delete 是**物理**删除，没有 archived / soft-delete 概念。
    如果需要"软删"（保留数据可恢复），在 ORM 层做（设 ``archived_at``），
    然后**不要**调本函数；调 :func:`insert` 把 doc 改成"废弃"状态。

    Args:
        collection: collection 名。
        doc_id: 要删除的 doc id。
    """
    with CollectionSession(collection) as coll:
        _low_delete_doc(coll, doc_id)


def delete_many(collection: str, doc_ids: Iterable[str]) -> dict:
    """批量物理删除指定 collection 里的 doc。

    ⚠️ 同 :func:`delete`，这是**物理**删除。zvec 0.6 对不存在的 id
    静默成功（不抛异常），所以 ``ok`` 会包含这些"实际没删"的 id。

    Args:
        collection: collection 名。
        doc_ids: doc id 列表（任意 Iterable）。

    Returns:
        ``{"ok": int, "fail": int, "errors": list[(doc_id, str)]}``
    """
    ids_list = list(doc_ids)
    if not ids_list:
        return {"ok": 0, "fail": 0, "errors": []}
    with CollectionSession(collection) as coll:
        return _low_delete_batch(coll, ids_list)


def update(collection: str, ddoc: DirectorDoc) -> None:
    """全量字段更新指定 collection 里的 doc（vectors 一并重算）。

    与 :func:`insert` 的区别只在**用户意图**：
    - ``insert``：表达"put it in"（创建或覆盖）。
    - ``update``：表达"更新已有 doc"（哪怕底层也是覆盖）。

    实际行为：调低层 :func:`src.dao.emb.indexer.update_doc`，
    传 ``fields`` 来自 ``ddoc.record``，**不**带 vectors（zvec
    行为：fields-only update 时保留已有 vectors — 见低层 docstring）。

    .. note::
        如果需要"fetch → 改 fields → update"的安全更新流，调用方
        自己 fetch + 包新 ddoc + 调本函数。本函数不做 fetch-merge。

    Args:
        collection: collection 名。
        ddoc: :class:`DirectorDoc`（``record`` 字段会作为新 fields 全量替换）。
    """
    with CollectionSession(collection) as coll:
        zvec_doc = zvec.Doc(
            id=ddoc.doc_id,
            fields=from_orm(ddoc.record).fields,
        )
        _low_update_doc(coll, zvec_doc)


def update_many(collection: str, ddocs: Iterable[DirectorDoc]) -> list:
    """批量全量字段更新指定 collection 里的 doc。

    Args:
        collection: collection 名。
        ddocs: :class:`DirectorDoc` 列表。

    Returns:
        zvec 原生返回值（list of Status，具体类型看 zvec 版本）。
    """
    ddocs_list = list(ddocs)
    if not ddocs_list:
        return []
    with CollectionSession(collection) as coll:
        zvec_docs = [
            zvec.Doc(id=ddoc.doc_id, fields=from_orm(ddoc.record).fields)
            for ddoc in ddocs_list
        ]
        return _low_update_batch(coll, zvec_docs)


def fetch(collection: str, doc_id: str) -> DirectorDoc | None:
    """按 id 从指定 collection 拉单条 doc，包成 :class:`DirectorDoc`。

    注意：返回的 :class:`DirectorDoc` 只填了 ``record`` 字段（从 zvec
    fetch 结果反推），``embedder`` 是 ``None``。如果需要再 update，
    请自己用真实 embedder 重新包一个。

    Args:
        collection: collection 名。
        doc_id: doc id。

    Returns:
        :class:`DirectorDoc`（``embedder=None``），或 None（不存在）。
    """
    with CollectionSession(collection) as coll:
        raw = _low_fetch_doc(coll, doc_id)
    if raw is None:
        return None
    # raw 是 zvec.Doc-like 对象，有 .fields 和 .id
    # 反向构造 ApiDocumentLike：用 SimpleNamespace 包出 duck-typed 对象
    from types import SimpleNamespace
    record = SimpleNamespace(
        chunk_id=raw.id,
        name=raw.fields.get("name", ""),
        namespace=raw.fields.get("namespace", ""),
        language=raw.fields.get("language", ""),
        category=raw.fields.get("kind", ""),
        title=raw.fields.get("api_name", ""),
        description=raw.fields.get("description", ""),
        params_md=raw.fields.get("parameters_md", ""),
        returns=raw.fields.get("returns_json", ""),
        examples=list(raw.fields.get("examples", []) or []),
        body_md=raw.fields.get("source_markdown", ""),
        product_support=[],  # zvec 存的是 version_support，反推太复杂；留空
        signature=raw.fields.get("signature", ""),
        deprecated=bool(raw.fields.get("deprecated", False)),
        deprecation_note=raw.fields.get("deprecation_note", ""),
        ingested_at=int(raw.fields.get("ingested_at", 0) or 0),
    )
    return DirectorDoc(record=record, embedder=None)  # type: ignore[arg-type]


def count(collection: str) -> int:
    """返回指定 collection 的 doc 数。

    Args:
        collection: collection 名。

    Returns:
        doc 数。collection 不存在时按 zvec 行为（一般抛 CollectionNotFoundError）。
    """
    with CollectionSession(collection) as coll:
        return _low_count_docs(coll)
