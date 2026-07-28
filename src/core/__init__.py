"""API 文档领域模型与共享校验工具。"""

from src.core.api_document_yaml import (
    ParsedYamlDocument,
    YamlDocumentError,
    read_yaml_document,
)

__all__ = ["ParsedYamlDocument", "YamlDocumentError", "read_yaml_document"]
