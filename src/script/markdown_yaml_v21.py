"""Markdown API 文档到最小化 Schema 2.1 YAML 的可配置流水线。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import yaml
from config import MarkdownAiNodeConfig, MarkdownToYamlConfig

SCHEMA_VERSION = "2.1"
ALLOWED_CATEGORIES = {"function", "data_structure"}
IMAGE_SUFFIX_PATTERN = re.compile(
    r"\.(?:avif|bmp|gif|jpe?g|png|svg|webp)(?:[?#][^\s]*)?$",
    flags=re.IGNORECASE,
)
INLINE_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<uri><[^>]+>|[^\s)]+)"
    r"(?:\s+[\"'](?P<title>.*?)[\"'])?\s*\)"
)
INLINE_LINK_PATTERN = re.compile(
    r"(?<!!)\[(?P<anchor>[^\]]+)\]\(\s*(?P<uri><[^>]+>|[^\s)]+)"
    r"(?:\s+[\"'](?P<title>.*?)[\"'])?\s*\)"
)
REFERENCE_DEFINITION_PATTERN = re.compile(
    r"^\s*\[(?P<key>[^\]]+)\]:\s*(?P<uri><[^>]+>|\S+)"
    r"(?:\s+[\"'](?P<title>.*?)[\"'])?\s*$",
    flags=re.MULTILINE,
)
REFERENCE_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\[(?P<key>[^\]]*)\]",
)
REFERENCE_LINK_PATTERN = re.compile(
    r"(?<!!)\[(?P<anchor>[^\]]+)\]\[(?P<key>[^\]]*)\]",
)
HTML_IMAGE_PATTERN = re.compile(r"<img\b(?P<attrs>[^>]*)>", flags=re.IGNORECASE)
HTML_ATTR_PATTERN = re.compile(
    r"(?P<name>src|alt|title)\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    flags=re.IGNORECASE,
)
AUTOLINK_PATTERN = re.compile(r"<(?P<uri>https?://[^>\s]+)>", flags=re.IGNORECASE)
BARE_IMAGE_PATTERN = re.compile(
    r"https?://[^\s<>\])]+?\.(?:avif|bmp|gif|jpe?g|png|svg|webp)(?:[?#][^\s<>\])]*)?",
    flags=re.IGNORECASE,
)
HEADING_PATTERN = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
FOOTER_PATTERN = re.compile(
    r"(?:版权所有|copyright|文档反馈|意见反馈|上一篇|下一篇|返回顶部)",
    flags=re.IGNORECASE,
)
FENCE_PATTERN = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})(?P<language>[^`]*)$")
SIGNATURE_PATTERN = re.compile(
    r"(?m)^[ \t]*(?:template\s*<[^>]+>\s*)?"
    r"(?:[\w:<>,*&\[\]\s]+\s+)?[A-Za-z_]\w*\s*\([^;{}]*\)\s*;?[ \t]*$"
)
MARKDOWN_TEXT_ESCAPE_PATTERN = re.compile(r"\\([\\`*_{}\[\]<>()#+\-.!|])")
MARKDOWN_MATH_PATTERN = re.compile(
    r"(?s)(?<!\\)\$\$.*?(?<!\\)\$\$|(?<!\\)\$(?:\\.|[^$\n])+(?<!\\)\$"
)
ImageAiCall = Callable[[str, list[dict[str, str]]], str]


@dataclass(frozen=True)
class PipelineResult:
    """保存最终 YAML 和可选调试阶段产物。"""

    document: dict[str, Any]
    evidence: dict[str, Any]
    image_prompts: list[str]
    image_responses: list[str]
    ai_prompt: str | None
    ai_response: str | None


def _value(value: Any, *, is_ai: bool = False) -> dict[str, Any]:
    """构造带来源标记的标量字段。"""
    return {"value": value, "is_ai": is_ai}


def _strip_angle_brackets(uri: str) -> str:
    """移除 Markdown URI 可选的尖括号。"""
    stripped = uri.strip()
    if stripped.startswith("<") and stripped.endswith(">"):
        return stripped[1:-1].strip()
    return stripped


def _unescape_markdown_text(value: str) -> str:
    """移除派生文本的 Markdown 标点转义，但保留 LaTeX 公式内容。"""
    output: list[str] = []
    start = 0
    for match in MARKDOWN_MATH_PATTERN.finditer(value):
        output.append(MARKDOWN_TEXT_ESCAPE_PATTERN.sub(r"\1", value[start : match.start()]))
        output.append(match.group(0))
        start = match.end()
    output.append(MARKDOWN_TEXT_ESCAPE_PATTERN.sub(r"\1", value[start:]))
    return "".join(output)


def _resolve_uri(
    uri: str,
    source_url: str | None,
    source_path: Path,
    *,
    kind: str,
    image_base_url: str,
) -> str | None:
    """把绝对或相对资源地址解析成可使用的 URI。"""
    raw_uri = _strip_angle_brackets(uri)
    parsed = urlparse(raw_uri)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return raw_uri
    if parsed.scheme.lower() == "data":
        return raw_uri
    if parsed.scheme:
        return None
    if raw_uri.startswith("//"):
        return f"https:{raw_uri}"
    if kind == "image" and image_base_url:
        return urljoin(image_base_url.rstrip("/") + "/", raw_uri)
    if source_url:
        return urljoin(source_url, raw_uri)
    if not raw_uri or raw_uri.startswith(("#", "data:", "javascript:")):
        return None
    try:
        return (source_path.parent / raw_uri).resolve().as_uri()
    except ValueError:
        return None


def _context(markdown: str, start: int, end: int, radius: int = 180) -> str:
    """截取资源附近的正文，供 AI 生成 alt/title 时参考。"""
    snippet = markdown[max(0, start - radius) : min(len(markdown), end + radius)]
    return _unescape_markdown_text(re.sub(r"\s+", " ", snippet).strip())


def _resource_raw(
    kind: str,
    *,
    resolved_uri: str | None,
    alt: str | None = None,
    anchor_text: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """按照资源类型构造最终 raw 节点。"""
    normalized_alt = _unescape_markdown_text(alt) if alt else None
    normalized_anchor = _unescape_markdown_text(anchor_text) if anchor_text else None
    normalized_title = _unescape_markdown_text(title) if title else None
    if kind == "image":
        return {
            "resolved_uri": resolved_uri,
            "alt": _value(normalized_alt),
            "title": _value(normalized_title),
        }
    return {
        "resolved_uri": resolved_uri,
        "anchor_text": _value(normalized_anchor),
        "title": _value(normalized_title),
        "criticality": "medium",
    }


def extract_resources(
    markdown: str,
    source_url: str | None,
    source_path: Path,
    *,
    image_base_url: str = "https://www.hiascend.com/",
    scan_markdown: str | None = None,
    deduplicate: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从非代码扫描视图提取 Markdown、HTML 和裸图片/链接资源。"""
    scanned = scan_markdown if scan_markdown is not None else markdown
    definitions = {
        match.group("key").strip().casefold(): {
            "uri": _strip_angle_brackets(match.group("uri")),
            "title": match.group("title"),
        }
        for match in REFERENCE_DEFINITION_PATTERN.finditer(scanned)
    }
    candidates: list[dict[str, Any]] = []

    def add_candidate(
        kind: str,
        uri: str,
        *,
        start: int,
        end: int,
        alt: str | None = None,
        anchor_text: str | None = None,
        title: str | None = None,
    ) -> None:
        """追加一个带上下文的资源候选。"""
        candidates.append(
            {
                "kind": kind,
                "uri": uri,
                "resolved_uri": _resolve_uri(
                    uri,
                    source_url,
                    source_path,
                    kind=kind,
                    image_base_url=image_base_url,
                ),
                "alt": alt,
                "anchor_text": anchor_text,
                "title": title,
                "context": _context(markdown, start, end),
                "line_number": markdown.count("\n", 0, start) + 1,
            }
        )

    for match in INLINE_IMAGE_PATTERN.finditer(scanned):
        add_candidate(
            "image",
            match.group("uri"),
            start=match.start(),
            end=match.end(),
            alt=match.group("alt"),
            title=match.group("title"),
        )
    for match in INLINE_LINK_PATTERN.finditer(scanned):
        add_candidate(
            "link",
            match.group("uri"),
            start=match.start(),
            end=match.end(),
            anchor_text=match.group("anchor"),
            title=match.group("title"),
        )
    for match in REFERENCE_IMAGE_PATTERN.finditer(scanned):
        key = (match.group("key") or match.group("alt")).strip().casefold()
        definition = definitions.get(key)
        if definition:
            add_candidate(
                "image",
                str(definition["uri"]),
                start=match.start(),
                end=match.end(),
                alt=match.group("alt"),
                title=definition.get("title"),
            )
    for match in REFERENCE_LINK_PATTERN.finditer(scanned):
        key = (match.group("key") or match.group("anchor")).strip().casefold()
        definition = definitions.get(key)
        if definition:
            add_candidate(
                "link",
                str(definition["uri"]),
                start=match.start(),
                end=match.end(),
                anchor_text=match.group("anchor"),
                title=definition.get("title"),
            )
    for match in HTML_IMAGE_PATTERN.finditer(scanned):
        attributes = {
            attribute.group("name").lower(): attribute.group("value")
            for attribute in HTML_ATTR_PATTERN.finditer(match.group("attrs"))
        }
        if attributes.get("src"):
            add_candidate(
                "image",
                attributes["src"],
                start=match.start(),
                end=match.end(),
                alt=attributes.get("alt"),
                title=attributes.get("title"),
            )
    for match in AUTOLINK_PATTERN.finditer(scanned):
        add_candidate(
            "link",
            match.group("uri"),
            start=match.start(),
            end=match.end(),
            anchor_text=match.group("uri"),
        )

    known_image_uris = {
        _strip_angle_brackets(str(candidate["uri"]))
        for candidate in candidates
        if candidate["kind"] == "image"
    }
    for match in BARE_IMAGE_PATTERN.finditer(scanned):
        if match.group(0) not in known_image_uris:
            add_candidate(
                "image",
                match.group(0),
                start=match.start(),
                end=match.end(),
            )

    resources: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    counters = {"image": 0, "link": 0}
    for candidate in candidates:
        identity = (
            str(candidate["kind"]),
            str(candidate["resolved_uri"] or candidate["uri"]),
            str(candidate["anchor_text"] or candidate["alt"] or ""),
        )
        if deduplicate and identity in seen:
            continue
        seen.add(identity)
        kind = str(candidate["kind"])
        counters[kind] += 1
        prefix = "img" if kind == "image" else "link"
        resource_id = f"res_{prefix}_{counters[kind]:03d}"
        resources.append(
            {
                "resource_id": resource_id,
                "kind": kind,
                "raw": _resource_raw(
                    kind,
                    resolved_uri=candidate["resolved_uri"],
                    alt=candidate["alt"],
                    anchor_text=candidate["anchor_text"],
                    title=candidate["title"],
                ),
            }
        )
        evidence.append(
            {
                "resource_id": resource_id,
                "kind": kind,
                "raw_uri": candidate["uri"],
                "resolved_uri": candidate["resolved_uri"],
                "context": candidate["context"],
                "line_number": candidate["line_number"],
            }
        )
    return resources, evidence


def _split_table_row(line: str) -> list[str]:
    """把 Markdown 表格行拆成去除外围空白的单元格。"""
    return [_unescape_markdown_text(cell.strip()) for cell in line.strip().strip("|").split("|")]


def _scan_blocks(
    markdown: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[int], set[int]]:
    """扫描围栏代码和 Markdown 表格，同时返回需要从正文删除的行号。"""
    lines = markdown.splitlines()
    code_blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    code_removed: set[int] = set()
    table_removed: set[int] = set()
    current_heading = ""
    index = 0
    while index < len(lines):
        heading = HEADING_PATTERN.match(lines[index])
        if heading:
            current_heading = _unescape_markdown_text(heading.group("title").strip())
        fence = FENCE_PATTERN.match(lines[index])
        if fence:
            marker = fence.group("fence")
            language = fence.group("language").strip()
            start = index
            index += 1
            content: list[str] = []
            while index < len(lines) and not re.match(
                rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$",
                lines[index],
            ):
                content.append(lines[index])
                index += 1
            end = min(index, len(lines) - 1)
            code_removed.update(range(start, end + 1))
            code_blocks.append(
                {
                    "language": language,
                    "heading": current_heading,
                    "content": "\n".join(content).strip(),
                    "start_line": start + 1,
                    "end_line": end + 1,
                }
            )
            index += 1
            continue
        if index + 1 < len(lines) and TABLE_SEPARATOR_PATTERN.match(lines[index + 1]):
            start = index
            header = _split_table_row(lines[index])
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_split_table_row(lines[index]))
                index += 1
            table_removed.update(range(start, index))
            tables.append(
                {
                    "heading": current_heading,
                    "headers": header,
                    "rows": rows[:100],
                    "start_line": start + 1,
                    "end_line": index,
                }
            )
            continue
        index += 1
    return code_blocks, tables, code_removed, table_removed


def _blank_code_match(match: re.Match[str]) -> str:
    """用等长空格屏蔽代码匹配，同时保留换行和字符位置。"""
    return "".join("\n" if character == "\n" else " " for character in match.group(0))


def mask_code_for_resource_scan(markdown: str) -> str:
    """屏蔽围栏、缩进、行内及 HTML 代码，防止示例语法被识别成资源。"""
    _, _, code_removed, _ = _scan_blocks(markdown)
    masked_lines: list[str] = []
    for index, line in enumerate(markdown.splitlines(keepends=True)):
        if index in code_removed or re.match(r"^(?: {4}|\t)\S", line):
            masked_lines.append("".join("\n" if character == "\n" else " " for character in line))
        else:
            masked_lines.append(line)
    masked = "".join(masked_lines)
    masked = re.sub(
        r"<code\b[^>]*>.*?</code>",
        _blank_code_match,
        masked,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(r"(`+)[^\n]*?\1", _blank_code_match, masked)


def find_unparsed_markdown_images(markdown: str) -> list[dict[str, Any]]:
    """找出非代码扫描视图中无法匹配合法图片语法的 ``![`` 标记。"""
    results: list[dict[str, Any]] = []
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        for marker in re.finditer(r"!\[", line):
            if marker.start() > 0 and line[marker.start() - 1] == "\\":
                continue
            candidate = line[marker.start() :]
            if INLINE_IMAGE_PATTERN.match(candidate) or REFERENCE_IMAGE_PATTERN.match(candidate):
                continue
            results.append(
                {
                    "line_number": line_number,
                    "raw_markdown": candidate[:500],
                }
            )
    return results


def _remove_resource_markup(text: str, remove_images: bool, remove_links: bool) -> str:
    """从主正文删除已提取的图片和链接节点。"""
    cleaned = text
    if remove_images:
        cleaned = INLINE_IMAGE_PATTERN.sub("", cleaned)
        cleaned = REFERENCE_IMAGE_PATTERN.sub("", cleaned)
        cleaned = HTML_IMAGE_PATTERN.sub("", cleaned)
        cleaned = BARE_IMAGE_PATTERN.sub("", cleaned)
    if remove_links:
        cleaned = INLINE_LINK_PATTERN.sub("", cleaned)
        cleaned = REFERENCE_LINK_PATTERN.sub("", cleaned)
        cleaned = AUTOLINK_PATTERN.sub("", cleaned)
        cleaned = REFERENCE_DEFINITION_PATTERN.sub("", cleaned)
    return cleaned


def _remove_empty_headings(lines: list[str]) -> list[str]:
    """删除清理后已经没有正文内容的空标题。"""
    kept: list[str] = []
    for index, line in enumerate(lines):
        if not HEADING_PATTERN.match(line):
            kept.append(line)
            continue
        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        if next_index >= len(lines) or HEADING_PATTERN.match(lines[next_index]):
            continue
        kept.append(line)
    return kept


def preprocess_markdown(
    markdown: str,
    config: MarkdownToYamlConfig,
) -> tuple[str, dict[str, Any]]:
    """先提取槽位证据，再按配置生成可供 AI 阅读的干净正文。"""
    code_blocks, tables, code_removed, table_removed = _scan_blocks(markdown)
    lines = markdown.splitlines()
    cleaned_lines: list[str] = []
    footer_started = False
    for index, line in enumerate(lines):
        heading = HEADING_PATTERN.match(line)
        if config.preprocess.remove_footer and (
            FOOTER_PATTERN.search(line)
            or (heading and FOOTER_PATTERN.search(heading.group("title")))
        ):
            footer_started = True
        if footer_started:
            continue
        if index in code_removed and config.preprocess.remove_code_blocks:
            continue
        if index in table_removed and config.preprocess.remove_tables:
            continue
        if config.preprocess.remove_code_blocks and re.match(r"^(?: {4}|\t)\S", line):
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"<table\b.*?</table>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = _remove_resource_markup(
        cleaned,
        config.preprocess.remove_images,
        config.preprocess.remove_links,
    )
    for invalid_value in config.preprocess.remove_invalid_values:
        cleaned = re.sub(
            rf"(?<!\w){re.escape(invalid_value)}(?!\w)",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
    cleaned = _unescape_markdown_text(cleaned)
    cleaned_lines = _remove_empty_headings(cleaned.splitlines())
    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    evidence = {
        "code_blocks": code_blocks,
        "tables": tables,
    }
    return cleaned, evidence


def _candidate_sections(markdown: str, fallback_name: str) -> list[dict[str, str]]:
    """按一级标题产生多个 API 候选，无法分割时回退为一个文档。"""
    matches = list(re.finditer(r"(?m)^#\s+(.+?)\s*$", markdown))
    if not matches:
        return [{"name": fallback_name, "content": markdown}]
    sections: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections.append(
            {
                "name": match.group(1).strip(),
                "content": markdown[match.start() : end].strip(),
            }
        )
    return sections


def _first_signature(code_blocks: list[dict[str, Any]], name: str) -> str:
    """从非示例代码块中选择最像当前 API 的函数原型。"""
    for block in code_blocks:
        heading = str(block.get("heading") or "")
        content = str(block.get("content") or "")
        if re.search(r"(?:示例|example)", heading, flags=re.IGNORECASE):
            continue
        signatures = SIGNATURE_PATTERN.findall(content)
        if signatures and (not name or name in signatures[0]):
            return signatures[0].strip()
    return ""


def _deterministic_examples(code_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从明确标记为示例的代码块生成非 AI 示例。"""
    examples: list[dict[str, Any]] = []
    for block in code_blocks:
        heading = str(block.get("heading") or "")
        content = str(block.get("content") or "")
        if content and re.search(r"(?:示例|example)", heading, flags=re.IGNORECASE):
            examples.append(_value(content))
    return examples


def build_skeleton(
    *,
    markdown: str,
    source_path: Path,
    source_url: str | None,
    preprocess: str,
    resources: list[dict[str, Any]],
    evidence: dict[str, Any],
    hints: Mapping[str, str | None],
    config: MarkdownToYamlConfig,
) -> dict[str, Any]:
    """用确定性信息构造 Schema 2.1 骨架。"""
    fixed = config.fixed_values
    namespace = fixed.namespace or hints.get("namespace") or ""
    version = fixed.version or hints.get("version") or ""
    language = fixed.language or hints.get("language") or "cpp"
    fallback_name = hints.get("name") or source_path.stem
    sections = _candidate_sections(preprocess, fallback_name)
    code_blocks = evidence.get("code_blocks", [])
    signature = _first_signature(code_blocks, fallback_name)
    examples = _deterministic_examples(code_blocks)
    documents: list[dict[str, Any]] = []
    for index, section in enumerate(sections):
        name = hints.get("name") if len(sections) == 1 and hints.get("name") else section["name"]
        category_hint = hints.get("category")
        category = (
            "data_structure"
            if category_hint in {"struct", "class", "data_structure"}
            or re.search(
                r"(?:数据结构|结构体|\bstruct\b|\bclass\b)",
                section["content"],
                re.IGNORECASE,
            )
            else "function"
        )
        documents.append(
            {
                "name": str(name or fallback_name),
                "namespace": namespace,
                "version": version,
                "language": language,
                "use": {
                    "summary": _value(""),
                    "category": _value(category),
                    "description": _value(""),
                    "product_support": [],
                    "prerequisites": [],
                    "function_details": {
                        "input_parameters": [],
                        "output_parameters": [],
                        "signature": _value(signature if index == 0 else ""),
                    },
                    "data_structure": {"fields": []},
                    "examples": examples if index == 0 else [],
                },
            }
        )

    source_path_value = None if source_url else str(source_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "uuid": None,
        "source": {
            "source_path": source_path_value,
            "source_url": source_url,
            "content_hash": f"sha256:{hashlib.sha256(markdown.encode('utf-8')).hexdigest()}",
            "source_markdown": markdown,
            "preprocess_markdown": preprocess,
            "resources": resources,
        },
        "documents": documents,
    }


def _enabled_nodes(config: MarkdownToYamlConfig) -> dict[str, dict[str, Any]]:
    """把启用的 AI 节点转换成提示词可读配置。"""
    image_nodes = {"resources[].raw.alt", "resources[].raw.title"}
    return {
        path: {
            "mode": node.mode,
            "max_chars": node.max_chars,
            "require_evidence": node.require_evidence,
            "allow_generate": node.allow_generate,
            "allowed_values": list(node.allowed_values),
        }
        for path, node in config.ai.nodes.items()
        if node.enabled and not (config.ai.image_understanding.enabled and path in image_nodes)
    }


def _read_prompt_template(prompt_file: str, project_root: Path) -> str:
    """读取外置提示词；相对路径统一相对于项目根目录解析。"""
    prompt_path = Path(prompt_file)
    if not prompt_path.is_absolute():
        prompt_path = project_root / prompt_path
    return prompt_path.read_text(encoding="utf-8")


def _image_resources_for_ai(
    resources: list[dict[str, Any]],
    evidence: Mapping[str, Any],
    config: MarkdownToYamlConfig,
) -> list[dict[str, str]]:
    """选出仍缺少说明且能以 HTTP(S) URL 发送给模型的图片。"""
    alt_enabled = _node(config, "resources[].raw.alt") is not None
    title_enabled = _node(config, "resources[].raw.title") is not None
    context_by_id = {
        item.get("resource_id"): item
        for item in evidence.get("resources", [])
        if isinstance(item, Mapping)
    }
    selected: list[dict[str, str]] = []
    for resource in resources:
        if resource.get("kind") != "image":
            continue
        raw = resource.get("raw")
        if not isinstance(raw, Mapping):
            continue
        uri = raw.get("resolved_uri")
        parsed = urlparse(str(uri or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        alt = _extract_text(raw.get("alt"))
        title = _extract_text(raw.get("title"))
        fields_to_fill = [
            field
            for field, enabled, value in (
                ("alt", alt_enabled, alt),
                ("title", title_enabled, title),
            )
            if enabled and not value
        ]
        if not fields_to_fill:
            continue
        resource_id = str(resource["resource_id"])
        resource_evidence = context_by_id.get(resource_id, {})
        selected.append(
            {
                "resource_id": resource_id,
                "url": str(uri),
                "context": str(resource_evidence.get("context") or ""),
                "current_alt": alt,
                "current_title": title,
                "fields_to_fill": ",".join(fields_to_fill),
            }
        )
    return selected


def build_image_prompt(
    images: list[dict[str, str]],
    config: MarkdownToYamlConfig,
    project_root: Path,
) -> str:
    """用外置模板和运行时资源清单构造图片理解提示词。"""
    image_config = config.ai.image_understanding
    template = _read_prompt_template(image_config.prompt_file, project_root)
    template = template.replace(
        "{{max_description_chars}}",
        str(image_config.max_description_chars),
    )
    manifest = {
        "images": [
            {
                "resource_id": image["resource_id"],
                "url": image["url"],
                "nearby_markdown": image["context"],
                "current_alt": image["current_alt"] or None,
                "current_title": image["current_title"] or None,
                "fields_to_fill": image["fields_to_fill"].split(","),
            }
            for image in images
        ]
    }
    serialized = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False)
    return f"{template.rstrip()}\n\n<image_input>\n{serialized}</image_input>\n"


def merge_image_updates(
    resources: list[dict[str, Any]],
    updates: Mapping[str, Any],
    config: MarkdownToYamlConfig,
) -> None:
    """仅把图片模型结果填入已有的空 alt/title 字段。"""
    resource_by_id = {
        resource["resource_id"]: resource
        for resource in resources
        if resource.get("kind") == "image"
    }
    image_updates = updates.get("image_updates")
    if not isinstance(image_updates, list):
        raise TypeError("图片理解结果必须包含 image_updates 列表")
    for update in image_updates:
        if not isinstance(update, Mapping):
            continue
        resource = resource_by_id.get(update.get("resource_id"))
        if resource is None:
            continue
        raw = resource["raw"]
        for field, max_chars in (
            ("alt", config.ai.image_understanding.max_description_chars),
            ("title", 40),
        ):
            node_path = f"resources[].raw.{field}"
            if _node(config, node_path) is None:
                continue
            if _extract_text(raw.get(field)) or field not in update:
                continue
            value = _unescape_markdown_text(_extract_text(update[field]))
            if value:
                raw[field] = _value(_trim_text(value, max_chars), is_ai=True)


def build_ai_prompt(
    document: Mapping[str, Any],
    evidence: Mapping[str, Any],
    config: MarkdownToYamlConfig,
    project_root: Path,
) -> str:
    """把干净正文、槽位证据和启用节点装配成一次 AI 请求。"""
    template = _read_prompt_template(config.ai.prompt_file, project_root)
    evidence_copy = {
        "code_blocks": [
            {
                **block,
                "content": str(block.get("content") or "")[:4_000],
            }
            for block in evidence.get("code_blocks", [])
        ],
        "tables": [
            {
                **table,
                "rows": list(table.get("rows") or [])[:50],
            }
            for table in evidence.get("tables", [])
        ],
        "resources": list(evidence.get("resources") or []),
    }
    payload = {
        "enabled_nodes": _enabled_nodes(config),
        "current_documents": document["documents"],
        "preprocess_markdown": document["source"]["preprocess_markdown"],
        "slot_evidence": evidence_copy,
    }
    serialized = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    limit = config.ai.max_input_chars
    if limit <= 1_000:
        raise ValueError("markdown_to_yaml.ai.max_input_chars 必须大于 1000")
    if len(template) + len(serialized) > limit:
        overflow = len(template) + len(serialized) - limit
        clean_text = str(payload["preprocess_markdown"])
        payload["preprocess_markdown"] = clean_text[: max(0, len(clean_text) - overflow - 80)]
        payload["preprocess_markdown"] += "\n\n[正文因输入长度限制已截断]"
        serialized = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    if len(template) + len(serialized) > limit:
        raise ValueError("结构化槽位证据超过 AI 输入上限，请减少单页内容")
    return f"{template.rstrip()}\n\n<slot_fill_input>\n{serialized}</slot_fill_input>\n"


def parse_ai_updates(response_text: str) -> dict[str, Any]:
    """解析 AI 返回的 JSON/YAML 槽位更新对象。"""
    cleaned = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL).strip()
    fenced = re.fullmatch(r"```(?:json|yaml|yml)?\s*\n(.*?)\n```", cleaned, flags=re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        result = yaml.safe_load(cleaned)
    except yaml.YAMLError as exc:
        raise ValueError(f"AI 槽位填充结果无法解析: {exc}") from exc
    if not isinstance(result, dict):
        raise TypeError("AI 槽位填充结果根节点必须是 mapping")
    return result


def _node(config: MarkdownToYamlConfig, path: str) -> MarkdownAiNodeConfig | None:
    """获取启用的节点配置。"""
    node = config.ai.nodes.get(path)
    return node if node and node.enabled else None


def _extract_text(value: Any) -> str:
    """从 AI 标量或 value 包装中读取字符串。"""
    if isinstance(value, Mapping):
        value = value.get("value")
    return str(value).strip() if value is not None else ""


def _trim_text(value: str, max_chars: int | None) -> str:
    """在句子边界内把文本限制到配置长度。"""
    if max_chars is None or len(value) <= max_chars:
        return value
    prefix = value[:max_chars]
    sentence_end = max(prefix.rfind(mark) for mark in ("。", "！", "？", ".", "!", "?"))
    return prefix[: sentence_end + 1].strip() if sentence_end >= max_chars // 2 else prefix.strip()


def _has_evidence(value: str, markdown: str) -> bool:
    """判断 AI 文本是否能在原始 Markdown 中找到直接证据。"""
    normalized_value = _unescape_markdown_text(re.sub(r"\s+", " ", value).strip()).casefold()
    normalized_source = _unescape_markdown_text(re.sub(r"\s+", " ", markdown)).casefold()
    return bool(normalized_value) and normalized_value in normalized_source


def _accept_value(
    value: str,
    node: MarkdownAiNodeConfig,
    markdown: str,
) -> tuple[str, bool] | None:
    """执行证据、枚举和长度约束并返回标准化值与来源标记。"""
    if not value:
        return None
    if node.allowed_values and value not in node.allowed_values:
        return None
    has_evidence = _has_evidence(value, markdown)
    if node.require_evidence and not has_evidence and not node.allow_generate:
        return None
    return _trim_text(value, node.max_chars), not has_evidence


def _merge_text(
    target: dict[str, Any],
    key: str,
    update: Mapping[str, Any],
    update_key: str,
    node: MarkdownAiNodeConfig | None,
    markdown: str,
) -> None:
    """把一个 AI 文本槽位安全合并到 value/is_ai 包装。"""
    if node is None or update_key not in update:
        return
    accepted = _accept_value(_extract_text(update[update_key]), node, markdown)
    if accepted:
        value, is_ai = accepted
        target[key] = _value(value, is_ai=is_ai)


def _merge_product_support(
    use: dict[str, Any],
    update: Mapping[str, Any],
    node: MarkdownAiNodeConfig | None,
    markdown: str,
) -> None:
    """合并具有原文产品证据的支持矩阵。"""
    values = update.get("product_support")
    if node is None or not isinstance(values, list):
        return
    output: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        product = _extract_text(item.get("product"))
        supported = item.get("supported")
        has_evidence = _has_evidence(product, markdown)
        if not product or not isinstance(supported, bool):
            continue
        if node.require_evidence and not has_evidence and not node.allow_generate:
            continue
        output.append({"product": product, "supported": supported, "is_ai": not has_evidence})
    if output:
        use["product_support"] = output


def _merge_text_list(
    use: dict[str, Any],
    target_key: str,
    update: Mapping[str, Any],
    update_key: str,
    node: MarkdownAiNodeConfig | None,
    markdown: str,
) -> None:
    """合并 prerequisites/examples 等文本列表。"""
    values = update.get(update_key)
    if node is None or not isinstance(values, list):
        return
    output: list[dict[str, Any]] = []
    for item in values:
        accepted = _accept_value(_extract_text(item), node, markdown)
        if accepted:
            value, is_ai = accepted
            output.append(_value(value, is_ai=is_ai))
    if output:
        use[target_key] = output


def _merge_named_items(
    target: dict[str, Any],
    target_key: str,
    update: Mapping[str, Any],
    update_key: str,
    node: MarkdownAiNodeConfig | None,
    markdown: str,
) -> None:
    """合并参数或数据结构字段列表。"""
    values = update.get(update_key)
    if node is None or not isinstance(values, list):
        return
    output: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        name = _extract_text(item.get("name"))
        item_type = _extract_text(item.get("type"))
        description = _extract_text(item.get("description"))
        evidence_text = " ".join(part for part in (name, item_type, description) if part)
        has_evidence = all(_has_evidence(part, markdown) for part in (name, item_type) if part)
        if not name or (node.require_evidence and not has_evidence and not node.allow_generate):
            continue
        output.append(
            {
                "name": name,
                "type": item_type,
                "description": description,
                "is_ai": not has_evidence or not _has_evidence(evidence_text, markdown),
            }
        )
    if output:
        target[target_key] = output


def merge_ai_updates(
    document: dict[str, Any],
    updates: Mapping[str, Any],
    config: MarkdownToYamlConfig,
) -> None:
    """按 config 白名单合并一次 AI 返回的全部资源和文档槽位。"""
    markdown = document["source"]["source_markdown"]
    resource_by_id = {
        resource["resource_id"]: resource for resource in document["source"]["resources"]
    }
    resource_updates = (
        None if config.ai.image_understanding.enabled else updates.get("resource_updates")
    )
    if isinstance(resource_updates, list):
        for update in resource_updates:
            if not isinstance(update, Mapping):
                continue
            resource = resource_by_id.get(update.get("resource_id"))
            if resource is None:
                continue
            raw = resource["raw"]
            if resource["kind"] == "image":
                _merge_text(
                    raw,
                    "alt",
                    update,
                    "alt",
                    _node(config, "resources[].raw.alt"),
                    markdown,
                )
            _merge_text(
                raw,
                "title",
                update,
                "title",
                _node(config, "resources[].raw.title"),
                markdown,
            )

    document_updates = updates.get("document_updates")
    if not isinstance(document_updates, list):
        return
    for update in document_updates:
        if not isinstance(update, Mapping):
            continue
        index = update.get("document_index")
        if not isinstance(index, int) or not 0 <= index < len(document["documents"]):
            continue
        target_document = document["documents"][index]
        name_node = _node(config, "documents[].name")
        if name_node and "name" in update:
            accepted = _accept_value(_extract_text(update["name"]), name_node, markdown)
            if accepted:
                target_document["name"] = accepted[0]
        use = target_document["use"]
        _merge_text(
            use,
            "summary",
            update,
            "summary",
            _node(config, "documents[].use.summary"),
            markdown,
        )
        _merge_text(
            use,
            "category",
            update,
            "category",
            _node(config, "documents[].use.category"),
            markdown,
        )
        _merge_text(
            use,
            "description",
            update,
            "description",
            _node(config, "documents[].use.description"),
            markdown,
        )
        _merge_product_support(
            use,
            update,
            _node(config, "documents[].use.product_support"),
            markdown,
        )
        _merge_text_list(
            use,
            "prerequisites",
            update,
            "prerequisites",
            _node(config, "documents[].use.prerequisites"),
            markdown,
        )
        function_details = use["function_details"]
        _merge_named_items(
            function_details,
            "input_parameters",
            update,
            "input_parameters",
            _node(config, "documents[].use.function_details.input_parameters"),
            markdown,
        )
        _merge_named_items(
            function_details,
            "output_parameters",
            update,
            "output_parameters",
            _node(config, "documents[].use.function_details.output_parameters"),
            markdown,
        )
        _merge_text(
            function_details,
            "signature",
            update,
            "signature",
            _node(config, "documents[].use.function_details.signature"),
            markdown,
        )
        _merge_named_items(
            use["data_structure"],
            "fields",
            update,
            "data_structure_fields",
            _node(config, "documents[].use.data_structure.fields"),
            markdown,
        )
        _merge_text_list(
            use,
            "examples",
            update,
            "examples",
            _node(config, "documents[].use.examples"),
            markdown,
        )


def _validate_value(value: Any, path: str, *, max_chars: int | None = None) -> None:
    """校验 value/is_ai 包装字段。"""
    if not isinstance(value, dict) or set(value) != {"value", "is_ai"}:
        raise ValueError(f"{path} 必须只包含 value 和 is_ai")
    if value["value"] is not None and not isinstance(value["value"], str):
        raise ValueError(f"{path}.value 必须是字符串或 null")
    if not isinstance(value["is_ai"], bool):
        raise TypeError(f"{path}.is_ai 必须是 bool")
    if (
        max_chars is not None
        and isinstance(value["value"], str)
        and len(value["value"]) > max_chars
    ):
        raise ValueError(f"{path}.value 不能超过 {max_chars} 字")


def validate_v21(document: Mapping[str, Any]) -> None:
    """严格校验最小化 Schema 2.1，阻止 AI 增加字段。"""
    if set(document) != {"schema_version", "uuid", "source", "documents"}:
        raise ValueError("Schema 2.1 顶层字段不符合约定")
    if document["schema_version"] != SCHEMA_VERSION or document["uuid"] is not None:
        raise ValueError("schema_version 必须是 2.1，uuid 当前必须是 null")
    source = document["source"]
    expected_source = {
        "source_path",
        "source_url",
        "content_hash",
        "source_markdown",
        "preprocess_markdown",
        "resources",
    }
    if not isinstance(source, Mapping) or set(source) != expected_source:
        raise ValueError("source 字段不符合 Schema 2.1")
    if bool(source["source_path"]) == bool(source["source_url"]):
        raise ValueError("source_path 和 source_url 必须且只能填写一个")
    if not isinstance(source["content_hash"], str) or not source["content_hash"].startswith(
        "sha256:"
    ):
        raise ValueError("source.content_hash 必须是 sha256 摘要")
    for field in ("source_markdown", "preprocess_markdown"):
        if not isinstance(source[field], str):
            raise TypeError(f"source.{field} 必须是字符串")
    if not isinstance(source["resources"], list):
        raise TypeError("source.resources 必须是列表")
    resource_ids: set[str] = set()
    for index, resource in enumerate(source["resources"]):
        path = f"source.resources[{index}]"
        if not isinstance(resource, Mapping) or set(resource) != {"resource_id", "kind", "raw"}:
            raise ValueError(f"{path} 字段不符合约定")
        resource_id = resource["resource_id"]
        if not isinstance(resource_id, str) or not resource_id or resource_id in resource_ids:
            raise ValueError(f"{path}.resource_id 必须非空且唯一")
        resource_ids.add(resource_id)
        raw = resource["raw"]
        if resource["kind"] == "image":
            if not isinstance(raw, Mapping) or set(raw) != {"resolved_uri", "alt", "title"}:
                raise ValueError(f"{path}.raw 图片字段不符合约定")
            _validate_value(raw["alt"], f"{path}.raw.alt")
        elif resource["kind"] == "link":
            expected = {"resolved_uri", "anchor_text", "title", "criticality"}
            if not isinstance(raw, Mapping) or set(raw) != expected:
                raise ValueError(f"{path}.raw 链接字段不符合约定")
            _validate_value(raw["anchor_text"], f"{path}.raw.anchor_text")
            if raw["criticality"] not in {"low", "medium", "high"}:
                raise ValueError(f"{path}.raw.criticality 非法")
        else:
            raise ValueError(f"{path}.kind 只能是 image 或 link")
        if raw["resolved_uri"] is not None and not isinstance(raw["resolved_uri"], str):
            raise ValueError(f"{path}.raw.resolved_uri 必须是字符串或 null")
        _validate_value(raw["title"], f"{path}.raw.title")

    documents = document["documents"]
    if not isinstance(documents, list) or not documents:
        raise ValueError("documents 必须是非空列表")
    for index, item in enumerate(documents):
        path = f"documents[{index}]"
        if not isinstance(item, Mapping) or set(item) != {
            "name",
            "namespace",
            "version",
            "language",
            "use",
        }:
            raise ValueError(f"{path} 字段不符合约定")
        for field in ("name", "namespace", "version", "language"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise ValueError(f"{path}.{field} 必须是非空字符串")
        use = item["use"]
        expected_use = {
            "summary",
            "category",
            "description",
            "product_support",
            "prerequisites",
            "function_details",
            "data_structure",
            "examples",
        }
        if not isinstance(use, Mapping) or set(use) != expected_use:
            raise ValueError(f"{path}.use 字段不符合约定")
        _validate_value(use["summary"], f"{path}.use.summary")
        _validate_value(use["category"], f"{path}.use.category")
        if use["category"]["value"] not in ALLOWED_CATEGORIES:
            raise ValueError(f"{path}.use.category.value 非法")
        _validate_value(use["description"], f"{path}.use.description", max_chars=300)
        for item_index, support in enumerate(use["product_support"]):
            if not isinstance(support, Mapping) or set(support) != {
                "product",
                "supported",
                "is_ai",
            }:
                raise ValueError(f"{path}.use.product_support[{item_index}] 字段非法")
            if (
                not isinstance(support["product"], str)
                or not isinstance(support["supported"], bool)
                or not isinstance(support["is_ai"], bool)
            ):
                raise TypeError(f"{path}.use.product_support[{item_index}] 类型非法")
        for list_name in ("prerequisites", "examples"):
            if not isinstance(use[list_name], list):
                raise TypeError(f"{path}.use.{list_name} 必须是列表")
            for item_index, value in enumerate(use[list_name]):
                _validate_value(value, f"{path}.use.{list_name}[{item_index}]")
        function_details = use["function_details"]
        if not isinstance(function_details, Mapping) or set(function_details) != {
            "input_parameters",
            "output_parameters",
            "signature",
        }:
            raise ValueError(f"{path}.use.function_details 字段非法")
        _validate_value(function_details["signature"], f"{path}.use.function_details.signature")
        data_structure = use["data_structure"]
        if not isinstance(data_structure, Mapping) or set(data_structure) != {"fields"}:
            raise ValueError(f"{path}.use.data_structure 字段非法")
        for list_path, values in (
            ("function_details.input_parameters", function_details["input_parameters"]),
            ("function_details.output_parameters", function_details["output_parameters"]),
            ("data_structure.fields", data_structure["fields"]),
        ):
            if not isinstance(values, list):
                raise TypeError(f"{path}.use.{list_path} 必须是列表")
            for item_index, value in enumerate(values):
                if not isinstance(value, Mapping) or set(value) != {
                    "name",
                    "type",
                    "description",
                    "is_ai",
                }:
                    raise ValueError(f"{path}.use.{list_path}[{item_index}] 字段非法")
                if any(
                    not isinstance(value[field], str) for field in ("name", "type", "description")
                ):
                    raise ValueError(f"{path}.use.{list_path}[{item_index}] 文本类型非法")
                if not isinstance(value["is_ai"], bool):
                    raise TypeError(f"{path}.use.{list_path}[{item_index}].is_ai 必须是 bool")


def run_pipeline(
    *,
    markdown: str,
    source_path: Path,
    source_url: str | None,
    hints: Mapping[str, str | None],
    config: MarkdownToYamlConfig,
    project_root: Path,
    ai_call: Callable[[str], str] | None,
    image_ai_call: ImageAiCall | None = None,
) -> PipelineResult:
    """执行规则预处理、图片理解、一次文本槽位填充和 Schema 2.1 校验。"""
    resource_scan_markdown = mask_code_for_resource_scan(markdown)
    resources, resource_evidence = extract_resources(
        markdown,
        source_url,
        source_path,
        image_base_url=config.preprocess.image_base_url,
        scan_markdown=resource_scan_markdown,
    )
    preprocess, evidence = preprocess_markdown(markdown, config)
    evidence["resources"] = resource_evidence
    image_prompts: list[str] = []
    image_responses: list[str] = []
    image_config = config.ai.image_understanding
    if config.ai.enabled and image_config.enabled:
        if image_config.max_images_per_call <= 0:
            raise ValueError("image_understanding.max_images_per_call 必须大于 0")
        if image_config.max_description_chars <= 0:
            raise ValueError("image_understanding.max_description_chars 必须大于 0")
        image_resources = _image_resources_for_ai(resources, evidence, config)
        if image_resources and image_ai_call is None:
            raise ValueError("图片理解已启用但没有可用的多模态调用器")
        for start in range(0, len(image_resources), image_config.max_images_per_call):
            batch = image_resources[start : start + image_config.max_images_per_call]
            image_prompt = build_image_prompt(batch, config, project_root)
            assert image_ai_call is not None
            image_response = image_ai_call(image_prompt, batch)
            merge_image_updates(
                resources,
                parse_ai_updates(image_response),
                config,
            )
            image_prompts.append(image_prompt)
            image_responses.append(image_response)
    document = build_skeleton(
        markdown=markdown,
        source_path=source_path,
        source_url=source_url,
        preprocess=preprocess,
        resources=resources,
        evidence=evidence,
        hints=hints,
        config=config,
    )
    prompt: str | None = None
    response: str | None = None
    if config.ai.enabled and _enabled_nodes(config):
        if not config.ai.single_pass:
            raise ValueError("Schema 2.1 Pipeline 当前只允许 single_pass: true")
        if ai_call is None:
            raise ValueError("AI 已启用但没有可用的调用器")
        prompt = build_ai_prompt(document, evidence, config, project_root)
        response = ai_call(prompt)
        merge_ai_updates(document, parse_ai_updates(response), config)
    validate_v21(document)
    return PipelineResult(
        document=document,
        evidence=evidence,
        image_prompts=image_prompts,
        image_responses=image_responses,
        ai_prompt=prompt,
        ai_response=response,
    )
