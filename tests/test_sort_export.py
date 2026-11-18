"""三态排序与 CSV 导出。"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ui.widgets import export_csv

def entry(name, size, is_dir=False, risk="safe", mtime=0.0):
    from core.fs_browser import FsEntry

    return FsEntry(human_path="C:\\" + name, long_path="C:\\" + name, name=name, is_dir=is_dir,
                   size=size, mtime=mtime, kind_label="文件夹" if is_dir else "文件",
                   purpose="说明", risk=risk)

class SortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def build(self):
        from ui.table_model import FsTableModel

        model = FsTableModel()
        # 用三个文件测三态，顺序清楚；文件夹前置规则另有测试
        model.set_entries([entry("b.txt", 20), entry("a.txt", 30), entry("c.txt", 10)])
        return model

    def names(self, model):
        return [model.entry_at(i).name for i in range(model.rowCount())]

    def test_three_states(self):
        from ui.table_model import COL_NAME

        model = self.build()
        original = self.names(model)

        col, order = model.cycle_sort(COL_NAME)
        self.assertEqual(col, COL_NAME)
        self.assertEqual(order, Qt.AscendingOrder)
        self.assertEqual(self.names(model), ["a.txt", "b.txt", "c.txt"])

        col, order = model.cycle_sort(COL_NAME)
        self.assertEqual(order, Qt.DescendingOrder)
        self.assertEqual(self.names(model), ["c.txt", "b.txt", "a.txt"])

        col, order = model.cycle_sort(COL_NAME)
        self.assertEqual(col, -1, "第三下应该回到默认顺序")
        self.assertEqual(self.names(model), original)

    def test_sort_by_size(self):
        from ui.table_model import COL_SIZE

        model = self.build()
        model.cycle_sort(COL_SIZE)
        sizes = [model.entry_at(i).size for i in range(model.rowCount())]
        self.assertEqual(sizes, [10, 20, 30])

    def test_folders_come_first(self):
        from ui.table_model import COL_NAME
        from ui.table_model import FsTableModel

        model = FsTableModel()
        model.set_entries([entry("a.txt", 1), entry("zzz", None, is_dir=True)])
        model.cycle_sort(COL_NAME)
        self.assertTrue(model.entry_at(0).is_dir, "文件夹排在文件前面")

    def test_sort_keeps_checks_and_filter(self):
        from ui.table_model import COL_NAME

        model = self.build()
        model.set_all_checked(True)
        checked_before = {e.name for e in model.checked_entries()}

        model.set_filter("a.txt")
        self.assertEqual(model.rowCount(), 1)
        model.cycle_sort(COL_NAME)
        self.assertEqual(model.rowCount(), 1, "排序不该把过滤结果弄丢")
        self.assertEqual({e.name for e in model.checked_entries()}, checked_before,
                         "排序不该动勾选")

    def test_sort_then_clear_filter(self):
        from ui.table_model import COL_NAME

        model = self.build()
        model.set_filter("a.txt")
        model.cycle_sort(COL_NAME)
        model.set_filter("")
        self.assertEqual(model.rowCount(), 3)
        self.assertEqual(model._sort_column, COL_NAME, "清空过滤后排序状态还在")

class ExportTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="exp-"))

    def test_writes_bom(self):
        path = self.dir / "a.csv"
        ok, _p = export_csv.write_csv(path, ("名称", "路径"), [("甲", "C:\\甲")])
        self.assertTrue(ok)
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "必须带 UTF-8 BOM，否则 Excel 乱码")

    def test_reads_back_correctly(self):
        path = self.dir / "b.csv"
        export_csv.write_csv(path, ("名称", "风险"), [("中文名字", "别删")])
        with open(path, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["名称", "风险"])
        self.assertEqual(rows[1], ["中文名字", "别删"])

    def test_no_sensitive_fields_in_headers(self):
        from ui.uninstall_view import UninstallView
        from ui.registry_view import RegistryView
        from ui.process_view import ProcessView

        forbidden = ("内容", "键值", "命令行", "command", "content", "内存转储")
        sources = [
            ("名称", "路径", "类型", "大小", "作用", "风险"),
            UninstallView.export_headers(None),
            RegistryView.export_headers(None),
            ProcessView.export_headers(None),
        ]
        for headers in sources:
            for name in headers:
                for bad in forbidden:
                    self.assertNotIn(bad, name, f"表头里不该出现「{bad}」")

class PruneTests(unittest.TestCase):
    def test_prune_keeps_count(self):
        old = export_csv.export_dir
        directory = Path(tempfile.mkdtemp(prefix="exp-prune-"))
        export_csv.export_dir = lambda: directory        # 临时指走，别动真实目录
        try:
            for index in range(export_csv.MAX_COUNT + 5):
                (directory / f"f{index:03d}.csv").write_text("x", encoding="utf-8")
                os.utime(directory / f"f{index:03d}.csv", (1000 + index, 1000 + index))
            export_csv.prune()
            self.assertEqual(len(list(directory.glob("*.csv"))), export_csv.MAX_COUNT)
        finally:
            export_csv.export_dir = old

if __name__ == "__main__":
    unittest.main(verbosity=2)
