"""按 Schema 成套组织解析、映射与检索能力。"""

from src.rag.api_document_profile import (
    DEFAULT_PROFILE_ID,
    SCHEMA21_PROFILE_ID,
    build_api_document_profile,
    build_api_document_v21_profile,
)
from src.rag.contracts import MarkdownParseRequest
from src.rag.profile import RagSchemaProfile, VectorStoreBinding
from src.rag.registry import RagProfileRegistry, get_rag_profile, reset_rag_profile_registry
from src.rag.schema_definition import RagSchemaDefinition, RagSchemaDefinitionError

__all__ = [
    "DEFAULT_PROFILE_ID",
    "SCHEMA21_PROFILE_ID",
    "MarkdownParseRequest",
    "RagProfileRegistry",
    "RagSchemaDefinition",
    "RagSchemaDefinitionError",
    "RagSchemaProfile",
    "VectorStoreBinding",
    "build_api_document_profile",
    "build_api_document_v21_profile",
    "get_rag_profile",
    "reset_rag_profile_registry",
]
