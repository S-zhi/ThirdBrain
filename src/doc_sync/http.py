"""HTTP Adapter 共用的限速、重试、重定向与 robots 客户端。"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Self
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from src.doc_sync.config import HttpDefaultsConfig, RedirectPolicy
from src.doc_sync.errors import FetchError
from src.doc_sync.models import FetchResult

REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


class HttpFetchClient:
    """为所有 HTTP Adapter 提供共享的安全异步请求能力。"""

    def __init__(
        self,
        config: HttpDefaultsConfig,
        redirects: RedirectPolicy,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """构造连接池、并发信号量和跨请求限速器。"""
        self._config = config
        self._redirects = redirects
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self._rate_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._robots_cache: dict[str, RobotFileParser | None] = {}
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": config.user_agent},
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        """返回已经构造好的异步客户端。"""
        return self

    async def __aexit__(self, *_: object) -> None:
        """退出上下文时关闭连接池。"""
        await self.close()

    async def close(self) -> None:
        """显式关闭 HTTP 连接池。"""
        await self._client.aclose()

    async def _wait_for_rate_limit(self) -> None:
        """按全局 QPS 串行分配下一次请求时间片。"""
        interval = 1.0 / self._config.requests_per_second
        async with self._rate_lock:
            now = time.monotonic()
            if self._next_request_at > now:
                await asyncio.sleep(self._next_request_at - now)
                now = time.monotonic()
            self._next_request_at = now + interval

    async def _read_limited_response(self, response: httpx.Response) -> bytes:
        """在最大响应大小内流式读取正文。"""
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > self._config.max_response_bytes:
                    raise FetchError(f"响应超过 {self._config.max_response_bytes} bytes")
            except ValueError:
                pass
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self._config.max_response_bytes:
                raise FetchError(f"响应超过 {self._config.max_response_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        """计算 Retry-After 或指数退避加随机抖动的等待时间。"""
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(retry_after)
                        return max(
                            0.0,
                            (retry_at - datetime.now(retry_at.tzinfo or UTC)).total_seconds(),
                        )
                    except (TypeError, ValueError):
                        pass
        retry = self._config.retry
        base = min(
            retry.max_backoff_seconds,
            retry.initial_backoff_seconds * (2 ** max(attempt - 1, 0)),
        )
        jitter = base * retry.jitter_ratio
        return max(0.0, base + random.uniform(-jitter, jitter))

    async def _robots_parser(self, uri: str) -> RobotFileParser | None:
        """获取并缓存当前站点 robots.txt；获取失败时安全降级为未知。"""
        parsed = urlparse(uri)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._robots_cache:
            return self._robots_cache[origin]
        robots_url = f"{origin}/robots.txt"
        try:
            response = await self._request_with_retries(robots_url)
            try:
                if response.status_code >= 400:
                    parser = None
                else:
                    body = await self._read_limited_response(response)
                    parser = RobotFileParser()
                    parser.set_url(robots_url)
                    parser.parse(body.decode("utf-8", errors="replace").splitlines())
            finally:
                await response.aclose()
        except (FetchError, httpx.HTTPError):
            parser = None
        self._robots_cache[origin] = parser
        return parser

    async def _ensure_robots_allowed(self, uri: str) -> None:
        """在启用 robots 策略时拒绝明确不允许抓取的 URI。"""
        if not self._config.respect_robots_txt:
            return
        parser = await self._robots_parser(uri)
        if parser is not None and not parser.can_fetch(self._config.user_agent, uri):
            raise FetchError(f"robots.txt 不允许抓取: {uri}")

    async def _request_with_retries(self, uri: str) -> httpx.Response:
        """对一个 URI 执行有限次数的网络与状态码重试。"""
        retry = self._config.retry
        last_error: Exception | None = None
        for attempt in range(1, retry.max_attempts + 1):
            response: httpx.Response | None = None
            try:
                await self._wait_for_rate_limit()
                request = self._client.build_request("GET", uri)
                response = await self._client.send(request, stream=True)
                if response.status_code not in retry.retry_status_codes:
                    return response
                await response.aclose()
                last_error = FetchError(f"HTTP {response.status_code}: {uri}")
            except httpx.HTTPError as exc:
                last_error = exc
            if attempt < retry.max_attempts:
                await asyncio.sleep(self._retry_delay(attempt, response))
        raise FetchError(f"请求重试耗尽: {uri}: {last_error}")

    async def fetch(
        self,
        uri: str,
        *,
        uri_validator: Callable[[str], bool],
    ) -> FetchResult:
        """获取一个 URI，并对每次重定向重新执行范围校验。"""
        if not uri_validator(uri):
            raise FetchError(f"URL 不在 Adapter allowlist: {uri}")
        async with self._semaphore:
            await self._ensure_robots_allowed(uri)
            requested_uri = uri
            current_uri = uri
            for redirect_count in range(self._redirects.max_redirects + 1):
                response = await self._request_with_retries(current_uri)
                if response.status_code in REDIRECT_STATUS_CODES:
                    location = response.headers.get("Location")
                    await response.aclose()
                    if not location:
                        raise FetchError(f"重定向缺少 Location: {current_uri}")
                    next_uri = urljoin(current_uri, location)
                    current_host = urlparse(current_uri).netloc.casefold()
                    next_host = urlparse(next_uri).netloc.casefold()
                    if not self._redirects.allow_cross_host and current_host != next_host:
                        raise FetchError(f"禁止跨 Host 重定向: {current_uri} -> {next_uri}")
                    if not uri_validator(next_uri):
                        raise FetchError(f"重定向目标不在 allowlist: {next_uri}")
                    await self._ensure_robots_allowed(next_uri)
                    current_uri = next_uri
                    continue
                try:
                    body = await self._read_limited_response(response)
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                    status_code = response.status_code
                    final_uri = str(response.url)
                finally:
                    await response.aclose()
                return FetchResult(
                    requested_uri=requested_uri,
                    final_uri=final_uri,
                    status_code=status_code,
                    content_type=content_type,
                    body=body,
                    fetched_at=datetime.now(UTC),
                    response_hash=hashlib.sha256(body).hexdigest(),
                )
            raise FetchError(f"重定向次数超过 {self._redirects.max_redirects}: {requested_uri}")
