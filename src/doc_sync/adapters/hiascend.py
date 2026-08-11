"""昇腾官网 CANN API 文档来源 Adapter。"""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.doc_sync.adapters.base import AdapterContext, HttpDocumentSourceAdapter
from src.doc_sync.errors import AdapterError
from src.doc_sync.models import DocumentRef, FetchResult, ParsedDocument
from src.doc_sync.storage import write_text_atomic


class HiascendSelectors(BaseModel):
    """定义昇腾页面正文、标题和父级链接选择器。"""

    model_config = ConfigDict(extra="forbid")

    article_body: str = ".the-article-body"
    title: list[str] = Field(default_factory=lambda: ["h1", ".topictitle1"])
    parent_links: list[str] = Field(default_factory=lambda: [".familylinks a"])


class HiascendExistingDocumentConfig(BaseModel):
    """定义现有 Markdown 中来源 URI 与外部 ID 的解析规则。"""

    model_config = ConfigDict(extra="forbid")

    source_url_pattern: str
    external_id_pattern: str


class HiascendOutputConfig(BaseModel):
    """定义昇腾 Markdown 元信息标签和未归类目录。"""

    model_config = ConfigDict(extra="forbid")

    source_label: str = "来源"
    external_id_label: str = "节点"
    unresolved_directory: str = "_待归类"


class HiascendBrowserConfig(BaseModel):
    """定义昇腾动态 HTML 下载所使用的浏览器和等待策略。"""

    model_config = ConfigDict(extra="forbid")

    channel: str = "chrome"
    headless: bool = True
    wait_until: Literal["load", "domcontentloaded", "networkidle"] = "domcontentloaded"
    navigation_timeout_ms: int = Field(default=60_000, ge=1)
    selector_timeout_ms: int = Field(default=60_000, ge=1)
    retry_attempts: int = Field(default=2, ge=1, le=5)
    retry_initial_backoff_seconds: float = Field(default=1.0, gt=0, le=60)
    fallback_to_http: bool = True
    rendered_html_directory: Path = Path("./data/doc_sync/rendered_html")


class HiascendAdapterConfig(BaseModel):
    """定义昇腾来源专属且经过强校验的配置。"""

    model_config = ConfigDict(extra="forbid")

    product: str
    version: str
    language: str
    root_urls: list[str] = Field(min_length=1)
    allowed_hosts: list[str] = Field(min_length=1)
    allowed_path_prefixes: list[str] = Field(min_length=1)
    document_url_pattern: str
    max_pages: int = Field(default=3000, ge=1)
    selectors: HiascendSelectors = Field(default_factory=HiascendSelectors)
    browser: HiascendBrowserConfig = Field(default_factory=HiascendBrowserConfig)
    existing_document: HiascendExistingDocumentConfig
    output: HiascendOutputConfig = Field(default_factory=HiascendOutputConfig)

    @model_validator(mode="after")
    def validate_root_urls(self) -> Self:
        """保证所有 root URL 都满足配置本身的 Host、路径和文件名规则。"""
        try:
            pattern = re.compile(self.document_url_pattern)
        except re.error as exc:
            raise ValueError(f"document_url_pattern 不是合法正则: {exc}") from exc
        hosts: set[str] = set()
        for host in self.allowed_hosts:
            if urlparse(f"//{host}").netloc != host or "/" in host:
                raise ValueError(f"allowed_hosts 只能包含 Host: {host}")
            hosts.add(host.casefold())
        if any(not prefix.startswith("/") for prefix in self.allowed_path_prefixes):
            raise ValueError("allowed_path_prefixes 必须是以 / 开头的绝对 URL 路径")
        for root_url in self.root_urls:
            parsed = urlparse(root_url)
            if parsed.scheme not in {"http", "https"}:
                raise ValueError(f"root_url 必须使用 HTTP(S): {root_url}")
            if parsed.netloc.casefold() not in hosts:
                raise ValueError(f"root_url Host 不在 allowed_hosts: {root_url}")
            if not any(parsed.path.startswith(prefix) for prefix in self.allowed_path_prefixes):
                raise ValueError(f"root_url 路径不在 allowed_path_prefixes: {root_url}")
            if pattern.search(Path(parsed.path).name) is None:
                raise ValueError(f"root_url 不匹配 document_url_pattern: {root_url}")
        metadata_patterns = {
            "source_url_pattern": self.existing_document.source_url_pattern,
            "external_id_pattern": self.existing_document.external_id_pattern,
        }
        for name, raw_pattern in metadata_patterns.items():
            try:
                compiled = re.compile(raw_pattern)
            except re.error as exc:
                raise ValueError(f"{name} 不是合法正则: {exc}") from exc
            if "value" not in compiled.groupindex:
                raise ValueError(f"{name} 必须包含命名组 (?P<value>...)")
        return self


RATE_LIMIT_STATUS_CODES = frozenset({403, 429, 500, 502, 503, 504})


class _BrowserDegradeError(RuntimeError):
    """标记浏览器响应需要重试或切换 HTTP 降级路径。"""


def _normalize_markdown(value: str) -> str:
    """统一 Markdown 的 Unicode、换行、行尾空格和空行。"""
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.splitlines()]
    compact: list[str] = []
    blank_count = 0
    for line in lines:
        if line:
            blank_count = 0
            compact.append(line)
        else:
            blank_count += 1
            if blank_count <= 2:
                compact.append("")
    return "\n".join(compact).strip() + "\n"


def _sanitize_path_component(value: str) -> str:
    """把来源标题转换为安全且可读的单个路径组件。"""
    normalized = unicodedata.normalize("NFC", value).strip()
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    return normalized[:120] or "_unknown_"


def _hierarchy_key(value: str) -> str:
    """生成用于匹配网页标题与本地目录名的宽松比较键。"""
    normalized = unicodedata.normalize("NFC", value).casefold()
    return re.sub(r"[\s_\-（）()]+", "", normalized)


def _is_selector_timeout(error: PlaywrightError) -> bool:
    """识别正文选择器等待超时，允许立即切换静态 HTML。"""
    message = str(error)
    return "Timeout" in message and ("wait_for" in message or "waitForSelector" in message)


def _formula_markdown(value: str, *, display: bool, is_latex: bool) -> str:
    """把单一公式表示转成稳定的 Markdown 片段。"""
    if is_latex:
        return f"\n\n$$\n{value}\n$$\n\n" if display else f"${value}$"
    return f"\n\n`{value}`\n\n" if display else f"`{value}`"


def _bootstrap_priority(path: Path, hierarchy: list[str]) -> tuple[int, int, int, str]:
    """优先保留层级更完整的路径，再用文件时间解决同层级重复项。"""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    return (-len(hierarchy), mtime, len(path.parts), path.as_posix())


class HiascendSourceAdapter(HttpDocumentSourceAdapter):
    """抓取、解析和发现昇腾官网 CANN API 文档。"""

    adapter_type = "hiascend"
    config_model = HiascendAdapterConfig

    def __init__(self, source_id: str, options: HiascendAdapterConfig) -> None:
        """缓存正则和 allowlist，供高频 URI 校验复用。"""
        super().__init__(source_id, options)
        self.options = options
        self._url_pattern = re.compile(options.document_url_pattern)
        self._source_pattern = re.compile(
            options.existing_document.source_url_pattern,
            flags=re.MULTILINE,
        )
        self._external_id_pattern = re.compile(
            options.existing_document.external_id_pattern,
            flags=re.MULTILINE,
        )
        self._allowed_hosts = {host.casefold() for host in options.allowed_hosts}
        self._known_refs_by_uri: dict[str, DocumentRef] = {}
        self._known_hierarchy_labels: dict[str, str] = {}
        self._browser_lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._browser_context: BrowserContext | None = None

    @property
    def max_documents(self) -> int:
        """返回配置限定的单次页面发现上限。"""
        return self.options.max_pages

    def canonicalize_uri(self, uri: str) -> str:
        """移除 fragment/query 并统一 Host 大小写，得到稳定来源 URI。"""
        parsed = urlparse(uri)
        path = quote(
            unquote(parsed.path),
            safe="/:@-._~!$&'()*+,;=",
        )
        return urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                path,
                "",
                "",
                "",
            )
        )

    def is_allowed_uri(self, uri: str) -> bool:
        """严格校验 Host、版本路径前缀和文档文件名。"""
        parsed = urlparse(uri)
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        if parsed.netloc.casefold() not in self._allowed_hosts:
            return False
        if not any(parsed.path.startswith(prefix) for prefix in self.options.allowed_path_prefixes):
            return False
        return self._url_pattern.search(Path(parsed.path).name) is not None

    def _document_id(self, uri: str) -> str:
        """从规范 URL 文件名提取来源内稳定文档 ID。"""
        path = unquote(urlparse(uri).path)
        matching_prefixes = [
            prefix for prefix in self.options.allowed_path_prefixes if path.startswith(prefix)
        ]
        if not matching_prefixes:
            raise AdapterError(f"URL 路径不在 allowlist: {uri}")
        prefix = max(matching_prefixes, key=len)
        relative = path[len(prefix) :].lstrip("/")
        without_suffix = PurePosixPath(relative).with_suffix("").as_posix()
        return without_suffix.replace("/", "::")

    def _page_id(self, uri: str) -> str:
        """提取来源页面文件名，作为仅存于 metadata 的页面 ID。"""
        return Path(unquote(urlparse(uri).path)).stem

    def _rendered_html_path(self, ref: DocumentRef) -> Path:
        """按稳定 document_id 生成渲染后 HTML 的落盘路径。"""
        root = self.options.browser.rendered_html_directory.expanduser().resolve()
        relative = PurePosixPath(*ref.document_id.split("::")).with_suffix(".html")
        target = (root / Path(*relative.parts)).resolve()
        if not target.is_relative_to(root):
            raise AdapterError(f"动态 HTML 路径逃逸输出目录: {ref.document_id}")
        return target

    def _expected_dynamic_heading(self, ref: DocumentRef) -> str | None:
        """为动态等待选择标题提示，Markdown 路由可回退到文件名。"""
        if ref.title_hint and ref.title_hint.strip():
            return ref.title_hint.strip()
        suffix = Path(unquote(urlparse(ref.canonical_uri).path)).suffix.casefold()
        return self._page_id(ref.canonical_uri) if suffix in {".md", ".markdown"} else None

    async def _ensure_browser_context(self) -> BrowserContext:
        """按 Adapter 生命周期懒启动并复用一个 Playwright 浏览器上下文。"""
        async with self._browser_lock:
            if (
                self._browser_context is not None
                and self._browser is not None
                and self._browser.is_connected()
            ):
                return self._browser_context
            await self._close_browser_resources()
            browser_config = self.options.browser
            playwright = await async_playwright().start()
            try:
                browser = await playwright.chromium.launch(
                    channel=browser_config.channel,
                    headless=browser_config.headless,
                )
                browser_context = await browser.new_context()
            except Exception:
                await playwright.stop()
                raise
            self._playwright = playwright
            self._browser = browser
            self._browser_context = browser_context
            return browser_context

    async def _close_browser_resources(self) -> None:
        """关闭当前 Adapter 持有的页面上下文、浏览器和 Playwright 资源。"""
        browser_context = self._browser_context
        browser = self._browser
        playwright = self._playwright
        self._browser_context = None
        self._browser = None
        self._playwright = None
        if browser_context is not None:
            try:
                await browser_context.close()
            except PlaywrightError:
                pass
        if browser is not None:
            try:
                await browser.close()
            except PlaywrightError:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except PlaywrightError:
                pass

    async def aclose(self) -> None:
        """在一次 source 完成后释放复用的浏览器资源。"""
        async with self._browser_lock:
            await self._close_browser_resources()

    async def _fetch_browser_once(self, ref: DocumentRef) -> FetchResult:
        """使用一个新 Page 执行一次浏览器抓取尝试。"""
        browser_config = self.options.browser
        browser_context = await self._ensure_browser_context()
        page = await browser_context.new_page()
        try:
            response = await page.goto(
                ref.canonical_uri,
                wait_until=browser_config.wait_until,
                timeout=browser_config.navigation_timeout_ms,
            )
            status_code = response.status if response is not None else 200
            if status_code in RATE_LIMIT_STATUS_CODES:
                raise _BrowserDegradeError(f"浏览器响应 HTTP {status_code}")
            # 这里只等待正文区域出现；文件名并不总是网页 H1（例如 asc_div），
            # 不能用旧 Markdown 文件名做精确等待，否则会把整批页面拖到超时。
            # parse() 会根据 page_id/title_hint 在多个正文候选中选择正确文章。
            article_heading = page.locator(f"{self.options.selectors.article_body} h1")
            await article_heading.first.wait_for(
                state="attached",
                timeout=browser_config.selector_timeout_ms,
            )
            rendered_html = await page.content()
            rendered_final_uri = page.url
        finally:
            try:
                await page.close()
            except PlaywrightError:
                pass

        rendered_path = self._rendered_html_path(ref)
        write_text_atomic(rendered_path, rendered_html)
        body = rendered_html.encode("utf-8")
        return FetchResult(
            requested_uri=ref.canonical_uri,
            final_uri=rendered_final_uri,
            status_code=status_code,
            content_type="text/html",
            body=body,
            fetched_at=datetime.now(UTC),
            response_hash=hashlib.sha256(body).hexdigest(),
            metadata={
                "fetch_mode": "dynamic_browser",
                "degraded": False,
                "rendered_final_uri": rendered_final_uri,
                "rendered_html_path": str(rendered_path),
            },
        )

    async def _fetch_http_fallback(
        self,
        ref: DocumentRef,
        context: AdapterContext,
        reason: str,
    ) -> FetchResult:
        """浏览器限流或超时后复用 HTTP 连接池获取 SSR HTML。"""
        if context.http is None:
            raise AdapterError("浏览器降级需要 AdapterContext.http，但当前未注入 HTTP Client")
        result = await context.http.fetch(
            ref.canonical_uri,
            uri_validator=self.is_allowed_uri,
        )
        if result.status_code in RATE_LIMIT_STATUS_CODES:
            raise AdapterError(f"HTTP 降级仍返回限流状态 HTTP {result.status_code}")
        try:
            rendered_html = result.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdapterError("HTTP 降级响应不是 UTF-8 HTML") from exc
        rendered_path = self._rendered_html_path(ref)
        write_text_atomic(rendered_path, rendered_html)
        return result.model_copy(
            update={
                "metadata": {
                    **result.metadata,
                    "fetch_mode": "http_fallback",
                    "degraded": True,
                    "degrade_reason": reason,
                    "rendered_html_path": str(rendered_path),
                }
            }
        )

    async def fetch(
        self,
        ref: DocumentRef,
        context: AdapterContext,
    ) -> FetchResult:
        """浏览器重试失败后自动切换到共享 HTTP Client。"""
        if not self.is_allowed_uri(ref.canonical_uri):
            raise AdapterError(f"URL 不在 Adapter allowlist: {ref.canonical_uri}")
        browser_config = self.options.browser
        last_error = "未知浏览器错误"
        for attempt in range(browser_config.retry_attempts):
            try:
                return await self._fetch_browser_once(ref)
            except _BrowserDegradeError as exc:
                last_error = str(exc)
            except PlaywrightError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if browser_config.fallback_to_http and context.http is not None and _is_selector_timeout(
                    exc
                ):
                    break
            if attempt + 1 < browser_config.retry_attempts:
                await asyncio.sleep(
                    browser_config.retry_initial_backoff_seconds * (2**attempt)
                )
        if browser_config.fallback_to_http and context.http is not None:
            try:
                return await self._fetch_http_fallback(ref, context, last_error)
            except Exception as exc:
                raise AdapterError(
                    f"动态 HTML 下载失败；已重试 {browser_config.retry_attempts} 次，"
                    f"HTTP 降级也失败: {exc}"
                ) from exc
        raise AdapterError(
            f"动态 HTML 下载失败；已重试 {browser_config.retry_attempts} 次: "
            f"{last_error}"
        )

    def _base_metadata(self) -> dict[str, str]:
        """构造只进入 metadata 的昇腾来源专属公共字段。"""
        return {
            "product": self.options.product,
            "version": self.options.version,
            "language": self.options.language,
        }

    def bootstrap(self, target_directory: Path) -> list[DocumentRef]:
        """扫描现有 Markdown 中的来源行和节点行，建立初始注册表。"""
        self._known_refs_by_uri = {}
        self._known_hierarchy_labels = {}
        if not target_directory.exists():
            return []
        candidates: dict[str, list[DocumentRef]] = {}
        for path in sorted(target_directory.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise AdapterError(f"无法读取现有 Markdown {path}: {exc}") from exc
            source_match = self._source_pattern.search(text)
            if source_match is None:
                continue
            uri = self.canonicalize_uri(source_match.group("value").strip())
            if not self.is_allowed_uri(uri):
                continue
            document_id = self._document_id(uri)
            external_match = self._external_id_pattern.search(text)
            metadata = self._base_metadata()
            if external_match is not None:
                metadata["node_id"] = external_match.group("value").strip()
            metadata["page_id"] = self._page_id(uri)
            relative_path = path.resolve().relative_to(target_directory.resolve())
            hierarchy = list(relative_path.parts[:-1])
            metadata["hierarchy"] = hierarchy
            candidates.setdefault(document_id, []).append(
                DocumentRef(
                    source_id=self.source_id,
                    document_id=document_id,
                    canonical_uri=uri,
                    title_hint=path.stem,
                    relative_path_hint=relative_path.as_posix(),
                    metadata=metadata,
                )
            )
        references: list[DocumentRef] = []
        for document_id, options in candidates.items():
            selected = min(
                options,
                key=lambda ref: _bootstrap_priority(
                    target_directory / (ref.relative_path_hint or ""),
                    [str(item) for item in ref.metadata.get("hierarchy", [])],
                ),
            )
            references.append(selected)
        self._known_refs_by_uri = {ref.canonical_uri: ref for ref in references}
        label_counts: dict[str, dict[str, int]] = {}
        for ref in references:
            for raw_label in ref.metadata.get("hierarchy", []):
                label = str(raw_label).strip()
                if label:
                    variants = label_counts.setdefault(_hierarchy_key(label), {})
                    variants[label] = variants.get(label, 0) + 1
        self._known_hierarchy_labels = {
            key: max(variants.items(), key=lambda item: (item[1], -len(item[0]), item[0]))[0]
            for key, variants in label_counts.items()
        }
        return references

    async def initial_refs(self) -> list[DocumentRef]:
        """把配置 root_urls 转换为首批稳定文档引用。"""
        references: list[DocumentRef] = []
        for root_url in self.options.root_urls:
            uri = self.canonicalize_uri(root_url)
            known = self._known_refs_by_uri.get(uri)
            if known is not None:
                references.append(known)
                continue
            references.append(
                DocumentRef(
                    source_id=self.source_id,
                    document_id=self._document_id(uri),
                    canonical_uri=uri,
                    metadata={
                        **self._base_metadata(),
                        "page_id": self._page_id(uri),
                        "hierarchy": [],
                    },
                )
            )
        return references

    def _extract_title(self, soup: BeautifulSoup, article: Tag, ref: DocumentRef) -> str:
        """按配置选择器依次提取正文标题并回退到引用提示。"""
        for selector in self.options.selectors.title:
            title_node = article.select_one(selector) or soup.select_one(selector)
            if title_node is not None:
                title = title_node.get_text(" ", strip=True)
                if title:
                    return title
        if ref.title_hint:
            return ref.title_hint
        raise AdapterError(f"文档缺少标题: {ref.canonical_uri}")

    def _extract_hierarchy(
        self,
        soup: BeautifulSoup,
        ref: DocumentRef,
        title: str,
    ) -> list[str]:
        """从现有路径、网页标题或父级链接恢复大中小目录层级。"""
        ref_hierarchy = [
            str(item).strip() for item in (ref.metadata.get("hierarchy") or []) if str(item).strip()
        ]
        if ref.relative_path_hint and ref_hierarchy:
            return ref_hierarchy

        page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        title_parts = [part.strip() for part in re.split(r"\s*-\s*", page_title) if part.strip()]
        matched: list[str] = []
        for part in title_parts[1:]:
            label = self._known_hierarchy_labels.get(_hierarchy_key(part))
            if label is not None and label not in matched:
                matched.append(label)
        if matched:
            return list(reversed(matched))

        hierarchy: list[str] = []
        for selector in self.options.selectors.parent_links:
            for link in soup.select(selector):
                label = link.get_text(" ", strip=True)
                if label and label != title and label not in hierarchy:
                    hierarchy.append(label)
        if hierarchy:
            return hierarchy
        return []

    def _discover_from_article(
        self,
        article: Tag,
        ref: DocumentRef,
        title: str,
        final_uri: str,
        hierarchy: list[str],
    ) -> list[DocumentRef]:
        """从正文链接发现同来源、同版本的新页面。"""
        discovered: dict[str, DocumentRef] = {}
        for link in article.select("a[href]"):
            href = str(link.get("href") or "").strip()
            if not href or href.startswith("#"):
                continue
            uri = self.canonicalize_uri(urljoin(final_uri, href))
            if not self.is_allowed_uri(uri):
                continue
            document_id = self._document_id(uri)
            if document_id == ref.document_id:
                continue
            known = self._known_refs_by_uri.get(uri)
            if known is not None:
                discovered[document_id] = known.model_copy(
                    update={"parent_document_id": ref.document_id}
                )
                continue
            discovered[document_id] = DocumentRef(
                source_id=self.source_id,
                document_id=document_id,
                canonical_uri=uri,
                parent_document_id=ref.document_id,
                title_hint=link.get_text(" ", strip=True) or None,
                metadata={
                    **self._base_metadata(),
                    "page_id": self._page_id(uri),
                    "hierarchy": list(hierarchy),
                },
            )
        return list(discovered.values())

    def _prepare_article(self, article: Tag, final_uri: str, title: str) -> str:
        """清理正文 DOM、规范链接并转换成稳定 Markdown。"""
        fragment = BeautifulSoup(str(article), "html.parser")
        for unwanted in fragment.select("script, style, noscript, .table-expand-btn"):
            unwanted.decompose()
        for selector in self.options.selectors.title:
            title_node = fragment.select_one(selector)
            if title_node is not None and title_node.get_text(" ", strip=True) == title:
                title_node.decompose()
                break
        formula_placeholders = self._replace_formula_nodes(fragment)
        for link in fragment.select("a[href]"):
            href = str(link.get("href") or "")
            link["href"] = urljoin(final_uri, href)
        parsed_final_uri = urlparse(final_uri)
        image_base_uri = f"{parsed_final_uri.scheme}://{parsed_final_uri.netloc}/"
        for image in fragment.select("img[src]"):
            src = str(image.get("src") or "")
            image["src"] = urljoin(image_base_uri, src)
        for tag in fragment.find_all(True):
            allowed: dict[str, object] = {}
            for attribute in ("href", "src", "alt", "title"):
                if tag.has_attr(attribute):
                    allowed[attribute] = tag.attrs[attribute]
            tag.attrs = allowed
        body_markdown = markdownify(
            str(fragment),
            heading_style="ATX",
            bullets="-",
        )
        for placeholder, formula in formula_placeholders.items():
            body_markdown = body_markdown.replace(placeholder, formula)
        return _normalize_markdown(body_markdown)

    def _replace_formula_nodes(self, fragment: BeautifulSoup) -> dict[str, str]:
        """用占位符替换 KaTeX DOM，优先保留页面内嵌的原始 LaTeX。"""
        formulas: dict[str, str] = {}
        for index, katex in enumerate(list(fragment.select(".katex")), start=1):
            annotation = katex.select_one('annotation[encoding="application/x-tex"]')
            latex = annotation.get_text(strip=True) if isinstance(annotation, Tag) else ""
            visible_node = katex.select_one(".katex-html")
            visible = (
                visible_node.get_text("", strip=True)
                if isinstance(visible_node, Tag)
                else katex.get_text("", strip=True)
            )
            formula = latex or visible
            if not formula:
                continue
            math = katex.select_one("math")
            display = (
                isinstance(math, Tag) and str(math.get("display") or "").casefold() == "block"
            ) or katex.find_parent(class_="katex-display") is not None
            placeholder = f"RAGFORMULAPLACEHOLDER{index:06d}"
            formulas[placeholder] = _formula_markdown(
                formula,
                display=display,
                is_latex=bool(latex),
            )
            katex.replace_with(NavigableString(placeholder))
        return formulas

    def _select_article(self, soup: BeautifulSoup, ref: DocumentRef) -> Tag:
        """从动态页面可能挂载的多篇正文中选择当前 URL 对应的文章。"""
        candidates = [
            item
            for item in soup.select(self.options.selectors.article_body)
            if isinstance(item, Tag)
        ]
        page_id = str(ref.metadata.get("page_id") or self._page_id(ref.canonical_uri)).strip()
        title_hint = str(ref.title_hint or "").strip()

        # 1. 优先根据 data-item 属性精确匹配 page_id
        for article in candidates:
            node = article
            data_item = None
            while node:
                if isinstance(node, Tag) and node.has_attr("data-item"):
                    data_item = node["data-item"]
                    break
                node = node.parent
            if data_item:
                data_item_str = str(data_item).strip()
                if data_item_str:
                    data_page_id = Path(unquote(urlparse(data_item_str).path)).stem
                    if data_page_id.casefold() == page_id.casefold():
                        return article

        # 2. 回退到 H1/标题 匹配
        expected_titles = {value.casefold() for value in (page_id, title_hint) if value}
        for article in candidates:
            for selector in self.options.selectors.title:
                title_node = article.select_one(selector)
                if not isinstance(title_node, Tag):
                    continue
                title = title_node.get_text(" ", strip=True)
                node_id = str(title_node.get("id") or "").strip()
                if title.casefold() in expected_titles or node_id.casefold() in expected_titles:
                    return article
        if len(candidates) == 1:
            return candidates[0]
        raise AdapterError(
            f"动态 HTML 中无法唯一定位正文 {page_id!r}: 命中 {len(candidates)} 个候选"
        )

    def parse(self, ref: DocumentRef, result: FetchResult) -> ParsedDocument:
        """从完整 HTML 提取正文、稳定制品和可继续发现的链接。"""
        if result.status_code != 200:
            raise AdapterError(f"HTTP {result.status_code}: {result.final_uri}")
        if result.content_type not in {"text/html", "application/xhtml+xml", ""}:
            raise AdapterError(f"不支持的 Content-Type {result.content_type!r}: {result.final_uri}")
        try:
            html = result.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdapterError(f"页面不是 UTF-8: {result.final_uri}") from exc
        soup = BeautifulSoup(html, "html.parser")
        article = self._select_article(soup, ref)
        title = self._extract_title(soup, article, ref)
        hierarchy = self._extract_hierarchy(soup, ref, title)
        discovered = self._discover_from_article(
            article,
            ref,
            title,
            result.final_uri,
            hierarchy,
        )
        body_markdown = self._prepare_article(article, result.final_uri, title)
        if len(body_markdown.strip()) < 20:
            raise AdapterError(f"规范化正文过短: {result.final_uri}")
        canonical_uri = self.canonicalize_uri(result.final_uri)
        external_id = str(ref.metadata.get("node_id") or "").strip() or None
        if external_id is None:
            anchor = article.find("a", attrs={"name": True})
            if isinstance(anchor, Tag):
                external_id = str(anchor.get("name") or "").strip() or None
        external_id = external_id or ref.document_id
        normalized_content = _normalize_markdown(f"# {title}\n\n{body_markdown}")
        artifact = (
            f"# {title}\n\n"
            f"> {self.options.output.source_label}: {canonical_uri}\n"
            f"> {self.options.output.external_id_label}: `{external_id}`\n\n"
            "---\n\n"
            f"{body_markdown}"
        )
        metadata = {
            **self._base_metadata(),
            "page_id": self._page_id(canonical_uri),
            "node_id": external_id,
        }
        fetch_mode = str(result.metadata.get("fetch_mode") or "").strip()
        if fetch_mode:
            metadata["fetch_mode"] = fetch_mode
        if result.metadata.get("degraded"):
            metadata["degraded"] = True
            metadata["degrade_reason"] = str(result.metadata.get("degrade_reason") or "")
        metadata["needs_classification"] = not bool(hierarchy)
        return ParsedDocument(
            source_id=self.source_id,
            document_id=ref.document_id,
            canonical_uri=canonical_uri,
            external_id=external_id,
            title=title,
            hierarchy=hierarchy,
            normalized_content=normalized_content,
            artifact_content=_normalize_markdown(artifact),
            discovered_refs=discovered,
            metadata=metadata,
        )

    def discover_refs(self, document: ParsedDocument) -> list[DocumentRef]:
        """直接返回 parse 阶段从正文提取并去重的候选引用。"""
        return document.discovered_refs

    def propose_relative_path(
        self,
        document: ParsedDocument,
    ) -> PurePosixPath:
        """根据发现层级生成路径，不足时放入配置的待归类目录。"""
        hierarchy = [
            _sanitize_path_component(item)
            for item in document.hierarchy
            if item and "API列表" not in item
        ]
        filename = f"{_sanitize_path_component(document.title)}.md"
        if not hierarchy:
            return PurePosixPath(
                self.options.output.unresolved_directory,
                f"{document.document_id}_{filename}",
            )
        return PurePosixPath(*hierarchy[-6:], filename)
