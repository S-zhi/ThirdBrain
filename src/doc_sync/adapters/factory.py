"""基于显式 Registry 的 SourceAdapter 工厂。"""

from __future__ import annotations

from typing import ClassVar

from pydantic import ValidationError

from src.doc_sync.adapters.base import SourceAdapter
from src.doc_sync.config import SourceConfig
from src.doc_sync.errors import AdapterRegistrationError


class AdapterFactory:
    """注册、校验并创建来源 Adapter，禁止从 YAML 动态导入类。"""

    _registry: ClassVar[dict[str, type[SourceAdapter]]] = {}

    @classmethod
    def register(cls, adapter_class: type[SourceAdapter]) -> None:
        """显式注册一个拥有唯一 adapter_type 的具体 Adapter。"""
        adapter_type = getattr(adapter_class, "adapter_type", "").strip()
        if not adapter_type:
            raise AdapterRegistrationError("Adapter 必须声明非空 adapter_type")
        if adapter_type in cls._registry:
            registered = cls._registry[adapter_type]
            raise AdapterRegistrationError(
                f"adapter_type {adapter_type!r} 已由 {registered.__name__} 注册"
            )
        if not issubclass(adapter_class, SourceAdapter):
            raise AdapterRegistrationError("注册对象必须继承 SourceAdapter")
        cls._registry[adapter_type] = adapter_class

    @classmethod
    def available_types(cls) -> tuple[str, ...]:
        """返回按字典序排列的已注册 Adapter 类型。"""
        return tuple(sorted(cls._registry))

    @classmethod
    def create(cls, source: SourceConfig) -> SourceAdapter:
        """根据 source.adapter.type 校验 options 并创建 Adapter。"""
        adapter_type = source.adapter.type
        adapter_class = cls._registry.get(adapter_type)
        if adapter_class is None:
            available = ", ".join(cls.available_types()) or "(无)"
            raise AdapterRegistrationError(
                f"未知 adapter.type {adapter_type!r}；可用类型: {available}"
            )
        try:
            options = adapter_class.config_model.model_validate(source.adapter.options)
        except ValidationError as exc:
            raise AdapterRegistrationError(
                f"source {source.id!r} 的 {adapter_type!r} options 无效: {exc}"
            ) from exc
        return adapter_class(source_id=source.id, options=options)

    @classmethod
    def validate_sources(cls, sources: list[SourceConfig]) -> None:
        """提前创建所有启用 Adapter，确保配置在写文件前失败。"""
        for source in sources:
            if source.enabled:
                cls.create(source)
