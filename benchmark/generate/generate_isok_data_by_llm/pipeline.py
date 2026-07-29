"""读取 benchmark JSONL，并发调用模型后追加 model_output 字段。"""

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .flow_types import AnswerFlow

SMALL_OUTPUT_MAX_CHARS = 200
CODING_OUTPUT_MAX_CHARS = 500
FINAL_OUTPUT_MAX_CHARS = 1500
CODING_TAGS = {"编写代码", "coding", "code"}
MODEL_OUTPUT_FIELD = "model_output"
MODEL_OUTPUT_LIMIT_FIELD = "model_output_max_chars"


@dataclass(frozen=True)
class CaseTask:
    """表示一条等待模型回答的输入 case。"""

    source_index: int
    record: dict[str, Any]
    question: str
    max_output_chars: int


@dataclass(frozen=True)
class EnrichmentStats:
    """记录本轮增强任务的执行统计。"""

    total: int
    resumed: int
    expanded_retries: int
    final_retries: int
    succeeded: int
    failed: int
    output_path: Path


@dataclass(frozen=True)
class ResumeState:
    """保存断点扫描后的有效结果和需要提升上限重跑的 question。"""

    kept_records: list[dict[str, Any]]
    completed_counts: Counter[str]
    retry_limits: dict[str, list[int]]
    needs_rewrite: bool


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """严格读取 JSONL，并在格式错误时报告具体行号。"""
    if not path.is_file():
        raise FileNotFoundError(f"JSONL 输入文件不存在: {path}")

    records = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"输入文件第 {line_number} 行不是合法 JSON: {exc}"
                ) from exc
            if not isinstance(record, dict):
                raise TypeError(f"输入文件第 {line_number} 行必须是 JSON object")
            records.append(record)
    return records


def _case_identity(record: dict[str, Any]) -> str:
    """使用去除首尾空白后的 question 作为断点续跑匹配名称。"""
    question = record.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("case 缺少有效 question，无法执行断点续跑匹配")
    return question.strip()


def _next_output_limit(record: dict[str, Any]) -> int | None:
    """在回答达到当前上限时返回下一档 500 或 1500 字限制。"""
    model_output = record.get(MODEL_OUTPUT_FIELD)
    applied_limit = record.get(MODEL_OUTPUT_LIMIT_FIELD)
    if not isinstance(model_output, str):
        return None

    current_limit = (
        applied_limit if isinstance(applied_limit, int) else _output_limit(record)
    )
    if len(model_output.strip()) != current_limit:
        return None
    if current_limit == SMALL_OUTPUT_MAX_CHARS:
        return CODING_OUTPUT_MAX_CHARS
    if current_limit == CODING_OUTPUT_MAX_CHARS:
        return FINAL_OUTPUT_MAX_CHARS
    return None


def _load_resume_state(
    output_path: Path,
    input_counts: Counter[str],
    expected_mode: str,
) -> ResumeState:
    """扫描已有输出，合并重跑结果并识别需要提升到下一档的记录。"""
    if not output_path.exists():
        return ResumeState([], Counter(), {}, False)

    output_records = _read_jsonl(output_path)
    completed_records: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    retry_records: dict[str, list[tuple[int, dict[str, Any], int]]] = {}
    for index, record in enumerate(output_records):
        record_mode = record.get("generation_mode", "direct")
        if record_mode != expected_mode:
            raise ValueError(
                f"已有输出属于 {record_mode!r} 流程，当前选择 {expected_mode!r}；"
                "请使用不同输出文件"
            )
        model_output = record.get(MODEL_OUTPUT_FIELD)
        if not isinstance(model_output, str) or not model_output.strip():
            raise ValueError(
                f"已有输出包含无效 {MODEL_OUTPUT_FIELD} 字段: {output_path}"
            )
        identity = _case_identity(record)
        if identity not in input_counts:
            raise ValueError(
                "已有输出包含不属于当前输入文件的 question，"
                "请更换输出路径或使用 --clear"
            )
        next_limit = _next_output_limit(record)
        if next_limit is not None:
            retry_records.setdefault(identity, []).append((index, record, next_limit))
            continue
        completed_records.setdefault(identity, []).append((index, record))

    completed_counts = Counter()
    retry_limits: dict[str, list[int]] = {}
    kept_indexes = set()
    for identity, expected_count in input_counts.items():
        completed = completed_records.get(identity, [])
        retries = retry_records.get(identity, [])
        if len(completed) > expected_count:
            raise ValueError(
                f"已有输出中的 question 完成次数超过输入次数: {identity[:80]}"
            )

        retry_slots = expected_count - len(completed)
        selected_retries = retries[-retry_slots:] if retry_slots else []
        completed_counts[identity] = len(completed)
        retry_limits[identity] = [next_limit for _, _, next_limit in selected_retries]
        kept_indexes.update(index for index, _ in completed)
        kept_indexes.update(index for index, _, _ in selected_retries)

    kept_records = [
        record for index, record in enumerate(output_records) if index in kept_indexes
    ]
    return ResumeState(
        kept_records=kept_records,
        completed_counts=completed_counts,
        retry_limits=retry_limits,
        needs_rewrite=len(kept_records) != len(output_records),
    )


def _rewrite_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """原子重写输出文件，清理已被更高上限结果替代的旧记录。"""
    temporary_path = path.with_name(f".{path.name}.resume.tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        file.flush()
    temporary_path.replace(path)


def _is_coding_case(record: dict[str, Any]) -> bool:
    """根据 case 标签判断是否使用 Coding 的 500 字限制。"""
    tag = record.get("tag", "")
    return isinstance(tag, str) and tag.strip().casefold() in CODING_TAGS


def _output_limit(record: dict[str, Any]) -> int:
    """返回当前 case 对应的模型输出字符上限。"""
    return (
        CODING_OUTPUT_MAX_CHARS if _is_coding_case(record) else SMALL_OUTPUT_MAX_CHARS
    )


def _limit_output(text: str, max_chars: int) -> str:
    """对模型输出执行确定性的 Unicode 字符数硬限制。"""
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return f"{cleaned[: max_chars - 1].rstrip()}…"


def _build_task(
    index: int,
    record: dict[str, Any],
    max_output_chars: int | None = None,
) -> CaseTask:
    """校验原 case 并构建单条模型调用任务。"""
    question = record.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError(f"输入第 {index + 1} 条 case 缺少有效 question")
    return CaseTask(
        source_index=index,
        record=record,
        question=question.strip(),
        max_output_chars=max_output_chars or _output_limit(record),
    )


def _ask_model(task: CaseTask, answer_flow: AnswerFlow) -> dict[str, Any]:
    """执行选定回答流程，并拼接回答、流程元数据和实际字符上限。"""
    flow_answer = answer_flow.answer(
        question=task.question,
        record=task.record,
        max_output_chars=task.max_output_chars,
    )
    limited_output = _limit_output(flow_answer.text, task.max_output_chars)
    if not limited_output:
        raise RuntimeError("模型返回内容为空")

    enriched = task.record.copy()
    reserved_fields = set(enriched) | {MODEL_OUTPUT_FIELD, MODEL_OUTPUT_LIMIT_FIELD}
    conflicting_fields = reserved_fields.intersection(flow_answer.metadata)
    if conflicting_fields:
        names = ", ".join(sorted(conflicting_fields))
        raise RuntimeError(f"回答流程元数据覆盖了 case 保留字段: {names}")
    enriched.update(flow_answer.metadata)
    enriched[MODEL_OUTPUT_FIELD] = limited_output
    enriched[MODEL_OUTPUT_LIMIT_FIELD] = task.max_output_chars
    return enriched


def _pending_tasks(
    records: list[dict[str, Any]],
    completed_counts: Counter[str],
    retry_limits: dict[str, list[int]],
) -> tuple[list[CaseTask], int, Counter[int]]:
    """根据已有输出的 question 筛选任务，并正确处理重复名称。"""
    input_counts = Counter(_case_identity(record) for record in records)
    retry_counts = Counter(
        {identity: len(limits) for identity, limits in retry_limits.items()}
    )
    output_counts = completed_counts + retry_counts
    extra_counts = output_counts - input_counts
    if extra_counts:
        raise ValueError(
            "已有输出包含不属于当前输入文件的 case，请更换输出路径或使用 --clear"
        )

    remaining_completed = completed_counts.copy()
    remaining_retry_limits = {
        identity: limits.copy() for identity, limits in retry_limits.items()
    }
    pending = []
    resumed = 0
    retry_tiers: Counter[int] = Counter()
    for index, record in enumerate(records):
        identity = _case_identity(record)
        if remaining_completed[identity] > 0:
            remaining_completed[identity] -= 1
            resumed += 1
            continue
        limits = remaining_retry_limits.get(identity)
        if limits:
            next_limit = limits.pop(0)
            pending.append(_build_task(index, record, max_output_chars=next_limit))
            retry_tiers[next_limit] += 1
            continue
        pending.append(_build_task(index, record))
    return pending, resumed, retry_tiers


def enrich_jsonl(
    input_path: Path,
    output_path: Path,
    answer_flow: AnswerFlow,
    workers: int,
    clear_output: bool = False,
) -> EnrichmentStats:
    """并发增强 JSONL，逐条落盘并支持基于输出文件断点续跑。"""
    resolved_input = input_path.expanduser().resolve()
    resolved_output = output_path.expanduser().resolve()
    if resolved_input == resolved_output:
        raise ValueError("输出文件不能与输入文件相同，原始 JSONL 不允许被覆盖")
    if workers < 1:
        raise ValueError("workers 必须 >= 1")

    records = _read_jsonl(resolved_input)
    if clear_output and resolved_output.exists():
        resolved_output.unlink()

    input_counts = Counter(_case_identity(record) for record in records)
    resume_state = _load_resume_state(resolved_output, input_counts, answer_flow.name)
    tasks, resumed, retry_tiers = _pending_tasks(
        records,
        resume_state.completed_counts,
        resume_state.retry_limits,
    )
    if resume_state.needs_rewrite:
        _rewrite_jsonl(resolved_output, resume_state.kept_records)
    total = len(records)
    expanded_retries = retry_tiers[CODING_OUTPUT_MAX_CHARS]
    final_retries = retry_tiers[FINAL_OUTPUT_MAX_CHARS]
    print(
        f"[准备] 总数 {total}，已完成 {resumed}，"
        f"200→500重跑 {expanded_retries}，500→1500重跑 {final_retries}，"
        f"待处理 {len(tasks)}，workers {workers}",
        flush=True,
    )
    if not tasks:
        return EnrichmentStats(
            total=total,
            resumed=resumed,
            expanded_retries=expanded_retries,
            final_retries=final_retries,
            succeeded=0,
            failed=0,
            output_path=resolved_output,
        )

    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    actual_workers = min(workers, len(tasks))
    succeeded = 0
    failed = 0
    processed = resumed
    started_at = time.monotonic()

    with (
        resolved_output.open("a", encoding="utf-8") as output_file,
        ThreadPoolExecutor(
            max_workers=actual_workers,
            thread_name_prefix="case-model-output",
        ) as executor,
    ):
        futures = {
            executor.submit(_ask_model, task, answer_flow): task for task in tasks
        }
        for future in as_completed(futures):
            task = futures[future]
            processed += 1
            try:
                enriched = future.result()
            except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整个批次
                failed += 1
                print(
                    f"  ✗ [{processed}/{total}] 输入行 {task.source_index + 1} "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                continue

            output_file.write(json.dumps(enriched, ensure_ascii=False) + "\n")
            output_file.flush()
            succeeded += 1
            answer_length = len(enriched[MODEL_OUTPUT_FIELD])
            preview = task.question.replace("\n", " ")[:50]
            print(
                f"  ✓ [{processed}/{total}] 输入行 {task.source_index + 1} "
                f"[{answer_length}/{task.max_output_chars}字] {preview}...",
                flush=True,
            )

    compacted_state = _load_resume_state(
        resolved_output,
        input_counts,
        answer_flow.name,
    )
    if compacted_state.needs_rewrite:
        _rewrite_jsonl(resolved_output, compacted_state.kept_records)

    elapsed = time.monotonic() - started_at
    print(
        f"[完成] 新增 {succeeded}，200→500重跑 {expanded_retries}，"
        f"500→1500重跑 {final_retries}，失败 {failed}，"
        f"续跑跳过 {resumed}，耗时 {elapsed:.1f}s",
        flush=True,
    )
    return EnrichmentStats(
        total=total,
        resumed=resumed,
        expanded_retries=expanded_retries,
        final_retries=final_retries,
        succeeded=succeeded,
        failed=failed,
        output_path=resolved_output,
    )
