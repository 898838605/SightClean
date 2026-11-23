"""一键清理 C 盘垃圾的测试。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core import junk


class RulesTests(unittest.TestCase):
    def test_loads_all_categories(self):
        rules = junk.load_rules()
        ids = {c.id for c in rules}
        for expected in ("temp", "logs", "thumbnails", "browser", "packages"):
            self.assertIn(expected, ids, "少了 %s 这一类" % expected)

    def test_every_category_has_purpose(self):
        for category in junk.load_rules():
            self.assertTrue(category.purpose, "%s 没写作用是干嘛的" % category.id)
            self.assertTrue(category.paths, "%s 没有路径" % category.id)

    def test_risk_values_valid(self):
        for category in junk.load_rules():
            self.assertIn(category.risk, ("safe", "caution", "risky"), category.id)

    def test_mode_values_valid(self):
        for category in junk.load_rules():
            self.assertIn(category.mode, ("contents", "pattern", "whole"), category.id)

    def test_broken_file_falls_back(self):
        """规则文件坏了也得有一条能用的，不能让按钮点了没反应。"""
        original = junk.rules_path
        bad = Path(tempfile.mkdtemp()) / "bad.json"
        bad.write_text("{ 这不是 json", encoding="utf-8")
        try:
            junk._RULES_CACHE = None
            junk.rules_path = lambda: bad
            rules = junk.load_rules()
            self.assertTrue(rules, "坏了就一条规则都没有了")
            self.assertTrue(rules[0].paths)
        finally:
            junk.rules_path = original
            junk._RULES_CACHE = None

    def test_custom_file_wins(self):
        """exe 旁边有用户改过的规则，就必须用它，不用内置的。"""
        import app_paths

        folder = Path(tempfile.mkdtemp())
        custom = folder / "垃圾清理规则.json"
        custom.write_text(json.dumps({"categories": [{
            "id": "mine", "label": "自定义", "purpose": "测试",
            "detail": "", "risk": "safe", "mode": "contents",
            "paths": ["%TEMP%"],
        }]}, ensure_ascii=False), encoding="utf-8")

        original_resolve = app_paths.resolve

        class Fake:
            junk = custom
            resources = Path("resources")

        try:
            app_paths.resolve = lambda: Fake()
            junk._RULES_CACHE = None
            self.assertEqual(junk.rules_path(), custom)
            rules = junk.load_rules()
            self.assertEqual(rules[0].id, "mine")
        finally:
            app_paths.resolve = original_resolve
            junk._RULES_CACHE = None

    def test_falls_back_to_builtin(self):
        """exe 旁边没有规则文件时，用内置的那份。"""
        import app_paths

        folder = Path(tempfile.mkdtemp())
        original_resolve = app_paths.resolve

        class Fake:
            junk = folder / "不存在.json"
            resources = Path("resources")

        try:
            app_paths.resolve = lambda: Fake()
            junk._RULES_CACHE = None
            picked = junk.rules_path()
            self.assertEqual(picked.name, "default_junk.json")
            self.assertTrue(picked.is_file())
        finally:
            app_paths.resolve = original_resolve
            junk._RULES_CACHE = None


class ExpandTests(unittest.TestCase):
    def test_env_variable(self):
        got = junk._expand("%TEMP%")
        self.assertTrue(got, "%TEMP% 展不开")
        self.assertTrue(all(os.path.isdir(p) or os.path.isfile(p) for p in got))

    def test_wildcard(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "a.txt").write_text("x", encoding="utf-8")
        (folder / "b.txt").write_text("y", encoding="utf-8")
        got = junk._expand(str(folder / "*.txt"))
        self.assertEqual(len(got), 2)

    def test_missing_path(self):
        self.assertEqual(junk._expand(r"C:\完全不存在的_zzz\*"), [])

    def test_unresolved_variable(self):
        self.assertEqual(junk._expand("%不存在的变量%\\x"), [])


class ScanTests(unittest.TestCase):
    def test_scan_returns_per_category(self):
        report = junk.scan()
        self.assertEqual(len(report.categories), len(junk.load_rules()))
        for result in report.categories:
            self.assertGreaterEqual(result.count, 0)
            self.assertGreaterEqual(result.size, 0)

    def test_totals_match(self):
        report = junk.scan()
        self.assertEqual(report.total_size, sum(c.size for c in report.categories))
        self.assertEqual(report.total_count, sum(c.count for c in report.categories))

    def test_category_filter(self):
        one = [c for c in junk.load_rules() if c.id == "thumbnails"]
        report = junk.scan(one)
        self.assertEqual(len(report.categories), 1)
        self.assertEqual(report.categories[0].category.id, "thumbnails")


class SourcePathTests(unittest.TestCase):
    """结果里要带「扫的是哪些位置」，给界面显示和双击打开用。"""

    def test_temp_reports_directories_not_children(self):
        """临时文件有几万个子项，界面要的是那两个目录，不是每个文件。"""
        rules = [c for c in junk.load_rules() if c.id == "temp"]
        report = junk.scan(rules)
        result = report.categories[0]
        self.assertLessEqual(len(result.paths), 5, "路径太多了，应该是几个目录而不是每个子项")
        for path in result.paths:
            self.assertTrue(os.path.isdir(path), path)

    def test_pattern_reports_files(self):
        """缩略图是 db 文件，本来就该按文件报。"""
        rules = [c for c in junk.load_rules() if c.id == "thumbnails"]
        report = junk.scan(rules)
        result = report.categories[0]
        if not result.paths:
            self.skipTest("这台机器没有缩略图缓存")
        for path in result.paths:
            self.assertTrue(os.path.exists(path), path)

    def test_paths_are_unique(self):
        report = junk.scan()
        for result in report.categories:
            self.assertEqual(len(result.paths), len(set(result.paths)),
                             "%s 的路径有重复" % result.label)

    def test_paths_exist(self):
        report = junk.scan()
        for result in report.categories:
            for path in result.paths:
                self.assertTrue(os.path.exists(path), "%s 报了个不存在的位置：%s"
                                % (result.label, path))


class CleanTests(unittest.TestCase):
    def test_cleans_temp_directory(self):
        folder = Path(tempfile.mkdtemp())
        for i in range(5):
            (folder / f"f{i}.tmp").write_bytes(b"x" * 100)
        category = junk.JunkCategory(
            id="t", label="测试", purpose="p", detail="", risk="safe",
            mode="contents", paths=[str(folder)],
        )
        result = junk.CategoryResult(category=category, count=5, size=500)
        outcome = junk.clean([result])
        self.assertEqual(outcome.removed, 5, outcome.errors)
        self.assertFalse(any(folder.iterdir()), "目录里的文件没删干净")
        self.assertTrue(folder.exists(), "目录本身不该被删")

    def test_pattern_deletes_file(self):
        folder = Path(tempfile.mkdtemp())
        target = folder / "cache_1.db"
        target.write_bytes(b"x" * 64)
        category = junk.JunkCategory(
            id="t", label="测试", purpose="p", detail="", risk="safe",
            mode="pattern", paths=[str(folder / "cache_*.db")],
        )
        outcome = junk.clean([junk.CategoryResult(category=category, count=1, size=64)])
        self.assertEqual(outcome.removed, 1, outcome.errors)
        self.assertFalse(target.exists())

    def test_clean_writes_log_and_snapshot(self):
        from core import snapshot

        with tempfile.TemporaryDirectory(prefix="junk-audit-") as tmp:
            root = Path(tmp) / "target"
            logs = Path(tmp) / "logs"
            root.mkdir()
            target = root / "cache.tmp"
            target.write_bytes(b"x")
            category = junk.JunkCategory(
                id="t", label="测试", purpose="p", detail="", risk="safe",
                mode="contents", paths=[str(root)],
            )
            outcome = junk.clean(
                [junk.CategoryResult(category=category, count=1, size=1)],
                log_dir=logs,
            )
            self.assertFalse(target.exists())
            self.assertIsNotNone(outcome.log_path)
            self.assertTrue(outcome.log_path.is_file())
            records = snapshot.load_all(logs)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].ok_count, 1)


if __name__ == "__main__":
    unittest.main()
