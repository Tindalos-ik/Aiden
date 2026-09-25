"""定时任务的运行控制：非阻塞实例锁与停止信号。

**为什么必须自己实现。** 本阶段不引入新的调度依赖：定时任务是独立进程，用“跑完一轮、按配置
周期等待、再跑下一轮”的循环就能表达执行周期，比引入调度框架更少隐藏状态。但有三件事必须由代码
保证，否则重复运行会破坏知识库的幂等性：

1. **同一时刻只允许一个任务实例。** 两个实例同时抽取会各自算出相同的批次签名，虽然有唯一键兜底
   （后写者跳过），但会白白消耗模型调用；更重要的是入库阶段会对同一组候选重复写 knowledge_chunks。
   因此用一个进程级非阻塞锁把并发实例挡在门外。
2. **锁要能被强杀后自动释放。** 锁绑定在打开的文件句柄上，进程无论正常退出还是被强杀，操作系统
   都会释放句柄，下一个实例立刻可以获取，不需要人工清理锁文件。
3. **能被优雅停止。** 定时模式可能长时间驻留，`Ctrl+C` 或者别的进程要求停止时必须让当前批次跑完
   再退出，而不是在调用模型或写库的中途被打断。

本模块不导入 FastAPI，也不在导入时启动任何线程或循环；任务只能通过命令行入口显式启动。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    """已有任务实例在运行，本次启动被拒绝。"""


class _InstanceLock:
    """基于文件句柄的非阻塞进程锁。

    需要说明一个边界：`msvcrt.locking` / `fcntl.flock` 属于建议锁，只约束同样走本实现的进程，
    不阻止运维手工启动的第二个副本。非 Windows 上退化为“独占创建 + 写入 pid”，仍然是打开文件
    后立即判断能否独占，语义一致。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def acquire(self) -> None:
        """尝试获取锁；已被占用时抛 `AlreadyRunningError`，不等待。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AlreadyRunningError(
                f"已有对话挖掘任务在运行（锁文件 {self._path}）。"
                "同一时刻只允许一个实例，避免重复抽取与重复写入知识库。"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        """释放锁并删除锁文件；重复调用是安全的。"""
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None
            try:
                self._path.unlink()
            except OSError:
                # 锁文件残留不影响正确性：下一次获取仍然以文件锁为准，而不是文件是否存在。
                pass


@dataclass
class MiningRuntimeControl:
    """一次任务进程的运行状态。

    `accepting` 是停止信号：收到中断后不再开始新的一轮，让当前一轮把已完成的工作写回数据库后
    正常退出，因此中断不会留下半写完的批次状态。
    """

    lock_path: Path
    accepting: bool = True
    _lock: _InstanceLock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._lock = _InstanceLock(self.lock_path)

    def acquire(self) -> None:
        """获取实例锁；失败时抛 `AlreadyRunningError`。"""
        self._lock.acquire()

    def release(self) -> None:
        """释放实例锁。"""
        self._lock.release()

    def request_stop(self) -> None:
        """请求优雅停止：当前轮次跑完后退出循环。"""
        self.accepting = False


def default_lock_path() -> Path:
    """默认锁文件位置：系统临时目录下按项目名固定命名。

    放在临时目录而不是仓库里，避免运行时产物污染工作区；按固定名而不是随机名，才能让并发实例
    真正撞上同一把锁。
    """
    return Path(tempfile.gettempdir()) / "aiden-conversation-mining.lock"
