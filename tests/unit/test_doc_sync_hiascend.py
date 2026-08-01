"""昇腾文档来源 Adapter 的解析、发现和资源生命周期测试。"""

import asyncio
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from bs4 import BeautifulSoup

from src.doc_sync.adapters.base import AdapterContext
from src.doc_sync.adapters.hiascend import (
    HiascendAdapterConfig,
    HiascendSourceAdapter,
)
from src.doc_sync.models import DocumentRef, FetchResult

BASE = "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/"


def _adapter() -> HiascendSourceAdapter:
    """构造使用测试选择器的昇腾 Adapter。"""
    options = HiascendAdapterConfig.model_validate(
        {
            "product": "CANNCommunityEdition",
            "version": "910beta3",
            "language": "zh",
            "root_urls": [f"{BASE}atlasascendc_api_07_0003.html"],
            "allowed_hosts": ["www.hiascend.com"],
            "allowed_path_prefixes": [
                "/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/"
            ],
            "document_url_pattern": (
                r"(?:(?:atlasascendc_api|atlas_ascendc)_[0-9A-Za-z_]+\.html|[^/]+\.md)$"
            ),
            "selectors": {
                "article_body": ".the-article-body",
                "title": ["h1"],
                "parent_links": [".familylinks a"],
            },
            "existing_document": {
                "source_url_pattern": r"^>\s*来源:\s*(?P<value>https?://.+)$",
                "external_id_pattern": r"^>\s*节点:\s*`?(?P<value>[^`]+)`?$",
            },
        }
    )
    return HiascendSourceAdapter("hiascend-test", options)


def test_hiascend_bootstrap_reads_existing_metadata(tmp_path: Path) -> None:
    """现有 Markdown 的 URI、节点和路径应进入注册表。"""
    path = tmp_path / "SIMD" / "Example.md"
    path.parent.mkdir()
    path.write_text(
        f"# Example\n\n> 来源: {BASE}atlasascendc_api_07_0004.html\n> 节点: `node-4`\n",
        encoding="utf-8",
    )
    refs = _adapter().bootstrap(tmp_path)
    assert len(refs) == 1
    assert refs[0].document_id == "atlasascendc_api_07_0004"
    assert refs[0].relative_path_hint == "SIMD/Example.md"
    assert refs[0].metadata["node_id"] == "node-4"
    assert refs[0].metadata["hierarchy"] == ["SIMD"]


def test_hiascend_bootstrap_prefers_deeper_existing_path(tmp_path: Path) -> None:
    """同一来源 URL 出现重复文件时应保留原有完整层级。"""
    shallow = tmp_path / "Kernel Tiling" / "REGISTER_TILING_DEFAULT.md"
    deep = tmp_path / "SIMD_API" / "基础API" / "Kernel_Tiling" / shallow.name
    shallow.parent.mkdir(parents=True)
    deep.parent.mkdir(parents=True)
    source = f"> 来源: {BASE}atlasascendc_api_07_0003.html\n"
    shallow.write_text(f"# duplicate\n\n{source}", encoding="utf-8")
    deep.write_text(f"# canonical\n\n{source}", encoding="utf-8")
    os.utime(shallow, (1, 1))
    os.utime(deep, (1, 1))

    refs = _adapter().bootstrap(tmp_path)

    assert len(refs) == 1
    assert refs[0].relative_path_hint == "SIMD_API/基础API/Kernel_Tiling/REGISTER_TILING_DEFAULT.md"
    assert refs[0].metadata["hierarchy"] == ["SIMD_API", "基础API", "Kernel_Tiling"]


def test_hiascend_discovery_reuses_known_hierarchy_path(tmp_path: Path) -> None:
    """正文发现已登记 URL 时必须复用原 Markdown 的层级路径。"""
    child_uri = f"{BASE}atlasascendc_api_07_0005.html"
    child = tmp_path / "SIMD_API" / "基础API" / "Memory矢量计算" / "Add.md"
    child.parent.mkdir(parents=True)
    child.write_text(f"# Add\n\n> 来源: {child_uri}\n", encoding="utf-8")
    adapter = _adapter()
    adapter.bootstrap(tmp_path)
    article = BeautifulSoup(
        '<div class="the-article-body"><a href="atlasascendc_api_07_0005.html">Add</a></div>',
        "html.parser",
    ).select_one(".the-article-body")
    assert article is not None
    ref = DocumentRef(
        source_id="hiascend-test",
        document_id="atlasascendc_api_07_0004",
        canonical_uri=f"{BASE}atlasascendc_api_07_0004.html",
        metadata={"hierarchy": ["SIMD_API", "基础API"]},
    )

    discovered = adapter._discover_from_article(
        article,
        ref,
        "Parent",
        ref.canonical_uri,
        ["SIMD_API", "基础API"],
    )

    assert discovered[0].relative_path_hint == "SIMD_API/基础API/Memory矢量计算/Add.md"


def test_hiascend_title_refines_inherited_hierarchy(tmp_path: Path) -> None:
    """未登记新页面应使用网页标题补全继承到的父级层级。"""
    known = tmp_path / "SIMD_API" / "基础API" / "Kernel_Tiling" / "Known.md"
    known.parent.mkdir(parents=True)
    known.write_text(
        f"# Known\n\n> 来源: {BASE}atlasascendc_api_07_0004.html\n",
        encoding="utf-8",
    )
    adapter = _adapter()
    adapter.bootstrap(tmp_path)
    ref = DocumentRef(
        source_id="hiascend-test",
        document_id="atlasascendc_api_07_0099",
        canonical_uri=f"{BASE}atlasascendc_api_07_0099.html",
        metadata={"hierarchy": ["SIMD_API"]},
    )
    soup = BeautifulSoup(
        "<html><head><title>New-Kernel Tiling-基础API-SIMD API-Ascend C API</title></head>"
        "<body><div class='the-article-body'>body content</div></body></html>",
        "html.parser",
    )

    assert adapter._extract_hierarchy(soup, ref, "New") == [
        "SIMD_API",
        "基础API",
        "Kernel_Tiling",
    ]


def test_hiascend_parse_normalizes_body_and_discovers_links() -> None:
    """正文转换应排除脚本并发现同版本链接。"""
    adapter = _adapter()
    uri = f"{BASE}atlasascendc_api_07_0004.html"
    html = """
    <html><body>
      <nav>动态导航</nav>
      <div class="familylinks"><a href="index.html">SIMD API</a></div>
      <div class="the-article-body">
        <h1>Example API</h1>
        <p>执行一个确定性的操作。</p>
        <pre><code>void Example();</code></pre>
        <a href="atlasascendc_api_07_0005.html">Next API</a>
        <script>volatile()</script>
      </div>
    </body></html>
    """.encode()
    ref = DocumentRef(
        source_id="hiascend-test",
        document_id="atlasascendc_api_07_0004",
        canonical_uri=uri,
        metadata={"node_id": "node-4", "hierarchy": ["SIMD API"]},
    )
    result = FetchResult(
        requested_uri=uri,
        final_uri=uri,
        status_code=200,
        content_type="text/html",
        body=html,
        fetched_at=datetime.now(UTC),
        response_hash=hashlib.sha256(html).hexdigest(),
    )
    document = adapter.parse(ref, result)
    assert document.title == "Example API"
    assert "动态导航" not in document.normalized_content
    assert "volatile" not in document.normalized_content
    assert "void Example();" in document.artifact_content
    assert document.metadata["node_id"] == "node-4"
    assert document.hierarchy == ["SIMD API"]
    assert [item.document_id for item in document.discovered_refs] == ["atlasascendc_api_07_0005"]
    assert document.discovered_refs[0].metadata["hierarchy"] == ["SIMD API"]
    assert adapter.propose_relative_path(document).as_posix() == "SIMD API/Example API.md"


def test_hiascend_prefers_embedded_latex_and_removes_katex_duplicates() -> None:
    """KaTeX 应只输出 annotation 中的单份块级 LaTeX。"""
    article = BeautifulSoup(
        """
        <div class="the-article-body">
          <h1>Formula API</h1>
          <p>计算公式如下：</p>
          <span class="katex-display"><span class="katex">
            <span class="katex-mathml"><math display="block"><semantics>
              <mrow><mi>duplicated-mathml</mi></mrow>
              <annotation encoding="application/x-tex">dst_i = \\frac{src_i}{scale}</annotation>
            </semantics></math></span>
            <span class="katex-html" aria-hidden="true">duplicated-rendered</span>
          </span></span>
        </div>
        """,
        "html.parser",
    ).select_one(".the-article-body")
    assert article is not None

    markdown = _adapter()._prepare_article(article, BASE, "Formula API")

    assert "$$\ndst_i = \\frac{src_i}{scale}\n$$" in markdown
    assert markdown.count("dst_i") == 1
    assert "duplicated-mathml" not in markdown
    assert "duplicated-rendered" not in markdown


def test_hiascend_preserves_inline_latex_formula() -> None:
    """行内 KaTeX 应恢复为单美元符包围的 LaTeX。"""
    article = BeautifulSoup(
        """
        <div class="the-article-body"><h1>Formula API</h1><p>
          value=<span class="katex"><span class="katex-mathml"><math><semantics>
          <annotation encoding="application/x-tex">x_i + 1</annotation>
          </semantics></math></span><span class="katex-html">duplicate</span></span>
        </p></div>
        """,
        "html.parser",
    ).select_one(".the-article-body")
    assert article is not None

    markdown = _adapter()._prepare_article(article, BASE, "Formula API")

    assert "value=$x_i + 1$" in markdown
    assert "duplicate" not in markdown


def test_hiascend_formula_without_annotation_uses_single_visible_value() -> None:
    """缺少 LaTeX annotation 时只保留一份可见公式文本。"""
    article = BeautifulSoup(
        """
        <div class="the-article-body"><h1>Formula API</h1><p>
          <span class="katex"><span class="katex-mathml">duplicate</span>
          <span class="katex-html">x₁+1</span></span>
        </p></div>
        """,
        "html.parser",
    ).select_one(".the-article-body")
    assert article is not None

    markdown = _adapter()._prepare_article(article, BASE, "Formula API")

    assert markdown.count("x₁+1") == 1
    assert "duplicate" not in markdown


def test_hiascend_nested_markdown_urls_have_unique_document_ids(
    tmp_path: Path,
) -> None:
    """相同文件名的嵌套 Markdown 来源必须形成不同稳定身份。"""
    first = tmp_path / "Memory" / "asc_add.md"
    second = tmp_path / "Register" / "asc_add.md"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text(
        f"# Memory\n\n> 来源: {BASE}context/vector_compute/asc_add.md\n> 节点: `node-memory`\n",
        encoding="utf-8",
    )
    second.write_text(
        f"# Register\n\n> 来源: {BASE}context/reg/reg_vector/asc_add.md\n> 节点: `node-register`\n",
        encoding="utf-8",
    )

    refs = _adapter().bootstrap(tmp_path)

    assert {ref.document_id for ref in refs} == {
        "context::reg::reg_vector::asc_add",
        "context::vector_compute::asc_add",
    }
    assert {ref.metadata["page_id"] for ref in refs} == {"asc_add"}


def test_hiascend_dynamic_heading_uses_title_hint_for_numbered_html() -> None:
    """编号 HTML 路由应按文档标题等待动态正文。"""
    adapter = _adapter()
    ref = DocumentRef(
        source_id="hiascend-test",
        document_id="atlasascendc_api_07_0207",
        canonical_uri=f"{BASE}atlasascendc_api_07_0207.html",
        title_hint="WaitPreBlock",
    )

    assert adapter._expected_dynamic_heading(ref) == "WaitPreBlock"


def test_hiascend_reuses_browser_between_pages(monkeypatch) -> None:
    """连续页面应复用一个浏览器和上下文，并只关闭各自的 Page。"""
    import src.doc_sync.adapters.hiascend as hiascend_module

    counters = {
        "launch": 0,
        "new_context": 0,
        "new_page": 0,
        "page_close": 0,
        "browser_close": 0,
        "playwright_stop": 0,
    }

    class FakeResponse:
        status = 200

    class FakeLocator:
        @property
        def first(self):
            return self

        def filter(self, **_kwargs):
            raise AssertionError("抓取阶段不应按旧文件名精确过滤 H1")

        async def wait_for(self, **_kwargs):
            return None

    class FakePage:
        url = "https://www.hiascend.com/final"

        async def goto(self, *_args, **_kwargs):
            return FakeResponse()

        def locator(self, _selector):
            return FakeLocator()

        async def content(self):
            return (
                "<html><div class='the-article-body'><h1>Example</h1>"
                "<p>rendered body content</p></div></html>"
            )

        async def close(self):
            counters["page_close"] += 1

    class FakeBrowserContext:
        async def new_page(self):
            counters["new_page"] += 1
            return FakePage()

        async def close(self):
            return None

    class FakeBrowser:
        def is_connected(self):
            return True

        async def new_context(self):
            counters["new_context"] += 1
            return FakeBrowserContext()

        async def close(self):
            counters["browser_close"] += 1

    class FakeChromium:
        async def launch(self, **_kwargs):
            counters["launch"] += 1
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self):
            counters["playwright_stop"] += 1

    class FakePlaywrightStarter:
        async def start(self):
            return FakePlaywright()

    monkeypatch.setattr(hiascend_module, "async_playwright", lambda: FakePlaywrightStarter())
    monkeypatch.setattr(hiascend_module, "write_text_atomic", lambda *_args: None)
    adapter = _adapter()

    async def fetch_two_pages():
        context = AdapterContext(run_id="browser-reuse-test")
        for index in range(2):
            uri = f"{BASE}atlasascendc_api_07_000{4 + index}.html"
            ref = DocumentRef(
                source_id="hiascend-test",
                document_id=f"page-{index}",
                canonical_uri=uri,
                title_hint="asc_div",
            )
            result = await adapter.fetch(ref, context)
            assert result.status_code == 200
        await adapter.aclose()

    asyncio.run(fetch_two_pages())

    assert counters == {
        "launch": 1,
        "new_context": 1,
        "new_page": 2,
        "page_close": 2,
        "browser_close": 1,
        "playwright_stop": 1,
    }


def test_hiascend_degrades_to_http_after_browser_rate_limit(monkeypatch) -> None:
    """浏览器连续限流后应退避并切换共享 HTTP Client。"""
    import src.doc_sync.adapters.hiascend as hiascend_module

    adapter = _adapter()
    adapter.options = adapter.options.model_copy(
        update={
            "browser": adapter.options.browser.model_copy(
                update={"retry_attempts": 2, "retry_initial_backoff_seconds": 0.01}
            )
        }
    )
    calls = {"browser": 0, "http": 0}

    async def failed_browser(_ref):
        calls["browser"] += 1
        raise hiascend_module._BrowserDegradeError("浏览器响应 HTTP 429")

    class FakeHTTP:
        async def fetch(self, uri, *, uri_validator):
            calls["http"] += 1
            assert uri_validator(uri)
            body = b"<html><div class='the-article-body'>SSR fallback</div></html>"
            return FetchResult(
                requested_uri=uri,
                final_uri=uri,
                status_code=200,
                content_type="text/html",
                body=body,
                fetched_at=datetime.now(UTC),
                response_hash=hashlib.sha256(body).hexdigest(),
            )

    monkeypatch.setattr(adapter, "_fetch_browser_once", failed_browser)
    monkeypatch.setattr(hiascend_module, "write_text_atomic", lambda *_args: None)
    ref = DocumentRef(
        source_id="hiascend-test",
        document_id="context::vector_compute::asc_add",
        canonical_uri=f"{BASE}context/vector_compute/asc_add.md",
        title_hint="asc_add",
    )

    result = asyncio.run(adapter.fetch(ref, AdapterContext("fallback-test", FakeHTTP())))

    assert calls == {"browser": 2, "http": 1}
    assert result.metadata["fetch_mode"] == "http_fallback"
    assert result.metadata["degraded"] is True
    assert "429" in result.metadata["degrade_reason"]


def test_hiascend_selects_matching_article_from_dynamic_html() -> None:
    """动态 HTML 同时挂载多篇正文时应选中当前 API。"""
    adapter = _adapter()
    ref = DocumentRef(
        source_id="hiascend-test",
        document_id="context::vector_compute::asc_target",
        canonical_uri=f"{BASE}context/vector_compute/asc_target.md",
        title_hint="asc_target",
    )
    soup = BeautifulSoup(
        """
        <div class="the-article-body"><h1 id="asc_neighbor">asc_neighbor</h1></div>
        <div class="the-article-body"><h1 id="asc_target">asc_target</h1></div>
        """,
        "html.parser",
    )

    article = adapter._select_article(soup, ref)

    assert article.h1 is not None
    assert article.h1.get_text(strip=True) == "asc_target"


def test_hiascend_selects_by_data_item_when_titles_are_ambiguous() -> None:
    """同名 API 的多个架构正文应按唯一 data-item 路径选择。"""
    adapter = _adapter()
    ref = DocumentRef(
        source_id="hiascend-test",
        document_id="context::cube_datamove::asc_copy_l0c2gm::asc_copy_l0c2gm_arch_2201",
        canonical_uri=(
            f"{BASE}context/cube_datamove/asc_copy_l0c2gm/"
            "asc_copy_l0c2gm_arch_2201.md"
        ),
        title_hint="asc_copy_l0c2gm",
    )
    soup = BeautifulSoup(
        """
        <div class="waterfull-item" data-item="/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/context/cube_datamove/asc_copy_l0c2gm/asc_copy_l0c2gm_arch_3510.md">
          <div class="the-article-body"><h1>asc_copy_l0c2gm</h1><p>3510</p></div>
        </div>
        <div class="waterfull-item" data-item="/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/context/cube_datamove/asc_copy_l0c2gm/asc_copy_l0c2gm_arch_2201.md">
          <div class="the-article-body"><h1>asc_copy_l0c2gm</h1><p>2201</p></div>
        </div>
        """,
        "html.parser",
    )

    article = adapter._select_article(soup, ref)

    assert article.p is not None
    assert article.p.get_text(strip=True) == "2201"


def test_repository_hiascend_registry_covers_all_markdown() -> None:
    """当前 2249 份来源文档应全部建立唯一身份与 URI 注册。"""
    project_root = Path(__file__).resolve().parents[2]
    target = project_root / "API参考"
    refs = _adapter().bootstrap(target)

    assert len(refs) == 2249
    assert len({ref.document_id for ref in refs}) == 2249
    assert len({ref.canonical_uri for ref in refs}) == 2249
