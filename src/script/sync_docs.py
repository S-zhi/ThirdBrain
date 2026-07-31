"""可扩展文档同步框架 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from src.doc_sync import DocumentSyncError, load_document_sync_config
from src.doc_sync.errors import SyncLockError
from src.doc_sync.service import DocumentSyncService

DEFAULT_CONFIG_PATH = Path("configs/document_sync.yaml")


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    """给 bootstrap/sync 子命令添加相同的运行参数。"""
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"独立同步 YAML；默认 {DEFAULT_CONFIG_PATH}",
    )
    apply_group = parser.add_mutually_exclusive_group()
    apply_group.add_argument(
        "--apply",
        action="store_true",
        help="验证后写入 Markdown 和持久化 state",
    )
    apply_group.add_argument(
        "--dry-run",
        action="store_true",
        help="只生成 staging/manifest；这是默认行为",
    )
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        help="只运行指定 source id；可重复",
    )
    parser.add_argument("--limit", type=int, help="限制每个 source 最多处理 N 页")
    parser.add_argument(
        "--trigger",
        choices=["manual", "scheduled"],
        default="manual",
        help="写入 manifest 的触发来源",
    )


def build_parser() -> argparse.ArgumentParser:
    """构造 bootstrap、sync 和 resume 三个子命令。"""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("bootstrap", "sync"):
        command_parser = subparsers.add_parser(command)
        _add_run_arguments(command_parser)
    resume = subparsers.add_parser("resume")
    resume.add_argument("--run-id", required=True, help="原 apply journal 的 run_id")
    resume.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"独立同步 YAML；默认 {DEFAULT_CONFIG_PATH}",
    )
    resume.add_argument(
        "--trigger",
        choices=["manual", "scheduled"],
        default="manual",
    )
    return parser


async def _run(args: argparse.Namespace) -> tuple[str, str, int]:
    """加载配置、执行命令并返回紧凑 CLI 汇总和退出码。"""
    config = load_document_sync_config(args.config)
    service = DocumentSyncService(config)
    if args.command == "resume":
        manifest, manifest_path = await service.resume(
            args.run_id,
            trigger=args.trigger,
        )
    else:
        manifest, manifest_path = await service.run(
            args.command,
            apply=bool(args.apply),
            selected_source_ids=set(args.sources) if args.sources else None,
            limit=args.limit,
            trigger=args.trigger,
        )
    exit_code = 0
    if manifest.status == "partial":
        exit_code = 2
    elif manifest.status == "failed":
        exit_code = 1
    summary = json.dumps(
        {
            "run_id": manifest.run_id,
            "status": manifest.status,
            "manifest_path": str(manifest_path),
            "updated_markdown": manifest.updated_markdown,
            "stats": manifest.stats.model_dump(),
            "errors": manifest.errors,
        },
        ensure_ascii=False,
        indent=2,
    )
    return summary, str(manifest_path), exit_code


def main(argv: list[str] | None = None) -> int:
    """执行 CLI 并用稳定退出码表达成功、失败、部分成功和锁冲突。"""
    args = build_parser().parse_args(argv)
    try:
        summary, _, exit_code = asyncio.run(_run(args))
    except SyncLockError as exc:
        print(f"同步锁冲突: {exc}", file=sys.stderr)
        return 3
    except (DocumentSyncError, OSError, ValueError) as exc:
        print(f"同步失败: {exc}", file=sys.stderr)
        return 1
    print(summary)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
