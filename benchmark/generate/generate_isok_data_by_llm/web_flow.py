"""实现 MiniMax 查询规划、Tavily 网络搜索与统一回答流程。"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .flow_types import FlowAnswer
from .retrieval_answer import RetrievalAnswerGenerator

ModelCall = Callable[..., str]


@dataclass(frozen=True)
class WebSettings:
    """保存 Tavily 网络搜索和上下文配置。"""

    api_url: str
    api_key: str
    max_queries: int
    max_results: int
    context_max_chars: int
    timeout: float

    def __post_init__(self) -> None:
        """校验网络搜索配置且不在错误信息中暴露密钥。"""
        if not self.api_url.strip():
            raise ValueError("Web Search api_url 不能为空")
        if not self.api_key.strip():
            raise ValueError("web 模式需要设置环境变量 TAVILY_API_KEY")
        if not 1 <= self.max_queries <= 20:
            raise ValueError("Web Search max_queries 必须在 1 到 20 之间")
        if not 1 <= self.max_results <= 20:
            raise ValueError("Web Search max_results 必须在 1 到 20 之间")
        if self.context_max_chars < 1:
            raise ValueError("Web Search context_max_chars 必须 >= 1")
        if self.timeout <= 0:
            raise ValueError("Web Search timeout 必须 > 0")


@dataclass(frozen=True)
class WebRetrieval:
    """保存多条网络查询及其去重结果。"""

    queries: list[str]
    results: list[dict[str, Any]]
    query_results: list[dict[str, Any]]


def _extract_json_payload(text: str) -> Any:
    """从纯 JSON 或 Markdown JSON 代码块中解析网络查询规划。"""
    stripped = text.strip()
    code_block = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    candidate = code_block.group(1).strip() if code_block else stripped
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"网络查询规划不是合法 JSON: {exc}") from exc
        raise RuntimeError("网络查询规划不是合法 JSON object")


def _parse_queries(text: str, max_queries: int) -> list[str]:
    """校验、去重并限制 MiniMax 规划出的网络查询。"""
    payload = _extract_json_payload(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("queries"), list):
        raise TypeError("网络查询规划必须包含 queries 数组")

    queries = []
    seen = set()
    for item in payload["queries"]:
        if not isinstance(item, str) or not item.strip():
            continue
        query = item.strip()
        if query in seen:
            continue
        seen.add(query)
        queries.append(query)
        if len(queries) >= max_queries:
            break
    if not queries:
        raise RuntimeError("网络查询规划没有生成有效 query")
    return queries


def _deduplicate_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按照 URL 去重多条网络查询返回的搜索结果。"""
    unique_results = []
    seen = set()
    for result in results:
        url = result.get("url")
        identity = url if isinstance(url, str) and url else result.get("title")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        unique_results.append(result)
    return unique_results


def _build_context(results: list[dict[str, Any]], max_chars: int) -> str:
    """在统一字符预算内组合网络搜索标题、链接和正文摘要。"""
    if not results:
        return "（本轮网络搜索未返回公开资料）"

    sections = []
    used_chars = 0
    for index, result in enumerate(results, 1):
        raw_content = result.get("raw_content")
        content = (
            raw_content.strip()
            if isinstance(raw_content, str) and raw_content.strip()
            else result.get("content", "")
        )
        section = (
            f"[网络资料 {index}]\n"
            f"title: {result.get('title', '')}\n"
            f"url: {result.get('url', '')}\n"
            f"content:\n{content}"
        )
        separator_chars = 2 if sections else 0
        remaining = max_chars - used_chars - separator_chars
        if remaining <= 0:
            break
        sections.append(section[:remaining])
        used_chars += min(len(section), remaining) + separator_chars
        if len(section) > remaining:
            break
    return "\n\n".join(sections)


class TavilySearchClient:
    """通过 Tavily HTTP Search API 查询公开互联网资料。"""

    def __init__(self, settings: WebSettings) -> None:
        """保存经过校验的 Tavily 配置。"""
        self._settings = settings

    def _search_once(self, query: str) -> list[dict[str, Any]]:
        """执行一条 Tavily 查询并返回合法结果对象。"""
        body = {
            "query": query,
            "search_depth": "basic",
            "max_results": self._settings.max_results,
            "topic": "general",
            "include_answer": False,
            "include_raw_content": "markdown",
            "include_images": False,
        }
        request = Request(
            self._settings.api_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._settings.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._settings.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Tavily HTTP {exc.code}: {error_body}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"Tavily 请求失败: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Tavily 返回非 JSON 内容: {exc}") from exc

        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw_results, list):
            raise TypeError("Tavily 响应缺少 results 数组")
        return [result for result in raw_results if isinstance(result, dict)]

    def search(self, queries: list[str]) -> WebRetrieval:
        """依次执行规划查询并汇总可审计的去重网络结果。"""
        all_results = []
        query_results = []
        for query in queries:
            results = self._search_once(query)
            all_results.extend(results)
            query_results.append(
                {
                    "query": query,
                    "total": len(results),
                    "results": [
                        {
                            "title": result.get("title", ""),
                            "url": result.get("url", ""),
                            "score": result.get("score"),
                        }
                        for result in results
                    ],
                }
            )
        return WebRetrieval(
            queries=queries,
            results=_deduplicate_results(all_results),
            query_results=query_results,
        )


class WebAnswerFlow:
    """规划公开网络查询，并用统一回答器生成对照组结果。"""

    name = "web"

    def __init__(
        self,
        model_call: ModelCall,
        search_client: TavilySearchClient,
        answer_generator: RetrievalAnswerGenerator,
        settings: WebSettings,
        planner_prompt_path: Path,
    ) -> None:
        """注入查询规划模型、网络客户端、统一回答器和提示词。"""
        self._model_call = model_call
        self._search_client = search_client
        self._answer_generator = answer_generator
        self._settings = settings
        self._planner_template = planner_prompt_path.read_text(encoding="utf-8")

    def answer(
        self,
        *,
        question: str,
        record: dict[str, Any],
        max_output_chars: int,
    ) -> FlowAnswer:
        """规划网络查询、搜索公开资料并以全新无历史请求生成回答。"""
        del record
        planner_prompt = self._planner_template.format(
            question=question,
            max_queries=self._settings.max_queries,
        )
        planner_output = self._model_call(
            prompt=planner_prompt,
            system_prompt=(
                "你是公开互联网搜索查询规划器。"
                "允许使用网络接入，但当前步骤只输出查询 JSON。"
            ),
        )
        queries = _parse_queries(planner_output, self._settings.max_queries)
        retrieval = self._search_client.search(queries)
        context = _build_context(
            retrieval.results,
            self._settings.context_max_chars,
        )
        answer = self._answer_generator.answer(
            question=question,
            context=context,
            source_label="公开互联网搜索",
            max_output_chars=max_output_chars,
        )
        return FlowAnswer(
            text=answer,
            metadata={
                "generation_mode": self.name,
                "web_queries": retrieval.queries,
                "web_query_results": retrieval.query_results,
                "web_result_urls": [
                    result.get("url", "") for result in retrieval.results
                ],
            },
        )
