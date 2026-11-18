"""快照与逐项结果：只存元数据、双上限、能力映射、结果分类。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import result_report, snapshot

class Item:
    def __init__(self, name, path, risk="caution"):
        self.name = name
        self.human_path = path
        self.risk = risk

class Result:
    def __init__(self, path, ok=True, error=None):
        self.human_path = path
        self.ok = ok
        self.error = error

class SnapshotBasicsTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="snap-"))

    def test_begin_creates_file(self):
        sid = snapshot.begin("delete", [Item("a.txt", "C:\\a.txt")], mode="recycle", log_dir=self.dir)
        self.assertTrue(sid)
        self.assertTrue((snapshot.snapshot_dir(self.dir) / f"{sid}.json").exists())

    def test_metadata_only(self):
        sid = snapshot.begin("delete", [Item("a.txt", "C:\\a.txt")], mode="recycle", log_dir=self.dir)
        data = json.loads((snapshot.snapshot_dir(self.dir) / f"{sid}.json").read_text(encoding="utf-8"))
        allowed = {"schema", "id", "op_type", "time", "items", "mode", "backup", "note"}
        self.assertTrue(set(data).issubset(allowed), set(data) - allowed)
        row = data["items"][0]
        self.assertTrue(set(row).issubset({"name", "path", "risk", "result", "error"}), row)
        blob = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("content", blob)
        self.assertNotIn("command", blob)

    def test_finish_fills_results(self):
        sid = snapshot.begin("delete", [Item("a.txt", "C:\\a.txt")], mode="recycle", log_dir=self.dir)
        snapshot.finish(sid, [Result("C:\\a.txt", True)], self.dir)
        snap = snapshot.load_all(self.dir)[0]
        self.assertEqual(snap.items[0].result, snapshot.RESULT_OK)
        self.assertEqual(snap.ok_count, 1)

    def test_restore_capability(self):
        sid = snapshot.begin("delete", [Item("a.txt", "C:\\a.txt")], mode="recycle", log_dir=self.dir)
        snapshot.finish(sid, [Result("C:\\a.txt", True)], self.dir)
        self.assertTrue(snapshot.load_all(self.dir)[0].can_restore)

    def test_permanent_delete_not_restorable(self):
        sid = snapshot.begin("delete", [Item("a.txt", "C:\\a.txt")], mode="permanent", log_dir=self.dir)
        snapshot.finish(sid, [Result("C:\\a.txt", True)], self.dir)
        snap = snapshot.load_all(self.dir)[0]
        self.assertFalse(snap.can_restore)
        self.assertEqual(snap.restore_hint, "")

    def test_registry_restorable_only_with_backup(self):
        sid = snapshot.begin("clean", [Item("HKLM\\X", "HKLM\\X")], log_dir=self.dir)
        snapshot.finish(sid, [Result("HKLM\\X", True)], self.dir, backup="reg_1.reg")
        self.assertTrue(snapshot.load_all(self.dir)[0].can_restore)

    def test_kill_and_uninstall_never_restorable(self):
        for op in ("kill", "uninstall"):
            sid = snapshot.begin(op, [Item("x", "1")], log_dir=self.dir)
            snapshot.finish(sid, [Result("1", True)], self.dir)
        for snap in snapshot.load_all(self.dir):
            self.assertFalse(snap.can_restore)

class LimitTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="snap-limit-"))
        self.snap_dir = snapshot.snapshot_dir(self.dir)
        self.snap_dir.mkdir(parents=True)

    def make_many(self, count):
        for index in range(count):
            path = self.snap_dir / f"20260101-0000{index:02d}-delete.json"
            path.write_text(json.dumps({"id": path.stem, "op_type": "delete", "time": "", "items": []}),
                            encoding="utf-8")
            os.utime(path, (1000 + index, 1000 + index))

    def test_count_limit(self):
        self.make_many(snapshot.MAX_COUNT + 15)
        removed, _freed = snapshot.prune(self.snap_dir)
        self.assertEqual(removed, 15)
        self.assertEqual(len(list(self.snap_dir.glob("*.json"))), snapshot.MAX_COUNT)

    def test_oldest_removed_first(self):
        self.make_many(snapshot.MAX_COUNT + 3)
        snapshot.prune(self.snap_dir)
        names = {path.stem for path in self.snap_dir.glob("*.json")}
        self.assertNotIn("20260101-000000-delete", names, "最旧的应该先被清掉")

    def test_size_limit(self):
        big = "x" * (1024 * 1024)
        for index in range(60):      # 60 MB，超过 50 MB 的上限
            path = self.snap_dir / f"20260101-0000{index:02d}-delete.json"
            path.write_text(json.dumps({"id": path.stem, "items": [big]}), encoding="utf-8")
            os.utime(path, (1000 + index, 1000 + index))
        total, _count = snapshot.usage(self.dir)
        self.assertGreater(total, snapshot.MAX_BYTES)
        snapshot.prune(self.snap_dir)
        total_after, _count_after = snapshot.usage(self.dir)
        self.assertLessEqual(total_after, snapshot.MAX_BYTES)

    def test_clear_leaves_backups_alone(self):
        backup_dir = self.dir / "backups"
        backup_dir.mkdir()
        (backup_dir / "reg_keep.reg").write_text("keep", encoding="utf-8")
        snapshot.begin("clean", [Item("k", "HKLM\\K")], log_dir=self.dir)
        removed, _freed = snapshot.clear(self.dir)
        self.assertEqual(removed, 1)
        self.assertTrue((backup_dir / "reg_keep.reg").exists(), "清快照不能动 backups")

class ResultReportTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(result_report.classify("文件被占用中"), "busy")
        self.assertEqual(result_report.classify("拒绝访问"), "denied")
        self.assertEqual(result_report.classify("这个键已经不在了"), "gone")
        self.assertEqual(result_report.classify("没有卸载命令"), "unsupported")
        self.assertEqual(result_report.classify("别的错误"), "other")
        self.assertEqual(result_report.classify(None), "other")

    def test_rows_from_report(self):
        report = type("R", (), {"items": [Result("C:\\a", True), Result("C:\\b", False, "拒绝访问")]})()
        rows = result_report.rows_from_report(report, "delete")
        self.assertEqual([row.status for row in rows], ["ok", "fail"])
        self.assertEqual(rows[1].category, "denied")
        self.assertIn("没有权限", rows[1].reason_text)

    def test_retryable_only_when_still_there(self):
        existing = Path(tempfile.mkdtemp()) / "here.txt"
        existing.write_text("x", encoding="utf-8")
        report = type("R", (), {"items": [
            Result(str(existing), False, "拒绝访问"),
            Result("C:\\definitely-gone-zzz\\x.txt", False, "拒绝访问"),
        ]})()
        rows = result_report.rows_from_report(report, "delete")
        self.assertTrue(rows[0].retryable)
        self.assertFalse(rows[1].retryable)

    def test_skipped_rows_are_separate(self):
        skipped = result_report.skipped_rows([Item("危险.txt", "C:\\危险.txt", "forbidden")])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].status, result_report.STATUS_SKIPPED)
        self.assertEqual(skipped[0].status_label, "已暂缓")

    def test_summarize(self):
        report = type("R", (), {"items": [Result("a", True), Result("b", False, "x")]})()
        rows = result_report.rows_from_report(report, "delete")
        info = result_report.summarize(rows + result_report.skipped_rows([Item("c", "c")]))
        self.assertEqual(info["ok"], 1)
        self.assertEqual(info["fail"], 1)
        self.assertEqual(info["skipped"], 1)

class RetryButtonTests(unittest.TestCase):
    """报告对话框里的「重试」按钮：只对还在的项亮，点了才回调。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def build(self, on_retry=None):
        from ui.dialogs import DeleteReportDialog

        report = type("R", (), {
            "items": [Result("C:\\definitely-gone-zzz\\a.txt", False, "拒绝访问")],
            "ok_count": 0, "fail_count": 1,
            "finished": 0.0, "started": 0.0,
            "as_log_text": lambda *_a, **_k: "记录",
            "failure_reasons": lambda *_a, **_k: [],
        })()
        return DeleteReportDialog(report, None, None, on_retry=on_retry, op_type="delete")

    def test_disabled_when_nothing_retryable(self):
        dialog = self.build(on_retry=lambda rows: None)
        self.assertFalse(dialog.btn_retry.isEnabled(), "项都没了，不该给能点的按钮")

    def test_disabled_without_callback(self):
        dialog = self.build(on_retry=None)
        self.assertFalse(dialog.btn_retry.isEnabled())

    def test_enabled_and_fires(self):
        import tempfile
        from pathlib import Path

        alive = Path(tempfile.mkdtemp()) / "alive.txt"
        alive.write_text("x", encoding="utf-8")
        from ui.dialogs import DeleteReportDialog

        report = type("R", (), {
            "items": [Result(str(alive), False, "拒绝访问")],
            "ok_count": 0, "fail_count": 1,
            "finished": 0.0, "started": 0.0,
            "as_log_text": lambda *_a, **_k: "记录",
            "failure_reasons": lambda *_a, **_k: [],
        })()
        calls = []
        dialog = DeleteReportDialog(report, None, None, on_retry=lambda rows: calls.append(rows),
                                    op_type="delete")
        self.assertTrue(dialog.btn_retry.isEnabled(), "项还在，应该能重试")
        dialog._do_retry()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0].path, str(alive))

    def test_skipped_rows_listed_separately(self):
        from ui.dialogs import DeleteReportDialog

        report = type("R", (), {
            "items": [Result("C:\\a", True)], "ok_count": 1, "fail_count": 0,
            "finished": 0.0, "started": 0.0,
            "as_log_text": lambda *_a, **_k: "记录", "failure_reasons": lambda *_a, **_k: [],
        })()
        dialog = DeleteReportDialog(report, None, None,
                                    skipped=[Item("危险.txt", "C:\\危险.txt", "forbidden")],
                                    op_type="delete")
        self.assertEqual(len(dialog._skipped), 1)
        self.assertEqual(dialog._skipped[0].status_label, "已暂缓")

if __name__ == "__main__":
    unittest.main(verbosity=2)
