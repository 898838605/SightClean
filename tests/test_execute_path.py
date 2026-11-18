"""真跑一遍执行路径 —— 不真删，但把参数准备那一段走完。

这条测试是为了挡住这类问题：代码里某个名字只在别的方法里 import 过，
单测因为全在方法级 mock，永远碰不到，一直到用户点按钮才炸。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

import app_paths
from core.config import CacheStore, SettingsStore
from core.knowledge import load_from_files

class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def build_window(self):
        from ui.main_window import MainWindow

        paths = app_paths.resolve()
        tmp = Path(tempfile.mkdtemp(prefix="exec-"))
        store = SettingsStore(tmp / "config.json", paths.resources / "default_config.json")
        settings = store.load()
        settings.last_module = 0
        settings.auto_size = False
        cache = CacheStore(paths.cache)
        cache.load()
        kb = load_from_files(paths.resources / "default_knowledge.json", paths.knowledge,
                             paths.user_rules)
        window = MainWindow(paths, settings, store, kb, cache)
        window.show()
        window.bootstrap()
        for _ in range(50):
            self.app.processEvents()
        return window, settings, store, kb, cache

class ExecutePathTests(Base):
    """四个模块的「点执行」入口都要真的能跑起来。"""

    def setUp(self):
        self.window, *_rest = self.build_window()
        self.dir = Path(tempfile.mkdtemp(prefix="execdir-"))
        (self.dir / "a.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        try:
            self.window.close()
        except Exception:  # noqa: BLE001
            pass

    def test_file_delete_path_does_not_raise(self):
        """文件模块：勾一项 → 点删除 → 走到 start_delete，全程不抛。"""
        window = self.window
        window.navigate(str(self.dir))
        for _ in range(60):
            self.app.processEvents()
        self.assertGreater(window.model.rowCount(), 0)

        entry = window.model.entry_at(0)
        window.model.set_checked(entry.human_path, True)

        started = []
        with mock.patch("ui.confirm_flow.ConfirmFlow.unlock", return_value=True), \
             mock.patch("ui.confirm_flow.ConfirmFlow.confirm") as confirm, \
             mock.patch("PySide6.QtCore.QThreadPool.start",
                        side_effect=lambda worker: started.append(worker)):
            from ui.confirm_flow import FlowDecision

            confirm.return_value = FlowDecision(items=window._build_delete_items(), mode="recycle")
            window._delete_selected()

        self.assertEqual(len(started), 1, "应该真的构造并启动了删除任务")
        self.assertIsNotNone(getattr(window, "_snap_id", None), "执行前应该留下快照")

    def test_retry_path_does_not_raise(self):
        from core import result_report

        window = self.window
        alive = self.dir / "b.txt"
        alive.write_text("y", encoding="utf-8")

        row = result_report.ResultRow(name="b.txt", path=str(alive), status="fail",
                                      error="拒绝访问", retryable=True)
        started = []
        with mock.patch("PySide6.QtCore.QThreadPool.start",
                        side_effect=lambda worker: started.append(worker)):
            window._retry_delete([row])

        self.assertEqual(len(started), 1, "重试应该重新启动一次删除")

    def test_registry_clean_path_does_not_raise(self):
        window = self.window
        page = window.registry_page
        page.navigate(r"HKEY_CURRENT_USER\Software")
        for _ in range(30):
            self.app.processEvents()

        from ui.confirm_flow import FlowDecision

        started = []
        with mock.patch("ui.confirm_flow.ConfirmFlow.unlock", return_value=True), \
             mock.patch("ui.confirm_flow.ConfirmFlow.confirm") as confirm, \
             mock.patch("PySide6.QtCore.QThreadPool.start",
                        side_effect=lambda worker: started.append(worker)):
            confirm.return_value = FlowDecision(items=[], mode=None)
            page._last_skipped = []
            page.execute_checked()

        # 没勾选时只是提示，不该崩
        self.assertEqual(len(started), 0)

    def test_process_kill_path_does_not_raise(self):
        window = self.window
        page = window.process_page
        page.refresh()
        for _ in range(20):
            self.app.processEvents()

        from ui.confirm_flow import FlowDecision

        started = []
        with mock.patch("ui.confirm_flow.ConfirmFlow.unlock", return_value=True), \
             mock.patch("ui.confirm_flow.ConfirmFlow.confirm") as confirm, \
             mock.patch("PySide6.QtCore.QThreadPool.start",
                        side_effect=lambda worker: started.append(worker)):
            confirm.return_value = FlowDecision(items=[], mode=None)
            page._last_skipped = []
            page.execute_checked()
        self.assertEqual(len(started), 0)

if __name__ == "__main__":
    unittest.main(verbosity=2)
