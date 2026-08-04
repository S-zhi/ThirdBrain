"""Knowledge Graph 维护 CLI。

子命令：
- ``build``：从已发布 Artifact 全量构建图（可 --dry-run / --clear）
- ``upsert``：增量同步指定 artifacts 的关系边
- ``stats``：查看图的节点/边/度数/孤儿节点统计
- ``export``：导出图为 JSON（供可视化 / 迁移）

低于 2.0/10 (= 0.2) 的边在构建立刻丢弃，不入图、不入召回。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from src.dao.mongo.database import MongoDatabase
from src.knowledge.graph import (
    BROKEN_EDGE_THRESHOLD,
    DEFAULT_BATCH_SIZE,
    MongoRelationGraphStore,
    RelationGraphBuilder,
    compute_graph_stats,
    export_graph_json,
    iter_graph_export_batches,
)
from src.knowledge.mongo_repository import MongoKnowledgeRepository


def build_parser() -> argparse.ArgumentParser:
    """构造 ``graph`` 子命令的参数解析器。"""

    parser = argparse.ArgumentParser(
        prog="python -m src.cli.graph",
        description=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # === build ========================================================
    build = subparsers.add_parser(
        "build",
        help="从已发布 Artifact 构建关系图（全量）",
    )
    build.add_argument("--wiki-id", required=True)
    build.add_argument("--namespace", required=True)
    build.add_argument("--version", required=True)
    build.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计并打印，不写 Mongo",
    )
    build.add_argument(
        "--clear",
        action="store_true",
        help="构建前清空该 scope 内的旧边",
    )
    build.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="每次批量写入边数（默认 500）",
    )

    # === upsert =======================================================
    upsert = subparsers.add_parser(
        "upsert",
        help="增量同步指定 artifacts 的关系边",
    )
    upsert.add_argument("--wiki-id", required=True)
    upsert.add_argument("--namespace", required=True)
    upsert.add_argument("--version", required=True)
    upsert.add_argument(
        "--artifact-id",
        action="append",
        dest="artifact_ids",
        required=True,
        help="要同步的 artifact_id，可重复传入",
    )
    upsert.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计，不写 Mongo",
    )

    # === stats ========================================================
    stats = subparsers.add_parser(
        "stats",
        help="查看图的节点/边/度数/孤儿统计",
    )
    stats.add_argument("--wiki-id", required=True)
    stats.add_argument("--namespace", required=True)
    stats.add_argument("--version", required=True)
    stats.add_argument(
        "--top-k",
        type=int,
        default=20,
        help="Top 度数节点数（默认 20）",
    )

    # === export =======================================================
    export = subparsers.add_parser(
        "export",
        help="导出图为 JSON（支持分批写入）",
    )
    export.add_argument("--wiki-id", required=True)
    export.add_argument("--namespace", required=True)
    export.add_argument("--version", required=True)
    export.add_argument(
        "--output",
        "-o",
        help="输出文件路径（单文件模式省略则打印到 stdout）",
    )
    export.add_argument(
        "--compact",
        action="store_true",
        help="单行 JSON（适合管道），默认 pretty 缩进",
    )
    export.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help=(
            f"每批最大边数（默认 {DEFAULT_BATCH_SIZE}）。"
            "指定后写入 --output-dir 下的 batch-NNN.json + manifest.json，"
            "单批内存峰值受 batch_size 约束，避免大图导出超时。"
        ),
    )
    export.add_argument(
        "--output-dir",
        help="分批写入的目标目录（与 --batch-size 配合使用）",
    )

    return parser


def _stats_to_payload(
    stats,
    *,
    written: int,
    dry_run: bool,
    cleared: int,
) -> dict[str, object]:
    return {
        "artifacts_scanned": stats.artifacts_scanned,
        "relations_seen": stats.relations_seen,
        "relations_kept": stats.relations_kept,
        "relations_broken": stats.relations_broken,
        "relations_out_of_scope": stats.relations_out_of_scope,
        "relations_unresolved_target": stats.relations_unresolved_target,
        "broken_edge_threshold": stats.broken_edge_threshold,
        "by_relation_type": stats.by_relation_type,
        "by_strength_tier": stats.by_strength_tier,
        "edges_written": written,
        "edges_cleared": cleared,
        "dry_run": dry_run,
    }


async def _run_build(args: argparse.Namespace) -> dict[str, object]:
    mongo = MongoDatabase()
    await mongo.connect()
    try:
        knowledge_repo = MongoKnowledgeRepository(mongo)
        graph_store = MongoRelationGraphStore(mongo)
        await graph_store.ensure_indexes()

        cleared = 0
        if args.clear and not args.dry_run:
            cleared = await graph_store.clear_scope(args.wiki_id, args.namespace, args.version)

        builder = RelationGraphBuilder(knowledge_repo, graph_store)
        edges, stats = await builder.build_for_scope(args.wiki_id, args.namespace, args.version)

        if args.dry_run:
            return _stats_to_payload(stats, written=0, dry_run=True, cleared=0)

        written = 0
        batch: list = []
        for edge in edges:
            batch.append(edge)
            if len(batch) >= args.batch_size:
                written += await graph_store.upsert_edges(batch)
                batch = []
        if batch:
            written += await graph_store.upsert_edges(batch)

        return _stats_to_payload(stats, written=written, dry_run=False, cleared=cleared)
    finally:
        await mongo.close()


async def _run_upsert(args: argparse.Namespace) -> dict[str, object]:
    mongo = MongoDatabase()
    await mongo.connect()
    try:
        knowledge_repo = MongoKnowledgeRepository(mongo)
        graph_store = MongoRelationGraphStore(mongo)
        await graph_store.ensure_indexes()

        builder = RelationGraphBuilder(knowledge_repo, graph_store)
        if args.dry_run:
            # 干跑：只跑 build_for_scope 不可行（输入是全量），改成手动读 artifact 校验

            active = await knowledge_repo.list_active_artifacts(
                args.wiki_id, args.namespace, args.version
            )
            active_ids = {artifact.artifact_id for artifact in active}
            requested = tuple(dict.fromkeys(args.artifact_ids))
            missing = [aid for aid in requested if aid not in active_ids]
            return {
                "status": "ok",
                "mode": "dry-run",
                "artifacts_requested": len(requested),
                "artifacts_processed": len(requested) - len(missing),
                "artifacts_missing": missing,
                "broken_edge_threshold": BROKEN_EDGE_THRESHOLD,
            }

        # 关键：先做存在性预检，再调用 builder（builder 内部会重复校验）
        active = await knowledge_repo.list_active_artifacts(
            args.wiki_id, args.namespace, args.version
        )
        active_ids = {artifact.artifact_id for artifact in active}
        requested = tuple(dict.fromkeys(args.artifact_ids))
        missing = [aid for aid in requested if aid not in active_ids]
        if missing:
            raise ValueError(f"unknown or non-active artifact_ids: {sorted(missing)}")

        stats_obj = await builder.upsert_artifact_edges(
            args.wiki_id,
            args.namespace,
            args.version,
            requested,
        )
        payload = stats_obj.to_payload()
        payload["status"] = "ok"
        payload["broken_edge_threshold"] = BROKEN_EDGE_THRESHOLD
        return payload
    finally:
        await mongo.close()


async def _run_stats(args: argparse.Namespace) -> dict[str, object]:
    mongo = MongoDatabase()
    await mongo.connect()
    try:
        graph_store = MongoRelationGraphStore(mongo)
        await graph_store.ensure_indexes()
        stats_obj = await compute_graph_stats(
            graph_store,
            args.wiki_id,
            args.namespace,
            args.version,
            top_k=args.top_k,
        )
        payload = stats_obj.to_payload()
        payload["status"] = "ok"
        return payload
    finally:
        await mongo.close()


async def _run_export(args: argparse.Namespace) -> dict[str, object]:
    """执行 export。

    - 默认单文件模式：返回 JSON 字符串，main 写到 --output 或 stdout
    - 分批模式（``--batch-size`` 指定）：写入 ``--output-dir`` 下的
      ``batch-NNN.json`` + ``manifest.json``，返回汇总 dict
    """

    if args.batch_size is not None and not args.output_dir:
        raise ValueError("--batch-size 必须与 --output-dir 一起使用")
    if args.output_dir and args.batch_size is None:
        raise ValueError("--output-dir 必须与 --batch-size 一起使用")
    if args.output_dir and args.output:
        raise ValueError("不能同时使用 --output 与 --output-dir")

    mongo = MongoDatabase()
    await mongo.connect()
    try:
        graph_store = MongoRelationGraphStore(mongo)
        await graph_store.ensure_indexes()

        # 分批写入模式
        if args.batch_size is not None:
            from pathlib import Path

            out_dir = Path(args.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)

            batch_size = int(args.batch_size)
            manifest = {
                "scope": {
                    "wiki_id": args.wiki_id,
                    "namespace": args.namespace,
                    "version": args.version,
                },
                "batch_size": batch_size,
                "broken_edge_threshold": BROKEN_EDGE_THRESHOLD,
                "batches": [],
                "total_edges": 0,
            }
            cumulative_edges = 0
            async for batch in iter_graph_export_batches(
                graph_store,
                args.wiki_id,
                args.namespace,
                args.version,
                batch_size=batch_size,
            ):
                file_name = f"batch-{batch.batch_index:04d}.json"
                file_path = out_dir / file_name
                payload = batch.to_payload()

                def _write_one(payload: dict, path: Path) -> None:
                    with open(path, "w", encoding="utf-8") as fp:
                        json.dump(payload, fp, ensure_ascii=False, indent=2)

                await asyncio.to_thread(_write_one, payload, file_path)
                manifest["batches"].append(
                    {
                        "index": batch.batch_index,
                        "file": file_name,
                        "edge_count": batch.edge_count,
                        "is_last": batch.is_last,
                    }
                )
                cumulative_edges += batch.edge_count
                manifest["total_edges"] = cumulative_edges

            def _write_manifest() -> None:
                with open(out_dir / "manifest.json", "w", encoding="utf-8") as fp:
                    json.dump(manifest, fp, ensure_ascii=False, indent=2)

            await asyncio.to_thread(_write_manifest)
            return {
                "mode": "batched",
                "output_dir": str(out_dir),
                "batch_count": len(manifest["batches"]),
                "total_edges": cumulative_edges,
                "batch_size": batch_size,
            }

        # 单文件模式（保持向后兼容）
        text = await export_graph_json(
            graph_store,
            args.wiki_id,
            args.namespace,
            args.version,
            pretty=not args.compact,
        )
        return {"mode": "single", "text": text}
    finally:
        await mongo.close()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。返回 0=成功，1=失败。"""

    args = build_parser().parse_args(argv)
    try:
        if args.command == "build":
            payload = asyncio.run(_run_build(args))
            payload["status"] = "ok"
            payload["broken_edge_threshold"] = BROKEN_EDGE_THRESHOLD
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "upsert":
            payload = asyncio.run(_run_upsert(args))
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "stats":
            payload = asyncio.run(_run_stats(args))
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "export":
            result = asyncio.run(_run_export(args))
            if result.get("mode") == "batched":
                print(
                    json.dumps(
                        {
                            "status": "ok",
                            "mode": "batched",
                            "output_dir": result["output_dir"],
                            "batch_count": result["batch_count"],
                            "total_edges": result["total_edges"],
                            "batch_size": result["batch_size"],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            # 单文件模式
            text = result["text"]
            if args.output:
                with open(args.output, "w", encoding="utf-8") as fp:
                    fp.write(text)
                print(
                    json.dumps(
                        {
                            "status": "ok",
                            "path": args.output,
                            "bytes": len(text.encode("utf-8")),
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                print(text)
            return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "broken_edge_threshold": BROKEN_EDGE_THRESHOLD,
                    "error": str(error),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps({"status": "failed", "error": "unknown command"}),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
