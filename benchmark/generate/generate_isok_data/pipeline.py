"""
主流程串联：逐条生成 & 逐条落盘，支持断点续跑 & 并行执行。
"""

import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

from answer_gen import AnswerResult, generate_answers
from question_gen import generate_questions
from scanner import ScanResult, scan_documents
from selector import SelectionResult, select_apis

from config import DEFAULT_OUTPUT, DEFAULT_QUESTION_COUNT


@dataclass
class BenchmarkRecord:
    """一条评测数据。"""

    question: str
    tag: str
    answer: str
    evaluation_note: str
    source_docs: list[str]
    selection_mode: str


def _read_existing_questions(output_path: Path) -> set:
    """读取已有的问题集合，用于去重和断点续跑。"""
    existing = set()
    if output_path.exists():
        with open(output_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        existing.add(data.get("question", ""))
                    except json.JSONDecodeError, KeyError:
                        pass
    return existing


def _append_jsonl(
    record: BenchmarkRecord,
    output_path: Path,
    lock: threading.Lock | None = None,
) -> None:
    """单条追加写入 JSONL 文件（线程安全）。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if lock:
        lock.acquire()
    try:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    finally:
        if lock:
            lock.release()


def _mark_skipped(
    round_num: int,
    total: int,
    reason: str,
    progress_counter: list,
    progress_lock: threading.Lock,
) -> None:
    """线程安全地记录一次跳过并输出进度。"""
    with progress_lock:
        progress_counter[1] += 1
        done = progress_counter[0] + progress_counter[1]
        print(
            f"  - [{done}/{total}] 第 {round_num} 轮跳过: {reason}",
            flush=True,
        )


def _pre_allocate_docs(scan_result: ScanResult, question_count: int) -> list[list[str]]:
    """
    预洗牌分配文档，确保均匀覆盖。
    返回每轮对应的文档列表。
    """
    all_filenames = [d.filename for d in scan_result.doc_summaries]
    if not all_filenames:
        raise ValueError("扫描结果中没有可用于生成数据的文档")

    shuffled = all_filenames[:]
    random.shuffle(shuffled)

    allocations = []
    for i in range(question_count):
        doc = shuffled[i % len(shuffled)]
        allocations.append([doc])
    return allocations


def _process_single_round(
    round_num: int,
    total: int,
    scan_result: ScanResult,
    docs_dir: Path,
    output_path: Path,
    existing_questions: set,
    write_lock: threading.Lock,
    progress_counter: list,
    progress_lock: threading.Lock,
    selection: SelectionResult | None = None,
    recently_used: set[str] | None = None,
) -> BenchmarkRecord | None:
    """
    处理单轮：选取→生成问题→生成答案→写入。
    被串行和并行模式共同调用。
    """
    # 选取 API
    if selection is None:
        selection = select_apis(scan_result, docs_dir, recently_used=recently_used)

    # 生成问题（每轮1个）
    questions = generate_questions(selection, docs_dir, question_count=1)
    if not questions:
        _mark_skipped(
            round_num,
            total,
            "问题生成失败",
            progress_counter,
            progress_lock,
        )
        return None

    q = questions[0]

    # 去重检查（线程安全）
    with write_lock:
        is_duplicate = q.question in existing_questions
        if not is_duplicate:
            existing_questions.add(q.question)
    if is_duplicate:
        _mark_skipped(
            round_num,
            total,
            "问题重复",
            progress_counter,
            progress_lock,
        )
        return None

    # 生成答案
    answers = generate_answers([q], selection.selected_docs, docs_dir)
    if not answers:
        with write_lock:
            existing_questions.discard(q.question)
        _mark_skipped(
            round_num,
            total,
            "答案生成失败",
            progress_counter,
            progress_lock,
        )
        return None

    a = answers[0]
    a.question = q.question
    a.tag = q.tag

    # 写入
    record = BenchmarkRecord(
        question=a.question,
        tag=a.tag,
        answer=a.answer,
        evaluation_note=a.evaluation_note,
        source_docs=selection.selected_docs.copy(),
        selection_mode=selection.selection_mode,
    )
    _append_jsonl(record, output_path, lock=write_lock)

    # 进度
    with progress_lock:
        progress_counter[0] += 1
        done = progress_counter[0] + progress_counter[1]
        print(
            f"  ✓ [{done}/{total}] 第 {round_num} 轮 [{a.tag}] {a.question[:60]}...",
            flush=True,
        )

    return record


def run_pipeline(
    docs_dir: Path,
    question_count: int = DEFAULT_QUESTION_COUNT,
    output_path: Path | None = None,
    scan_result: ScanResult | None = None,
    mock_mode: bool = False,
    workers: int = 1,
) -> list[BenchmarkRecord]:
    """
    执行 benchmark 生成 pipeline。
    逐条生成、逐条落盘，支持断点续跑和并行执行。
    """
    if workers < 1:
        raise ValueError("workers 必须 >= 1")

    if mock_mode:
        return _run_mock_pipeline(docs_dir, question_count, output_path)

    output_path = output_path or DEFAULT_OUTPUT

    print("=" * 60)
    print("  Benchmark 评测数据生成 Pipeline")
    if workers > 1:
        print(f"  并行模式: {workers} 个 worker")
    else:
        print("  串行模式")
    print("=" * 60)

    # ── 节点2：全局扫描 ──
    print("\n▶ 节点2：全局扫描")
    if scan_result is None:
        scan_result = scan_documents(docs_dir, workers=workers)
    else:
        print("[跳过] 使用预计算的扫描结果")
    if not scan_result.doc_summaries:
        raise RuntimeError("全局扫描没有产出任何文档摘要，无法继续生成数据")

    # ── 读取已有记录 ──
    existing_questions = _read_existing_questions(output_path)
    if existing_questions:
        print(f"\n  已有 {len(existing_questions)} 条记录，将跳过重复问题")

    # ── 共享状态 ──
    write_lock = threading.Lock()
    progress_lock = threading.Lock()
    progress_counter = [0, 0]  # [success, skip]
    records: list[BenchmarkRecord] = []

    if workers > 1:
        # ── 并行模式 ──
        allocations = _pre_allocate_docs(scan_result, question_count)
        selections = [
            select_apis(scan_result, docs_dir, doc_override=alloc)
            for alloc in allocations
        ]

        actual_workers = min(workers, question_count)
        print(f"\n▶ 开始并行生成 ({actual_workers} workers × {question_count} 轮)")
        with ThreadPoolExecutor(
            max_workers=actual_workers,
            thread_name_prefix="benchmark-generate",
        ) as executor:
            futures = {}
            for i in range(question_count):
                future = executor.submit(
                    _process_single_round,
                    round_num=i + 1,
                    total=question_count,
                    scan_result=scan_result,
                    docs_dir=docs_dir,
                    output_path=output_path,
                    existing_questions=existing_questions,
                    write_lock=write_lock,
                    progress_counter=progress_counter,
                    progress_lock=progress_lock,
                    selection=selections[i],
                )
                futures[future] = i + 1

            for future in as_completed(futures):
                round_num = futures[future]
                try:
                    result = future.result()
                    if result:
                        records.append(result)
                except Exception as e:  # noqa: BLE001 - 单轮失败不能中断整个批次
                    _mark_skipped(
                        round_num,
                        question_count,
                        f"{type(e).__name__}: {e}",
                        progress_counter,
                        progress_lock,
                    )
    else:
        # ── 串行模式 ──
        recently_used: set[str] = set()

        print(f"\n▶ 开始串行生成 ({question_count} 轮)")
        for round_num in range(1, question_count + 1):
            print(f"\n{'=' * 40}")
            print(f"  第 {round_num}/{question_count} 轮")
            print(f"{'=' * 40}")

            selection = select_apis(scan_result, docs_dir, recently_used=recently_used)
            print(f"  选取: {selection.selected_docs} ({selection.selection_mode})")
            recently_used.update(selection.selected_docs)

            result = _process_single_round(
                round_num=round_num,
                total=question_count,
                scan_result=scan_result,
                docs_dir=docs_dir,
                output_path=output_path,
                existing_questions=existing_questions,
                write_lock=write_lock,
                progress_counter=progress_counter,
                progress_lock=progress_lock,
                selection=selection,
            )
            if result:
                records.append(result)

    # ── 汇总 ──
    success_count = progress_counter[0]
    skip_count = progress_counter[1]
    print(f"\n{'=' * 60}")
    print(f"✓ 完成！本轮新增 {success_count} 条，跳过 {skip_count} 条")
    print(f"  输出文件: {output_path}")
    return records


def _run_mock_pipeline(
    docs_dir: Path,
    question_count: int,
    output_path: Path | None,
) -> list[BenchmarkRecord]:
    """Mock 模式：跳过 LLM 调用，验证流程逻辑。"""
    output_path = output_path or DEFAULT_OUTPUT

    print("=" * 60)
    print("  Benchmark Pipeline (MOCK MODE)")
    print("=" * 60)

    from scanner import DocSummary, SimilarGroup

    md_files = sorted(docs_dir.glob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"在 {docs_dir} 下未找到 .md 文件")

    doc_summaries = []
    for f in md_files:
        ds = DocSummary(
            filename=f.name,
            api_list=["mock_api_1", "mock_api_2"],
            summary=f"{f.stem} 模块的 API 文档",
            module=f.stem.replace("_", "."),
            api_count=2,
            file_size=f.stat().st_size,
        )
        doc_summaries.append(ds)
        print(f"  - {f.name}: [{ds.module}] {ds.summary}")

    similar_groups = []
    if len(doc_summaries) >= 2:
        similar_groups.append(
            SimilarGroup(
                group_id=1,
                apis=["mock_api_1", "mock_api_2"],
                reason="功能相似的 mock API 组",
                source_files=[d.filename for d in doc_summaries[:2]],
            )
        )

    print(f"  扫描完成: {len(doc_summaries)} 个文档, {len(similar_groups)} 个相似组")

    existing_questions = _read_existing_questions(output_path)
    records: list[BenchmarkRecord] = []
    recently_used: set[str] = set()

    for round_num in range(1, question_count + 1):
        print(f"\n--- 第 {round_num}/{question_count} 轮 (mock) ---")

        unused = [d for d in doc_summaries if d.filename not in recently_used]
        if not unused:
            unused = doc_summaries[:]
            recently_used.clear()
        primary = random.choice(unused)
        selection = SelectionResult(
            selected_docs=[primary.filename],
            selection_mode="单API",
            primary_doc=primary.filename,
        )
        recently_used.update(selection.selected_docs)

        q_text = f"Mock 问题 {round_num}: 如何使用 {primary.filename} 的基本功能？"
        q_tag = "简单用法"

        if q_text in existing_questions:
            print(f"  [跳过] 已存在: {q_text[:60]}...")
            continue

        a = AnswerResult(
            answer="```python\nimport example_module\nresult = example_module.api_call(param='value')\nprint(result)\n```\n\n说明：mock 答案。",
            evaluation_note="关键判定点：必须使用正确的 API 名称和参数。常见错误：参数名称拼写错误。宽容点：变量命名不影响评分。",
            question=q_text,
            tag=q_tag,
        )

        record = BenchmarkRecord(
            question=a.question,
            tag=a.tag,
            answer=a.answer,
            evaluation_note=a.evaluation_note,
            source_docs=selection.selected_docs.copy(),
            selection_mode=selection.selection_mode,
        )
        _append_jsonl(record, output_path)
        records.append(record)
        print(f"  ✓ 已写入: [{q_tag}] {q_text[:60]}...")

    print(f"\n✓ Mock 完成！生成 {len(records)} 条评测数据")
    print(f"  输出文件: {output_path}")
    return records
