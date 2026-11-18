"""四个模块的真实 report 各构造一次结果对话框。

这条测试是为了挡住这类问题：报告对话框按文件的字段名写死（human_path），
换成软件/注册表/进程的 report 就 AttributeError，
而以前测试全用 mock 的 Result（恰好有 human_path）。
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog_for(self, report, **kwargs):
        from ui.dialogs import DeleteReportDialog

        return DeleteReportDialog(report, None, None, **kwargs)


class RealReportTests(Base):
    def test_file_report(self):
        from core.delete import DeleteReport, ItemResult, Mode

        now = time.time()
        report = DeleteReport(
            items=[ItemResult(human_path="C:\\a.txt", ok=True, mode=Mode.RECYCLE, error=None),
                   ItemResult(human_path="C:\\b.txt", ok=False, mode=Mode.RECYCLE, error="拒绝访问")],
            started=now - 1, finished=now, mode=Mode.RECYCLE,
        )
        dialog = self.dialog_for(report, op_type="delete")
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertIn("b.txt", dialog._build_body())

    def test_uninstall_report(self):
        from core.uninstall import ERR_NO_COMMAND, UninstallReport, UninstallResult

        now = time.time()
        report = UninstallReport(
            items=[UninstallResult("HKLM\\a", "某个软件", True, "uninst.exe", None, now),
                   UninstallResult("HKLM\\b", "坏掉的软件", False, "", ERR_NO_COMMAND, now)],
            started=now - 1, finished=now,
        )
        dialog = self.dialog_for(report, op_type="uninstall")
        self.assertEqual(dialog.table.rowCount(), 2)
        body = dialog._build_body()
        self.assertIn("某个软件", body)
        self.assertIn("坏掉的软件", body)

    def test_registry_report(self):
        from core.registry import RegistryReport, RegistryResult

        now = time.time()
        report = RegistryReport(
            items=[RegistryResult("HKLM\\X", "X", True, None, now),
                   RegistryResult("HKLM\\Y", "Y", False, "没有权限动这个键", now)],
            started=now - 1, finished=now,
            backup_path=Path("backups/reg_1.reg"),
        )
        dialog = self.dialog_for(report, op_type="clean")
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertIn("HKLM\\Y", dialog._build_body())

    def test_process_report(self):
        from core.processes import ProcessReport, ProcessResult

        now = time.time()
        report = ProcessReport(
            items=[ProcessResult(100, "chrome.exe", True, None, now),
                   ProcessResult(200, "lsass.exe", False, "没有权限结束它", now)],
            started=now - 1, finished=now,
        )
        dialog = self.dialog_for(report, op_type="kill")
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertIn("lsass.exe", dialog._build_body())

    def test_permission_failure_gets_hint(self):
        """权限类失败要告诉用户去提权，不能只说一句「没成」。"""
        import time as t

        from PySide6.QtWidgets import QLabel

        from core.delete import ERR_NEED_ADMIN, DeleteReport, ItemResult, Mode

        now = t.time()
        report = DeleteReport(
            items=[ItemResult(human_path=r"C:\Program Files\Npcap", ok=False,
                              mode=Mode.RECYCLE, error=ERR_NEED_ADMIN)],
            started=now - 1, finished=now, mode=Mode.RECYCLE,
        )
        dialog = self.dialog_for(report, op_type="delete")
        texts = [label.text() for label in dialog.findChildren(QLabel)]
        joined = " ".join(texts)
        self.assertIn("管理员", joined, joined)
        self.assertIn("以管理员身份重新启动", joined)
        self.assertEqual(dialog._rows[0].category, "denied")

    def test_all_four_have_copyable_text(self):
        """复制/另存走的是 as_log_text，四个都得有。"""
        from core.delete import DeleteReport, Mode
        from core.processes import ProcessReport
        from core.registry import RegistryReport
        from core.uninstall import UninstallReport

        now = time.time()
        for report in (DeleteReport(items=[], started=now, finished=now, mode=Mode.RECYCLE),
                       UninstallReport(items=[], started=now, finished=now),
                       RegistryReport(items=[], started=now, finished=now),
                       ProcessReport(items=[], started=now, finished=now)):
            dialog = self.dialog_for(report)
            text = dialog._report_text()
            self.assertTrue(isinstance(text, str) and text)


class FieldsTests(unittest.TestCase):
    def test_every_report_item_reaches_rows(self):
        """四个 report 的 item 都要能被摊成统一行，字段不丢。"""
        import time as t

        from core import result_report
        from core.delete import ItemResult, Mode
        from core.processes import ProcessResult
        from core.registry import RegistryResult
        from core.uninstall import UninstallResult

        now = t.time()
        cases = [
            (ItemResult(human_path="C:\\a.txt", ok=True, mode=Mode.RECYCLE), "a.txt"),
            (UninstallResult("HKLM\\k", "某软件", True, "x", None, now), "某软件"),
            (RegistryResult("HKLM\\X", "X", True, None, now), "X"),
            (ProcessResult(42, "p.exe", True, None, now), "p.exe"),
        ]
        for item, expected in cases:
            rows = result_report.rows_from_report(type("R", (), {"items": [item]})(), "")
            self.assertEqual(len(rows), 1)
            self.assertIn(expected, rows[0].name + rows[0].path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
