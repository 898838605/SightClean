"""已修复问题的回归测试：重复常量、WOW64 视图、未收录等级。

这些点都属于「看起来没事、实际会误导用户」的问题，单独列出来防止再犯。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from core import delete, knowledge, leftover, registry_scan
from core.risk import Risk, evaluate

ROOT = Path(__file__).resolve().parent.parent


class DeleteMessageTests(unittest.TestCase):
    def test_err_in_use_defined_once(self):
        """ERR_IN_USE 曾经在同一段里定义两次，后一条静默覆盖前一条。"""
        tree = ast.parse((ROOT / "core" / "delete.py").read_text(encoding="utf-8"))
        names = []
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "ERR_IN_USE":
                    names.append(target.id)
        self.assertEqual(names, ["ERR_IN_USE"])

    def test_locked_windows_error_is_reported_as_in_use(self):
        for code in (32, 33, 145):
            exc = OSError("locked")
            exc.winerror = code
            self.assertEqual(delete._human_error(exc), delete.ERR_IN_USE, code)

    def test_in_use_message_tells_user_what_to_do(self):
        self.assertIn("关掉", delete.ERR_IN_USE)


class RegistryViewTests(unittest.TestCase):
    """32 位视图必须显式写在 WOW6432Node 下，四份登记都要扫。"""

    def test_uninstall_has_four_views(self):
        paths = [path for _hive, _name, path in registry_scan._UNINSTALL_SOURCES]
        self.assertEqual(len(paths), 4)
        self.assertEqual(sum("WOW6432Node" in path for path in paths), 2)

    def test_app_paths_has_four_views(self):
        paths = [path for _hive, _name, path in registry_scan._APP_PATHS]
        self.assertEqual(len(paths), 4)
        self.assertEqual(sum("WOW6432Node" in path for path in paths), 2)

    def test_run_keys_cover_32_bit_views(self):
        paths = [path for _hive, _name, path in registry_scan._RUN_KEYS]
        for root in ("Run", "RunOnce"):
            for hive in ("HKEY_LOCAL_MACHINE", "HKEY_CURRENT_USER"):
                expected = (
                    rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\{root}"
                )
                self.assertIn(expected, paths, f"{hive} 的 32 位 {root} 没扫")

    def test_leftover_uninstall_has_four_views(self):
        self.assertEqual(len(leftover._UNINSTALL_PATHS), 4)
        self.assertEqual(
            sum("WOW6432Node" in path for _hive, path, _label in leftover._UNINSTALL_PATHS),
            2,
        )


class UnlistedRiskTests(unittest.TestCase):
    """未收录在数据层和展示层都必须是 unlisted，不能悄悄变成黄色 caution。"""

    def test_constant_matches_display_enum(self):
        self.assertEqual(knowledge.UNLISTED_RISK, Risk.UNLISTED.value)

    def test_unknown_file_is_unlisted(self):
        kb = knowledge.KnowledgeBase.build({"rules": []})
        result = kb.match_detail(r"Z:\没听过\a.bin", False)
        self.assertFalse(result.known)
        self.assertEqual(result.risk, "unlisted")

    def test_unknown_folder_is_unlisted(self):
        kb = knowledge.KnowledgeBase.build({"rules": []})
        self.assertEqual(kb.folder_risk(r"Z:\没听过"), "unlisted")
        verdict = evaluate(r"Z:\没听过", True, kb)
        self.assertIs(verdict.level, Risk.UNLISTED)
        self.assertFalse(verdict.known)

    def test_unlisted_rank_is_lowest(self):
        self.assertLess(Risk.UNLISTED.rank, Risk.SAFE.rank)
        self.assertLess(Risk.SAFE.rank, Risk.CAUTION.rank)
        self.assertLess(Risk.CAUTION.rank, Risk.FORBIDDEN.rank)


class DocumentTests(unittest.TestCase):
    def test_readme_points_at_tests(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("测试套件不在本仓库中", text)
        self.assertIn("unittest discover", text)

    def test_user_guide_uses_same_data_dir(self):
        """文档里的回退目录必须和 app_paths 实际用的名字一致。"""
        from app_paths import FALLBACK_DIR_NAME

        text = (ROOT / "使用说明.txt").read_text(encoding="utf-8")
        self.assertIn(rf"%LOCALAPPDATA%\{FALLBACK_DIR_NAME}", text)
        self.assertNotIn("DiskCleanupHelper", text)

    def test_readme_uses_same_data_dir(self):
        from app_paths import FALLBACK_DIR_NAME

        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(rf"%LOCALAPPDATA%\{FALLBACK_DIR_NAME}", text)


if __name__ == "__main__":
    unittest.main()
