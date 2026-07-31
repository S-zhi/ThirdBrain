"""容错扫描非规范 Markdown，并审计非代码区域图片能否正常显示。"""

from __future__ import annotations

import argparse
import base64
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urlparse

import httpx

from config import get_config
from src.script.markdown_yaml_v21 import (
    extract_resources,
    find_unparsed_markdown_images,
    mask_code_for_resource_scan,
)

DEFAULT_ROOT = Path("API参考")
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_WORKERS = 16
DEFAULT_EXAMPLE_LIMIT = 20


def _looks_like_image(content_type: str, prefix: bytes) -> bool:
    """根据响应类型和文件头判断对象是否可作为图片显示。"""
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type.startswith("image/"):
        return True
    stripped = prefix.lstrip()
    signatures = (
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"GIF87a",
        b"GIF89a",
        b"BM",
        b"II*\x00",
        b"MM\x00*",
    )
    if any(prefix.startswith(signature) for signature in signatures):
        return True
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return True
    if b"ftypavif" in prefix[:32] or b"ftypavis" in prefix[:32]:
        return True
    return stripped.startswith((b"<svg", b"<?xml")) and b"<svg" in stripped[:1_024]


def _check_data_image(uri: str) -> dict[str, Any]:
    """离线校验 data:image URI 是否包含完整且可识别的图片数据。"""
    try:
        metadata, payload = uri.split(",", 1)
        content_type = metadata[5:].split(";", 1)[0].lower()
        binary = (
            base64.b64decode(payload, validate=True)
            if ";base64" in metadata.lower()
            else unquote_to_bytes(payload)
        )
        displayable = content_type.startswith("image/") and _looks_like_image(
            content_type,
            binary[:4_096],
        )
        if content_type == "image/svg+xml":
            normalized = binary.lower()
            displayable = (
                b"<svg" in normalized[:1_024]
                and b"</svg>" in normalized
            )
        return {
            "url": uri,
            "displayable": displayable,
            "status_code": None,
            "content_type": content_type,
            "final_url": uri,
            "reason": None if displayable else "invalid_data_image",
        }
    except (ValueError, TypeError):
        return {
            "url": uri,
            "displayable": False,
            "status_code": None,
            "content_type": None,
            "final_url": uri,
            "reason": "invalid_data_image",
        }


def _read_response_prefix(response: httpx.Response, limit: int = 4_096) -> bytes:
    """从流式响应读取足够识别格式的前缀并立即停止。"""
    prefix = bytearray()
    for chunk in response.iter_bytes():
        prefix.extend(chunk[: max(0, limit - len(prefix))])
        if len(prefix) >= limit:
            break
    return bytes(prefix)


def check_image_url(url: str, timeout_seconds: float) -> dict[str, Any]:
    """联网检查一个图片 URL 的状态码、响应类型和文件头。"""
    headers = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": "https://www.hiascend.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
        ),
    }
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout_seconds,
            headers=headers,
        ) as client, client.stream("GET", url) as response:
            prefix = _read_response_prefix(response)
            content_type = response.headers.get("content-type", "")
            displayable = response.is_success and _looks_like_image(content_type, prefix)
            if not response.is_success:
                reason = f"http_{response.status_code}"
            elif not displayable:
                reason = f"not_image:{content_type or 'missing_content_type'}"
            else:
                reason = None
            return {
                "url": url,
                "displayable": displayable,
                "status_code": response.status_code,
                "content_type": content_type,
                "final_url": str(response.url),
                "reason": reason,
            }
    except httpx.TimeoutException:
        return {
            "url": url,
            "displayable": False,
            "status_code": None,
            "content_type": None,
            "final_url": None,
            "reason": "timeout",
        }
    except httpx.HTTPError as exc:
        return {
            "url": url,
            "displayable": False,
            "status_code": None,
            "content_type": None,
            "final_url": None,
            "reason": f"network:{type(exc).__name__}",
        }


def scan_markdown_images(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """扫描目录内所有 Markdown 的非代码图片出现位置。"""
    resolved_root = root.expanduser().resolve()
    occurrences: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    image_base_url = get_config().markdown_to_yaml.preprocess.image_base_url
    for path in sorted(
        candidate
        for candidate in resolved_root.rglob("*")
        if candidate.is_file() and candidate.suffix.lower() in {".md", ".markdown"}
    ):
        try:
            markdown = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        masked = mask_code_for_resource_scan(markdown)
        for invalid in find_unparsed_markdown_images(masked):
            occurrences.append(
                {
                    "path": str(path),
                    "line_number": invalid["line_number"],
                    "raw_uri": invalid["raw_markdown"],
                    "resolved_uri": None,
                    "was_relative": False,
                    "reason": "unparsed_image_syntax",
                }
            )
        resources, evidence = extract_resources(
            markdown,
            source_url=None,
            source_path=path,
            image_base_url=image_base_url,
            scan_markdown=masked,
            deduplicate=False,
        )
        for resource, resource_evidence in zip(resources, evidence, strict=True):
            if resource["kind"] != "image":
                continue
            raw_uri = str(resource_evidence["raw_uri"])
            resolved_uri = resource["raw"]["resolved_uri"]
            occurrences.append(
                {
                    "path": str(path),
                    "line_number": resource_evidence["line_number"],
                    "raw_uri": raw_uri,
                    "resolved_uri": resolved_uri,
                    "was_relative": not bool(urlparse(raw_uri).scheme),
                }
            )
    return occurrences, errors


def count_image_markers(root: Path) -> tuple[int, int]:
    """统计全部及非代码区域的 Markdown ``![`` 标记数量。"""
    raw_count = 0
    outside_code_count = 0
    for path in root.expanduser().resolve().rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".md", ".markdown"}:
            continue
        try:
            markdown = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        raw_count += markdown.count("![")
        outside_code_count += mask_code_for_resource_scan(markdown).count("![")
    return raw_count, outside_code_count


def _select_failure_examples(
    failures: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """按失败原因均衡抽取 case，避免单一高频原因占满报告。"""
    if limit <= 0:
        return []
    by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for failure in failures:
        by_reason[str(failure.get("reason") or "unknown")].append(failure)
    selected: list[dict[str, Any]] = []
    per_reason = max(1, limit // max(1, len(by_reason)))
    for reason in sorted(by_reason):
        selected.extend(by_reason[reason][:per_reason])
    if len(selected) < limit:
        selected_ids = {id(item) for item in selected}
        selected.extend(
            failure
            for failure in failures
            if id(failure) not in selected_ids
        )
    return selected[:limit]


def audit_images(
    root: Path,
    *,
    workers: int,
    timeout_seconds: float,
    example_limit: int,
) -> dict[str, Any]:
    """扫描本地语料并并发检查所有唯一图片 URL。"""
    occurrences, scan_errors = scan_markdown_images(root)
    occurrence_by_url: dict[str, list[dict[str, Any]]] = defaultdict(list)
    invalid_occurrences: list[dict[str, Any]] = []
    for occurrence in occurrences:
        resolved_uri = occurrence["resolved_uri"]
        parsed = urlparse(str(resolved_uri or ""))
        if parsed.scheme.lower() == "data":
            occurrence_by_url[str(resolved_uri)].append(occurrence)
            continue
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            invalid_occurrences.append(
                {
                    **occurrence,
                    "reason": occurrence.get("reason") or "invalid_resolved_uri",
                }
            )
            continue
        occurrence_by_url[str(resolved_uri)].append(occurrence)

    checks = {
        url: _check_data_image(url)
        for url in occurrence_by_url
        if url.startswith("data:image/")
    }
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="image-audit") as executor:
        futures = {
            executor.submit(check_image_url, url, timeout_seconds): url
            for url in occurrence_by_url
            if not url.startswith("data:")
        }
        for future in as_completed(futures):
            url = futures[future]
            checks[url] = future.result()

    failed_unique = {
        url: result for url, result in checks.items() if not result["displayable"]
    }
    failed_occurrences = list(invalid_occurrences)
    for url, result in failed_unique.items():
        failed_occurrences.extend(
            {
                **occurrence,
                "reason": result["reason"],
                "status_code": result["status_code"],
                "content_type": result["content_type"],
                "final_url": result["final_url"],
            }
            for occurrence in occurrence_by_url[url]
        )
    reason_counts = Counter(
        str(occurrence.get("reason") or "unknown")
        for occurrence in failed_occurrences
    )
    markdown_files = {
        candidate
        for candidate in root.expanduser().resolve().rglob("*")
        if candidate.is_file() and candidate.suffix.lower() in {".md", ".markdown"}
    }
    invalid_unique_keys = {
        (str(item.get("reason")), str(item.get("raw_uri")))
        for item in invalid_occurrences
    }
    raw_markers, outside_code_markers = count_image_markers(root)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "root": str(root.expanduser().resolve()),
        "scanned_markdown_files": len(markdown_files),
        "markdown_files_with_images": len(
            {occurrence["path"] for occurrence in occurrences}
        ),
        "raw_markdown_image_markers": raw_markers,
        "image_markers_in_code": raw_markers - outside_code_markers,
        "image_markers_outside_code": outside_code_markers,
        "image_occurrences_outside_code": len(occurrences),
        "unparsed_image_occurrences": sum(
            occurrence.get("reason") == "unparsed_image_syntax"
            for occurrence in occurrences
        ),
        "relative_image_occurrences": sum(
            1 for occurrence in occurrences if occurrence["was_relative"]
        ),
        "unique_resolved_images": len(occurrence_by_url),
        "unique_image_objects_including_unparsed": (
            len(occurrence_by_url) + len(invalid_unique_keys)
        ),
        "displayable_unique_images": len(checks) - len(failed_unique),
        "failed_unique_images": len(failed_unique) + len(invalid_unique_keys),
        "failed_image_occurrences": len(failed_occurrences),
        "failure_reason_counts": dict(sorted(reason_counts.items())),
        "scan_errors": scan_errors,
        "failed_examples": _select_failure_examples(
            failed_occurrences,
            example_limit,
        ),
    }


def write_report(path: Path, report: dict[str, Any]) -> Path:
    """原子写入 JSON 审计报告。"""
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_suffix(f"{resolved.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(resolved)
    return resolved


def build_parser() -> argparse.ArgumentParser:
    """构造图片审计 CLI 参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=DEFAULT_ROOT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--examples", type=int, default=DEFAULT_EXAMPLE_LIMIT)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    """运行图片审计并输出完整 JSON 报告。"""
    args = build_parser().parse_args()
    if args.workers <= 0 or args.timeout <= 0 or args.examples < 0:
        raise SystemExit("workers/timeout 必须大于 0，examples 不能小于 0")
    report = audit_images(
        args.root,
        workers=args.workers,
        timeout_seconds=args.timeout,
        example_limit=args.examples,
    )
    if args.output:
        report["report_path"] = str(write_report(args.output, report))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failed_image_occurrences"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
