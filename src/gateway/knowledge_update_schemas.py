"""LLM Knowledge Wiki 写入 Gateway 的请求与错误契约。"""

from __future__ import annotations

from pydantic import Field

from src.gateway.schemas import GatewayModel
from src.knowledge import UpdateOptions, UpdateResult, WikiUpdateInput


class KnowledgeUpdateRequest(GatewayModel):
    """一次独立 Wiki 更新请求。

    ``wiki`` 是领域层的完整写入输入，``options`` 描述本次编译和发布行为。
    Gateway 不接受底层 RAG 的 collection 句柄；领域模型中保留的来源标识只
    是文档元数据，不会触发对外部检索器的调用。
    """

    wiki: WikiUpdateInput
    options: UpdateOptions = Field(default_factory=UpdateOptions)


class KnowledgeUpdateGatewayError(GatewayModel):
    """写入服务不可用或执行失败时的脱敏错误响应。"""

    code: str
    message: str
    request_id: str
    operation_id: str | None = None


__all__ = [
    "KnowledgeUpdateGatewayError",
    "KnowledgeUpdateRequest",
    "UpdateResult",
]
