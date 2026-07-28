"""src.dao.emb 异常体系。

所有 emb 模块抛出的异常都继承自 :class:`EmbError`，调用方可以一个
``except EmbError`` 兜底。细分异常用于区分失败原因。

调用方使用建议：
- Embedding 模型相关 → :class:`EmbedderError`（可能是 4xx / 5xx / 维度不匹配）。
- Zvec 读写相关 → :class:`SearchError`（用于通用的 IO / 序列化错误；命名沿用历史）。
- 写错 schema 字段 → :class:`SchemaMismatchError`。
- 操作在当前 backend 不支持 → :class:`NotSupportedError`。
- collection 路径不存在 → :class:`CollectionNotFoundError`。
- ORM → zvec.Doc 转换失败 → :class:`DocBuildError`。
- :class:`ConfigError` 是顶层 Exception（继承 :class:`Exception`，**不**继承
  :class:`EmbError`），因为配置异常可能在任何地方抛。
"""

from __future__ import annotations

# config.ConfigError 是全局唯一的（避免重复定义导致 except 抓不到跨模块异常）
from config import (
    ConfigError,  # type: ignore[unused-ignore]  # noqa: F401  重新导出，方便调用方统一 import
)


class EmbError(Exception):
    """emb 模块的基础异常。"""


class EmbedderError(EmbError):
    """Embedding 生成失败。

    触发场景：
    - dashscope 模块未安装（在 :class:`BailianEmbedder` 构造时抛）。
    - DashScope 4xx（立即抛、不重试）。
    - DashScope 5xx（重试耗尽后抛）。
    - 网络层异常（ConnectionError / 超时）重试耗尽后包装抛。
    - 返回的向量维度与配置不匹配。
    - :class:`LocalEmbedder` 构造时 sentence-transformers 缺失。
    """


class SchemaMismatchError(EmbError):
    """Zvec schema 与 doc 字段不匹配（缺字段、类型错误、维度错误）。

    触发场景：
    - :func:`get_collection_schema` 拿到的 schema 和要写入的 doc 字段冲突。
    - dense / sparse 维度不匹配。
    """


class CollectionNotFoundError(EmbError):
    """指定的 Zvec collection 路径不存在。

    调用 :func:`src.dao.emb.indexer.open_collection` 时如果目录不在会抛这个；
    :func:`src.dao.emb.indexer.open_or_create_collection` 不会（会自动建）。
    """


class DocBuildError(EmbError):
    """ORM / Dict → :class:`zvec.Doc` 转换失败。

    通常是字段缺失（``name`` / ``chunk_id`` / ``namespace`` 等必填字段为 None
    或类型错），由 :func:`src.dao.emb.doc.from_orm` 在 try/except 里捕获后包装抛。
    """


class SearchError(EmbError):
    """检索 / 写入 Zvec 过程失败。

    触发场景：
    - :func:`src.dao.emb.searcher.search` 时 embed 调用失败。
    - :func:`src.dao.emb.indexer.insert_doc` / :func:`src.dao.emb.indexer.update_doc`
      时 zvec 写失败。
    - 其他 Zvec IO / 序列化错误。

    命名"Search"是历史原因（最初只有 search 会抛）；现在 insert / update 也用它。
    """


class NotSupportedError(EmbError):
    """底层 backend 不支持的操作。

    目前已知：
    - :func:`src.dao.emb.indexer.list_ids`：zvec 0.6 没有 list_all API。

    后续 zvec 升级后这条限制可能解除。
    """
