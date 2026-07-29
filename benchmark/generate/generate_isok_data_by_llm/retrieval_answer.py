"""提供 web 与 rag_id 共用的无历史最终回答器。"""

from collections.abc import Callable
from pathlib import Path

ModelCall = Callable[..., str]


class RetrievalAnswerGenerator:
    """使用统一提示词根据本轮检索上下文生成最终回答。"""

    def __init__(self, model_call: ModelCall, prompt_path: Path) -> None:
        """注入 MiniMax 调用函数并加载统一回答提示词。"""
        self._model_call = model_call
        self._prompt_template = prompt_path.read_text(encoding="utf-8")

    def answer(
        self,
        *,
        question: str,
        context: str,
        source_label: str,
        max_output_chars: int,
    ) -> str:
        """通过全新请求回答问题，不传递查询规划或任何聊天历史。"""
        prompt = self._prompt_template.format(
            question=question,
            context=context,
            source_label=source_label,
            max_output_chars=max_output_chars,
        )
        return self._model_call(
            prompt=prompt,
            system_prompt=(
                "你是一个仅依据本轮检索资料回答问题的编程助手。"
                "这是独立请求，不得假设存在任何历史消息。"
            ),
        )
