"""CANN Judge 公开只读 API 客户端。"""

from collections.abc import Mapping
from typing import Any, Self

import httpx


class CannJudgeError(RuntimeError):
    """CANN Judge 网络或数据契约错误。"""


class CannJudgeClient:
    """封装 CANN Judge 公开题库 API，不处理登录态或提交。"""

    def __init__(
        self,
        base_url: str = "https://cannjudge.cn",
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "ThirdBrain-Benchmark-Sync/1.0"},
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """仅关闭由当前实例创建的 HTTP client。"""
        if self._owns_client:
            self._client.close()

    def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CannJudgeError(f"读取 CANN Judge 失败: GET {path}: {exc}") from exc

    @staticmethod
    def _expect_object(payload: Any, endpoint: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CannJudgeError(f"{endpoint} 应返回 JSON object，实际为 {type(payload).__name__}")
        return payload

    @staticmethod
    def _expect_list(payload: Any, endpoint: str) -> list[dict[str, Any]]:
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise CannJudgeError(f"{endpoint} 应返回 JSON object 数组")
        return payload

    def public_group(self) -> dict[str, Any]:
        endpoint = "/api/groups/public"
        return self._expect_object(self._get_json(endpoint), endpoint)

    def contests(self, group_id: str) -> list[dict[str, Any]]:
        endpoint = f"/api/contests/group/{group_id}"
        return self._expect_list(self._get_json(endpoint), endpoint)

    def problems(self, contest_id: str) -> list[dict[str, Any]]:
        endpoint = f"/api/problems/contest/{contest_id}"
        return self._expect_list(self._get_json(endpoint), endpoint)

    def problem_stats(self, contest_id: str) -> list[dict[str, Any]]:
        endpoint = f"/api/submissions/contest/{contest_id}/stats"
        payload = self._get_json(endpoint, params={"group": "problem"})
        return self._expect_list(payload, endpoint)
