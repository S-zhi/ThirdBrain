"""检索层：精确名称、dense 语义与历史 hybrid 检索实现。

设计：
- 编排 dense + sparse 双路召回，在 Python 端用 RRF（k=60）合流。
- 默认强制 ``deprecated = false``，防 LLM 拿到过时 API。
- ``search_by_name`` 短路：query 像精确 API 名时直接走 ``name = ...`` 等值过滤，
  失败再试 ``api_id = ...``，两个都不命中才退化到 embedding 召回。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import zvec

from src.dao.emb.embedder import Embedder
from src.dao.emb.exceptions import SearchError
from src.dao.emb.schema import (
    FIELD_API_ID,
    FIELD_DENSE_EMBEDDING,
    FIELD_DEPRECATED,
    FIELD_LANGUAGE,
    FIELD_NAME,
    FIELD_NAMESPACE,
    FIELD_SPARSE_EMBEDDING,
    FIELD_VERSION,
)

# 形如 Python 标识符 / 蛇形命名 / 驼峰 — 看着像 API 名的简单启发式
#: 严格：必须以字母或下划线开头，后续是字母数字下划线，长度 ≤ 65。
#: 不接受带空格 / 点 / 短横线 / 中文 / 数字开头的输入。
_NAME_LIKE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,64}$")
#: 明显的"多词"或带空格的 query → 不走短路
_HAS_SPACE_RE = re.compile(r"\s")


def _esc(s: str) -> str:
    """转义 Zvec filter 字符串里的单引号和反斜杠。

    Zvec filter 用单引号包字符串字面量，里面再有单引号会破坏语法。
    反斜杠也转义避免反斜杠被当作转义字符。

    ⚠️ 不处理：换行符 / null 字节 / 控制字符。如果 caller 传了带换行的
    namespace，filter 可能会被 zvec 拒掉；上层应自己校验 namespace 格式。
    """
    return s.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class SearchQuery:
    """用户的一次检索意图。

    字段语义：
    - ``text``: 原始查询字符串（必填）。
    - ``namespace``: 限定 namespace；为空时**不**加 namespace 过滤，
      召回会跨 namespace 命中 — 当前架构约定（architecture §11.2）**强烈建议**
      caller 显式指定，避免跨 namespace 误召回。
    - ``version`` / ``language``: 同理，可选硬过滤。
    - ``include_deprecated``: 默认 ``False``，filter 自动加 ``deprecated=false``；
      真要看弃用 API 时才传 True。
    - ``topk``: 单路召回 top-K；RRF 融合后仍是 K 条（不算两路之和）。
    """
    text: str
    namespace: str | None = None        # 限定命名空间（architecture §11.2 强制要求）
    version: str | None = None          # 限定版本
    language: str | None = None         # 限定语言
    include_deprecated: bool = False    # 默认排除弃用
    topk: int = 10


@dataclass
class SearchResult:
    """单条召回结果。

    - ``doc_id``: zvec 内部 id（对应 :class:`zvec.Doc.id`，通常 = chunk_id）。
    - ``score``: RRF 融合后的分数（dense + sparse 累加），**不是**原始余弦距离。
    - ``fields``: doc 的 fields 字典（**已展开**为普通 dict）。
    """
    doc_id: str
    score: float
    fields: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 过滤字符串构造
# ---------------------------------------------------------------------------

def _build_filter(q: SearchQuery) -> str | None:
    """根据 :class:`SearchQuery` 拼出 Zvec filter 字符串。

    拼接规则（AND 关系）：
    - ``namespace`` 设了 → ``namespace = 'xxx'``
    - ``version`` 设了 → ``version = 'xxx'``
    - ``language`` 设了 → ``language = 'xxx'``
    - 没设 ``include_deprecated`` → ``deprecated = false``

    字符串字面量走 :func:`_esc` 转义。返回 ``None`` 表示"不过滤"（慎用，
    一般意味着会跨 namespace / 跨 version 召回）。

    Returns:
        filter 字符串（Zvec 表达式语法），或 ``None``。
    """
    parts: list[str] = []
    if q.namespace:
        parts.append(f"{FIELD_NAMESPACE} = '{_esc(q.namespace)}'")
    if q.version:
        parts.append(f"{FIELD_VERSION} = '{_esc(q.version)}'")
    if q.language:
        parts.append(f"{FIELD_LANGUAGE} = '{_esc(q.language)}'")
    if not q.include_deprecated:
        parts.append(f"{FIELD_DEPRECATED} = false")
    if not parts:
        return None
    return " AND ".join(parts)


def _require_versioned_scope(query: SearchQuery) -> None:
    """校验查询必须限定 namespace 与 version，禁止跨域召回。"""
    if not query.namespace or not query.namespace.strip():
        raise SearchError("query.namespace 不能为空")
    if not query.version or not query.version.strip():
        raise SearchError("query.version 不能为空")


def _join_filters(*parts: str | None) -> str:
    """以 AND 连接非空 Zvec 过滤条件。"""
    return " AND ".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# RRF（纯函数，从 searcher 里独立出来方便单测）
# ---------------------------------------------------------------------------

def rrf(
    results_lists: list[list[SearchResult]],
    k: int = 60,
) -> list[SearchResult]:
    """Reciprocal Rank Fusion。**纯函数**，方便单测。

    公式：``score(doc) = Σ 1 / (k + rank_i)``，rank 从 1 开始。

    行为：
    - 跨列表按 ``doc_id`` 去重。
    - ``fields`` 字段：取**第一次出现**时的 fields（后续列表里同一 doc 的
      fields 被忽略 — 是"first seen wins"语义，不是"merge"）。
    - score 越高的 doc 排在前面。
    - score 相同时保持输入顺序（稳定排序，依赖 :func:`sorted` 的稳定性）。

    Args:
        results_lists: 多路召回结果，每路是一个 :class:`SearchResult` 列表。
        k: RRF 平滑常数；k=60 是 RAG 社区常用值，越大"rank 靠后"权重衰减越慢。

    Returns:
        融合后的 :class:`SearchResult` 列表（按 score 降序）。
    """
    score_map: dict[str, float] = {}
    field_map: dict[str, dict[str, Any]] = {}
    for results in results_lists:
        for rank, r in enumerate(results, start=1):
            if r.doc_id not in score_map:
                score_map[r.doc_id] = 0.0
                field_map[r.doc_id] = r.fields
            score_map[r.doc_id] += 1.0 / (k + rank)

    sorted_ids = sorted(
        score_map.keys(),
        key=lambda did: (-score_map[did],),
    )
    return [
        SearchResult(
            doc_id=did,
            score=score_map[did],
            fields=field_map[did],
        )
        for did in sorted_ids
    ]


# ---------------------------------------------------------------------------
# 检索主函数
# ---------------------------------------------------------------------------

def search_by_name(
    coll: zvec.Collection,
    name: str,
    topk: int = 10,
) -> list[SearchResult]:
    """短路精确匹配：先 ``name = name``，失败再 ``api_id = name``。

    适用：用户输入形如 ``"DataStoreBarrier"``（函数名）或完整 chunk_id
    ``"com.huawei.cann.ascendc.op.910beta3.datastorebarrier"``。

    行为：
    - 输入先过 :data:`_NAME_LIKE_RE`（"看着像 identifier"）和 :data:`_HAS_SPACE_RE`
      启发式；不像就**直接返空**（**不**退化到 embedding 召回，避免误命中）。
    - 第一轮按 ``name`` 精确查；命中即返回（最多 ``topk`` 条）。
    - 第二轮按 ``api_id`` 精确查；命中返回。
    - 两轮都没命中 → 返空（让 caller 决定是否退化）。

    Returns:
        精确匹配结果（可能为空），**不**做排序（zvec 按索引自然顺序）。
    """
    if not _NAME_LIKE_RE.match(name) or _HAS_SPACE_RE.search(name):
        return []

    # 先试 name
    raw = coll.query(filter=f"{FIELD_NAME} = '{_esc(name)}'", topk=topk)
    res = _to_results(raw)
    if res:
        return res

    # 再试 api_id（用户可能输入完整 chunk_id）
    raw = coll.query(filter=f"{FIELD_API_ID} = '{_esc(name)}'", topk=topk)
    return _to_results(raw)


def search_exact_name(
    coll: zvec.Collection,
    query: SearchQuery,
) -> list[SearchResult]:
    """在强制版本范围内按 name 或完整 api_id 做单路精确查询。"""
    if not query.text or not query.text.strip():
        raise SearchError("query.text 不能为空")
    _require_versioned_scope(query)

    text = query.text.strip()
    identity_field = FIELD_API_ID if "." in text else FIELD_NAME
    identity_filter = f"{identity_field} = '{_esc(text)}'"
    flt = _join_filters(identity_filter, _build_filter(query))
    try:
        raw = coll.query(filter=flt, topk=query.topk)
    except Exception as error:
        raise SearchError(f"名称精确查询失败: {error}") from error
    return _to_results(raw)


def search_dense(
    coll: zvec.Collection,
    query: SearchQuery,
    embedder: Embedder,
) -> list[SearchResult]:
    """在强制版本范围内仅执行 dense 语义召回。"""
    if not query.text or not query.text.strip():
        raise SearchError("query.text 不能为空")
    _require_versioned_scope(query)

    try:
        dense_vector = embedder.embed_dense(query.text, mode="query")
    except Exception as error:
        raise SearchError(f"query embed 失败: {error}") from error

    try:
        raw = coll.query(
            queries=zvec.Query(
                field_name=FIELD_DENSE_EMBEDDING,
                vector=dense_vector,
            ),
            filter=_build_filter(query),
            topk=query.topk,
        )
    except Exception as error:
        raise SearchError(f"dense 召回失败: {error}") from error
    return _to_results(raw)


def search(
    coll: zvec.Collection,
    query: SearchQuery,
    embedder: Embedder,
) -> list[SearchResult]:
    """完整检索主流程：短路 → embed query → 双路召回 → RRF 合流。

    流程：
    1. 校验 ``query.text`` 非空（空 → :class:`SearchError`）。
    2. 拼 filter（:func:`_build_filter`）。
    3. **短路**：query 像 identifier → 调 :func:`search_by_name`，命中直接返回。
       短路没命中（比如大小写不对）→ 退化到 embedding 召回。
    4. embed query（``mode="query"``，与 doc 的 ``"document"`` mode 区分）。
    5. 双路召回（dense 必跑；sparse 为空 dict 时跳过 — zvec 拒收空 sparse 向量）。
    6. :func:`rrf` 融合。

    Returns:
        融合后的 :class:`SearchResult` 列表。

    Raises:
        SearchError: query.text 空 / embed 失败 / dense 或 sparse 召回失败。
    """
    if not query.text or not query.text.strip():
        raise SearchError("query.text 不能为空")

    flt = _build_filter(query)

    # 短路：query 像精确 API 名 → 先试 search_by_name，命中就直接返回
    if _NAME_LIKE_RE.match(query.text) and not _HAS_SPACE_RE.search(query.text):
        by_name = search_by_name(coll, query.text, topk=query.topk)
        if by_name:
            return by_name
        # 短路没命中（比如 chunk_id 是大写、用户输入小写），
        # 退化到 embedding 召回，避免漏召回

    # 1. embed query（按"query"模式，区别于 doc 的"document"模式）
    try:
        dense_vec = embedder.embed_dense(query.text, mode="query")
        sparse_vec = embedder.embed_sparse(query.text, mode="query")
    except Exception as e:
        raise SearchError(f"query embed 失败: {e}") from e

    # 2. 双路召回（sparse 为空时优雅降级为只跑 dense）
    results_lists: list[list[SearchResult]] = []

    try:
        raw_dense = coll.query(
            queries=zvec.Query(
                field_name=FIELD_DENSE_EMBEDDING,
                vector=dense_vec,
            ),
            filter=flt,
            topk=query.topk,
        )
        results_lists.append(_to_results(raw_dense))
    except Exception as e:
        raise SearchError(f"dense 召回失败: {e}") from e

    # sparse 召回：sparse_vec 为空时跳过（zvec 拒收空 sparse 向量）
    if sparse_vec:
        try:
            raw_sparse = coll.query(
                queries=zvec.Query(
                    field_name=FIELD_SPARSE_EMBEDDING,
                    vector=sparse_vec,
                ),
                filter=flt,
                topk=query.topk,
            )
            results_lists.append(_to_results(raw_sparse))
        except Exception as e:
            raise SearchError(f"sparse 召回失败: {e}") from e

    # 3. RRF 合流
    return rrf(results_lists, k=60)


# ---------------------------------------------------------------------------
# Zvec 返回值 → SearchResult 转换
# ---------------------------------------------------------------------------

def _to_results(raw: Any) -> list[SearchResult]:
    """把 zvec 的 query 返回值转成 :class:`SearchResult` 列表。

    兼容 zvec 多种返回 shape:
    - list of Doc-like  ← 最常见
    - dict[doc_id, Doc-like]  ← fetch 风格
    """
    if raw is None:
        return []

    # fetch 风格:{doc_id: doc-like}
    if isinstance(raw, dict) and all(
        isinstance(v, dict) or hasattr(v, "id") for v in raw.values()
    ):
        items = list(raw.values())
    elif isinstance(raw, (list, tuple)) or (hasattr(raw, "__iter__") and not isinstance(raw, (str, bytes, dict))):
        try:
            items = list(raw)
        except TypeError:
            return []
    else:
        return []

    out: list[SearchResult] = []
    for item in items:
        if isinstance(item, dict):
            doc_id = item.get("id") or item.get("doc_id") or getattr(item, "id", None)
            score = item.get("score") or 0.0
            fields = item.get("fields", {}) or {}
        else:
            doc_id = getattr(item, "id", None)
            score = getattr(item, "score", 0.0) or 0.0
            fields = getattr(item, "fields", None) or {}

        if doc_id is None:
            continue

        out.append(SearchResult(
            doc_id=str(doc_id),
            score=float(score),
            fields=dict(fields) if isinstance(fields, dict) else {},
        ))
    return out
