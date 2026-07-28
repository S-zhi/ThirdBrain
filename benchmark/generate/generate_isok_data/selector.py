"""
节点3：API 选取
- 90% 概率随机模式：选1个API文档，有10%概率扩展为多选
- 10% 概率对比模式：从similar_groups中选2-3个API，100%多选
"""

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from config import COMPARISON_MODE_PROB, RANDOM_EXPAND_PROB, MAX_EXTRA_DOCS
from scanner import ScanResult, DocSummary, SimilarGroup


@dataclass
class SelectionResult:
    """API 选取结果。"""
    selected_docs: List[str] = field(default_factory=list)  # 文件名列表
    selection_mode: str = "单API"  # 单API | 多API对比 | 混合
    primary_doc: str = ""  # 主文档（单API/混合模式时有意义）


def select_apis(scan_result: ScanResult, docs_dir: Path) -> SelectionResult:
    """
    根据扫描结果和概率策略，选取 API 文档。
    
    策略：
    - 以 COMPARISON_MODE_PROB (10%) 概率进入对比模式
    - 否则进入随机模式：
      - 选 1 个随机文档
      - 以 RANDOM_EXPAND_PROB (10%) 概率扩展为多选
    """
    all_docs = scan_result.doc_summaries
    similar_groups = scan_result.similar_groups

    if not all_docs:
        raise ValueError("没有可用的文档，无法选取")

    # 判断是否进入对比模式
    use_comparison = (
        random.random() < COMPARISON_MODE_PROB
        and len(similar_groups) > 0
    )

    if use_comparison:
        return _comparison_mode(all_docs, similar_groups)
    else:
        return _random_mode(all_docs, scan_result)


def _random_mode(
    all_docs: List[DocSummary],
    scan_result: ScanResult
) -> SelectionResult:
    """
    随机模式：选1个文档，有概率扩展为多选。
    """
    # 随机选 1 个主文档
    primary = random.choice(all_docs)
    selected = [primary.filename]

    print(f"[选取] 随机模式 - 主文档: {primary.filename}")

    # 以 RANDOM_EXPAND_PROB 概率扩展
    if random.random() < RANDOM_EXPAND_PROB:
        # 尝试找到包含相似 API 的其他文档
        extra_docs = _find_related_docs(primary, scan_result, exclude=[primary.filename])
        extra_docs = extra_docs[:MAX_EXTRA_DOCS]

        if extra_docs:
            selected.extend(extra_docs)
            mode = "混合"
            print(f"[选取] 扩展为多选模式: {selected}")
        else:
            mode = "单API"
            print("[选取] 无可扩展的相关文档，保持单选")
    else:
        mode = "单API"

    return SelectionResult(
        selected_docs=selected,
        selection_mode=mode,
        primary_doc=primary.filename,
    )


def _comparison_mode(
    all_docs: List[DocSummary],
    similar_groups: List[SimilarGroup]
) -> SelectionResult:
    """
    对比模式：从相似组中选 2-3 个文档，生成对比性问题。
    """
    # 随机选一个相似组
    group = random.choice(similar_groups)

    # 从该组的源文件中选取 2-3 个
    candidate_files = [f for f in group.source_files if f in [d.filename for d in all_docs]]

    if len(candidate_files) < 2:
        # 如果相似组源文件不足 2 个，退回到随机模式
        print(f"[选取] 相似组 '{group.reason}' 源文件不足，退回随机模式")
        primary = random.choice(all_docs)
        return SelectionResult(
            selected_docs=[primary.filename],
            selection_mode="单API",
            primary_doc=primary.filename,
        )

    # 选取 2-3 个文档
    select_count = min(random.choice([2, 3]), len(candidate_files))
    selected = random.sample(candidate_files, select_count)

    print(f"[选取] 对比模式 - 相似组: {group.reason}")
    print(f"[选取] 选取文档: {selected}")

    return SelectionResult(
        selected_docs=selected,
        selection_mode="多API对比",
        primary_doc=selected[0],
    )


def _find_related_docs(
    primary: DocSummary,
    scan_result: ScanResult,
    exclude: List[str] = None
) -> List[str]:
    """
    根据主文档查找相关的其他文档。
    优先从相似组中查找同组的其他文档，其次按模块匹配。
    """
    exclude = set(exclude or [])
    related = []

    # 1. 从相似组中查找
    for group in scan_result.similar_groups:
        if primary.filename in group.source_files:
            for f in group.source_files:
                if f not in exclude and f not in related:
                    related.append(f)

    # 2. 按模块匹配
    if len(related) < MAX_EXTRA_DOCS:
        for doc in scan_result.doc_summaries:
            if doc.filename in exclude or doc.filename in related:
                continue
            if doc.module and doc.module == primary.module:
                related.append(doc.filename)
                if len(related) >= MAX_EXTRA_DOCS:
                    break

    # 3. 如果还不够，随机补充
    if not related:
        remaining = [
            d.filename for d in scan_result.doc_summaries
            if d.filename not in exclude
        ]
        if remaining:
            related = random.sample(remaining, min(MAX_EXTRA_DOCS, len(remaining)))

    return related
