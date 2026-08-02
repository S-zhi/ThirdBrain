"""LLM Wiki 与底层 API RAG 的架构隔离验收测试。

这些测试故意检查模块边界，而不是某一个实现细节：Knowledge 应该能够只依赖
自己的 Artifact Reader、Mongo Catalog 和 Knowledge Zvec，不能在包内导入底层
``agent_query_service``，也不能由应用装配时把原 RAG Collection 传入 Knowledge。

当独立 LLM Wiki 尚未完成解耦时，本文件会失败；这正是第一阶段重构的验收信号。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from src.knowledge.models import ArtifactType, Confidence
from src.knowledge.query_contracts import (
    KnowledgeItem,
    QueryEvidenceRef,
    QueryKnowledgeOptions,
    QueryScope,
    ReaderSearchResult,
    RetrievalChannel,
    RetrievalHit,
)
from src.knowledge.query_service import KnowledgeQueryService
from src.knowledge.readers import EmptyRelationReader

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_ROOT = PROJECT_ROOT / "src" / "knowledge"

FORBIDDEN_MODULES = {"src.service.agent_query_service"}
FORBIDDEN_SYMBOLS = {
    "AgentQueryCommand",
    "AgentQueryFilters",
    "AgentQueryRetriever",
    "AgentQueryType",
    "ZvecAgentQueryRetriever",
}


def _python_files(root: Path) -> tuple[Path, ...]:
    """返回 Knowledge 包内所有源码文件，排除缓存目录。"""
    return tuple(sorted(path for path in root.glob("*.py") if path.is_file()))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _dotted_name(node: ast.expr) -> str:
    """把 AST 中的导入/调用目标转换成可比较的点分隔名称。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def test_knowledge_package_has_no_bottom_rag_runtime_imports() -> None:
    """Knowledge 代码不能运行时导入原 RAG Retriever。"""
    violations: list[str] = []
    for path in _python_files(KNOWLEDGE_ROOT):
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module in FORBIDDEN_MODULES:
                    violations.append(f"{path}:{node.lineno}: from {module}")
                for alias in node.names:
                    if alias.name in FORBIDDEN_SYMBOLS:
                        violations.append(f"{path}:{node.lineno}: imported {alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_MODULES:
                        violations.append(f"{path}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.Name) and node.id in FORBIDDEN_SYMBOLS:
                violations.append(f"{path}:{node.lineno}: referenced {node.id}")

    assert not violations, "Knowledge must not depend on the bottom RAG: " + "; ".join(violations)


def test_knowledge_builder_does_not_construct_a_source_reader() -> None:
    """Knowledge builder 只能装配自己的 Artifact 查询面。"""
    path = KNOWLEDGE_ROOT / "query_service.py"
    tree = _parse(path)
    builders = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "build_knowledge_query_service"
    ]
    assert len(builders) == 1, "the public Knowledge builder must remain discoverable"
    builder = builders[0]

    parameter_names = {
        argument.arg
        for argument in (*builder.args.posonlyargs, *builder.args.args, *builder.args.kwonlyargs)
    }
    assert "collection_name" not in parameter_names
    assert "rag_collection_id" not in parameter_names

    forbidden_calls = {
        _dotted_name(node.func)
        for node in ast.walk(builder)
        if isinstance(node, ast.Call)
        and _dotted_name(node.func) in {"build_zvec_source_reader", "ZvecSourceKnowledgeReader"}
    }
    assert not forbidden_calls, "Knowledge builder must not wire the bottom RAG source reader"

    source_reader_keywords = {
        keyword.arg
        for node in ast.walk(builder)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "source_reader"
    }
    assert not source_reader_keywords, "Knowledge builder must not pass source_reader"


def test_application_does_not_pass_bottom_rag_collection_to_knowledge_builder() -> None:
    """应用可以装配原 RAG，但不能把它的 Collection 注入 LLM Wiki。"""
    path = PROJECT_ROOT / "src" / "main.py"
    tree = _parse(path)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _dotted_name(node.func) == "build_knowledge_query_service"
    ]
    assert len(calls) == 1, "the application must have one Knowledge service assembly point"
    call = calls[0]

    assert all(keyword.arg != "collection_name" for keyword in call.keywords)
    assert all(keyword.arg != "rag_collection_id" for keyword in call.keywords)
    assert not (
        call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == "collection_name"
    ), "the bottom RAG collection_name must not be passed to Knowledge"


class _ArtifactOnlyReader:
    """最小的只读 Artifact Reader，用于证明查询不需要底层 RAG。"""

    def __init__(self, hit: RetrievalHit) -> None:
        self.hit = hit
        self.calls = 0

    async def search(
        self,
        query: str,
        options: QueryKnowledgeOptions,
        *,
        limit: int,
    ) -> ReaderSearchResult:
        del query, options
        self.calls += 1
        return ReaderSearchResult(hits=(self.hit,)[:limit])


def test_artifact_only_query_runs_without_rag_scope_or_source_reader() -> None:
    """独立 Wiki 查询只用 Artifact Reader 也应能生成 Recall Capsule。"""
    signature = inspect.signature(KnowledgeQueryService)
    assert "source_reader" not in signature.parameters
    assert "artifact_reader" in signature.parameters

    evidence = QueryEvidenceRef(
        wiki_id="wiki:ascendc",
        document_id="doc:reduce",
        part_id="part:description",
        content_hash="sha256:reduce",
        version="910beta3",
        quote_hint="returns the maximum value",
    )
    item = KnowledgeItem(
        id="artifact:asc_reduce_max",
        kind=ArtifactType.ENTITY,
        wiki_id="wiki:ascendc",
        namespace="AscendC.API",
        version="910beta3",
        title="asc_reduce_max",
        summary="Returns the maximum value in the reduction range.",
        confidence=Confidence.HIGH,
        provenance=(evidence,),
    )
    reader = _ArtifactOnlyReader(
        RetrievalHit(channel=RetrievalChannel.EXACT, ranking="artifact:exact", item=item)
    )
    service = KnowledgeQueryService(
        artifact_reader=reader,
        relation_reader=EmptyRelationReader(),
    )

    import asyncio

    result = asyncio.run(
        service.query_knowledge(
            "asc_reduce_max",
            QueryKnowledgeOptions(
                scope=QueryScope(
                    wiki_id="wiki:ascendc",
                    namespace="AscendC.API",
                    version="910beta3",
                ),
                expand_relations=False,
            ),
        )
    )

    assert reader.calls == 1
    assert result.found is True
    assert result.source_hits == ()
    assert tuple(hit.id for hit in result.knowledge_hits) == ("artifact:asc_reduce_max",)
    assert "SOURCE_READER_UNAVAILABLE" not in result.warnings
