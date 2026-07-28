"""
节点2：全局扫描（浅扫）
- 读取文档摘要/目录/API列表
- 提取每个文档的 API 列表、一句话摘要、所属模块
- 识别功能相似的 API 组
"""

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Dict, Any, Optional

from config import SCAN_MAX_LINES, load_prompt, llm_call


@dataclass
class DocSummary:
    """单个文档的摘要卡片。"""
    filename: str
    api_list: List[str] = field(default_factory=list)
    summary: str = ""
    module: str = ""
    api_count: int = 0  # 估算的 API 数量
    file_size: int = 0  # 文件大小（字节）


@dataclass
class SimilarGroup:
    """功能相似的 API 组。"""
    group_id: int
    apis: List[str] = field(default_factory=list)
    reason: str = ""
    source_files: List[str] = field(default_factory=list)


@dataclass
class ScanResult:
    """全局扫描的完整结果。"""
    doc_summaries: List[DocSummary] = field(default_factory=list)
    similar_groups: List[SimilarGroup] = field(default_factory=list)


def extract_shallow_content(filepath: Path) -> str:
    """
    浅扫策略：提取文件的"摘要区"内容，包含标题、功能说明、函数签名、参数表等。
    在遇到"示例"、"Examples"等详细示例章节时停止，避免读取大量代码示例。
    最多读取 SCAN_MAX_LINES 行。
    """
    if not filepath.exists():
        raise FileNotFoundError(f"文档文件不存在: {filepath}")

    # 遇到这些标题时停止（示例章节通常很长且对摘要无用）
    stop_patterns = [
        re.compile(r"^##\s*示例", re.IGNORECASE),
        re.compile(r"^##\s*Examples?", re.IGNORECASE),
        re.compile(r"^##\s*使用案例", re.IGNORECASE),
        re.compile(r"^##\s*详细示例", re.IGNORECASE),
    ]

    lines = []
    with open(filepath, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= SCAN_MAX_LINES:
                break
            # 遇到示例章节时停止
            if i > 0 and any(p.match(line) for p in stop_patterns):
                break
            lines.append(line)

    return "".join(lines).strip()


def scan_documents(docs_dir: Path) -> ScanResult:
    """
    对 docs_dir 下所有 .md 文件进行浅扫，返回扫描结果。
    
    流程：
    1. 提取每个文件的浅扫内容
    2. 调用 LLM 提取结构化摘要
    3. 调用 LLM 识别相似 API 组
    """
    # 收集所有 markdown 文件
    md_files = sorted(docs_dir.glob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"在 {docs_dir} 下未找到 .md 文件")

    print(f"[扫描] 发现 {len(md_files)} 个文档文件")

    # 步骤1：浅扫每个文件
    doc_summaries_raw = []
    for filepath in md_files:
        content = extract_shallow_content(filepath)
        file_size = filepath.stat().st_size
        doc_summaries_raw.append({
            "filename": filepath.name,
            "content": content,
            "file_size": file_size,
        })
        print(f"  - {filepath.name}: {len(content)} 字, {file_size} 字节")

    # 步骤2：LLM 提取结构化摘要
    summaries_text = "\n\n".join(
        f"### 文件: {d['filename']}\n{d['content']}"
        for d in doc_summaries_raw
    )

    scan_prompt = load_prompt("scan.md")
    scan_prompt = scan_prompt.replace("{doc_summaries}", summaries_text)

    print("[扫描] 调用 LLM 提取结构化摘要...")
    scan_response = llm_call(
        prompt=scan_prompt,
        system_prompt="你是一个 API 文档分析专家。请严格按 JSON 格式输出。"
    )

    scan_data = _parse_json_response(scan_response, "scan_result")
    doc_summaries = []
    for item in scan_data.get("scan_result", []):
        ds = DocSummary(
            filename=item.get("filename", "unknown"),
            api_list=item.get("api_list", []),
            summary=item.get("summary", ""),
            module=item.get("module", ""),
            api_count=len(item.get("api_list", [])),
        )
        # 补充文件大小
        for raw in doc_summaries_raw:
            if raw["filename"] == ds.filename:
                ds.file_size = raw["file_size"]
                break
        doc_summaries.append(ds)

    print(f"[扫描] 提取到 {len(doc_summaries)} 个文档摘要")

    # 步骤3：LLM 识别相似 API 组
    api_summaries_text = "\n".join(
        f"- 文件: {ds.filename} | 模块: {ds.module} | API: {', '.join(ds.api_list)} | 摘要: {ds.summary}"
        for ds in doc_summaries
    )

    sim_prompt = load_prompt("similarity.md")
    sim_prompt = sim_prompt.replace("{api_summaries}", api_summaries_text)

    print("[扫描] 调用 LLM 识别相似 API 组...")
    sim_response = llm_call(
        prompt=sim_prompt,
        system_prompt="你是一个 API 分析专家。请严格按 JSON 格式输出。"
    )

    sim_data = _parse_json_response(sim_response, "similar_groups")
    similar_groups = []
    for item in sim_data.get("similar_groups", []):
        sg = SimilarGroup(
            group_id=item.get("group_id", 0),
            apis=item.get("apis", []),
            reason=item.get("reason", ""),
            source_files=item.get("source_files", []),
        )
        similar_groups.append(sg)

    print(f"[扫描] 发现 {len(similar_groups)} 个相似 API 组")

    return ScanResult(doc_summaries=doc_summaries, similar_groups=similar_groups)


def _parse_json_response(response: str, expected_key: str) -> dict:
    """
    解析 LLM 返回的 JSON，支持 markdown 代码块包裹。
    如果解析失败，返回包含空列表的默认结构。
    """
    # 尝试从 markdown 代码块中提取 JSON
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
    if json_match:
        json_str = json_match.group(1).strip()
    else:
        json_str = response.strip()

    try:
        data = json.loads(json_str)
        return data
    except json.JSONDecodeError as e:
        print(f"[警告] JSON 解析失败: {e}")
        print(f"[警告] 原始响应前200字: {response[:200]}")
        return {expected_key: []}
