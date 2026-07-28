"""
节点5：标准答案生成
- 为每个问题生成标准答案（代码+简要说明）
- 附带评估说明（≤200字）
- 失败的问题会被跳过，不纳入输出
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from config import load_prompt, llm_call, EVAL_NOTE_MAX_CHARS
from question_gen import Question


@dataclass
class AnswerResult:
    """答案生成结果。"""
    answer: str
    evaluation_note: str
    question: str = ""
    tag: str = ""


def generate_answers(
    questions: List[Question],
    source_docs: List[str],
    docs_dir: Path,
    max_retries: int = 2,
) -> List[AnswerResult]:
    """
    为每个问题生成标准答案和评估说明。
    失败的问题会被跳过，不加入返回列表。
    """
    doc_contents = _read_docs(source_docs, docs_dir)
    results: List[AnswerResult] = []

    for i, q in enumerate(questions, 1):
        print(f"[答案生成] 正在处理第 {i}/{len(questions)} 个问题...")

        prompt = load_prompt("answer.md")
        prompt = prompt.replace("{question}", q.question)
        prompt = prompt.replace("{tag}", q.tag)
        prompt = prompt.replace("{source_docs}", ", ".join(source_docs))
        prompt = prompt.replace("{doc_contents}", doc_contents)

        system_prompt = "你是一个编程评测专家。请严格按 JSON 格式输出。"

        success = False
        for attempt in range(1, max_retries + 1):
            try:
                response = llm_call(prompt=prompt, system_prompt=system_prompt)
                print(f"  [DEBUG] LLM 原始返回(前300字):\n{response[:300]}")

                result = _parse_answer_response(response)
                if result.answer not in ("（答案生成失败）", "（答案为空）"):
                    result.question = q.question
                    result.tag = q.tag
                    results.append(result)
                    success = True

                    if len(result.evaluation_note) > EVAL_NOTE_MAX_CHARS:
                        print(f"  [警告] 评估说明超长 ({len(result.evaluation_note)} > {EVAL_NOTE_MAX_CHARS} 字)")
                    break
                else:
                    print(f"  [重试] JSON 解析失败，第 {attempt}/{max_retries} 次")
            except RuntimeError as e:
                print(f"  [错误] API 调用失败 (第 {attempt}/{max_retries} 次): {e}")

        if not success:
            print(f"  [跳过] 问题 {i} 答案生成失败，不纳入输出")

    print(f"[答案生成] 完成: {len(results)}/{len(questions)} 个成功")
    return results


def _read_docs(filenames: List[str], docs_dir: Path) -> str:
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



def _parse_answer_response(response: str) -> AnswerResult:
    """解析 LLM 返回的答案，支持结构化标记格式。"""
    # 提取 [ANSWER]...[/ANSWER]
    answer_match = re.search(r"\[ANSWER\](.*?)\[/ANSWER\]", response, re.DOTALL)
    answer = answer_match.group(1).strip() if answer_match else ""

    # 提取 [EVAL]...[/EVAL]
    eval_match = re.search(r"\[EVAL\](.*?)\[/EVAL\]", response, re.DOTALL)
    eval_note = eval_match.group(1).strip() if eval_match else ""

    if not answer:
        print(f"  [警告] 未找到 [ANSWER] 标记")
        print(f"  [DEBUG] LLM 返回内容:\n{response[:500]}")
        return AnswerResult(answer="（答案生成失败）", evaluation_note="（生成失败）")
    if not eval_note:
        eval_note = "（评估说明缺失）"

    return AnswerResult(answer=answer, evaluation_note=eval_note)