"""
节点5：标准答案生成
- 为每个问题生成标准答案（代码+简要说明）
- 附带评估说明（≤200字）
- 失败的问题会被跳过，不纳入输出
"""

import re
from dataclasses import dataclass
from pathlib import Path

from question_gen import Question

from config import EVAL_NOTE_MAX_CHARS, llm_call, load_prompt


@dataclass
class AnswerResult:
    """答案生成结果。"""

    answer: str
    evaluation_note: str
    question: str = ""
    tag: str = ""


def generate_answers(
    questions: list[Question],
    source_docs: list[str],
    docs_dir: Path,
    max_retries: int = 2,
) -> list[AnswerResult]:
    """
    为每个问题生成标准答案和评估说明。
    失败的问题会被跳过，不加入返回列表。
    """
    doc_contents = _read_docs(source_docs, docs_dir)
    results: list[AnswerResult] = []

    for i, q in enumerate(questions, 1):
        print(f"[答案生成] 正在处理第 {i}/{len(questions)} 个问题...")

        prompt = load_prompt("answer.md")
        prompt = prompt.replace("{question}", q.question)
        prompt = prompt.replace("{tag}", q.tag)
        prompt = prompt.replace("{source_docs}", ", ".join(source_docs))
        prompt = prompt.replace("{doc_contents}", doc_contents)

        system_prompt = "你是一个编程评测专家。请严格按 [ANSWER]...[/ANSWER] 和 [EVAL]...[/EVAL] 标记格式输出。"

        success = False
        for attempt in range(1, max_retries + 1):
            try:
                response = llm_call(prompt=prompt, system_prompt=system_prompt)
                print(f"  [DEBUG] LLM 原始返回(前300字):\n{response[:300]}")

                result = _parse_answer_response(response)
                if result.answer not in ("（答案生成失败）", "（答案为空）"):
                    result.question = q.question
                    result.tag = q.tag
                    result.evaluation_note = _limit_evaluation_note(
                        result.evaluation_note,
                        EVAL_NOTE_MAX_CHARS,
                    )
                    results.append(result)
                    success = True
                    break
                else:
                    print(f"  [重试] 未识别到有效标记，第 {attempt}/{max_retries} 次")
            except RuntimeError as e:
                print(f"  [错误] API 调用失败 (第 {attempt}/{max_retries} 次): {e}")

        if not success:
            print(f"  [跳过] 问题 {i} 答案生成失败，不纳入输出")

    print(f"[答案生成] 完成: {len(results)}/{len(questions)} 个成功")
    return results


def _read_docs(filenames: list[str], docs_dir: Path) -> str:
    """读取文档完整内容。"""
    contents = []
    for fname in filenames:
        filepath = docs_dir / fname
        if not filepath.exists():
            print(f"[警告] 文档不存在，跳过: {filepath}")
            continue
        text = filepath.read_text(encoding="utf-8")
        contents.append(f"---\n## 文档: {fname}\n\n{text}")
    return "\n\n".join(contents)


def _limit_evaluation_note(note: str, max_chars: int) -> str:
    """将评估说明限制在数据契约规定的最大字符数内。"""
    if len(note) <= max_chars:
        return note
    print(f"  [警告] 评估说明超长 ({len(note)} > {max_chars} 字)，已截断")
    return f"{note[: max_chars - 1].rstrip()}…"


def _parse_answer_response(response: str) -> AnswerResult:
    """解析 LLM 返回的答案，支持结构化标记格式。"""
    # 提取 [ANSWER]...[/ANSWER]
    answer_match = re.search(r"\[ANSWER\](.*?)\[/ANSWER\]", response, re.DOTALL)
    answer = answer_match.group(1).strip() if answer_match else ""

    # 提取 [EVAL]...[/EVAL]
    eval_match = re.search(r"\[EVAL\](.*?)\[/EVAL\]", response, re.DOTALL)
    eval_note = eval_match.group(1).strip() if eval_match else ""

    if not answer:
        print("  [警告] 未找到 [ANSWER] 标记")
        print(f"  [DEBUG] LLM 返回内容:\n{response[:500]}")
        return AnswerResult(answer="（答案生成失败）", evaluation_note="（生成失败）")
    if not eval_note:
        eval_note = "（评估说明缺失）"

    return AnswerResult(answer=answer, evaluation_note=eval_note)
