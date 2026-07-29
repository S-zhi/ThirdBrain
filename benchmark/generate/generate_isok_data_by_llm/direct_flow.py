"""实现不使用 RAG 的 MiniMax 直接回答流程。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .flow_types import FlowAnswer

ModelCall = Callable[..., str]


class DirectAnswerFlow:
    """使用单次无历史 MiniMax 请求直接回答 question。"""

    name = "direct"

    def __init__(self, model_call: ModelCall, prompt_path: Path) -> None:
        """注入模型函数并加载可编辑的直接回答提示词。"""
        self._model_call = model_call
        self._prompt_template = prompt_path.read_text(encoding="utf-8")

    def answer(
        self,
        *,
        question: str,
        record: dict[str, Any],
        max_output_chars: int,
    ) -> FlowAnswer:
        """独立调用一次模型并返回直接回答。"""
        del record
        prompt = self._prompt_template.format(
            question=question,
            max_output_chars=max_output_chars,
        )
        text = self._model_call(
            prompt=prompt,
            system_prompt="你是一个简洁、准确的编程问答助手，必须严格遵守字符数限制。",
        )
        return FlowAnswer(text=text, metadata={"generation_mode": self.name})
