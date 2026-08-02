"""Zvec 写入层：insert / delete / collection 开关。

职责：
- 把 doc.py 生成的 ``zvec.Doc`` 调 embedder 生成向量，合并成完整 doc，写入 Zvec。
- 暴露 batch 写入、collection 打开/创建、删除等基础操作。
- 业务层（ingest/）调这里；上层不要直接 import ``zvec``。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import zvec

from config import get_config
from src.dao.emb.embedder import Embedder
from src.dao.emb.exceptions import (
    CollectionNotFoundError,
    EmbedderError,
    NotSupportedError,
    SchemaMismatchError,
    SearchError,
)
from src.dao.emb.schema import (
    FIELD_DENSE_EMBEDDING,
    FIELD_SPARSE_EMBEDDING,
    get_collection_schema,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# collection 打开 / 创建
# ---------------------------------------------------------------------------

def collection_path(collection_name: str | None = None) -> str:
    """根据 config 拼出 collection 的绝对磁盘路径。

    公式：``{cfg.zvec.collection_path} / {collection_name}``，其中前者会
    ``expanduser()`` 处理 ``~``、``resolve()`` 转绝对。

    Returns:
        绝对路径字符串（不是 :class:`Path`，因为 zvec 接口要 str）。
    """
    cfg = get_config()
    name = collection_name or cfg.zvec.default_collection
    base = Path(cfg.zvec.collection_path).expanduser().resolve()
    return str(base / name)


#: zvec ``add_column`` 0.6 支持的 scalar 类型 → 默认 backfill 表达式。
#:
#: 关键约束（zvec 0.6 实际行为，2026-07 测过）：
#: - 仅 ``INT32 / INT64 / UINT32 / UINT64 / FLOAT / DOUBLE`` 6 个数值类型
#:   可以 ``add_column``；STRING / BOOL / ARRAY_* 都被拒（"Only support
#:   basic numeric data type"）。
#: - ``add_column`` 强制 ``nullable=True``（"Add column is not supported
#:   for non-nullable column"）。
#: - vector 字段不能用 ``add_column`` 加；只能 drop + recreate（数据丢）。
#:
#: 所以 C 端 schema 加了"非数值 / 非 nullable"字段时，auto-expand 会失败，
#: 见 :func:`_migrate_schema_if_needed` 的报错分支。
_AUTO_ADD_EXPRESSIONS: dict[zvec.DataType, str] = {
    zvec.DataType.INT32: "0",
    zvec.DataType.INT64: "0",
    zvec.DataType.UINT32: "0",
    zvec.DataType.UINT64: "0",
    zvec.DataType.FLOAT: "0.0",
    zvec.DataType.DOUBLE: "0.0",
}


def _migrate_schema_if_needed(
    coll: zvec.Collection,
    desired: zvec.CollectionSchema,
) -> None:
    """打开已存在的 collection 后, 兜底把 C 端 schema 比磁盘上多出来的字段补上。

    判定 / 处理规则（按"保数据 + 不擅自改旧字段"原则）：

    1. **C 端有 + 磁盘没 (scalar 数值类 + nullable)**:
       → 调 ``coll.add_column(field_schema_nullable=True, expression=...)``
       自动 backfill 旧 doc；新 doc 在 upsert 时正常带这个字段即可。

    2. **C 端有 + 磁盘没 (scalar 非数值 / 非 nullable)**:
       → 抛 :class:`SchemaMismatchError`。zvec 0.6 的 ``add_column`` 拒收
       这些类型 / 强制 nullable；自动迁移会失败。**正确做法**：让 schema
       模块把这些字段改成 nullable 数值类（如果语义允许），或人工迁移。

    3. **C 端有 + 磁盘没 (vector 字段)**:
       → 抛 :class:`SchemaMismatchError`。vector 字段的 backfill 需要重新
       embed 所有 doc, 这是 ingest 流水线的工作, 不能在打开时偷做。

    4. **磁盘有 + C 端没 (任何字段)**:
       → 记 WARN 日志，**不删**。删除字段是破坏性操作, 由管理员显式
       调 ``coll.drop_column(name)`` 处理。

    5. **同名字段, data_type / dimension 不一致**:
       → 抛 :class:`SchemaMismatchError`。这种属于"配置漂移"或"模型升级
       维度改了", 留给 ingest 流水线重建, 不能在打开时擅自改。

    Args:
        coll: 已经打开的（read-write）collection。
        desired: C 端期望的 schema（通常来自 :func:`get_collection_schema`）。

    Raises:
        SchemaMismatchError: 规则 2 / 3 / 5 任一触发。
    """
    cur = coll.schema
    cur_fields_by_name: dict[str, Any] = {f.name: f for f in cur.fields}
    cur_vectors_by_name: dict[str, Any] = {v.name: v for v in cur.vectors}
    desired_fields_by_name: dict[str, Any] = {f.name: f for f in desired.fields}
    desired_vectors_by_name: dict[str, Any] = {v.name: v for v in desired.vectors}

    # 1-3: C 端有 + 磁盘没 → add_column / 报错
    for name, f_desired in desired_fields_by_name.items():
        if name in cur_fields_by_name:
            continue  # 已存在, 走规则 5
        # 新字段
        expr = _AUTO_ADD_EXPRESSIONS.get(f_desired.data_type)
        if expr is None:
            # 非数值 / 不支持 add_column 的类型
            raise SchemaMismatchError(
                f"schema 演进失败: 字段 {name!r} 在 C 端新增, 类型 "
                f"{f_desired.data_type.name} 不被 zvec 0.6 add_column 支持 "
                f"(仅支持 nullable 数值类型)。需要人工迁移: drop collection "
                f"or 把字段改成 nullable numeric。"
            )
        # 强制 nullable（zvec 0.6 add_column 硬要求）
        f_nullable = zvec.FieldSchema(
            name=f_desired.name,
            data_type=f_desired.data_type,
            nullable=True,
            index_param=f_desired.index_param,  # 倒排索引参数照搬
        )
        logger.info(
            "zvec.schema.auto_add_field collection=%s field=%s type=%s expr=%s",
            coll.schema.name,
            name,
            f_desired.data_type.name,
            expr,
        )
        coll.add_column(
            field_schema=f_nullable,
            expression=expr,
            option=zvec.AddColumnOption(),
        )

    for name in desired_vectors_by_name:
        if name in cur_vectors_by_name:
            continue
        # vector 字段缺失
        raise SchemaMismatchError(
            f"schema 演进失败: vector 字段 {name!r} 在 C 端新增, 但磁盘上不存在。"
            f"vector 字段的 backfill 需要重新 embed 所有 doc, 必须在 ingest 流水线"
            f"里完成, 不能在打开 collection 时自动做。"
        )

    # 4: 磁盘有 + C 端没 → WARN
    for name in cur_fields_by_name:
        if name not in desired_fields_by_name:
            logger.warning(
                "zvec.schema.orphan_field collection=%s field=%s 磁盘上有但 C 端"
                " schema 期望中没有; 保留不删 (如需删除请人工调 drop_column)",
                cur.name,
                name,
            )
    for name in cur_vectors_by_name:
        if name not in desired_vectors_by_name:
            logger.warning(
                "zvec.schema.orphan_vector collection=%s vector=%s 磁盘上有但 C"
                " 端 schema 期望中没有; 保留不删 (删除 vector 字段会丢全部数据, "
                "请人工评估)",
                cur.name,
                name,
            )

    # 5: 同名字段 type / dim 不一致 → 报错
    for name, f_desired in desired_fields_by_name.items():
        f_cur = cur_fields_by_name.get(name)
        if f_cur is None:
            continue  # 上面 add_column 过了
        if f_cur.data_type != f_desired.data_type:
            raise SchemaMismatchError(
                f"schema 演进失败: 字段 {name!r} 类型不一致 "
                f"(disk={f_cur.data_type.name}, c-side={f_desired.data_type.name})。"
                f"需要人工迁移 (drop + recreate 会丢数据)。"
            )
    for name, v_desired in desired_vectors_by_name.items():
        v_cur = cur_vectors_by_name.get(name)
        if v_cur is None:
            continue
        if v_cur.data_type != v_desired.data_type or v_cur.dimension != v_desired.dimension:
            raise SchemaMismatchError(
                f"schema 演进失败: vector 字段 {name!r} 定义不一致 "
                f"(disk={v_cur.data_type.name}/dim={v_cur.dimension}, "
                f"c-side={v_desired.data_type.name}/dim={v_desired.dimension})。"
                f"维度变更需要重新 embed 所有 doc。"
            )


def open_or_create_collection(
    collection_name: str | None = None,
    schema: zvec.CollectionSchema | None = None,
    *,
    read_only: bool = False,
) -> zvec.Collection:
    """打开现有 collection；不存在则用给定 schema 创建。

    行为：
    - 目录**不在**:
      - ``read_only=False`` → ``zvec.create_and_open(path, schema)``。
      - ``read_only=True`` → 抛 :class:`CollectionNotFoundError`（read-only
        句柄不能用于创建）。
    - 目录**在**:
      - ``read_only=False`` → ``zvec.open(path)``（读写模式），
        走 :func:`_migrate_schema_if_needed` 兜底迁移 C 端新增字段。
      - ``read_only=True`` → ``zvec.open(path, option=CollectionOption(
        enable_mmap=1, read_only=1))``，仅比对 schema 不迁移。
        如有 schema 漂移会**记 WARN 日志**（read-only 写不了, 不能修）。

    Args:
        collection_name: 留空用 :attr:`ZvecConfig.default_collection`。
        schema: 留空用 :func:`get_collection_schema(collection_name)`。
        read_only: True → 拿 read-only + mmap 句柄（不能 upsert/delete/
            update/add_column；只允许 search / fetch / count）。**搜索路径
            推荐用这个**，避免被误调写操作。默认 False（read-write）。

    Returns:
        :class:`zvec.Collection` 句柄。

    Raises:
        CollectionNotFoundError: ``read_only=True`` 但目录不在；
            或目录在但打开失败。
        SchemaMismatchError: schema 漂移（见 :func:`_migrate_schema_if_needed`）。
    """
    name = collection_name or get_config().zvec.default_collection
    path = collection_path(name)
    if schema is None:
        schema = get_collection_schema(name)

    if not os.path.isdir(path):
        if read_only:
            raise CollectionNotFoundError(
                f"collection 不存在（read_only=True 不能创建）: {path}"
            )
        return zvec.create_and_open(path=path, schema=schema)

    # 存在 → 打开
    if read_only:
        option = zvec.CollectionOption(enable_mmap=1, read_only=1)
        coll = zvec.open(path=path, option=option)
        # read-only: 比对 schema 但不迁移, 漂移只记 WARN
        cur = coll.schema
        cur_field_names = {f.name for f in cur.fields}
        cur_vector_names = {v.name for v in cur.vectors}
        missing_fields = {f.name for f in schema.fields} - cur_field_names
        missing_vectors = {v.name for v in schema.vectors} - cur_vector_names
        if missing_fields or missing_vectors:
            logger.warning(
                "zvec.schema.read_only_drift collection=%s missing_fields=%s "
                "missing_vectors=%s; read-only 句柄不能补, 写入方走 "
                "open_or_create_collection() 即可自动迁移",
                cur.name,
                sorted(missing_fields),
                sorted(missing_vectors),
            )
        return coll

    # 读写模式 + 兜底迁移
    try:
        coll = zvec.open(path=path)
    except Exception as e:
        raise CollectionNotFoundError(
            f"collection 目录存在但打开失败: {path} ({e})"
        ) from e
    _migrate_schema_if_needed(coll, schema)
    return coll


def open_collection(
    collection_name: str | None = None,
    *,
    read_only: bool = False,
) -> zvec.Collection:
    """只打开现有 collection；不存在抛 :class:`CollectionNotFoundError`。

    与 :func:`open_or_create_collection` 的区别：本函数**不创建**、**不
    迁移**，仅"打开"。适合"我知道 collection 一定在, 我只是要个句柄"的场景。

    Args:
        collection_name: 留空用 :attr:`ZvecConfig.default_collection`。
        read_only: True → 拿 read-only + mmap 句柄（防止误调写操作）；
            False（默认）→ 读写句柄。

    Returns:
        :class:`zvec.Collection` 句柄。

    Raises:
        CollectionNotFoundError: 目录不在。
    """
    name = collection_name or get_config().zvec.default_collection
    path = collection_path(name)
    if not os.path.isdir(path):
        raise CollectionNotFoundError(f"collection 不存在: {path}")
    if read_only:
        option = zvec.CollectionOption(enable_mmap=1, read_only=1)
        return zvec.open(path=path, option=option)
    return zvec.open(path=path)


# ---------------------------------------------------------------------------
# Doc 准备：把向量塞进 doc
# ---------------------------------------------------------------------------

def _attach_vectors(
    doc: zvec.Doc,
    embedder: Embedder,
    dense_text: str | None = None,
    sparse_text: str | None = None,
) -> zvec.Doc:
    """用 embedder 生成 dense + sparse 向量，**返回新的**带 vectors 的 :class:`zvec.Doc`。

    dense_text / sparse_text 默认拼法（与 architecture 约定一致）：
    - dense:  ``description + api_name + name``（语义重心）
    - sparse: ``api_id + signature + name``（词面重心）

    行为：
    - dense_text 或 sparse_text 为空 → :class:`EmbedderError`（**不**调用
      embedder，免得拿不到向量还装作成功）。
    - 调 embedder 失败 → 包成 :class:`EmbedderError`。
    - 调 embedder 成功 → 复制原 fields（保留任何元信息）+ 塞 vectors，构造
      新 :class:`zvec.Doc` 返回；**不**改入参。

    Args:
        doc: 来自 :func:`src.dao.emb.doc.from_orm` 的 doc（fields 已填，vectors 空）。
        embedder: 任何 :class:`Embedder` ABC 实现。
        dense_text: 覆盖默认 dense 输入文本（一般测试用）。
        sparse_text: 覆盖默认 sparse 输入文本（一般测试用）。

    Returns:
        新的 :class:`zvec.Doc`，带 ``dense_embedding`` 和 ``sparse_embedding``。
    """
    if dense_text is None:
        # 按你定的规则：description + api_name + name
        f = doc.fields
        dense_text = " ".join([
            f.get("description", ""),
            f.get("api_name", ""),
            f.get("name", ""),
        ]).strip()
    if sparse_text is None:
        # 按你定的规则：api_id + signature + name
        f = doc.fields
        sparse_text = " ".join([
            f.get("api_id", ""),
            f.get("signature", ""),
            f.get("name", ""),
        ]).strip()

    if not dense_text:
        raise EmbedderError(f"doc {doc.id} dense_text 为空，无法生成向量")
    if not sparse_text:
        raise EmbedderError(f"doc {doc.id} sparse_text 为空，无法生成稀疏向量")

    try:
        dense_vec = embedder.embed_dense(dense_text, mode="document")
        sparse_vec = embedder.embed_sparse(sparse_text, mode="document")
    except EmbedderError:
        raise
    except Exception as e:
        raise EmbedderError(f"embedder 调用失败 for {doc.id}: {e}") from e

    return zvec.Doc(
        id=doc.id,
        fields=dict(doc.fields),
        vectors={
            FIELD_DENSE_EMBEDDING: dense_vec,
            FIELD_SPARSE_EMBEDDING: sparse_vec,
        },
    )


# ---------------------------------------------------------------------------
# 写入操作（低层 API — 直接传 open collection 句柄）
#
# 调用方需要自己管 collection 的 open/close；如要"传 collection 名 + DirectorDoc
# 自动 open"的便利入口，看 :mod:`src.dao.emb.director` 里的高层 CRUD。
# ---------------------------------------------------------------------------

def insert_doc(
    coll: zvec.Collection,
    doc: zvec.Doc,
    embedder: Embedder,
) -> None:
    """单条 embed + 写入。同 id 已存在则覆盖（Zvec upsert 语义）。

    注意：函数名 ``insert`` 是友好叫法，**不**是严格 SQL ``INSERT``。
    zvec 的写操作只有一种（``coll.upsert``），等价于"存在就覆盖、不存在就新增"。
    如果你需要严格 insert 语义（id 存在报错），调用方在调用前自己检查。

    流程：:func:`_attach_vectors` → ``coll.upsert(full_doc)``。
    zvec 内部行为：先到 flat buffer（**不**立刻建 HNSW 索引），后台会
    把 buffer flush 到 HNSW。如果需要"写完立刻能搜"，调用方在批 insert
    后等 :func:`src.dao.emb.wait_for_index_ready`。

    Raises:
        EmbedderError: embed 阶段失败。
        SearchError: zvec 写阶段失败（IO / 序列化等）。包装原始异常，
            不让 zvec 的内部错误类型泄漏到上层。
    """
    full_doc = _attach_vectors(doc, embedder)
    try:
        coll.upsert(full_doc)
    except EmbedderError:
        raise
    except Exception as e:
        raise SearchError(f"zvec insert failed for {doc.id}: {e}") from e


def insert_batch(
    coll: zvec.Collection,
    docs: Iterable[zvec.Doc],
    embedder: Embedder,
) -> dict:
    """批量 embed + 写入。

    行为：
    - **串行** embed → 串行 insert（Zvec 写是单进程独占，串行更安全）。
      如果将来 dense embedder 支持 batch 调用，可以改成真 batch 加速。
    - 单条失败不影响其他 doc；embed 失败和 zvec 失败都被捕获。
    - 真·未知异常（既不是 :class:`EmbedderError` 也不是 :class:`SearchError`）
      包装成 "unexpected: ..." 字符串再记进 errors。

    Args:
        coll: 已打开的 :class:`zvec.Collection`。
        docs: 待写入的 doc 列表（任意 Iterable，会一次性 materialize）。
        embedder: 任何 :class:`Embedder`。

    Returns:
        ``{"ok": int, "fail": int, "errors": list[(doc_id, str)]}``
        - ``ok``: 成功数。
        - ``fail``: 失败数。
        - ``errors``: 失败列表，每条 ``(doc_id, error_message)``。
    """
    ok, fail, errors = 0, 0, []
    docs_list = list(docs)  # 一次性 materialize，避免迭代两次

    # 串行 embed → 串行 insert（Zvec 写是单进程独占，串行更安全）
    # （如果以后 dense embedder 支持 batch，可以在这里改成 batch 调用）
    for doc in docs_list:
        try:
            full_doc = _attach_vectors(doc, embedder)
            coll.upsert(full_doc)
            ok += 1
        except (EmbedderError, SearchError) as e:
            fail += 1
            errors.append((doc.id, str(e)))
        except Exception as e:
            # 真·未知异常：包装成 SearchError 再记录，避免上层看到裸 Exception
            fail += 1
            errors.append((doc.id, f"unexpected: {e}"))

    return {"ok": ok, "fail": fail, "errors": errors}


def delete_doc(coll: zvec.Collection, doc_id: str) -> None:
    """按 id **物理**删除单条 doc。

    ⚠️ zvec 的 delete 是**物理**删除，没有 archived / soft-delete 概念。
    如果你需要"软删"（保留数据可恢复），在 ORM 层做（设置 ``archived_at``），
    然后**不要**调本函数；调 :func:`insert_doc` 把 doc 改成"废弃"状态。
    """
    coll.delete(ids=doc_id)


def delete_batch(coll: zvec.Collection, doc_ids: Iterable[str]) -> dict:
    """批量物理删除。

    ⚠️ 同 :func:`delete_doc`，这是**物理**删除。zvec 0.6 对不存在的 id
    静默成功（不抛异常），所以 ``ok`` 会包含这些"实际没删"的 id。

    Returns:
        ``{"ok": int, "fail": int, "errors": list[(doc_id, str)]}``
    """
    ok, fail = 0, 0
    errors: list[tuple[str, str]] = []
    for did in doc_ids:
        try:
            coll.delete(ids=did)
            ok += 1
        except Exception as e:
            fail += 1
            errors.append((did, str(e)))
    return {"ok": ok, "fail": fail, "errors": errors}


# ---------------------------------------------------------------------------
# 读操作：fetch / count / list_ids
# ---------------------------------------------------------------------------

def fetch_doc(coll: zvec.Collection, doc_id: str, *, include_vector: bool = True) -> Any:
    """按 id 查单条 doc（直接 lookup，无搜索/打分，无向量距离计算）。

    Args:
        coll: 已打开的 zvec.Collection。
        doc_id: 要查的 doc id。
        include_vector: 是否包含 vectors（默认 True，展示/验证用 False 省 IO）。

    Returns:
        zvec 0.6 返回的是 **Doc-like 对象**（带 ``.id`` / ``.fields`` /
        ``.vectors`` 属性），**不是 dict**。不存在返回 ``None``。

        ⚠️ 调用方访问字段时：
        - 用 ``doc.fields["xxx"]`` 或 ``getattr(doc, "xxx", default)``；
        - **不要**当 dict 用（``doc["namespace"]`` 会爆）。
    """
    raw = coll.fetch(ids=doc_id, include_vector=include_vector)
    if not raw:
        return None
    # zvec 0.6 fetch 返回 dict[str, Doc-like]
    return raw.get(doc_id)


def fetch_batch(
    coll: zvec.Collection,
    doc_ids: Iterable[str],
    *,
    include_vector: bool = True,
) -> dict[str, Any]:
    """批量 fetch。返回 ``{doc_id: Doc-like}``，不存在的 id 静默忽略（Zvec 语义）。

    Args:
        coll: 已打开的 zvec.Collection。
        doc_ids: 要查的 doc id 列表。
        include_vector: 是否包含 vectors（默认 True，验证存在性时可传 False）。

    提示：如果 ``doc_ids`` 很长，分批传（建议每批 ≤ 1000）以免一次性 IO 太大；
    zvec 0.6 对超长 id list 行为未定义。
    """
    ids = list(doc_ids)
    if not ids:
        return {}
    return dict(coll.fetch(ids=ids, include_vector=include_vector))


def count_docs(coll: zvec.Collection) -> int:
    """当前 collection 的 doc 数。直接从 ``coll.stats`` 读，**零 IO 开销**。

    注：``coll.stats`` 返回的是 :class:`CollectionStats` 对象（带
    ``doc_count`` / ``index_completeness`` 属性），**不是 dict**。
    """
    return int(coll.stats.doc_count)


def list_ids(coll: zvec.Collection) -> list[str]:
    """枚举 collection 内所有 doc id。

    ⚠️ **zvec 0.6 不支持原生枚举**（没有 list / iter / scan API）。本函数
    直接抛 :class:`NotSupportedError`。

    实际工作流：**用 ORM 作为真源**，遍历 ORM 的 ``chunk_id`` 列表调
    :func:`fetch_doc` / :func:`fetch_batch` 来对账。详见
    ``docs/architecture.md`` §4。

    后续 zvec 升级支持 list_all 后可以解除这个限制。
    """
    raise NotSupportedError(
        "zvec 0.6 不支持 list_all API。"
        "请遍历 ORM 的 chunk_id 列表调 fetch_doc / fetch_batch 来对账。"
    )


# ---------------------------------------------------------------------------
# 部分更新：update（Zvec 原生，**只改指定字段，其他不变**）
# ---------------------------------------------------------------------------

def update_doc(coll: zvec.Collection, doc: zvec.Doc) -> None:
    """部分更新：只改 ``doc`` 里显式给出的字段，其他保持不变。

    跟 :func:`insert_doc` 的关键区别：
    - ``insert_doc``: 完整替换 doc 全部 fields + vectors。
    - ``update_doc``: 只改 doc 里出现的 fields，其他原值保留。

    .. warning::
        **踩雷警告**：vectors 字段（包括 sparse）必须完整传，**不传视为 None 覆盖**。
        如果只想改 fields 不动 vectors，**必须**先用 :func:`fetch_doc` 拿完整 Doc，
        改完 fields 再 update；直接 ``zvec.Doc(id=..., fields={...})`` 不带 vectors
        会把向量清零 → 后续语义检索召回不到。

    Raises:
        SearchError: zvec update 阶段失败。
    """
    try:
        coll.update(doc)
    except Exception as e:
        raise SearchError(f"zvec update failed for {doc.id}: {e}") from e


def update_batch(coll: zvec.Collection, docs: Iterable[zvec.Doc]) -> list:
    """批量部分更新。返回每条 doc 的 zvec Status（具体类型看 zvec 版本）。

    ⚠️ 同样有"vectors 必传"的踩雷规则（见 :func:`update_doc`）。
    """
    return coll.update(list(docs))
