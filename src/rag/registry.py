"""RAG Profile 显式白名单注册表。"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.rag.api_document_profile import (
    DEFAULT_PROFILE_ID,
    SCHEMA21_PROFILE_ID,
    build_api_document_profile,
    build_api_document_v21_profile,
)
from src.rag.profile import RagSchemaProfile


@dataclass(slots=True)
class RagProfileRegistry:
    """Profile 的显式白名单注册表。

    不根据外部 YAML 动态 import 类，避免配置文件获得任意代码加载能力；所有可用
    Profile 必须先由应用代码构造并注册。
    """

    profiles: dict[str, RagSchemaProfile] = field(default_factory=dict)

    def register(self, profile: RagSchemaProfile) -> None:
        """注册能力包；拒绝同 ID 覆盖，避免启动顺序改变最终实现。"""
        if profile.profile_id in self.profiles:
            raise LookupError(f"Profile 已注册: {profile.profile_id}")
        self.profiles[profile.profile_id] = profile

    def resolve(self, profile_id: str) -> RagSchemaProfile:
        """按稳定 ID 解析能力包，未知 ID 立即失败而不回退默认 Schema。"""
        try:
            return self.profiles[profile_id]
        except KeyError as error:
            raise LookupError(f"未知 RAG Profile: {profile_id}") from error


_registry: RagProfileRegistry | None = None


def get_rag_profile(
    profile_id: str = DEFAULT_PROFILE_ID,
    *,
    collection_name: str | None = None,
) -> RagSchemaProfile:
    """获取绑定指定 Zvec collection 的 Profile。

    ``profile_id`` 决定 YAML Schema、字段映射和检索器；``collection_name`` 只决定
    当前物理库目录。因而同一 Profile Schema 可安全映射到多个 collection。
    """
    global _registry
    if _registry is None:
        _registry = RagProfileRegistry()
        _registry.register(build_api_document_profile())
        _registry.register(build_api_document_v21_profile())
    if collection_name is None:
        return _registry.resolve(profile_id)
    if profile_id == DEFAULT_PROFILE_ID:
        return build_api_document_profile(collection_name)
    if profile_id == SCHEMA21_PROFILE_ID:
        return build_api_document_v21_profile(collection_name)
    return _registry.resolve(profile_id)


def reset_rag_profile_registry() -> None:
    """清空 Registry 和工厂缓存，仅用于隔离配置相关的单元测试。"""
    global _registry
    _registry = None
    build_api_document_profile.cache_clear()
    build_api_document_v21_profile.cache_clear()
