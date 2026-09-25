"""离线建库命令行入口：把 Markdown 知识文档与现有商品 FAQ 导入 MySQL 并向量化。

用法（在 backend 目录下执行）：

    python -m scripts.build_knowledge_base import-markdown
    python -m scripts.build_knowledge_base import-markdown --directory ./knowledge --pattern "**/*.md"
    python -m scripts.build_knowledge_base import-faq
    python -m scripts.build_knowledge_base vectorize
    python -m scripts.build_knowledge_base scan-pending
    python -m scripts.build_knowledge_base stats
    python -m scripts.build_knowledge_base cleanup-vectors

命令之间可以任意重复执行：写入以 knowledge_chunks 主键与 chunk_key 幂等，Milvus 侧使用
upsert，因此重跑不会产生重复向量。数据库连接、向量服务地址与 Milvus 地址都来自
`backend/.env`（参考 backend/.env.example），本脚本不接受也不回显任何凭据。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from app.config.rag import rag_settings

from app.services.rag import indexing
from app.persistence.mysql import knowledge as knowledge_repo


def _print_json(payload: object) -> None:
    """以稳定的 UTF-8 JSON 输出结果，便于人工核对或重定向保存。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _summarize(results: list[indexing.SourceImportResult]) -> dict:
    """把逐来源结果汇总成便于阅读的统计。"""
    return {
        "sources": len(results),
        "chunks": sum(item.chunks for item in results),
        "inserted": sum(item.inserted for item in results),
        "updated": sum(item.updated for item in results),
        "reset_to_pending": sum(item.reset_to_pending for item in results),
        "need_manual_review": sum(item.need_manual_review for item in results),
        "superseded": sum(item.superseded for item in results),
    }


def _run_import_markdown(args: argparse.Namespace) -> int:
    """导入目录下的 Markdown 文件；只写 knowledge_chunks，不触碰业务表。"""
    directory = Path(args.directory).expanduser() if args.directory else rag_settings.knowledge_directory
    print(f"长度计量：{indexing.length_measurement_note()}")
    print(f"知识目录：{directory}")
    results = indexing.index_markdown_directory(directory, pattern=args.pattern)
    payload = {
        "command": "import-markdown",
        "directory": str(directory),
        **_summarize(results),
        "details": [asdict(item) for item in results],
    }
    _print_json(payload)
    return 0


def _run_import_faq(args: argparse.Namespace) -> int:
    """导入启用中的 FAQ 行；只读取 faq 表，不修改或清空任何已有数据。"""
    rows = knowledge_repo.list_active_faq_rows(args.limit)
    results = indexing.index_faq_rows(rows)
    payload = {
        "command": "import-faq",
        "active_faq_rows": len(rows),
        **_summarize(results),
        "details": [asdict(item) for item in results],
    }
    _print_json(payload)
    return 0


def _run_vectorize(args: argparse.Namespace) -> int:
    """扫描待向量化块并补齐，成功回填 vector_id 与状态。"""
    if not rag_settings.embedding_service_configured:
        print("未配置 EMBEDDING_BASE_URL，无法向量化；请先在 backend/.env 中配置向量服务地址。")
        return 2
    result = indexing.vectorize_pending(limit=args.limit)
    removed = indexing.cleanup_superseded_vectors() if args.cleanup else 0
    payload = {
        "command": "vectorize",
        "scanned_pending": result.scanned,
        "vectorized": result.vectorized,
        "deleted_superseded_vectors": removed,
        "remaining_by_status": knowledge_repo.count_chunks_by_status(),
    }
    _print_json(payload)
    return 0 if result.scanned == result.vectorized else 1


def _run_scan_pending(args: argparse.Namespace) -> int:
    """只扫描待向量化记录，用于确认中断后还有多少块需要补齐（不写任何数据）。"""
    pending = knowledge_repo.list_pending_chunks(args.limit)
    payload = {
        "command": "scan-pending",
        "pending": len(pending),
        "chunk_ids": [item.id for item in pending],
        "remaining_by_status": knowledge_repo.count_chunks_by_status(),
    }
    _print_json(payload)
    return 0 if not pending else 1


def _run_cleanup_vectors(args: argparse.Namespace) -> int:
    """删除已作废块在 Milvus 中的向量，可重复执行。"""
    removed = indexing.cleanup_superseded_vectors()
    _print_json({"command": "cleanup-vectors", "deleted_superseded_vectors": removed})
    return 0


def _run_stats(args: argparse.Namespace) -> int:
    """输出各向量状态的块数，用于建库后的自检。"""
    payload = {
        "command": "stats",
        "by_status": knowledge_repo.count_chunks_by_status(),
        "length_measurement": indexing.length_measurement_note(),
        "milvus_collection": rag_settings.milvus_collection,
        "embedding_model": rag_settings.embedding_model,
        "embedding_dimension": rag_settings.embedding_dimension,
    }
    _print_json(payload)
    return 0


def _run_chunks(args: argparse.Namespace) -> int:
    """分页查看已入库的知识块，作为后续在线检索读取接口的自检入口。"""
    rows = knowledge_repo.list_knowledge_chunks(
        category=args.category, limit=args.limit, offset=args.offset
    )
    payload = {"command": "chunks", "count": len(rows), "chunks": [asdict(row) for row in rows]}
    _print_json(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.build_knowledge_base",
        description="离线建库：Markdown 知识与现有 FAQ -> MySQL -> BGE-M3 -> Milvus。",
    )
    # 外部依赖（向量服务、Milvus）未就绪时默认只给一行可读错误；需要细节时再用该开关。
    parser.add_argument(
        "--traceback", action="store_true", help="出错时打印完整堆栈，便于排查外部服务问题"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    markdown = sub.add_parser("import-markdown", help="导入目录下的 Markdown 知识文件")
    markdown.add_argument(
        "--directory",
        default=None,
        help="知识目录；缺省用 RAG_KNOWLEDGE_DIR（默认 backend/knowledge）",
    )
    markdown.add_argument("--pattern", default="**/*.md", help="文件匹配模式，默认 **/*.md")
    markdown.set_defaults(handler=_run_import_markdown)

    faq = sub.add_parser("import-faq", help="导入启用中的 faq 行")
    faq.add_argument("--limit", type=int, default=None, help="最多导入多少条 FAQ 行")
    faq.set_defaults(handler=_run_import_faq)

    vectorize = sub.add_parser("vectorize", help="扫描待向量化块并补齐 Milvus 向量")
    vectorize.add_argument("--limit", type=int, default=None, help="本次最多处理多少块")
    vectorize.add_argument(
        "--cleanup",
        action="store_true",
        help="补齐后顺便删除已作废块的旧向量（等价于再执行 cleanup-vectors）",
    )
    vectorize.set_defaults(handler=_run_vectorize)

    scan = sub.add_parser("scan-pending", help="只看还有多少块待向量化，不写数据")
    scan.add_argument("--limit", type=int, default=None, help="最多列出多少块")
    scan.set_defaults(handler=_run_scan_pending)

    cleanup = sub.add_parser("cleanup-vectors", help="删除已作废块的 Milvus 旧向量")
    cleanup.set_defaults(handler=_run_cleanup_vectors)

    stats = sub.add_parser("stats", help="输出各向量状态的块数等自检信息")
    stats.set_defaults(handler=_run_stats)

    chunks = sub.add_parser("chunks", help="分页查看已入库知识块")
    chunks.add_argument("--category", default=None, help="只查看指定分类")
    chunks.add_argument("--limit", type=int, default=20)
    chunks.add_argument("--offset", type=int, default=0)
    chunks.set_defaults(handler=_run_chunks)

    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口；返回进程退出码，便于脚本判断是否还有遗留待处理块。

    建库依赖 MySQL、BGE-M3 服务和 Milvus，任一不可用时给出一行可读错误并返回退出码 2；
    加 `--traceback` 可以看到完整堆栈。退出码约定：0 成功，1 还有遗留待向量化块，2 出错。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except FileNotFoundError as exc:
        print(f"错误：{exc}")
        return 2
    except ValueError as exc:
        print(f"错误：{exc}")
        return 2
    except RuntimeError as exc:
        print(f"错误：{exc}")
        return 2
    except KeyboardInterrupt:
        # 与中断恢复设计配套：中断是预期情况，提示重跑命令而不是堆栈。
        print("已中断。重新执行同一命令即可从待向量化块继续，不会产生重复向量。")
        return 130
    except Exception as exc:
        if args.traceback:
            raise
        print(f"错误：{type(exc).__name__}: {exc}")
        print("提示：确认 backend/.env 中的 DATABASE_URL、EMBEDDING_BASE_URL 与 MILVUS_URI 可用；")
        print("      加 --traceback 可查看完整堆栈。")
        return 2


if __name__ == "__main__":
    sys.exit(main())
