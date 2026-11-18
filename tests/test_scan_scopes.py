"""扫描位置选择的测试。"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

_QT_APP = QApplication.instance() or QApplication([])

from core import scan_scopes
from core.config import Settings


class ScopeListTests(unittest.TestCase):
    def test_all_risks_present(self):
        risks = {scope.risk for scope in scan_scopes.available_scopes()}
        self.assertTrue(risks <= {"safe", "caution", "risky"})
        self.assertIn("safe", risks, "至少要有一个安全位置")

    def test_keys_unique(self):
        keys = [scope.key for scope in scan_scopes.build_scopes()]
        self.assertEqual(len(keys), len(set(keys)), "key 不能重名")

    def test_each_has_note(self):
        for scope in scan_scopes.build_scopes():
            self.assertTrue(scope.note, "%s 没有说明" % scope.key)
            self.assertTrue(scope.label, "%s 没有名字" % scope.key)

    def test_defaults_are_safe_only(self):
        """默认勾选的必须全是安全档，不能默认勾危险的。"""
        by_key = scan_scopes.by_key()
        for key in scan_scopes.default_keys():
            self.assertEqual(by_key[key].risk, "safe", "%s 不该默认勾" % key)

    def test_risky_never_default(self):
        by_key = scan_scopes.by_key()
        for key, scope in by_key.items():
            if scope.risk == "risky":
                self.assertNotIn(key, scan_scopes.default_keys())

    def test_resolve_filters_missing(self):
        chosen = scan_scopes.resolve(["appdata", "完全不存在的key"])
        self.assertTrue(all(scope.key == "appdata" for scope in chosen))

    def test_available_means_some_dir_exists(self):
        for scope in scan_scopes.available_scopes():
            self.assertTrue(any(os.path.isdir(p) for p in scope.paths), scope.key)

    def test_program_files_keys_distinct(self):
        """同名的 Program Files（C 盘 x64/x86）不能撞 key。"""
        keys = [scope.key for scope in scan_scopes.build_scopes()
                if "programfiles" in scope.key]
        self.assertEqual(len(keys), len(set(keys)))


class PortabilityTests(unittest.TestCase):
    """不能只对某一台电脑有效 —— 盘符和用户名都必须动态取。"""

    def test_drives_not_hardcoded(self):
        """固定盘是探测出来的，不是写死 C/D/E。"""
        drives = scan_scopes.fixed_drives()
        self.assertTrue(drives, "至少要返回一个盘")
        for letter in drives:
            self.assertRegex(letter, r"^[A-Z]$")
            self.assertTrue(os.path.isdir(f"{letter}:\\"))

    def test_program_files_follow_drives(self):
        """Program Files 的路径要跟着实际存在的盘走。"""
        scope = scan_scopes.by_key()["program_files"]
        drives = set(scan_scopes.fixed_drives())
        self.assertTrue(scope.paths, "一个 Program Files 都没找到")
        for path in scope.paths:
            self.assertIn(path[0], drives, "%s 不在固定盘列表里" % path)

    def test_program_files_is_one_option(self):
        """不能把每块盘各列成一项 —— 那样换台机器列表就变了。"""
        keys = [scope.key for scope in scan_scopes.build_scopes()
                if scope.key.startswith("program_files")]
        self.assertEqual(keys, ["program_files"], "Program Files 应该只有一个选项")

    def test_no_drive_letters_in_labels(self):
        """界面上的名字不能带具体盘符。"""
        for scope in scan_scopes.build_scopes():
            self.assertNotRegex(scope.label, r"^[A-Z]:", "%s 的名字带盘符了" % scope.key)

    def test_no_hardcoded_user_path_in_source(self):
        """源码里不能出现写死的 C:\\Users\\Public —— 必须走 %PUBLIC%。"""
        from pathlib import Path

        source = Path("core/scan_scopes.py").read_text(encoding="utf-8")
        # 允许出现在注释里，但不能出现在构造参数里
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            self.assertNotIn(r'"C:\Users\Public"', stripped,
                             "写死了公共目录：%s" % stripped)
            self.assertNotIn(r"'C:\Users\Public'", stripped,
                             "写死了公共目录：%s" % stripped)

    def test_public_path_from_env(self):
        """%PUBLIC% 来自环境变量，换台机器 / 换系统盘都对。"""
        public = scan_scopes.by_key()["public"]
        self.assertEqual(public.path, os.environ.get("PUBLIC"))

    def test_uses_environment_variables(self):
        """关键位置必须来自环境变量，换台机器也对。"""
        by_key = scan_scopes.by_key()
        self.assertEqual(by_key["appdata"].paths[0], os.environ.get("APPDATA"))
        self.assertEqual(by_key["localappdata"].paths[0], os.environ.get("LOCALAPPDATA"))
        self.assertEqual(by_key["programdata"].paths[0], os.environ.get("PROGRAMDATA"))
        self.assertEqual(by_key["public"].paths[0], os.environ.get("PUBLIC"))

    def test_expand_splits_multi_path(self):
        """「所有盘的 Program Files」展开后要变成一条条真实路径。"""
        pairs = scan_scopes.expand(["program_files"])
        keys = [scope.key for scope in scan_scopes.build_scopes()]
        if "program_files" in keys:
            self.assertTrue(pairs, "展开后不该是空的")
            for label, path in pairs:
                self.assertTrue(os.path.isdir(path), path)

    def test_user_level_programs_included(self):
        """%LOCALAPPDATA%\\Programs —— VS Code / Chrome 这类装在这儿，必须有。"""
        self.assertIn("local_programs", scan_scopes.by_key())

    def test_scope_count_reasonable(self):
        """位置要足够全，但别失控。"""
        count = len(scan_scopes.build_scopes())
        self.assertGreaterEqual(count, 15, "位置太少了，容易漏")
        self.assertLessEqual(count, 40, "位置太多了，用户勾不过来")

    def test_every_scope_has_unique_label(self):
        labels = [scope.label for scope in scan_scopes.build_scopes()]
        self.assertEqual(len(labels), len(set(labels)), "名字不能重复，否则用户分不清")

    def test_paths_are_tuples(self):
        for scope in scan_scopes.build_scopes():
            self.assertIsInstance(scope.paths, tuple, "%s 的 paths 该是元组" % scope.key)


class ScopeDialogTests(unittest.TestCase):
    def make(self, selected=None):
        from ui.scan_scope_dialog import ScanScopeDialog

        dialog = ScanScopeDialog(selected or [])
        # 不要 show()：真实窗口在解释器退出时析构会触发 Qt 的访问违例
        # （进程退出码 0xC0000005，虽然测试全过，但会被当成失败）
        _QT_APP.processEvents()
        return dialog

    def tearDown(self):
        for widget in list(_QT_APP.allWidgets()):
            widget.close()
            widget.deleteLater()
        _QT_APP.processEvents()

    def test_defaults_preselected(self):
        dialog = self.make()
        self.assertEqual(sorted(dialog.chosen()), sorted(scan_scopes.default_keys()))

    def test_select_all(self):
        dialog = self.make()
        dialog._set_all(True)
        self.assertEqual(len(dialog.chosen()), len(dialog._boxes))

    def test_select_none(self):
        dialog = self.make()
        dialog._set_all(False)
        self.assertEqual(dialog.chosen(), [])

    def test_pick_safe_only(self):
        dialog = self.make()
        dialog._set_all(True)
        dialog._pick_safe()
        by_key = scan_scopes.by_key()
        for key in dialog.chosen():
            self.assertEqual(by_key[key].risk, "safe")

    def test_restore_default(self):
        dialog = self.make()
        dialog._set_all(True)
        dialog._restore_default()
        self.assertEqual(sorted(dialog.chosen()), sorted(scan_scopes.default_keys()))

    def test_count_label(self):
        dialog = self.make()
        dialog._set_all(True)
        total = len(dialog._boxes)
        self.assertIn(str(total), dialog.count.text())

    def test_remembered_selection(self):
        keys = scan_scopes.default_keys()[:2]
        dialog = self.make(keys)
        self.assertEqual(sorted(dialog.chosen()), sorted(keys))


class SettingsRoundTripTests(unittest.TestCase):
    def test_round_trip(self):
        settings = Settings()
        settings.scan_scopes = ["appdata", "packages"]
        restored = Settings.from_mapping(settings.to_mapping())
        self.assertEqual(restored.scan_scopes, ["appdata", "packages"])

    def test_garbage_becomes_empty(self):
        self.assertEqual(Settings.from_mapping({"scan_scopes": "abc"}).scan_scopes, [])
        self.assertEqual(Settings.from_mapping({"scan_scopes": 5}).scan_scopes, [])

    def test_empty_means_default(self):
        """空列表表示「用默认那套」。"""
        self.assertEqual(Settings.from_mapping({"scan_scopes": []}).scan_scopes, [])


if __name__ == "__main__":
    unittest.main()
