"""删除相关界面与留档的测试：清单逐项取消、二次确认、报告导出、只留最近 N 份。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

import app_paths
from core.delete import (
    LOG_KEEP_COUNT,
    LOG_PREFIX,
    LOG_SUFFIX,
    DeleteItem,
    DeleteReport,
    ItemResult,
    Mode,
    purge_old_logs,
)

def item(name: str, risk: str = "safe", size: int | None = 1024, effect: str = "临时文件，可以删。") -> DeleteItem:
    return DeleteItem(human_path=f"C:\\测试\\{name}", display=name, is_dir=False,
                      risk=risk, size=size, effect=effect)

class ReportDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def build_report(self) -> DeleteReport:
        now = time.time()
        items = [
            ItemResult(human_path="C:\\测试\\ok.txt", ok=True, mode=Mode.RECYCLE, error=None,
                       at=now, display="ok.txt", size=2048, risk="safe"),
            ItemResult(human_path="C:\\测试\\busy.txt", ok=False, mode=Mode.RECYCLE,
                       error="正被别的程序用着，删不掉", at=now, display="busy.txt", size=1024, risk="safe"),
        ]
        report = DeleteReport(mode=Mode.RECYCLE, started=now - 1, finished=now, items=items)
        report.freed = 2048
        return report

    def test_report_shows_counts_and_reasons(self):
        from ui.dialogs import DeleteReportDialog

        dialog = DeleteReportDialog(self.build_report(), Path("C:\\logs\\删除记录-20260101-000000.txt"))
        body = dialog._browser.toPlainText()
        self.assertIn("成功的 1 项", body)
        self.assertIn("没删成的 1 项", body)
        self.assertIn("正被别的程序用着", body)
        self.assertIn("busy.txt", body)

    def test_report_text_can_be_copied(self):
        from ui.dialogs import DeleteReportDialog

        dialog = DeleteReportDialog(self.build_report())
        text = dialog._report_text()
        self.assertIn("删除记录", text)
        self.assertIn("ok.txt", text)

class WordingTests(unittest.TestCase):
    """回收站模式不能说「省出/释放」，文件还在盘上。"""

    def build(self, mode: Mode) -> DeleteReport:
        now = time.time()
        items = [ItemResult(human_path="C:\\t\\a.txt", ok=True, mode=mode, error=None,
                            at=now, display="a.txt", size=4096, risk="safe")]
        report = DeleteReport(mode=mode, started=now - 1, finished=now, items=items)
        report.freed = 4096
        return report

    def test_recycle_does_not_claim_freed_space(self):
        report = self.build(Mode.RECYCLE)
        summary = report.summary
        self.assertIn("移入回收站", summary)
        self.assertNotIn("省出", summary, summary)
        self.assertIn("清空回收站后", summary, summary)
        log = report.as_log_text()
        self.assertIn("移入回收站：约", log)
        self.assertIn("清空回收站后才真正释放", log)
        self.assertNotIn("释放空间：", log, "回收站模式不该写「释放空间」")

    def test_permanent_says_released(self):
        report = self.build(Mode.PERMANENT)
        self.assertIn("释放", report.summary)
        self.assertNotIn("回收站", report.summary, report.summary)
        self.assertIn("释放空间：", report.as_log_text())

class LogRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dch-log-"))

    def tearDown(self):
        shutil.rmtree(app_paths.to_long(str(self.tmp)), ignore_errors=True)

    def make_logs(self, count: int) -> list[Path]:
        made = []
        for index in range(count):
            path = self.tmp / f"{LOG_PREFIX}2026010{index + 1}-000000{LOG_SUFFIX}"
            path.write_text(f"记录 {index}", encoding="utf-8")
            stamp = time.time() - (count - index) * 60
            os.utime(str(path), (stamp, stamp))
            made.append(path)
        return made

    def test_keeps_only_latest_n(self):
        made = self.make_logs(5)
        removed = purge_old_logs(self.tmp, 3)
        self.assertEqual(removed, 2)
        left = sorted(path.name for path in self.tmp.glob(f"{LOG_PREFIX}*"))
        self.assertEqual(len(left), 3)
        self.assertNotIn(made[0].name, left, "最旧的两份应该被删掉")
        self.assertNotIn(made[1].name, left)
        self.assertIn(made[-1].name, left, "最新的一份必须留着")

    def test_old_log_suffix_also_cleaned(self):
        legacy = self.tmp / f"{LOG_PREFIX}20250101-000000.log"
        legacy.write_text("老格式", encoding="utf-8")
        oldest = time.time() - 9999
        os.utime(str(legacy), (oldest, oldest))
        for index in range(LOG_KEEP_COUNT):
            path = self.tmp / f"{LOG_PREFIX}202602{index:02d}-000000{LOG_SUFFIX}"
            path.write_text("新格式", encoding="utf-8")
            stamp = time.time() - (LOG_KEEP_COUNT - index) * 60
            os.utime(str(path), (stamp, stamp))
        removed = purge_old_logs(self.tmp, LOG_KEEP_COUNT)
        self.assertEqual(removed, 1, "多出来的那一份应该正好是老的 .log")
        self.assertFalse(legacy.exists(), "升级前留下的 .log 也该被清")

    def test_unrelated_files_untouched(self):
        keeper = self.tmp / "别动我.txt"
        keeper.write_text("x", encoding="utf-8")
        self.make_logs(4)
        purge_old_logs(self.tmp, 2)
        self.assertTrue(keeper.exists())

class RemoveRowsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_successful_rows_removed_from_model(self):
        from core.fs_browser import FsEntry
        from ui.table_model import FsTableModel

        model = FsTableModel()
        entries = [
            FsEntry(human_path=r"C:\a", long_path=r"\\?\C:\a", name="a", is_dir=True,
                    size=1, mtime=0.0, kind_label="文件夹", risk="safe"),
            FsEntry(human_path=r"C:\b", long_path=r"\\?\C:\b", name="b", is_dir=False,
                    size=2, mtime=0.0, kind_label="文本文档", risk="caution"),
        ]
        model.set_entries(entries)
        model.set_all_checked(True)
        self.assertEqual(len(model.checked_entries()), 2)
        removed = model.remove_paths([r"C:\a"])
        self.assertEqual(removed, 1)
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.entry_at(0).name, "b")

class LockDiagnosisTests(unittest.TestCase):
    """删除失败要说出真实原因 —— 尤其是「哪个程序占着」。"""

    def test_finds_running_process(self):
        """靠 exe 路径匹配占用者。用 mock 造进程表，不依赖真实环境。"""
        from unittest import mock

        from core.delete import _likely_locks

        from types import SimpleNamespace

        fake = [SimpleNamespace(info={"name": "chrome.exe",
                                      "exe": "C:\\Apps\\Browser\\chrome.exe"})]
        with mock.patch("psutil.process_iter", return_value=iter(fake)):
            names = _likely_locks(r"C:\Apps\Browser")
        self.assertEqual(names, ["chrome.exe"])

    def test_empty_path_returns_nothing(self):
        from core.delete import _likely_locks

        self.assertEqual(_likely_locks(""), [])

    def test_missing_dir_returns_nothing(self):
        from core.delete import _likely_locks

        self.assertEqual(_likely_locks(r"C:\完全不存在的路径zzz_123"), [])

    def test_lock_reason_names_the_process(self):
        from unittest import mock

        from core.delete import _lock_reason

        from types import SimpleNamespace

        fake = [SimpleNamespace(info={"name": "chrome.exe",
                                  "exe": "C:\\Apps\\Browser\\chrome.exe"})]
        with mock.patch("psutil.process_iter", return_value=iter(fake)):
            text = _lock_reason(r"C:\Apps\Browser")
        self.assertIn("正在运行", text)
        self.assertIn("chrome.exe", text)

    def test_lock_reason_falls_back(self):
        import tempfile

        from core.delete import ERR_LOCKED, _lock_reason

        self.assertEqual(_lock_reason(tempfile.mkdtemp()), ERR_LOCKED)

    def test_non_admin_says_admin_needed(self):
        """非管理员时不该说「被占用」，那是两回事。"""
        from unittest import mock

        from core.delete import _is_admin, _reason_for_access_denied

        if _is_admin():
            self.skipTest("当前就是管理员，测不到这个分支")
        with mock.patch.object(__import__("core.delete", fromlist=["x"]), "_is_admin",
                               return_value=False):
            text = _reason_for_access_denied("C:\\x")
        self.assertIn("管理员", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
