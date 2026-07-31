"""内置 Adapter 注册入口。"""

from src.doc_sync.adapters.base import (
    AdapterContext,
    HttpDocumentSourceAdapter,
    SourceAdapter,
)
from src.doc_sync.adapters.factory import AdapterFactory
from src.doc_sync.adapters.hiascend import HiascendSourceAdapter

AdapterFactory.register(HiascendSourceAdapter)

__all__ = [
    "AdapterContext",
    "AdapterFactory",
    "HiascendSourceAdapter",
    "HttpDocumentSourceAdapter",
    "SourceAdapter",
]
