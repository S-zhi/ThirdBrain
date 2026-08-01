"""tmp/retry_failed_10.py — 精准重抓那 10 个 failed 页面。

策略：
  1. 复用 sync_docs 内部的 HiascendSourceAdapter
  2. 直接对 10 个 URL 调 fetch + parse
  3. 写入 API参考/ + 写回 data/doc_sync/state/hiascend-cann-910beta3.json
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import yaml

ROOT = Path("/Users/wenzhengfeng/code/agent/ragWithColdApiDocument")
sys.path.insert(0, str(ROOT))

from src.doc_sync import load_document_sync_config
from src.doc_sync.adapters.hiascend import HiascendSourceAdapter
from src.doc_sync.service import DocumentSyncService

FAILED_URLS = [
    "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/atlasascendc_api_07_10053.html",
    "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/atlasascendc_api_07_10108.html",
    "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/atlasascendc_api_07_10109.html",
    "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/atlasascendc_api_07_10110.html",
    "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/atlasascendc_api_07_10183.html",
    "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/atlasascendc_api_07_10184.html",
    "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/atlasascendc_api_07_10444.html",
    "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/atlasascendc_api_07_10445.html",
    "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/atlasascendc_api_07_10446.html",
    "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/atlasascendc_api_07_10447.html",
]


async def main() -> int:
    config = load_document_sync_config(ROOT / "configs/document_sync.yaml")
    source = config.sources[0]
    # config.sources[0].adapter.options 可能是 dict（HiascendAdapterConfig）
    opts = source.adapter.options
    if isinstance(opts, dict):
        from src.doc_sync.adapters.hiascend import HiascendAdapterConfig
        opts = HiascendAdapterConfig(**opts)
    adapter = HiascendSourceAdapter(source_id=source.id, options=opts)

    started = time.time()
    success = 0
    failed = []
    written = []

    try:
        # 适配器启动（拉起 chrome browser context）
        await adapter._ensure_browser_context()
        for url in FAILED_URLS:
            # 构造 DocumentRef（最小字段）
            from src.doc_sync.models import DocumentRef

            page_id = adapter._page_id(url)
            ref = DocumentRef(
                source_id=source.id,
                document_id=page_id,
                canonical_uri=url,
                metadata={"page_id": page_id, "external_id": f"manual-retry-{page_id}"},
            )
            try:
                from src.doc_sync.adapters.base import AdapterContext
                from src.doc_sync.http import HttpFetchClient

                http = HttpFetchClient(config.http_defaults, config.policies.redirects)
                async with http:
                    context = AdapterContext(run_id="retry-10", http=http)
                    result = await adapter.fetch(ref, context)
                    if result.status_code != 200:
                        failed.append((url, f"HTTP {result.status_code}"))
                        print(f"  ❌ {page_id}: HTTP {result.status_code}")
                        continue
                    document = adapter.parse(ref, result)
                    if not document.normalized_content:
                        failed.append((url, "empty normalized_content"))
                        print(f"  ❌ {page_id}: empty content")
                        continue
                    # relative_path 由 doc_sync 内部决定（基于 hierarchy + title）；用 source id 索引
                    # 简化：直接由 page_id 推 relative_path
                    rel_path = f"API参考/SIMT_API/{page_id}.md"  # 保守默认
                    # 写 .md（前后加 frontmatter/source）
                    md_content = (
                        f"# {document.title or page_id}\n\n"
                        f"> 来源: {url}\n"
                        f"> 节点: `manual-retry-{page_id}`\n\n"
                        f"---\n\n"
                        f"{document.normalized_content}\n"
                    )
                    api_path = ROOT / rel_path
                    api_path.parent.mkdir(parents=True, exist_ok=True)
                    api_path.write_text(md_content, encoding="utf-8")
                    written.append(rel_path)
                    success += 1
                    print(f"  ✅ {page_id}: {len(md_content)} bytes → {rel_path}")
            except Exception as e:  # noqa: BLE001
                failed.append((url, f"{type(e).__name__}: {e}"))
                print(f"  ❌ {page_id}: {type(e).__name__}: {e}")
    finally:
        await adapter.aclose()

    elapsed = time.time() - started
    print()
    print(f"=== retry_10 完成 ===")
    print(f"  success: {success}/{len(FAILED_URLS)}")
    print(f"  failed:  {len(failed)}")
    print(f"  written: {len(written)}")
    print(f"  elapsed: {elapsed:.1f}s")
    if failed:
        for url, err in failed:
            print(f"    {url}: {err}")
    return 0 if success == len(FAILED_URLS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
