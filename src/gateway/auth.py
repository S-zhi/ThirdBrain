"""Service API 的安全默认鉴权依赖。"""

from __future__ import annotations

import os
import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

ENV_KNOWLEDGE_API_KEY = "KNOWLEDGE_API_KEY"
ENV_AGENT_PLATFORM_API_KEY = "AGENT_PLATFORM_API_KEY"


def require_service_auth(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """要求配置的 API key；未配置时安全地关闭接口。"""
    expected = os.environ.get(ENV_KNOWLEDGE_API_KEY, "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service authentication is not configured",
        )
    bearer = ""
    if authorization and authorization.startswith("Bearer "):
        bearer = authorization.removeprefix("Bearer ").strip()
    candidate = (x_api_key or bearer).strip()
    if not candidate or not secrets.compare_digest(candidate, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_agent_platform_auth(
    x_agent_platform_key: Annotated[
        str | None,
        Header(alias="X-Agent-Platform-Key"),
    ] = None,
) -> None:
    """仅允许 Agent Platform 调用 Core 的私有数据 Gateway。"""

    expected = os.environ.get(ENV_AGENT_PLATFORM_API_KEY, "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent Platform authentication is not configured",
        )
    candidate = (x_agent_platform_key or "").strip()
    if not candidate or not secrets.compare_digest(candidate, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Agent Platform credentials",
        )
