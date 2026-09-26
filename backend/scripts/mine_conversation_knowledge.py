"""对话挖掘任务的独立入口：从历史客服对话抽取知识并入库。

用法（在 backend 目录下执行）：

    # 手动运行一次（默认），处理完本次批次就退出
    python -m scripts.mine_conversation_knowledge

    # 只做抽取与暂存，不写 knowledge_chunks、不补向量
    python -m scripts.mine_conversation_knowledge --staging-only

    # 以固定周期常驻运行（周期也可用 MINING_INTERVAL_SECONDS 配置）
    python -m scripts.mine_conversation_knowledge --interval 3600

    # 查看当前进度：批次状态、候选状态、留待处理的候选
    python -m scripts.mine_conversation_knowledge stats

    # 从上次中断处继续：默认把遗留的 extracting 批次放回 pending 后重跑一次
    python -m scripts.mine_conversation_knowledge

**这是离线任务，不绑定 FastAPI。** 它不在应用导入阶段启动任何线程或循环，也不由 HTTP 请求触发；
定时执行由本入口自己的“跑一轮、按周期等待、再跑一轮”循环完成，因此 API 进程的重启与在线流量都
不影响抽取节奏。需要停止时按 `Ctrl+C`，当前批次会跑完并把状态写回数据库。

**并发保护。** 入口启动时获取一个非阻塞的进程锁，已有实例在运行时直接拒绝启动，避免两个实例同时
抽取同一批消息、重复写入知识库。锁绑定在文件句柄上，进程被强杀后由操作系统释放，不需要人工清理。

**中断续跑。** 每批次的抽取结果都先写暂存表再更新批次状态；被强杀时停在 `extracting` 的批次会在
下一次运行开始时放回 `pending`，所以直接重跑同一命令即可继续，不会重复抽取已完成的批次。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from app.config.conversation_mining import mining_settings
from app.persistence.mysql import conversation_mining as staging_repo

from app.services.rag import conversation_mining
from app.services.rag.extraction import ConversationExtractionClient, ExtractionError
from app.services.rag.runtime_control import (
    AlreadyRunningError,
    MiningRuntimeControl,
    default_lock_path,
)


def _print_json(payload: object) -> None:
    """以稳定的 UTF-8 JSON 输出结果，便于人工核对或重定向保存。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _check_llm_configured() -> bool:
    """确认抽取模型配置齐全；缺失时给出一行可读提示而不是抛底层鉴权错误。"""
    if mining_settings.llm_configured:
        return True
    print(
        "未配置抽取模型：请在 backend/.env 中配置 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL，"
        "或用 MINING_LLM_API_KEY / MINING_LLM_BASE_URL / MINING_LLM_MODEL 单独指定。"
    )
    return False


def _run_one_round(*, staging_only: bool, reset_stale: bool, verbose: bool) -> dict:
    """执行一轮任务；仅暂存模式不触发整体去重、正式入库或向量化。

    返回可序列化的统计，供单次模式直接输出，也供定时模式逐轮打印。`reset_stale` 为真时先把上次
    中断留下的 `extracting` 批次放回 `pending`，这是“从上次中断处继续”的开关。
    """
    stale_reset = staging_repo.reset_stale_extracting_batches() if reset_stale else 0
    client = ConversationExtractionClient()

    def progress(message: str) -> None:
        if verbose:
            print(f"  {message}")

    result = conversation_mining.run_pipeline(
        client=client, promote=not staging_only, on_progress=progress
    )
    payload = {
        "run_id": result.extraction.run_id,
        "stale_batches_reset": stale_reset,
        "extraction": asdict(result.extraction),
        "skipped_dedupe": result.skipped_dedupe,
        "notes": list(result.notes),
    }
    if result.promotion is not None:
        payload["promotion"] = asdict(result.promotion)
    payload["batches_by_status"] = staging_repo.count_batches_by_status()
    payload["run_candidates_by_status"] = staging_repo.count_run_candidates_by_status(
        result.extraction.run_id
    )
    return payload


def _run_once(args: argparse.Namespace) -> int:
    """手动运行一次，使用与定时模式相同的实例锁保护完整抽取与入库流程。"""
    if not _check_llm_configured():
        return 2
    lock_path = Path(args.lock_file).expanduser() if args.lock_file else default_lock_path()
    control = MiningRuntimeControl(lock_path=lock_path)
    control.acquire()
    try:
        payload = _run_one_round(
            staging_only=args.staging_only,
            reset_stale=not args.no_reset_stale,
            verbose=not args.quiet,
        )
    finally:
        control.release()
    payload["command"] = "once"
    _print_json(payload)
    # 退出码：0 表示本轮工作已完整结束；1 表示还有遗留（批次上限或抽取失败），可再次运行继续。
    return 1 if payload["skipped_dedupe"] else 0


def _run_scheduled(args: argparse.Namespace) -> int:
    """以固定周期常驻运行。

    每轮结束才等待，等待期间不持有任何数据库连接；轮次之间互相独立，因此某一轮失败不会中断后续
    轮次。收到 `Ctrl+C` 时停止循环并正常退出。
    """
    if not _check_llm_configured():
        return 2
    interval = max(1, args.interval or mining_settings.interval_seconds)
    print(
        f"对话挖掘定时任务启动：每 {interval} 秒一轮（可用 --interval 或 "
        "MINING_INTERVAL_SECONDS 配置）。按 Ctrl+C 停止。"
    )
    # 命令行给出的是字符串，这里显式转成 Path，避免把裸字符串当成路径对象使用。
    lock_path = Path(args.lock_file).expanduser() if args.lock_file else default_lock_path()
    control = MiningRuntimeControl(lock_path=lock_path)
    try:
        control.acquire()
    except AlreadyRunningError as exc:
        print(f"错误：{exc}")
        return 3
    try:
        round_index = 0
        while control.accepting:
            round_index += 1
            print(f"[第 {round_index} 轮] {time.strftime('%Y-%m-%d %H:%M:%S')}")
            try:
                payload = _run_one_round(
                    staging_only=args.staging_only,
                    reset_stale=not args.no_reset_stale,
                    verbose=not args.quiet,
                )
                _print_json(payload)
            except Exception as exc:  # 单轮失败不应终止定时任务
                print(f"本轮失败：{type(exc).__name__}: {exc}")
                if args.traceback:
                    raise
            if not control.accepting:
                break
            print(f"等待 {interval} 秒后进入下一轮……")
            # 用短间隔轮询等待，使 Ctrl+C 能在一秒内响应，而不是等满整个周期。
            waited = 0.0
            while control.accepting and waited < interval:
                time.sleep(min(1.0, interval - waited))
                waited += 1.0
    except KeyboardInterrupt:
        print("收到中断，已停止定时任务；当前轮的数据库状态已提交，可重跑继续。")
    finally:
        control.release()
    return 0


def _run_stats(args: argparse.Namespace) -> int:
    """输出批次与候选的状态统计，不调用模型、不写任何数据。"""
    payload = {
        "command": "stats",
        "batches_by_status": staging_repo.count_batches_by_status(),
        "candidates_by_status": staging_repo.count_candidates_by_status(),
        "latest_run_id": staging_repo.latest_run_id(),
        "batch_batch_size": {
            "max_turns_per_batch": mining_settings.max_turns_per_batch,
            "max_batch_chars": mining_settings.max_batch_chars,
        },
        "interval_seconds": mining_settings.interval_seconds,
    }
    _print_json(payload)
    # 退出码 1 表示还有待处理候选或待抽取批次，便于脚本化巡检。
    pending = payload["candidates_by_status"].get(
        staging_repo.CANDIDATE_STATUS_STAGED, 0
    ) + payload["batches_by_status"].get(staging_repo.BATCH_STATUS_PENDING, 0)
    return 1 if pending else 0


def _run_list_rejected(args: argparse.Namespace) -> int:
    """列出留待处理的候选及其原因，供人工复核。

    只读；用于确认“哪些候选没有进入正式知识库以及为什么”。
    """
    rows = staging_repo.list_candidates_for_review(
        status=args.status, limit=args.limit, run_id=args.run_id
    )
    payload = {"command": "list-candidates", "count": len(rows), "candidates": rows}
    _print_json(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.mine_conversation_knowledge",
        description="对话挖掘：历史客服对话 -> 分批 LLM 抽取 -> 暂存 -> 整体去重 -> knowledge_chunks -> Milvus。",
    )
    parser.add_argument(
        "--traceback", action="store_true", help="出错时打印完整堆栈，便于排查外部服务问题"
    )
    # 默认行为是“手动运行一次”，方便人工触发；定时模式必须显式给出周期。
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help=(
            "以该秒数为周期常驻运行；不指定时只运行一次。"
            f"缺省周期取 MINING_INTERVAL_SECONDS（当前配置 {mining_settings.interval_seconds} 秒）"
        ),
    )
    parser.add_argument(
        "--staging-only",
        action="store_true",
        help="只抽取并写入暂存表，不做整体去重与入库，也不补向量",
    )
    parser.add_argument(
        "--no-reset-stale",
        action="store_true",
        help="不把上次中断遗留的 extracting 批次放回 pending（默认会放回，即从断点续跑）",
    )
    parser.add_argument("--quiet", action="store_true", help="不打印逐批进度")
    parser.add_argument(
        "--lock-file", default=None, help="实例锁文件路径；默认放在系统临时目录"
    )

    sub = parser.add_subparsers(dest="command")
    once = sub.add_parser("once", help="手动运行一次（默认行为）")
    once.set_defaults(handler=_run_once)

    schedule = sub.add_parser(
        "schedule", help="按周期常驻运行（等价于不指定子命令时传入 --interval）"
    )
    # 这几个开关在主解析器上定义，写子命令时容易顺手把它们放在 schedule 之后；这里一并注册，
    # 使 `schedule --interval 60` 和 `--interval 60` 两种写法都成立。
    schedule.add_argument("--interval", type=int, default=None, help="定时周期秒数")
    schedule.add_argument("--staging-only", action="store_true", help="只抽取并写入暂存表")
    schedule.add_argument("--no-reset-stale", action="store_true", help="不把遗留的 extracting 批次放回 pending")
    schedule.add_argument("--quiet", action="store_true", help="不打印逐批进度")
    schedule.add_argument("--lock-file", default=None, help="实例锁文件路径")
    schedule.set_defaults(handler=_run_scheduled)

    stats = sub.add_parser("stats", help="查看批次与候选状态统计，不写数据")
    stats.set_defaults(handler=_run_stats)

    review = sub.add_parser("list-candidates", help="列出留待处理或已拒绝的候选及原因")
    review.add_argument(
        "--status",
        default=staging_repo.CANDIDATE_STATUS_REJECTED,
        help="候选状态：staged（待处理）/ rejected（未入库）/ promoted（已入库）",
    )
    review.add_argument("--run-id", default=None, help="只看某次运行的候选")
    review.add_argument("--limit", type=int, default=50)
    review.set_defaults(handler=_run_list_rejected)
    return parser


def _resolve_handler(args: argparse.Namespace):
    """决定本次执行哪个动作。

    * 写了子命令就用子命令的处理器；
    * 只写了 `--interval` 按定时模式运行，因为“周期”只有在常驻运行时才有意义；
    * 什么都没写就是“手动运行一次”，这是最常用的用法。
    """
    handler = getattr(args, "handler", None)
    if handler is not None:
        return handler
    if args.interval:
        return _run_scheduled
    return _run_once


def main(argv: list[str] | None = None) -> int:
    """命令行入口；返回进程退出码。

    退出码约定：0 本轮工作完整结束，1 还有遗留（批次上限、抽取失败或存在待处理候选），
    2 配置或外部服务出错，3 已有实例在运行，130 收到中断。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _resolve_handler(args)
    try:
        return int(handler(args))
    except AlreadyRunningError as exc:
        print(f"错误：{exc}")
        return 3
    except ExtractionError as exc:
        print(f"错误：{exc}")
        return 2
    except ValueError as exc:
        print(f"错误：{exc}")
        return 2
    except RuntimeError as exc:
        print(f"错误：{exc}")
        return 2
    except KeyboardInterrupt:
        print("已中断。重新执行同一命令即可从待抽取批次继续，不会重复抽取已完成的批次。")
        return 130
    except Exception as exc:
        if args.traceback:
            raise
        print(f"错误：{type(exc).__name__}: {exc}")
        print("提示：确认 backend/.env 中的 DATABASE_URL、OPENAI_* 与 EMBEDDING_BASE_URL 可用；")
        print("      加 --traceback 可查看完整堆栈。")
        return 2


if __name__ == "__main__":
    sys.exit(main())
