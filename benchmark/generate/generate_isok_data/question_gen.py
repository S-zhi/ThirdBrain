"""
节点4：问题生成
- 基于选中 API 文档批量生成问题
- 单 API 时：用法、参数、返回值等问题
- 多 API 时：对比性问题
- LLM 自动分配标签
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from selector import SelectionResult

from config import DEFAULT_QUESTION_COUNT, llm_call, load_prompt


@dataclass
class Question:
    """生成的评测问题。"""

    question: str
    tag: str  # 简单用法 | 编写代码 | 平台适配 | 其他


VALID_TAGS = {"简单用法", "编写代码", "平台适配", "其他"}


def generate_questions(
    selection: SelectionResult,
    docs_dir: Path,
    question_count: int = DEFAULT_QUESTION_COUNT,
) -> list[Question]:
    """
    基于选中的 API 文档生成评测问题。

    Args:
        selection: API 选取结果
        docs_dir: 文档目录
        question_count: 生成问题数量

    Returns:
        问题列表
    """
    # 读取选中文档的完整内容
    doc_contents = _read_selected_docs(selection.selected_docs, docs_dir)

    # 构造提示词
    prompt = load_prompt("question.md")
    prompt = prompt.replace("{selection_mode}", selection.selection_mode)
    prompt = prompt.replace("{selected_docs}", ", ".join(selection.selected_docs))
    prompt = prompt.replace("{doc_contents}", doc_contents)
    prompt = prompt.replace("{question_count}", str(question_count))

    print(f"[问题生成] 模式: {selection.selection_mode}, 数量: {question_count}")
    print(f"[问题生成] 选中 {len(doc_contents)} 字文档内容")

    # 调用 LLM
    response = llm_call(
        prompt=prompt,
        system_prompt="你是一个编程评测出题专家。请严格按 JSON 格式输出。",
    )

    # 解析响应
    questions = _parse_questions_response(response, question_count)

    print(f"[问题生成] 成功生成 {len(questions)} 个问题")
    for i, q in enumerate(questions, 1):
        print(f"  {i}. [{q.tag}] {q.question[:60]}...")

    return questions


def _read_selected_docs(filenames: list[str], docs_dir: Path) -> str:
    """读取选中文档的完整内容，拼接为字符串。"""
    contents = []
    for fname in filenames:
        filepath = docs_dir / fname
        if not filepath.exists():
            print(f"[警告] 文档不存在，跳过: {filepath}")
            continue
        text = filepath.read_text(encoding="utf-8")
        contents.append(f"---\n## 文档: {fname}\n\n{text}")
    return "\n\n".join(contents)


def _parse_questions_response(response: str, expected_count: int) -> list[Question]:
    """
    解析 LLM 返回的问题列表 JSON。
    对标签做校验，无效标签替换为"其他"。
    """
    # 提取 JSON
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
    if json_match:
        json_str = json_match.group(1).strip()
    else:
        json_str = response.strip()

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"[警告] 问题 JSON 解析失败: {e}")
        return []

    if not isinstance(data, dict):
        print("[警告] 问题响应的 JSON 根节点不是对象")
        return []
    raw_questions = data.get("questions", [])
    if not isinstance(raw_questions, list) or not raw_questions:
        print("[警告] 未解析到问题列表")
        return []

    questions = []
    for item in raw_questions:
        if not isinstance(item, dict):
            continue
        q_text = item.get("question", "").strip()
        q_tag = item.get("tag", "其他").strip()

        if not q_text:
            continue

        # 校验标签
        if q_tag not in VALID_TAGS:
            print(f"[警告] 无效标签 '{q_tag}'，替换为 '其他'")
            q_tag = "其他"

        questions.append(Question(question=q_text, tag=q_tag))

    return questions[:expected_count]
