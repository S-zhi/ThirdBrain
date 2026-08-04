"""重建独立 LLM Knowledge Wiki 索引。

示例：

    python -m src.cli.knowledge reindex --dry-run
    python -m src.cli.knowledge reindex --wiki-id wiki-1 \
        --namespace AscendC.910beta3 --version 910beta3

命令只连接 Knowledge 的 MongoDB 和 ``knowledge_wiki_v1``，不会启动或查询
底层 API RAG。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from src.dao.mongo.database import MongoDatabase
from src.knowledge.mongo_repository import MongoKnowledgeRepository
from src.knowledge.reindex import (
    KnowledgeReindexResult,
    KnowledgeReindexService,
    ReindexScope,
    ReindexStatus,
)
from src.knowledge.zvec_index import ZvecKnowledgeIndexWriter


def build_parser() -> argparse.ArgumentParser:
    """构造 Knowledge 维护命令。"""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    reindex = subparsers.add_parser("reindex", help="从正式 Catalog 重建 Knowledge Zvec 索引")
    reindex.add_argument("--wiki-id", help="精确 Wiki；与 --namespace/--version 一起使用")
    reindex.add_argument("--namespace", help="官方 namespace；保留原始大小写")
    reindex.add_argument("--version", help="官方 version；保留原始大小写")
    reindex.add_argument(
        "--dry-run",
        action="store_true",
        help="只读取并统计 active Artifact，不写索引",
    )
    reindex.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="仅 fallback upsert 使用的批大小；默认 100",
    )
    return parser


def _scope_from_args(args: argparse.Namespace) -> ReindexScope:
    """把 CLI 参数转换成全量或完整精确 Scope。"""

    return ReindexScope(
        wiki_id=args.wiki_id,
        namespace=args.namespace,
        version=args.version,
    )


async def _run_live(args: argparse.Namespace) -> KnowledgeReindexResult:
    """建立 Knowledge 专属依赖并运行一次重建。"""

    scope = _scope_from_args(args)
    mongo = MongoDatabase()
    await mongo.connect()
    try:
        repository = MongoKnowledgeRepository(mongo)
        await repository.ensure_indexes()
        service = KnowledgeReindexService(repository, ZvecKnowledgeIndexWriter())
        return await service.reindex(
            scope,
            dry_run=bool(args.dry_run),
            batch_size=args.batch_size,
        )
    finally:
        await mongo.close()


def _exit_code(result: KnowledgeReindexResult) -> int:
    """映射机器状态到稳定退出码。"""

    if result.status == ReindexStatus.FAILED:
        return 1
    if result.status == ReindexStatus.PARTIAL:
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """执行 CLI，输出 JSON 结果。"""

    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(_run_live(args))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Knowledge reindex failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
