"""ORM 记录 → zvec.Doc 转换。

只做数据搬运，不计算向量（向量由 indexer 调用 embedder 后填上）。
通过 ApiDocumentLike Protocol 解耦 ORM 实现，doc.py 不依赖 core.models。
"""

from __future__ import annotations

import re
from typing import Protocol

import zvec

from config import get_config
from src.dao.emb.exceptions import DocBuildError
from src.dao.emb.schema import (
    FIELD_API_ID,
    FIELD_API_NAME,
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
    FIELD_VERSION,
    FIELD_VERSION_SUPPORT,
)

# ---------------------------------------------------------------------------
# 鸭子类型：ORM 文档的最小契约
# ---------------------------------------------------------------------------

class ApiDocumentLike(Protocol):
    """doc.py 只看这个接口。任何满足形状的对象都能传 :func:`from_orm`。

    字段来源：上游 ingest 步骤产出的 ORM 记录（参见 ``ingest/skills/api-doc-extractor``）。
    命名沿用 ingest 侧已有的字段名（``category`` / ``returns`` / ``params_md``
    等），不强行映射到 zvec schema 字段名。

    必填字段：``chunk_id`` / ``name`` / ``namespace`` / ``language`` /
    ``category`` / ``title`` / ``description`` / ``params_md`` / ``returns`` /
    ``examples`` / ``body_md`` / ``product_support``。
    可选字段（带默认值）：``signature`` / ``deprecated`` / ``deprecation_note`` /
    ``ingested_at``。
    """
    chunk_id: str
    name: str
    namespace: str
    language: str
    category: str
    title: str
    description: str
    params_md: str
    returns: str
    examples: list[str]
    body_md: str
    product_support: list[dict]

    # 可选字段（带默认值）
    signature: str
    deprecated: bool
    deprecation_note: str
    ingested_at: int


# ---------------------------------------------------------------------------
# 字段提取规则（与 config.api_name.strip_pattern 配套）
# ---------------------------------------------------------------------------

#: 从 ``body_md`` 抓 ``#### 函数原型`` 段落的正则。
#: 匹配范围：从 ``#### 函数原型`` 头开始，到下一个 ``####`` 头或文档末尾。
#: 这是"硬编码"规则：依赖 ingest 输出的固定章节结构。如果将来 ingest 改了
#: 章节写法（比如换 ``###`` 或换英文 "Function Signature"），这里会失效 —
#: 需要同步更新。
_SIG_RE = re.compile(
    r"####[^\S\r\n]*函数原型[^\S\r\n]*\r?\n(.*?)(?=\r?\n####|\Z)",
    re.DOTALL,
)


def extract_api_name(record: ApiDocumentLike) -> str:
    """从 ``title`` 字段去掉 ``"{name} {namespace} "`` 前缀，得到 LLM 友好的 api_name。

    例：
        title    = "DataStoreBarrier com.huawei.cann.ascendc.op.910beta3 数据同步..."
        name     = "DataStoreBarrier"
        namespace = "com.huawei.cann.ascendc.op.910beta3"
        → "数据同步..."

    行为：
    - 用 :attr:`ZvecConfig.strip_pattern` 拼前缀（默认 ``"{name} {namespace} "``）。
    - 匹配上 → 去掉前缀并 ``strip()``。
    - 匹配不上（格式变了 / title 异常）→ fallback 用整段 title；不抛错。
      这样新 ingest 格式不会让 zvec 写入挂掉，只是 api_name 字段不干净。
    """
    cfg = get_config()
    prefix = cfg.api_name.strip_pattern.format(name=record.name, namespace=record.namespace)
    if record.title.startswith(prefix):
        return record.title[len(prefix):].strip()
    # fallback: title 整个用
    return record.title.strip()


def extract_version_from_namespace(namespace: str) -> str:
    """从 namespace 提取 version（取最后一段）。

    约定：namespace 形如 ``"org.product.api.{version}"``。
    例：
        ``"com.huawei.cann.ascendc.op.910beta3"`` → ``"910beta3"``
        ``"com.huawei.cann.ascendc.op"``         → ``"op"``（退化，不抛错）

    空 namespace 返空串（**不**抛错，调用方自己决定怎么处理）。
    """
    if not namespace:
        return ""
    return namespace.rsplit(".", 1)[-1]


def extract_version_support(record: ApiDocumentLike) -> list[str]:
    """从 :attr:`product_support` 列表里只取 ``supported=True`` 的产品名。

    输入格式：``[{"product": "910B", "supported": True}, {"product": "310P", "supported": False}]``
    输出：``["910B"]``（只留 supported 的）。

    如果 ``product_support`` 为 None / 字段缺失 → 返空 list，不抛错。
    """
    out: list[str] = []
    for item in record.product_support or []:
        if item.get("supported"):
            p = item.get("product")
            if p:
                out.append(p)
    return out


def extract_signature(record: ApiDocumentLike) -> str:
    """从 ORM 拿 signature，没有就尝试从 body_md 抓 ``#### 函数原型`` 段。

    抓不到就返空串（**不**抛错）。空串会让 :func:`src.dao.emb.indexer._attach_vectors`
    的 sparse_text 退化成 ``api_id + name``，精度下降但仍能跑。
    """
    if record.signature:
        return record.signature
    if not record.body_md:
        return ""
    m = _SIG_RE.search(record.body_md)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def from_orm(record: ApiDocumentLike) -> zvec.Doc:
    """ORM 记录 → :class:`zvec.Doc`（**不含向量**，由 indexer 调用 embedder 后填上）。

    字段映射：
    - 身份/路由字段：``chunk_id`` → ``id`` + ``api_id``；``namespace`` → ``namespace``；
      ``name`` → ``name``；``language`` → ``language``；``category`` → ``kind``。
    - 派生字段：``namespace`` → ``version``（最后一段）；``title`` → ``api_name``
      （去前缀）；``product_support`` → ``version_support``（只留 supported=True）。
    - 载荷字段：``description`` / ``signature``（或 body_md 抓的）/ ``params_md`` /
      ``returns`` / ``examples`` / ``body_md`` / ``deprecation_note`` 直接搬运。
    - ``ingested_at`` 强制 ``int(...)``；为 0 / 缺失 → 0。

    行为：
    - 所有"可选"字段（None / 空）都做兜底转成默认值（空串 / 空 list / False /
      0），保证 :class:`zvec.Doc` 构造不出错。
    - **不**算向量；向量在 :func:`src.dao.emb.indexer._attach_vectors` 里
      调 embedder 后填。

    Raises:
        DocBuildError: :class:`zvec.Doc` 构造抛任何异常时包装抛出（带
            ``chunk_id`` 上下文）。常见原因：字段类型严重不对（应该是 str
            的地方给了 dict 等）。
    """
    try:
        return zvec.Doc(
            id=record.chunk_id,
            fields={
                FIELD_NAMESPACE: record.namespace,
                FIELD_API_ID: record.chunk_id,
                FIELD_NAME: record.name,
                FIELD_API_NAME: extract_api_name(record),
                FIELD_VERSION: extract_version_from_namespace(record.namespace),
                FIELD_KIND: record.category,
                FIELD_LANGUAGE: record.language,
                FIELD_VERSION_SUPPORT: extract_version_support(record),
                FIELD_DEPRECATED: bool(record.deprecated),
                FIELD_INGESTED_AT: int(record.ingested_at or 0),
                FIELD_DESCRIPTION: record.description or "",
                FIELD_SIGNATURE: extract_signature(record),
                FIELD_PARAMETERS_MD: record.params_md or "",
                FIELD_RETURNS_JSON: str(record.returns) if record.returns else "",
                FIELD_EXAMPLES: list(record.examples or []),
                FIELD_SOURCE_MARKDOWN: record.body_md or "",
                FIELD_DEPRECATION_NOTE: record.deprecation_note or "",
            },
        )
    except Exception as e:
        raise DocBuildError(
            f"failed to build zvec.Doc from ORM record {getattr(record, 'chunk_id', '?')}: {e}"
        ) from e
