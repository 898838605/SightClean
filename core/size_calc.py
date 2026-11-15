"""文件夹大小：后台算、随时能停、切目录立刻作废。

设计要点：
1. 一个文件夹一个 QRunnable，丢进本引擎自己的 QThreadPool 并发跑（默认 2 个线程，
   再多只是让磁盘来回寻道，不会更快）。
2. 任务带 CancelFlag：切换目录时整组作废，在途的任务在下一个检查点自行退出，
   迟到的结果按 generation 丢弃，绝不会串到新目录上。
3. 递归用 os.scandir + 显式栈，不跟随 junction / 符号链接，并且用 (设备号, inode)
   去重、限制最大深度 —— 三道保险，防止绕圈和重复统计。
4. 打不开的子目录只计入「跳过了几个」，不影响其他项，也不让整个任务失败。
"""
from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from app_paths import to_long

MAX_DEPTH = 64          # 再深基本可以确定是绕圈了
CHECK_EVERY = 128       # 每读这么多个条目检查一次取消标志
DEFAULT_CONCURRENCY = 2


@dataclass(frozen=True)
class SizeResult:
    human_path: str
    size: int | None
    state: str          # ok / partial / denied / cancelled
    skipped: int = 0    # 跳过的子项：没权限、超深度、绕圈的、链接
    reason: str = ""


class CancelFlag:
    """一组任务的取消标志。切换目录时把整组标记作废。"""

    __slots__ = ("_event", "generation")

    def __init__(self, generation: int) -> None:
        self._event = threading.Event()
        self.generation = generation

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


def _is_link(st: os.stat_result) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        return True
    return stat.S_ISLNK(st.st_mode)


def measure(human_path: str, cancel: CancelFlag) -> SizeResult:
    """算一个文件夹的总大小。纯函数，方便单测。"""
    root = to_long(human_path)

    # 先探一下根目录能不能进：进不去就整条算「打不开」，不要显示成 0
    try:
        with os.scandir(root):
            pass
    except OSError as exc:
        return SizeResult(human_path, None, "denied", 0, _read_error(exc))

    total = 0
    skipped = 0
    seen: set[tuple[int, int]] = set()
    stack: list[tuple[str, int]] = [(root, 0)]
    visited = 0

    while stack:
        if cancel.cancelled:
            return SizeResult(human_path, None, "cancelled", skipped, "已经停下")
        current, depth = stack.pop()
        try:
            scanner = os.scandir(current)
        except OSError:
            skipped += 1
            continue
        with scanner:
            for entry in scanner:
                visited += 1
                if visited % CHECK_EVERY == 0 and cancel.cancelled:
                    return SizeResult(human_path, None, "cancelled", skipped, "已经停下")
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    skipped += 1
                    continue
                if _is_link(info):
                    # junction / 符号链接一律不跟：跟进去可能算到别的盘，也可能绕圈
                    skipped += 1
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    skipped += 1
                    continue
                if is_dir:
                    if depth + 1 > MAX_DEPTH:
                        skipped += 1
                        continue
                    # inode 去重只在真拿得到 inode 时才用：带 \\?\ 前缀遍历时
                    # DirEntry.stat() 往往给 0（Win32 的目录枚举不带 inode），
                    # 此时若照常比较，所有目录都会被当成同一个，整棵子树被跳过。
                    inode = getattr(info, "st_ino", 0)
                    if inode:
                        key = (getattr(info, "st_dev", 0), inode)
                        if key in seen:
                            skipped += 1      # 真绕回来了
                            continue
                        seen.add(key)
                    stack.append((entry.path, depth + 1))
                else:
                    total += info.st_size

    state = "partial" if skipped else "ok"
    reason = f"有 {skipped} 个子项打不开或者没跟进去，这个数字是估算的" if skipped else ""
    return SizeResult(human_path, total, state, skipped, reason)


def _read_error(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "这个文件夹打不开（系统不让读）"
    return "这个文件夹打不开"


class _SizeTask(QRunnable):
    """算一个文件夹。结果通过引擎的信号回主线程。"""

    def __init__(self, engine: "SizeEngine", human_path: str, cancel: CancelFlag) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._engine = engine
        self._path = human_path
        self._cancel = cancel

    def run(self) -> None:  # noqa: D102
        try:
            result = measure(self._path, self._cancel)
        except Exception:  # noqa: BLE001 - 单个任务绝不能把线程炸掉
            result = SizeResult(self._path, None, "denied", 0, "算的时候出了点问题")
        if self._cancel.cancelled:
            return                      # 已经切走了，什么都不用说
        try:
            self._engine.sizeReady.emit(
                self._cancel.generation, result.human_path, result.size, result.state
            )
            self._engine.task_finished(self._cancel.generation)
        except RuntimeError:
            # 窗口已经关掉，底层 C++ 对象没了，安静退出
            pass


class SizeEngine(QObject):
    """对外只有四个动作：start / enqueue / cancel / stop。"""

    sizeReady = Signal(int, str, object, str)   # generation, human_path, bytes 或 None, state
    batchDone = Signal(int)

    def __init__(self, parent: QObject | None = None, max_concurrency: int = DEFAULT_CONCURRENCY) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(max_concurrency)))
        self._cancel: CancelFlag | None = None
        self._lock = threading.Lock()
        self._pending = 0
        self._stopped = False

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        self._stopped = False

    def set_max_concurrency(self, count: int) -> None:
        self.setMaxThreadCountCompat(count)

    def setMaxThreadCountCompat(self, count: int) -> None:  # noqa: N802
        self._pool.setMaxThreadCount(max(1, min(8, int(count))))

    def stop(self) -> None:
        """关窗口时调用：先让在途任务知道要停，再等它们收尾。"""
        self._stopped = True
        self.cancel()
        self._pool.waitForDone(3000)

    # -- 任务 ---------------------------------------------------------------
    def enqueue(self, paths: list[str], generation: int) -> None:
        self.cancel()
        flag = CancelFlag(generation)
        self._cancel = flag
        paths = [path for path in paths if path]
        with self._lock:
            self._pending = len(paths)
        if not paths:
            self.batchDone.emit(generation)
            return
        for path in paths:
            if flag.cancelled:
                break
            self._pool.start(_SizeTask(self, path, flag))

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()
            self._cancel = None
        self._pool.clear()          # 还没开始的任务直接丢掉
        with self._lock:
            self._pending = 0

    def task_finished(self, generation: int) -> None:
        with self._lock:
            if self._pending > 0:
                self._pending -= 1
            remaining = self._pending
        if remaining == 0:
            self.batchDone.emit(generation)

    # -- 便捷 ---------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._cancel is not None
