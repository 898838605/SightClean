"""反馈：可记录、可导出，但不联网、不改知识库。"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import feedback

ROOT = Path(__file__).resolve().parent.parent


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fb-"))
        self.file = self.dir / "feedback.json"
        self._original = feedback.feedback_path
        feedback.feedback_path = lambda: self.file

    def tearDown(self):
        feedback.feedback_path = self._original

    def test_add_writes_all_fields(self):
        ok, _msg = feedback.add("C:\\a\\b.txt", "b.txt", "临时文件", "其实是缓存", "files")
        self.assertTrue(ok)
        item = feedback.load_all()[0]
        self.assertEqual(item.path, "C:\\a\\b.txt")
        self.assertEqual(item.name, "b.txt")
        self.assertEqual(item.purpose, "临时文件")
        self.assertEqual(item.note, "其实是缓存")
        self.assertEqual(item.module, "files")
        self.assertTrue(item.time)

    def test_note_is_optional(self):
        ok, _msg = feedback.add("C:\\x", "x", "说明", "", "files")
        self.assertTrue(ok)
        self.assertEqual(feedback.load_all()[0].note, "")

    def test_count_and_clear(self):
        for index in range(3):
            feedback.add("C:\\" + str(index), str(index), "说明", "", "files")
        self.assertEqual(feedback.count(), 3)
        feedback.clear()
        self.assertEqual(feedback.count(), 0)

    def test_cap_at_max_items(self):
        for index in range(feedback.MAX_ITEMS + 10):
            feedback.add("C:\\" + str(index), str(index), "说明", "", "files")
        self.assertEqual(feedback.count(), feedback.MAX_ITEMS)

    def test_export_has_bom_and_chinese(self):
        feedback.add("C:\\测试\\文件.txt", "文件.txt", "临时文件", "不对，是缓存", "files")
        target = self.dir / "out.csv"
        ok, _msg = feedback.export(target)
        self.assertTrue(ok)
        self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))
        with open(target, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0][0], "时间")
        self.assertIn("文件.txt", rows[1])
        self.assertIn("不对，是缓存", rows[1])

    def test_never_touches_knowledge_base(self):
        """写反馈绝不动知识库 —— 这是硬要求。"""
        kb_files = [ROOT / "resources" / "default_knowledge.json", ROOT / "知识库.json"]
        before = {path: path.read_bytes() for path in kb_files if path.exists()}
        feedback.add("C:\\a", "a", "说明", "这条不对", "files")
        feedback.load_all()
        feedback.export(self.dir / "x.csv")
        for path, blob in before.items():
            self.assertEqual(path.read_bytes(), blob, f"{path.name} 不该被改动")

    def test_no_network_code(self):
        """源码里不许出现任何联网调用。"""
        source = (ROOT / "core" / "feedback.py").read_text(encoding="utf-8")
        for bad in ("urllib", "requests", "http", "socket", "urlopen", "post(", "webhook"):
            self.assertNotIn(bad, source.lower(), f"feedback.py 里不该出现 {bad}")

    def test_broken_file_does_not_crash(self):
        self.file.write_text("这不是 JSON", encoding="utf-8")
        self.assertEqual(feedback.count(), 0)
        ok, _msg = feedback.add("C:\\a", "a", "说明", "", "files")
        self.assertTrue(ok, "坏文件应该被当成空的，而不是崩掉")
        self.assertEqual(feedback.count(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
