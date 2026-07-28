"""
主流程串联：将各节点连接为完整的 benchmark 生成 pipeline。
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

from config import DEFAULT_QUESTION_COUNT, DEFAULT_OUTPUT
from scanner import ScanResult, scan_documents
from selector import SelectionResult, select_apis
from question_gen import Question, generate_questions
from answer_gen import AnswerResult, generate_answers


@dataclass
class BenchmarkRecord:
    """一条评测数据。"""
    question: str
    tag: str
    answer: str
    evaluation_note: str
    source_docs: List[str]
    selection_mode: str


def run_pipeline(
    docs_dir: Path,
    question_count: int = DEFAULT_QUESTION_COUNT,
    output_path: Optional[Path] = None,
    scan_result: Optional[ScanResult] = None,
    mock_mode: bool = False,
) -> List[BenchmarkRecord]:
    """
    执行完整的 benchmark 生成 pipeline。
    
    Args:
        docs_dir: API 文档目录
        question_count: 生成问题数量
        output_path: 输出文件路径（JSONL）
        scan_result: 预计算的扫描结果（可选，用于跨轮复用）
        mock_mode: 是否使用 mock 模式（跳过 LLM 调用，用于测试流程）
    
    Returns:
        生成的评测数据列表
    """
    if mock_mode:
        return _run_mock_pipeline(docs_dir, question_count, output_path)

    output_path = output_path or DEFAULT_OUTPUT

    print("=" * 60)
    print("  Benchmark 评测数据生成 Pipeline")
    print("=" * 60)

    # ── 节点2：全局扫描 ──
    print("\n▶ 节点2：全局扫描")
    if scan_result is None:
        scan_result = scan_documents(docs_dir)
    else:
        print("[跳过] 使用预计算的扫描结果")

    # ── 节点3：API 选取 ──
    print("\n▶ 节点3：API 选取")
    selection = select_apis(scan_result, docs_dir)

    # ── 节点4：问题生成 ──
    print("\n▶ 节点4：问题生成")
    questions = generate_questions(selection, docs_dir, question_count)

    # ── 节点5：标准答案生成 ──
    print("\n▶ 节点5：标准答案生成")
    answers = generate_answers(questions, selection.selected_docs, docs_dir)

    # ── 节点6：输出 ──
    print("\n▶ 节点6：组装输出")
    records = _assemble_records(answers, selection)
    _write_jsonl(records, output_path)

    print(f"\n✓ 完成！生成 {len(records)} 条评测数据")
    print(f"  输出文件: {output_path}")

    return records

# 方案：在 AnswerResult 中增加 question 引用字段
@dataclass
class AnswerResult:
    answer: str
    evaluation_note: str
    question: str = ""  # 新增，关联原始问题

def _assemble_records(answers: List[AnswerResult], selection: SelectionResult):
    records = []
    for a in answers:  # 只遍历成功的答案
        record = BenchmarkRecord(
            question=a.question,
            tag=a.tag,
            answer=a.answer,
            evaluation_note=a.evaluation_note,
            source_docs=selection.selected_docs.copy(),
            selection_mode=selection.selection_mode,
        )
        records.append(record)
    return records


def _write_jsonl(records: List[BenchmarkRecord], output_path: Path):
    """将评测数据写入 JSONL 文件（追加模式）。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "a", encoding="utf-8") as f:
        for record in records:
            line = json.dumps(asdict(record), ensure_ascii=False)
            f.write(line + "\n")

    print(f"  写入 {len(records)} 行到 {output_path}")


def _run_mock_pipeline(
    docs_dir: Path,
    question_count: int,
    output_path: Optional[Path],
) -> List[BenchmarkRecord]:
    """
    Mock 模式：跳过 LLM 调用，使用模拟数据验证流程逻辑。
    用于测试 pipeline 的结构正确性。
    """
    output_path = output_path or DEFAULT_OUTPUT

    print("=" * 60)
    print("  Benchmark Pipeline (MOCK MODE)")
    print("=" * 60)

    # 节点2：模拟扫描
    print("\n▶ 节点2：全局扫描 (mock)")
    from scanner import DocSummary, SimilarGroup, ScanResult

    md_files = sorted(docs_dir.glob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"在 {docs_dir} 下未找到 .md 文件")

    doc_summaries = []
    for f in md_files:
        ds = DocSummary(
            filename=f.name,
            api_list=[f"mock_api_1", f"mock_api_2"],
            summary=f"{f.stem} 模块的 API 文档",
            module=f.stem.replace("_", "."),
            api_count=2,
            file_size=f.stat().st_size,
        )
        doc_summaries.append(ds)
        print(f"  - {f.name}: [{ds.module}] {ds.summary}")

    similar_groups = []
    if len(doc_summaries) >= 2:
        similar_groups.append(SimilarGroup(
            group_id=1,
            apis=["mock_api_1", "mock_api_2"],
            reason="功能相似的 mock API 组",
            source_files=[d.filename for d in doc_summaries[:2]],
        ))

    scan_result = ScanResult(
        doc_summaries=doc_summaries,
        similar_groups=similar_groups,
    )
    print(f"  扫描完成: {len(doc_summaries)} 个文档, {len(similar_groups)} 个相似组")

    # 节点3：模拟选取
    print("\n▶ 节点3：API 选取 (mock)")
    import random
    if similar_groups and random.random() < 0.3:
        group = similar_groups[0]
        selection = SelectionResult(
            selected_docs=group.source_files[:2],
            selection_mode="多API对比",
            primary_doc=group.source_files[0],
        )
    else:
        primary = doc_summaries[0]
        selection = SelectionResult(
            selected_docs=[primary.filename],
            selection_mode="单API",
            primary_doc=primary.filename,
        )
    print(f"  选取模式: {selection.selection_mode}")
    print(f"  选中文档: {selection.selected_docs}")

    # 节点4：模拟问题生成
    print("\n▶ 节点4：问题生成 (mock)")
    questions = []
    for i in range(question_count):
        if selection.selection_mode == "多API对比":
            q = Question(
                question=f"Mock 对比问题 {i+1}: {selection.selected_docs[0]} 和 {selection.selected_docs[1]} 的主要区别是什么？",
                tag="其他",
            )
        else:
            q = Question(
                question=f"Mock 问题 {i+1}: 如何使用 {doc_summaries[0].api_list[0]} 的基本功能？",
                tag="简单用法",
            )
        questions.append(q)
        print(f"  {i+1}. [{q.tag}] {q.question[:60]}")

    # 节点5：模拟答案生成
    print("\n▶ 节点5：标准答案生成 (mock)")
    answers = []
    for q in questions:
        a = AnswerResult(
            answer=f"```python\n# Mock 答案\n# 问题: {q.question[:30]}...\nimport example_module\n\nresult = example_module.api_call(param='value')\nprint(result)\n```\n\n说明：以上为 mock 答案，演示了 API 的基本调用方式。",
            evaluation_note="关键判定点：必须使用正确的 API 名称和参数。常见错误：参数名称拼写错误、忘记导入模块。宽容点：变量命名不影响评分，代码风格不影响评分。",
        )
        answers.append(a)
    print(f"  生成 {len(answers)} 个答案")

    # 节点6：输出
    print("\n▶ 节点6：组装输出")
    records = _assemble_records( answers, selection)
    _write_jsonl(records, output_path)

    print(f"\n✓ Mock 完成！生成 {len(records)} 条评测数据")
    print(f"  输出文件: {output_path}")

    return records
