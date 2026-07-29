"""
节点2：全局扫描（浅扫）
- 读取文档摘要/目录/API列表
- 提取每个文档的 API 列表、一句话摘要、所属模块
- 识别功能相似的 API 组
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from config import SCAN_MAX_LINES, llm_call, load_prompt


@dataclass
class DocSummary:
    """单个文档的摘要卡片。"""

    filename: str
    api_list: list[str] = field(default_factory=list)
    summary: str = ""
    module: str = ""
    api_count: int = 0  # 估算的 API 数量
    file_size: int = 0  # 文件大小（字节）


@dataclass
class SimilarGroup:
    """功能相似的 API 组。"""

    group_id: int
    apis: list[str] = field(default_factory=list)
    reason: str = ""
    source_files: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    """全局扫描的完整结果。"""

    doc_summaries: list[DocSummary] = field(default_factory=list)
    similar_groups: list[SimilarGroup] = field(default_factory=list)


@dataclass(frozen=True)
class _ShallowDocument:
    """保存单个文档的浅扫原文和文件元数据。"""

    filename: str
    content: str
    file_size: int


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


def _fallback_summary(document: _ShallowDocument) -> DocSummary:
    """为最终调用失败的单个文档创建可继续生产的降级摘要。"""
    return DocSummary(
        filename=document.filename,
        summary="MiniMax 摘要生成失败，后续直接使用文档原文出题",
        module=Path(document.filename).stem,
        file_size=document.file_size,
    )


def _scan_single_document(document: _ShallowDocument) -> DocSummary:
    """只向 MiniMax 发送一个文档并解析它的结构化摘要。"""
    document_text = f"### 文件: {document.filename}\n{document.content}"
    scan_prompt = load_prompt("scan.md").replace("{doc_summaries}", document_text)
    scan_response = llm_call(
        prompt=scan_prompt,
        system_prompt="你是一个 API 文档分析专家。请严格按 JSON 格式输出。",
    )
    scan_data = _parse_json_response(scan_response, "scan_result")
    raw_items = scan_data.get("scan_result", [])
    if not isinstance(raw_items, list) or not raw_items:
        raise RuntimeError(f"MiniMax 未返回文档摘要: {document.filename}")

    item = raw_items[0]
    if not isinstance(item, dict):
        raise TypeError(f"MiniMax 文档摘要格式错误: {document.filename}")

    raw_api_list = item.get("api_list", [])
    api_list = (
        [api for api in raw_api_list if isinstance(api, str)]
        if isinstance(raw_api_list, list)
        else []
    )
    summary = item.get("summary", "")
    module = item.get("module", "")
    return DocSummary(
        filename=document.filename,
        api_list=api_list,
        summary=summary if isinstance(summary, str) else "",
        module=module if isinstance(module, str) else "",
        api_count=len(api_list),
        file_size=document.file_size,
    )


def _identify_similar_groups(doc_summaries: list[DocSummary]) -> list[SimilarGroup]:
    """按单文档独立生成的 module 字段在本地构建相似组。"""
    summaries_by_module: dict[str, list[DocSummary]] = {}
    for summary in doc_summaries:
        module = summary.module.strip()
        if module:
            summaries_by_module.setdefault(module, []).append(summary)

    similar_groups = []
    group_id = 1
    for module, summaries in sorted(summaries_by_module.items()):
        if len(summaries) < 2:
            continue

        for offset in range(0, len(summaries), 5):
            group_summaries = summaries[offset : offset + 5]
            if len(group_summaries) < 2:
                continue
            apis = list(
                dict.fromkeys(
                    api for summary in group_summaries for api in summary.api_list
                )
            )
            similar_groups.append(
                SimilarGroup(
                    group_id=group_id,
                    apis=apis,
                    reason=f"同属 {module} 模块",
                    source_files=[summary.filename for summary in group_summaries],
                )
            )
            group_id += 1
    return similar_groups


def scan_documents(docs_dir: Path, workers: int = 1) -> ScanResult:
    """
    对 docs_dir 下所有 .md 文件进行浅扫，返回扫描结果。

    流程：
    1. 提取每个文件的浅扫内容
    2. 每个文档单独调用 MiniMax 提取结构化摘要
    3. 仅基于单文档摘要识别相似 API 组
    """
    if workers < 1:
        raise ValueError("扫描 workers 必须 >= 1")

    # 收集所有 markdown 文件
    md_files = sorted(docs_dir.glob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"在 {docs_dir} 下未找到 .md 文件")

    print(f"[扫描] 发现 {len(md_files)} 个文档文件")

    # 步骤1：浅扫每个文件
    shallow_documents = []
    for filepath in md_files:
        content = extract_shallow_content(filepath)
        file_size = filepath.stat().st_size
        shallow_documents.append(
            _ShallowDocument(
                filename=filepath.name,
                content=content,
                file_size=file_size,
            )
        )
        print(f"  - {filepath.name}: {len(content)} 字, {file_size} 字节")

    # 步骤2：每个文档单独调用 MiniMax，workers 只控制并发数。
    actual_workers = min(workers, len(shallow_documents))
    print(
        f"[扫描] 单文档独立请求: {len(shallow_documents)} 次，"
        f"并发 {actual_workers} workers",
        flush=True,
    )
    summaries_by_filename: dict[str, DocSummary] = {}
    with ThreadPoolExecutor(
        max_workers=actual_workers,
        thread_name_prefix="document-scan",
    ) as executor:
        futures = {
            executor.submit(_scan_single_document, document): document
            for document in shallow_documents
        }
        for completed, future in enumerate(as_completed(futures), 1):
            document = futures[future]
            try:
                summary = future.result()
                status = "✓"
            except Exception as exc:  # noqa: BLE001 - 单文档失败不能中断全部扫描
                summary = _fallback_summary(document)
                status = f"降级: {type(exc).__name__}: {exc}"
            summaries_by_filename[document.filename] = summary
            print(
                f"  {status} [{completed}/{len(shallow_documents)}] {document.filename}",
                flush=True,
            )

    doc_summaries = [summaries_by_filename[doc.filename] for doc in shallow_documents]
    print(f"[扫描] 提取到 {len(doc_summaries)} 个文档摘要", flush=True)

    # 步骤3：本地按每个文档独立生成的 module/主题字段归组，不再调用模型。
    print("[扫描] 本地按单文档主题构建相似 API 组...", flush=True)
    similar_groups = _identify_similar_groups(doc_summaries)
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
        if not isinstance(data, dict):
            print("[警告] JSON 根节点不是对象")
            return {expected_key: []}
        return data
    except json.JSONDecodeError as e:
        print(f"[警告] JSON 解析失败: {e}")
        print(f"[警告] 原始响应前200字: {response[:200]}")
        return {expected_key: []}
