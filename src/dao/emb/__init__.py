"""src.dao.emb — Zvec + Embedder 数据访问层。

模块列表：
- ``exceptions`` — 异常体系
- ``config``      — 读 ``config.yaml``（在项目根目录的 ``config.py``）
- ``schema``      — Zvec CollectionSchema 定义
- ``doc``         — ORM → zvec.Doc 转换
- ``embedder``    — Embedder ABC + Bailian + Local
- ``indexer``     — 写入层（upsert / delete）
- ``searcher``    — 检索层（search / search_by_name / RRF）
"""

from __future__ import annotations

import time

import zvec

from src.dao.emb.doc import (
    ApiDocumentLike,
    extract_api_name,
    extract_signature,
    extract_version_from_namespace,
    extract_version_support,
    from_orm,
)
from src.dao.emb.embedder import (
    BailianEmbedder,
    Embedder,
    LocalEmbedder,
    TFIDFSparseEncoder,
    build_embedder,
)
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
from src.dao.emb.indexer import (
    collection_path,
    count_docs,
    delete_batch,
    delete_doc,
    fetch_batch,
    fetch_doc,
    list_ids,
    open_collection,
    open_or_create_collection,
    update_batch,
    update_doc,
    upsert_batch,
    upsert_doc,
)
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
from src.dao.emb.searcher import (
    SearchQuery,
    SearchResult,
    rrf,
    search,
    search_by_name,
)


__all__ = [
    # exceptions
    "EmbError", "ConfigError", "EmbedderError", "SchemaMismatchError",
    "CollectionNotFoundError", "DocBuildError", "SearchError", "NotSupportedError",
    # schema
    "get_collection_schema",
    "FIELD_NAMESPACE", "FIELD_API_ID", "FIELD_NAME", "FIELD_API_NAME",
    "FIELD_VERSION", "FIELD_KIND", "FIELD_LANGUAGE", "FIELD_VERSION_SUPPORT",
    "FIELD_DEPRECATED", "FIELD_INGESTED_AT", "FIELD_DESCRIPTION",
    "FIELD_SIGNATURE", "FIELD_PARAMETERS_MD", "FIELD_RETURNS_JSON",
    "FIELD_EXAMPLES", "FIELD_SOURCE_MARKDOWN", "FIELD_DEPRECATION_NOTE",
    "FIELD_DENSE_EMBEDDING", "FIELD_SPARSE_EMBEDDING",
    # doc
    "ApiDocumentLike", "from_orm",
    "extract_api_name", "extract_signature",
    "extract_version_from_namespace", "extract_version_support",
    # embedder
    "Embedder", "BailianEmbedder", "LocalEmbedder", "TFIDFSparseEncoder",
    "build_embedder",
    # indexer
    "open_or_create_collection", "open_collection", "collection_path",
    "upsert_doc", "upsert_batch", "delete_doc", "delete_batch",
    "fetch_doc", "fetch_batch", "count_docs", "list_ids",
    "update_doc", "update_batch",
    # searcher
    "SearchQuery", "SearchResult", "search", "search_by_name", "rrf",
]


# ---------------------------------------------------------------------------
# 工具：阻塞等索引构建完成
# ---------------------------------------------------------------------------

def wait_for_index_ready(coll: zvec.Collection, timeout: float = 300.0) -> bool:
    """阻塞等待 coll 的所有 vector 字段 ``index_completeness == 1.0``。

    Zvec 设计：写入先到 flat buffer，调 ``coll.optimize()`` 后台建 HNSW。
    这个函数是给"写完想立刻搜"用的——通常测试或 ingest 流程末尾。

    ⚠️ **mock 行为**：
    - 如果 :attr:`coll.stats` 抛任何异常（zvec C++ binding 偶发）→ 返 ``False``。
    - 如果 ``stats.index_completeness`` 是空 dict（没有 vector 字段或未建）→ 立即返 ``True``（"已经就绪"的乐观假设）。
    - 如果超时 → 返 ``False``，调用方自己决定怎么办（重试 / 报错）。

    Returns:
        True = 在 timeout 内索引就绪；False = 超时或 stats 异常。
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            stats = coll.stats
        except Exception:
            return False
        completeness = stats.get("index_completeness", {}) if isinstance(stats, dict) else {}
        if not completeness:
            # 没有 vector 字段？认为就绪
            return True
        if all(v >= 1.0 for v in completeness.values()):
            return True
        time.sleep(0.5)
    return False
