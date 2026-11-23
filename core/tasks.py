"""后台任务的公共外壳。

以前每个模块都写一遍「QRunnable + 三个信号 + try/except + RuntimeError 兜底」，
抄来抄去容易出现「异常被吞、界面一直转圈」这种问题。这里统一成一个小基类：

- 子类只实现 work()，返回值会自动通过 finished 发出去；
- work() 抛异常时发 None（和旧代码的约定一致），界面据此显示失败；
- autoDelete 和「窗口已经关了就不要再 emit」都在基类处理。
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal


class WorkerSignals(QObject):
    """通用信号：进度（已完成、总数、当前项）+ 结果。"""

    progress = Signal(int, int, str)
    finished = Signal(object)


class Worker(QRunnable):
    """QRunnable 基类。子类实现 work() 即可。"""

    Signals = WorkerSignals

    def __init__(self) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.signals = self.Signals()

    def work(self) -> Any:
        raise NotImplementedError

    def run(self) -> None:
        try:
            result = self.work()
        except BaseException:  # noqa: BLE001 - 后台线程不能把异常抛给 Qt
            result = None
        self.emit_finished(result)

    def emit_finished(self, value: Any) -> None:
        """发结果；窗口已经销毁时安静收场。"""
        try:
            self.signals.finished.emit(value)
        except RuntimeError:
            pass


def make_call_worker(func):
    """Wrap a callable in the shared Worker contract."""

    class _CallWorker(Worker):
        def work(self):
            return func()

    return _CallWorker()
