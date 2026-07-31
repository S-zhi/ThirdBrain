"""HTTP Adapter 公共客户端的安全策略测试。"""

import asyncio

import httpx
import pytest

from src.doc_sync.config import HttpDefaultsConfig, RedirectPolicy
from src.doc_sync.errors import FetchError
from src.doc_sync.http import HttpFetchClient


def _http_config(**overrides: object) -> HttpDefaultsConfig:
    """构造不会拖慢单元测试的 HTTP 默认配置。"""
    raw: dict[str, object] = {
        "concurrency": 4,
        "requests_per_second": 1_000_000,
        "respect_robots_txt": False,
        "retry": {
            "max_attempts": 3,
            "initial_backoff_seconds": 0.001,
            "max_backoff_seconds": 0.001,
            "jitter_ratio": 0,
        },
    }
    raw.update(overrides)
    return HttpDefaultsConfig.model_validate(raw)


def _same_host(uri: str) -> bool:
    """仅允许 example.com 作为测试抓取范围。"""
    return httpx.URL(uri).host == "example.com"


@pytest.mark.asyncio
async def test_retries_429_and_honors_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 应按 Retry-After 重试并最终返回成功响应。"""
    attempts = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """第一次返回 429，第二次返回正文。"""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, content=b"ok", request=request)

    async def record_sleep(delay: float) -> None:
        """记录重试等待时间而不实际暂停。"""
        delays.append(delay)

    monkeypatch.setattr("src.doc_sync.http.asyncio.sleep", record_sleep)
    transport = httpx.MockTransport(handler)
    async with HttpFetchClient(
        _http_config(),
        RedirectPolicy(),
        transport=transport,
    ) as client:
        result = await client.fetch("https://example.com/doc", uri_validator=_same_host)

    assert attempts == 2
    assert delays == [0.0]
    assert result.body == b"ok"


@pytest.mark.asyncio
async def test_retries_transport_timeout() -> None:
    """网络超时应进入统一重试流程。"""
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        """第一次模拟超时，第二次返回成功。"""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, content=b"recovered", request=request)

    async with HttpFetchClient(
        _http_config(),
        RedirectPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.fetch("https://example.com/doc", uri_validator=_same_host)

    assert attempts == 2
    assert result.body == b"recovered"


@pytest.mark.asyncio
async def test_allows_same_host_redirect_and_rejects_cross_host() -> None:
    """同域重定向应成功，默认策略下跨域重定向应失败。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        """按路径返回同域或跨域重定向。"""
        if request.url.path == "/same":
            return httpx.Response(302, headers={"Location": "/final"}, request=request)
        if request.url.path == "/cross":
            return httpx.Response(
                302,
                headers={"Location": "https://other.example/doc"},
                request=request,
            )
        return httpx.Response(200, content=b"final", request=request)

    async with HttpFetchClient(
        _http_config(),
        RedirectPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.fetch("https://example.com/same", uri_validator=_same_host)
        with pytest.raises(FetchError, match="跨 Host"):
            await client.fetch("https://example.com/cross", uri_validator=lambda _: True)

    assert result.final_uri == "https://example.com/final"


@pytest.mark.asyncio
async def test_rejects_disallowed_url_before_request() -> None:
    """初始 URL 不在 allowlist 时不能发起任何请求。"""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        """记录意外发出的请求。"""
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request)

    async with HttpFetchClient(
        _http_config(),
        RedirectPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(FetchError, match="allowlist"):
            await client.fetch("https://outside.example/doc", uri_validator=_same_host)

    assert calls == 0


@pytest.mark.asyncio
async def test_enforces_streamed_response_size_limit() -> None:
    """正文超过配置上限时应在读取阶段中止。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        """返回超过三字节限制的正文。"""
        return httpx.Response(200, content=b"four", request=request)

    async with HttpFetchClient(
        _http_config(max_response_bytes=3),
        RedirectPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(FetchError, match="响应超过"):
            await client.fetch("https://example.com/doc", uri_validator=_same_host)


@pytest.mark.asyncio
async def test_respects_robots_txt() -> None:
    """robots.txt 明确禁止的路径不能抓取正文。"""
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """提供禁止 private 路径的 robots.txt。"""
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /private\n",
                request=request,
            )
        return httpx.Response(200, content=b"private", request=request)

    async with HttpFetchClient(
        _http_config(respect_robots_txt=True),
        RedirectPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(FetchError, match="robots.txt"):
            await client.fetch("https://example.com/private/doc", uri_validator=_same_host)

    assert paths == ["/robots.txt"]


@pytest.mark.asyncio
async def test_limits_concurrent_requests() -> None:
    """并行抓取数量不能超过配置的并发上限。"""
    active = 0
    peak_active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        """短暂保持请求活跃以测量最大并发数。"""
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, content=b"ok", request=request)

    async with HttpFetchClient(
        _http_config(concurrency=2),
        RedirectPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        await asyncio.gather(
            *[
                client.fetch(f"https://example.com/{index}", uri_validator=_same_host)
                for index in range(4)
            ]
        )

    assert peak_active == 2


@pytest.mark.asyncio
async def test_enforces_requests_per_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连续请求必须等待由 requests_per_second 计算出的时间片。"""
    clock = 0.0
    delays: list[float] = []

    def monotonic() -> float:
        """返回测试控制的单调时钟。"""
        return clock

    async def advance_clock(delay: float) -> None:
        """记录等待并推进测试时钟。"""
        nonlocal clock
        delays.append(delay)
        clock += delay

    async def handler(request: httpx.Request) -> httpx.Response:
        """立即返回成功以隔离 QPS 等待。"""
        return httpx.Response(200, content=b"ok", request=request)

    monkeypatch.setattr("src.doc_sync.http.time.monotonic", monotonic)
    monkeypatch.setattr("src.doc_sync.http.asyncio.sleep", advance_clock)
    async with HttpFetchClient(
        _http_config(requests_per_second=2),
        RedirectPolicy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.fetch("https://example.com/one", uri_validator=_same_host)
        await client.fetch("https://example.com/two", uri_validator=_same_host)

    assert delays == [0.5]
