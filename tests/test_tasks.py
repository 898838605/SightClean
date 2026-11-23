"""后台任务基类的测试：正常返回、抛异常、窗口没了不炸。"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from core.tasks import Worker


class WorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def make(self, value=None, exc=None):
        class _Task(Worker):
            def work(self):
                if exc is not None:
                    raise exc
                return value

        return _Task()

    def test_returns_value(self):
        received = []
        worker = self.make({"ok": True})
        worker.signals.finished.connect(received.append)
        worker.run()
        self.assertEqual(received, [{"ok": True}])

    def test_exception_becomes_none(self):
        """旧约定：后台任务失败发 None，界面据此显示失败，不能把异常抛给 Qt。"""
        received = []
        worker = self.make(exc=RuntimeError("炸了"))
        worker.signals.finished.connect(received.append)
        worker.run()
        self.assertEqual(received, [None])

    def test_progress_signal_shape(self):
        received = []
        worker = self.make()
        worker.signals.progress.connect(lambda *args: received.append(args))
        worker.signals.progress.emit(1, 3, r"C:\x")
        self.assertEqual(received, [(1, 3, r"C:\x")])

    def test_auto_delete_enabled(self):
        self.assertTrue(self.make().autoDelete())

    def test_call_worker_runs_function_and_returns_value(self):
        from core.tasks import make_call_worker

        received = []
        worker = make_call_worker(lambda: 42)
        worker.signals.finished.connect(received.append)
        worker.run()
        self.assertEqual(received, [42])


if __name__ == "__main__":
    unittest.main()
