"""搜索过滤与预演汇总。"""
from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


@dataclass
class FakeItem:
    name: str
    risk: str = "caution"
    detail: str = ""
    size_bytes: int | None = 1024
    effect: str = ""


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class FileModelFilterTests(Base):
    def build(self):
        from core.fs_browser import FsEntry
        from ui.table_model import FsTableModel

        model = FsTableModel()
        model.set_entries([
            FsEntry(human_path="C:\\a.txt", long_path="C:\\a.txt", name="a.txt", is_dir=False,
                    size=10, mtime=0.0, kind_label="文本文档", risk="safe"),
            FsEntry(human_path="C:\\b.tmp", long_path="C:\\b.tmp", name="b.tmp", is_dir=False,
                    size=20, mtime=0.0, kind_label="TMP", risk="safe"),
            FsEntry(human_path="C:\\temp", long_path="C:\\temp", name="temp", is_dir=True,
                    size=30, mtime=0.0, kind_label="文件夹", risk="caution"),
        ])
        return model

    def test_filter_by_name(self):
        model = self.build()
        model.set_filter("temp")
        self.assertEqual(model.rowCount(), 1)      # 只有 temp 文件夹含这个词
        self.assertEqual(model.total_count(), 3)

    def test_filter_by_extension(self):
        model = self.build()
        model.set_filter("tmp")
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.entry_at(0).name, "b.tmp")

    def test_clear_restores(self):
        model = self.build()
        model.set_filter("a.txt")
        self.assertEqual(model.rowCount(), 1)
        model.set_filter("")
        self.assertEqual(model.rowCount(), 3)
        self.assertFalse(model.is_filtered())

    def test_select_all_only_visible(self):
        model = self.build()
        model.set_filter("tmp")
        model.set_all_checked(True)
        self.assertEqual(len(model.checked_entries()), 1, "只该勾上可见的那一项")

    def test_hidden_checked_kept(self):
        model = self.build()
        model.set_all_checked(True)
        model.set_filter("tmp")
        self.assertEqual(len(model.checked_entries()), 3, "过滤不该让已勾的消失")
        self.assertEqual(model.hidden_checked_count(), 2)


class SoftwareModelFilterTests(Base):
    def build(self):
        from core.uninstall import SoftwareItem
        from ui.uninstall_view import SoftwareModel, SoftwareRow

        def row(name, publisher):
            item = SoftwareItem(name=name, version="1", publisher=publisher, install_date="",
                                size_bytes=100, install_location="", uninstall_string="x",
                                quiet_uninstall_string="", key_path="HKLM\\" + name, hive="HKLM")
            return SoftwareRow(item=item, effect="", risk="caution")

        model = SoftwareModel()
        model.set_rows([row("7-Zip", "Igor"), row("Adobe Photoshop", "Adobe"), row("WeChat", "Tencent")])
        return model

    def test_filter_by_publisher(self):
        model = self.build()
        model.set_filter("adobe")
        self.assertEqual(model.rowCount(), 1)

    def test_filter_by_name(self):
        model = self.build()
        model.set_filter("wechat")
        self.assertEqual(model.rowCount(), 1)


class ProcessModelFilterTests(Base):
    def build(self):
        from core.processes import ProcessItem
        from ui.process_view import ProcessModel, ProcessRow

        def row(pid, name):
            return ProcessRow(item=ProcessItem(pid=pid, name=name, username="u", rss=1, cpu=0.0,
                                               exe="C:\\" + name, publisher=""), effect="", risk="caution")

        model = ProcessModel()
        model.set_rows([row(100, "chrome.exe"), row(200, "lsass.exe"), row(300, "notepad.exe")])
        return model

    def test_filter_by_name(self):
        model = self.build()
        model.set_filter("lsass")
        self.assertEqual(model.rowCount(), 1)

    def test_filter_by_pid(self):
        model = self.build()
        model.set_filter("300")
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.rows()[0].item.pid, 300)


class PreflightTests(Base):
    def make(self, items, action="删除", **kwargs):
        from ui.confirm_flow import ConfirmDialog

        return ConfirmDialog(items, action, "提示", **kwargs)

    def test_counts_and_unit(self):
        dialog = self.make([FakeItem("a"), FakeItem("b")], action="结束")
        text = dialog.summary.text()
        self.assertIn("将结束 2 个进程", text)

    def test_no_risk_warning_in_summary(self):
        # 红色不再单独提示 —— 三道坎去掉了，汇总只报数量和大小
        dialog = self.make([FakeItem("a", "forbidden"), FakeItem("b")])
        self.assertNotIn("高风险", dialog.summary.text())
        self.assertIn("将删除 2 项", dialog.summary.text())

    def test_live_update(self):
        dialog = self.make([FakeItem("a"), FakeItem("b")])
        self.assertIn("将删除 2 项", dialog.summary.text())
        dialog.table.item(0, 0).setCheckState(Qt.Unchecked)
        self.assertIn("将删除 1 项", dialog.summary.text())

    def test_recycle_mode_says_restorable(self):
        dialog = self.make([FakeItem("a")], modes=(("移入回收站", "recycle"), ("彻底删除", "permanent")))
        self.assertIn("可从回收站还原", dialog.summary.text())

    def test_permanent_mode_says_gone(self):
        dialog = self.make([FakeItem("a")], modes=(("移入回收站", "recycle"), ("彻底删除", "permanent")))
        dialog._mode_group.buttons()[1].setChecked(True)
        self.assertIn("找不回来", dialog.summary.text())

    def test_backup_note_shown(self):
        dialog = self.make([FakeItem("k1")], action="清理",
                           backup_note="动手前会先备份到 backups/")
        self.assertIn("backups/", dialog.summary.text())

    def test_empty_selection(self):
        dialog = self.make([FakeItem("a")])
        dialog.table.item(0, 0).setCheckState(Qt.Unchecked)
        self.assertIn("什么都没选", dialog.summary.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
