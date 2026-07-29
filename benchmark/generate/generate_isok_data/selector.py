"""
节点3：API 选取
- 均匀轮转：优先选近期没被选过的文档，确保所有 API 被均匀覆盖
- 90% 概率随机模式：选1个API文档，有10%概率扩展为多选
- 10% 概率对比模式：从similar_groups中选2-3个API，100%多选
- 支持并行模式下的预分配（doc_override）
"""

import random
from dataclasses import dataclass, field
from pathlib import Path

from scanner import DocSummary, ScanResult, SimilarGroup

from config import COMPARISON_MODE_PROB, MAX_EXTRA_DOCS, RANDOM_EXPAND_PROB


@dataclass
class SelectionResult:
    """API 选取结果。"""

    selected_docs: list[str] = field(default_factory=list)
    selection_mode: str = "单API"
    primary_doc: str = ""


def select_apis(
    scan_result: ScanResult,
    docs_dir: Path,
    recently_used: set[str] | None = None,
    doc_override: list[str] | None = None,
) -> SelectionResult:
    """
    根据扫描结果和概率策略，选取 API 文档。

    Args:
        scan_result: 扫描结果
        docs_dir: 文档目录
        recently_used: 近期已选过的文档文件名集合（串行模式去重用）
        doc_override: 预分配的文档列表（并行模式用，跳过随机选取）
    """
    all_docs = scan_result.doc_summaries
    similar_groups = scan_result.similar_groups
    recently_used = recently_used or set()

    if not all_docs:
        raise ValueError("没有可用的文档，无法选取")

    # 并行模式：固定主文档保证均匀覆盖，但仍保留对比/混合策略。
    if doc_override is not None:
        known_names = {doc.filename for doc in all_docs}
        override_docs = [name for name in doc_override if name in known_names]
        if not override_docs:
            raise ValueError("预分配的文档不在扫描结果中")
        if len(override_docs) > 1:
            return SelectionResult(
                selected_docs=override_docs,
                selection_mode="混合",
                primary_doc=override_docs[0],
            )

        primary = next(doc for doc in all_docs if doc.filename == override_docs[0])
        comparable_groups = [
            group
            for group in similar_groups
            if primary.filename in group.source_files
            and len(set(group.source_files) & known_names) >= 2
        ]
        if comparable_groups and random.random() < COMPARISON_MODE_PROB:
            return _comparison_mode_for_primary(primary, all_docs, comparable_groups)
        remaining = [doc for doc in all_docs if doc.filename != primary.filename]
        return _random_mode([primary], remaining, scan_result)

    # 串行模式：未用过的优先
    unused = [d for d in all_docs if d.filename not in recently_used]
    used = [d for d in all_docs if d.filename in recently_used]

    if not unused:
        unused = all_docs[:]
        used = []
        random.shuffle(unused)

    use_comparison = random.random() < COMPARISON_MODE_PROB and len(similar_groups) > 0

    if use_comparison:
        return _comparison_mode(all_docs, similar_groups, unused)
    else:
        return _random_mode(unused, used, scan_result)


def _random_mode(
    unused: list[DocSummary],
    used: list[DocSummary],
    scan_result: ScanResult,
) -> SelectionResult:
    """以未使用文档为主池执行单 API 或混合模式选取。"""
    pool = unused if unused else used
    primary = random.choice(pool)
    selected = [primary.filename]

    print(f"[选取] 随机模式 - 主文档: {primary.filename}")

    if random.random() < RANDOM_EXPAND_PROB:
        extra_docs = _find_related_docs(
            primary, scan_result, exclude=[primary.filename]
        )
        extra_docs = extra_docs[:MAX_EXTRA_DOCS]
        if extra_docs:
            selected.extend(extra_docs)
            mode = "混合"
            print(f"[选取] 扩展为多选: {selected}")
        else:
            mode = "单API"
    else:
        mode = "单API"

    return SelectionResult(
        selected_docs=selected,
        selection_mode=mode,
        primary_doc=primary.filename,
    )


def _comparison_mode(
    all_docs: list[DocSummary],
    similar_groups: list[SimilarGroup],
    unused: list[DocSummary],
) -> SelectionResult:
    """优先从覆盖未使用文档最多的相似组中执行对比选取。"""
    unused_names = {d.filename for d in unused}

    scored_groups = []
    for group in similar_groups:
        score = sum(1 for f in group.source_files if f in unused_names)
        scored_groups.append((score, group))
    scored_groups.sort(key=lambda x: -x[0])

    top_score = scored_groups[0][0]
    top_groups = [g for s, g in scored_groups if s == top_score]
    group = random.choice(top_groups)

    candidate_files = [
        f for f in group.source_files if f in [d.filename for d in all_docs]
    ]

    if len(candidate_files) < 2:
        print(f"[选取] 相似组 '{group.reason}' 源文件不足，退回随机模式")
        pool = unused if unused else all_docs
        primary = random.choice(pool)
        return SelectionResult(
            selected_docs=[primary.filename],
            selection_mode="单API",
            primary_doc=primary.filename,
        )

    select_count = min(random.choice([2, 3]), len(candidate_files))
    selected = random.sample(candidate_files, select_count)

    print(f"[选取] 对比模式 - 相似组: {group.reason}")
    print(f"[选取] 选取文档: {selected}")

    return SelectionResult(
        selected_docs=selected,
        selection_mode="多API对比",
        primary_doc=selected[0],
    )


def _comparison_mode_for_primary(
    primary: DocSummary,
    all_docs: list[DocSummary],
    similar_groups: list[SimilarGroup],
) -> SelectionResult:
    """围绕预分配主文档构造对比选取结果。"""
    group = random.choice(similar_groups)
    known_names = {doc.filename for doc in all_docs}
    candidates = [
        name
        for name in group.source_files
        if name in known_names and name != primary.filename
    ]
    extra_count = min(random.choice([1, 2]), len(candidates))
    selected = [primary.filename, *random.sample(candidates, extra_count)]
    print(f"[选取] 对比模式 - 相似组: {group.reason}")
    print(f"[选取] 选取文档: {selected}")
    return SelectionResult(
        selected_docs=selected,
        selection_mode="多API对比",
        primary_doc=primary.filename,
    )


def _find_related_docs(
    primary: DocSummary,
    scan_result: ScanResult,
    exclude: list[str] | None = None,
) -> list[str]:
    """按相似组、模块和随机回退顺序查找关联文档。"""
    exclude = set(exclude or [])
    related = []

    for group in scan_result.similar_groups:
        if primary.filename in group.source_files:
            for f in group.source_files:
                if f not in exclude and f not in related:
                    related.append(f)

    if len(related) < MAX_EXTRA_DOCS:
        for doc in scan_result.doc_summaries:
            if doc.filename in exclude or doc.filename in related:
                continue
            if doc.module and doc.module == primary.module:
                related.append(doc.filename)
                if len(related) >= MAX_EXTRA_DOCS:
                    break

    if not related:
        remaining = [
            d.filename for d in scan_result.doc_summaries if d.filename not in exclude
        ]
        if remaining:
            related = random.sample(remaining, min(MAX_EXTRA_DOCS, len(remaining)))

    return related
