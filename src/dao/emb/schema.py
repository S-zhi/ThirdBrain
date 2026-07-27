"""Zvec CollectionSchema 定义。

设计原则（详见 docs/architecture.md）：
- 一条 doc = 一个具体 API 实体（函数/类/枚举/异常等），不是一段连续文本。
- 🔑 元信息字段 = 高选择性 + 必建倒排索引（namespace 路由、版本过滤、语言过滤）。
- 📦 载荷字段 = 大文本/JSON，不建索引，只在召回后塞进 Context Package。
- 🎯 向量字段 = dense（语义）+ sparse（词面）双路，召回时 RRF 融合。
"""

from __future__ import annotations

import zvec

from config import get_config


# ---------------------------------------------------------------------------
# 向量字段名常量（doc.py / embedder.py / indexer.py / searcher.py 共用）
# ---------------------------------------------------------------------------

#: dense 向量字段名。在 doc 里以 ``vectors["dense_embedding"]`` 形式存。
FIELD_DENSE_EMBEDDING = "dense_embedding"
#: sparse 向量字段名。同上，``vectors["sparse_embedding"]``。
FIELD_SPARSE_EMBEDDING = "sparse_embedding"

# 🔑 元信息字段（必建倒排索引，用于过滤 / 路由）
FIELD_NAMESPACE = "namespace"
FIELD_API_ID = "api_id"
FIELD_NAME = "name"
FIELD_VERSION = "version"
FIELD_KIND = "kind"
FIELD_LANGUAGE = "language"
FIELD_VERSION_SUPPORT = "version_support"
FIELD_DEPRECATED = "deprecated"

# 🕓 生命周期
FIELD_INGESTED_AT = "ingested_at"

# 📦 载荷字段（不建索引，只在召回后塞进 Context Package）
FIELD_API_NAME = "api_name"          # LLM 友好名（人类可读）
FIELD_DESCRIPTION = "description"
FIELD_SIGNATURE = "signature"
FIELD_PARAMETERS_MD = "parameters_md"
FIELD_RETURNS_JSON = "returns_json"
FIELD_EXAMPLES = "examples"
FIELD_SOURCE_MARKDOWN = "source_markdown"
FIELD_DEPRECATION_NOTE = "deprecation_note"


# ---------------------------------------------------------------------------
# Schema 构造
# ---------------------------------------------------------------------------

def _build_scalar_fields() -> list[zvec.FieldSchema]:
    """构造所有 17 个标量字段。

    字段分类：
    - 🔑 **元信息**（8 个）：建倒排索引，用于召回前过滤和按 namespace 路由。
      - ``namespace`` / ``api_id`` / ``name`` / ``version`` / ``kind``:
        强制建倒排索引；召回前先按这些字段 hard filter 缩范围。
      - ``language``: 单值 STRING（不是 ARRAY_STRING），按设计。
      - ``version_support``: ARRAY_STRING，多产品适配。
      - ``deprecated``: BOOL；检索默认 ``deprecated=false`` 强制过滤。
    - 🕓 **生命周期**（1 个）：``ingested_at`` INT64，开启 range 优化，方便按
      时间范围扫。
    - 📦 **载荷**（8 个）：``api_name`` / ``description`` / ``signature`` /
      ``parameters_md`` / ``returns_json`` / ``examples`` /
      ``source_markdown`` / ``deprecation_note``，全部不建索引，只在召回后
      随 doc 一起返回给 LLM。``source_markdown`` 可能很大（10K+ char），
      内存敏感场景下需要考虑单独存对象存储。

    Returns:
        17 个 :class:`zvec.FieldSchema` 列表，顺序固定（schema 演进时**只追加**，
        不要重排已有字段的位置）。
    """
    return [
        # ===== 🔑 元信息字段（必建倒排索引）=====
        zvec.FieldSchema(
            name=FIELD_NAMESPACE,
            data_type=zvec.DataType.STRING,
            index_param=zvec.InvertIndexParam(),
        ),
        zvec.FieldSchema(
            name=FIELD_API_ID,
            data_type=zvec.DataType.STRING,
            index_param=zvec.InvertIndexParam(),
        ),
        zvec.FieldSchema(
            name=FIELD_NAME,
            data_type=zvec.DataType.STRING,
            index_param=zvec.InvertIndexParam(),
        ),
        zvec.FieldSchema(
            name=FIELD_VERSION,
            data_type=zvec.DataType.STRING,
            index_param=zvec.InvertIndexParam(),
        ),
        zvec.FieldSchema(
            name=FIELD_KIND,
            data_type=zvec.DataType.STRING,
            index_param=zvec.InvertIndexParam(),
        ),
        zvec.FieldSchema(
            name=FIELD_LANGUAGE,
            data_type=zvec.DataType.STRING,  # 单值（不是 ARRAY_STRING）
            index_param=zvec.InvertIndexParam(),
        ),
        zvec.FieldSchema(
            name=FIELD_VERSION_SUPPORT,
            data_type=zvec.DataType.ARRAY_STRING,
            index_param=zvec.InvertIndexParam(enable_range_optimization=False),
        ),
        zvec.FieldSchema(
            name=FIELD_DEPRECATED,
            data_type=zvec.DataType.BOOL,
            index_param=zvec.InvertIndexParam(),
        ),

        # ===== 🕓 生命周期 =====
        zvec.FieldSchema(
            name=FIELD_INGESTED_AT,
            data_type=zvec.DataType.INT64,
            index_param=zvec.InvertIndexParam(enable_range_optimization=True),
        ),

        # ===== 📦 载荷字段（不建索引）=====
        zvec.FieldSchema(
            name=FIELD_API_NAME,
            data_type=zvec.DataType.STRING,
            # 不建索引
        ),
        zvec.FieldSchema(
            name=FIELD_DESCRIPTION,
            data_type=zvec.DataType.STRING,
        ),
        zvec.FieldSchema(
            name=FIELD_SIGNATURE,
            data_type=zvec.DataType.STRING,
        ),
        zvec.FieldSchema(
            name=FIELD_PARAMETERS_MD,
            data_type=zvec.DataType.STRING,
        ),
        zvec.FieldSchema(
            name=FIELD_RETURNS_JSON,
            data_type=zvec.DataType.STRING,
        ),
        zvec.FieldSchema(
            name=FIELD_EXAMPLES,
            data_type=zvec.DataType.ARRAY_STRING,
        ),
        zvec.FieldSchema(
            name=FIELD_SOURCE_MARKDOWN,
            data_type=zvec.DataType.STRING,
        ),
        zvec.FieldSchema(
            name=FIELD_DEPRECATION_NOTE,
            data_type=zvec.DataType.STRING,
        ),
    ]


def _build_vector_fields() -> list[zvec.VectorSchema]:
    """构造两个向量字段：dense + sparse。

    维度从 config 读（``cfg.embedder.bailian.dimension`` 或
    ``cfg.embedder.local.dimension``），不写死在代码里 — 但**调用方需要保证
    config 里的维度与实际 embedder 模型输出一致**，否则 :class:`BailianEmbedder`
    / :class:`LocalEmbedder` 会在 embed 时抛 :class:`EmbedderError`。

    度量距离：
    - dense 用 COSINE（绝大多数 dense 模型都是 cos 距离）。
    - sparse 用 IP（sparse 向量必须用 IP 度量，否则召回质量无法保证）。

    Returns:
        2 个 :class:`zvec.VectorSchema` 列表：[dense, sparse]。
    """
    cfg = get_config()
    if cfg.embedder.type == "bailian":
        dim = cfg.embedder.bailian.dimension
    else:
        dim = cfg.embedder.local.dimension

    return [
        zvec.VectorSchema(
            name=FIELD_DENSE_EMBEDDING,
            data_type=zvec.DataType.VECTOR_FP32,  # FP32 基线（先保证召回质量）
            dimension=dim,
            index_param=zvec.HnswIndexParam(
                metric_type=zvec.MetricType.COSINE,  # 绝大多数 dense 模型都是 COSINE
            ),
        ),
        zvec.VectorSchema(
            name=FIELD_SPARSE_EMBEDDING,
            data_type=zvec.DataType.SPARSE_VECTOR_FP32,
            index_param=zvec.HnswIndexParam(
                metric_type=zvec.MetricType.IP,  # sparse 必须用 IP
            ),
        ),
    ]


def get_collection_schema(name: str | None = None) -> zvec.CollectionSchema:
    """构造完整的 :class:`zvec.CollectionSchema`（含 17 标量 + 2 向量）。

    Args:
        name: collection 名；不传则用 :attr:`ZvecConfig.default_collection`。
            如果 name 已存在但 schema 不一致，Zvec 自己会处理（具体行为看
            zvec 版本；通常要手动迁移）。

    Returns:
        :class:`zvec.CollectionSchema` 实例。

    Note:
        这个函数**只构造** schema，不会创建 / 打开 collection。创建/打开
        见 :func:`src.dao.emb.indexer.open_or_create_collection`。
    """
    cfg = get_config()
    if name is None:
        name = cfg.zvec.default_collection

    return zvec.CollectionSchema(
        name=name,
        fields=_build_scalar_fields(),
        vectors=_build_vector_fields(),
    )
