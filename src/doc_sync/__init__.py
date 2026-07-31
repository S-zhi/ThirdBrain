"""可扩展、来源无关的文档定时同步框架。"""

from src.doc_sync.config import DocumentSyncConfig, load_document_sync_config
from src.doc_sync.errors import DocumentSyncError
from src.doc_sync.models import (
    DocumentRef,
    FetchResult,
    ParsedDocument,
    RunManifest,
    SyncStateEntry,
)

__all__ = [
    "DocumentRef",
    "DocumentSyncConfig",
    "DocumentSyncError",
    "FetchResult",
    "ParsedDocument",
    "RunManifest",
    "SyncStateEntry",
    "load_document_sync_config",
]
