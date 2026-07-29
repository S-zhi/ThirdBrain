"""定义 direct 与 RAG 回答流程共享的类型。"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class FlowAnswer:
    """保存流程生成的回答正文及可审计元数据。"""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class AnswerFlow(Protocol):
    """定义单条 case 回答流程的公共契约。"""

    name: str

    def answer(
        self,
        *,
        question: str,
        record: dict[str, Any],
        max_output_chars: int,
    ) -> FlowAnswer:
        """根据单条 question 返回回答，不复用任何历史消息。"""
        ...
