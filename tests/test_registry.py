"""注册表模块：路径解析、关键位判定、懒加载、备份、删除、报告、页面规则。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import uuid
import winreg
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt

from core.knowledge import load_from_files
from core.registry import (
    ERR_HAS_CHILDREN,
    ERR_NO_PERMISSION,
    RegistryReport,
    RegistryResult,
    _split,
    clean_keys,
    delete_key,
    export_keys,
    import_backup,
    is_critical,
    list_children,
    list_root_keys,
)

ROOT_DIR = Path(__file__).resolve().parent.parent


class SplitTests(unittest.TestCase):
    def test_full_name(self):
        hive, full, rest = _split(r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft")
        self.assertEqual(full, "HKEY_LOCAL_MACHINE")
        self.assertEqual(rest, r"SOFTWARE\Microsoft")

    def test_short_name(self):
        hive, full, rest = _split(r"HKCU\Software\X")
        self.assertEqual(full, "HKEY_CURRENT_USER")
        self.assertEqual(rest, r"Software\X")

    def test_unknown_root(self):
        with self.assertRaises(ValueError):
            _split(r"NOT_A_ROOT\X")


class CriticalTests(unittest.TestCase):
    def test_system_is_critical(self):
        self.assertTrue(is_critical(r"HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services"))
        self.assertTrue(is_critical(r"HKEY_LOCAL_MACHINE\SAM"))
        self.assertTrue(is_critical(r"HKEY_LOCAL_MACHINE\SECURITY"))

    def test_normal_key_not_critical(self):
        self.assertFalse(is_critical(r"HKEY_CURRENT_USER\Software\SomeApp"))


class EnumerateTests(unittest.TestCase):
    def test_five_roots(self):
        roots = list_root_keys()
        self.assertEqual(len(roots), 5)
        self.assertEqual([root.short_root for root in roots],
                         ["HKLM", "HKCU", "HKCR", "HKU", "HKCC"])

    def test_lazy_listing(self):
        children, error = list_children(r"HKEY_CURRENT_USER\Software")
        self.assertIsNone(error)
        self.assertIsNotNone(children)
        self.assertGreater(len(children), 0)
        sample = children[0]
        self.assertTrue(sample.full_path.startswith("HKEY_CURRENT_USER\\Software\\"))
        self.assertTrue(sample.last_write != "")

    def test_missing_path_reports_plainly(self):
        children, error = list_children(r"HKEY_CURRENT_USER\Software\根本不存在的键_zzz")
        self.assertIsNone(children)
        self.assertTrue(error)


class ExportDeleteTests(unittest.TestCase):
    """在自己新建的测试键上真刀真枪地备份 + 删除，不碰任何系统键。"""

    def setUp(self):
        self.name = f"_dch_test_{uuid.uuid4().hex[:8]}"
        self.sub = rf"Software\{self.name}"
        self.full = rf"HKEY_CURRENT_USER\{self.sub}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.sub) as key:
            winreg.SetValueEx(key, "Mark", 0, winreg.REG_SZ, "hello")
        self.tmpdir = Path(tempfile.mkdtemp(prefix="dch-reg-"))

    def tearDown(self):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.sub)
        except OSError:
            pass

    def test_export_creates_usable_reg_file(self):
        target = self.tmpdir / "backup.reg"
        path, error = export_keys([self.full], target)
        self.assertIsNone(error, error)
        self.assertIsNotNone(path)
        self.assertTrue(Path(path).exists())
        text = Path(path).read_text(encoding="utf-16")
        self.assertIn("Windows Registry Editor Version 5.00", text)
        self.assertIn(self.name, text)

    def test_delete_removes_the_key(self):
        result = delete_key(self.full)
        self.assertTrue(result.ok, result.error)
        with self.assertRaises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.sub, 0, winreg.KEY_READ)

    def test_root_cannot_be_deleted(self):
        result = delete_key("HKEY_CURRENT_USER")
        self.assertFalse(result.ok)
        self.assertIn("根键", result.error or "")

    def test_key_with_children_is_refused(self):
        child = rf"{self.sub}\_child"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, child):
            pass
        result = delete_key(self.full)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, ERR_HAS_CHILDREN)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, child)

    def test_clean_keys_orders_deep_first(self):
        child = rf"{self.sub}\_child2"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, child):
            pass
        report = clean_keys([self.full, rf"HKEY_CURRENT_USER\{child}"], self.tmpdir)
        self.assertTrue(report.backup_path is not None, "应该生成了备份")
        self.assertEqual(report.ok_count, 2, [item.error for item in report.items])
        self.assertTrue(report.log_path is not None)

    def test_import_missing_file(self):
        ok, message = import_backup(self.tmpdir / "不存在.reg")
        self.assertFalse(ok)
        self.assertIn("找不到", message)


class ReportTests(unittest.TestCase):
    def test_counts_and_text(self):
        now = time.time()
        report = RegistryReport(
            items=[
                RegistryResult("HKCU\\A", "A", True, None, now),
                RegistryResult("HKCU\\B", "B", False, ERR_NO_PERMISSION, now),
            ],
            started=now - 1, finished=now,
        )
        self.assertEqual(report.ok_count, 1)
        self.assertEqual(report.fail_count, 1)
        self.assertIn("清掉 1 个键", report.summary)
        text = report.as_log_text()
        self.assertIn("成功 | HKCU", text)
        self.assertIn("失败 | HKCU", text)


class RegistryKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.kb = load_from_files(ROOT_DIR / "resources" / "default_knowledge.json", ROOT_DIR / "知识库.json")

    def test_system_forbidden(self):
        result = self.kb.match_registry(r"HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\Tcpip")
        self.assertTrue(result.known)
        self.assertEqual(result.risk, "forbidden")

    def test_run_caution(self):
        result = self.kb.match_registry(r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run")
        self.assertEqual(result.risk, "caution")

    def test_unlisted(self):
        result = self.kb.match_registry(r"HKEY_CURRENT_USER\Software\我的软件\Settings")
        self.assertFalse(result.known)

    def test_kind_count(self):
        self.assertEqual(self.kb.counts.get("registry"), 9)


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def build(self):
        from core.registry import RegistryKey
        from ui.registry_view import RegistryModel, RegistryRow

        model = RegistryModel()
        model.set_rows([
            RegistryRow(key=RegistryKey(r"HKCU\Software\A", "HKCU", "A", 1, 2, "2026-01-01"),
                        effect="普通键", risk="caution"),
            RegistryRow(key=RegistryKey(r"HKLM\SYSTEM\X", "HKLM", "X", 3, 0, "2026-01-02"),
                        effect="系统关键位", risk="forbidden"),
            RegistryRow(key=RegistryKey(r"HKCU\Software\Locked", "HKCU", "Locked", locked=True),
                        effect="", risk="unlisted", locked=True),
            RegistryRow(key=RegistryKey(r"HKEY_CURRENT_USER", "HKCU", "HKCU"), effect="根", risk="caution"),
        ])
        return model

    def test_six_columns(self):
        from ui.registry_view import HEADERS

        model = self.build()
        self.assertEqual(model.columnCount(), 6)
        self.assertEqual(HEADERS, ("键名", "最后修改", "类型", "作用", "风险", "是否清理"))

    def test_red_checkable(self):
        from ui.registry_view import COL_CHECK

        model = self.build()
        self.assertTrue(model.flags(model.index(1, COL_CHECK)) & Qt.ItemIsUserCheckable)

    def test_locked_not_checkable(self):
        from ui.registry_view import COL_CHECK

        model = self.build()
        self.assertFalse(model.flags(model.index(2, COL_CHECK)) & Qt.ItemIsUserCheckable)

    def test_root_not_checkable(self):
        from ui.registry_view import COL_CHECK

        model = self.build()
        self.assertFalse(model.flags(model.index(3, COL_CHECK)) & Qt.ItemIsUserCheckable)
        model.set_all_checked(True)
        self.assertEqual(len(model.checked_rows()), 2, "根键和锁定项都不该被全选带上")

    def test_ai_cannot_downgrade(self):
        model = self.build()
        model.update_ai(r"HKLM\SYSTEM\X", "AI 说没事", "safe", "AI 判断")
        self.assertEqual(model.rows()[1].risk, "forbidden")


class RecursiveCleanTests(unittest.TestCase):
    """勾了父键就该连子键一起删 —— 有子键就拒绝是没道理的。"""

    def setUp(self):
        import uuid

        self.name = f"_dch_clean_{uuid.uuid4().hex[:8]}"
        self.root = rf"SOFTWARE\{self.name}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.root) as key:
            with winreg.CreateKey(key, "a") as a:
                with winreg.CreateKey(a, "a1"):
                    pass
            with winreg.CreateKey(key, "b"):
                pass

    def tearDown(self):
        import subprocess

        subprocess.run(["reg", "delete", rf"HKCU\{self.root}", "/f"],
                       capture_output=True)

    def test_counts_whole_tree(self):
        from core.registry import count_tree

        self.assertEqual(count_tree(rf"HKEY_CURRENT_USER\{self.root}"), 3)

    def test_deletes_whole_tree(self):
        import tempfile
        from pathlib import Path

        from core.registry import clean_keys

        log_dir = Path(tempfile.mkdtemp())
        report = clean_keys([rf"HKEY_CURRENT_USER\{self.root}"], log_dir)
        self.assertEqual(report.ok_count, 1, [i.error for i in report.items])
        with self.assertRaises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.root, 0, winreg.KEY_READ)

    def test_parent_and_child_together(self):
        """父子同时勾选：父键删掉子树后，子键那条不该报失败。"""
        import tempfile
        from pathlib import Path

        from core.registry import clean_keys

        log_dir = Path(tempfile.mkdtemp())
        paths = [rf"HKEY_CURRENT_USER\{self.root}",
                 rf"HKEY_CURRENT_USER\{self.root}\a"]
        report = clean_keys(paths, log_dir)
        self.assertEqual(report.fail_count, 0, [i.error for i in report.items])

    def test_backup_contains_subtree(self):
        import tempfile
        from pathlib import Path

        from core.registry import clean_keys

        log_dir = Path(tempfile.mkdtemp())
        report = clean_keys([rf"HKEY_CURRENT_USER\{self.root}"], log_dir)
        self.assertIsNotNone(report.backup_path)
        text = Path(report.backup_path).read_text(encoding="utf-16")
        self.assertIn(self.name, text)
        # 子树也应该在备份里
        self.assertIn("a1", text)


class TreeDeleteDiagnosticsTests(unittest.TestCase):
    """删子树失败时要说清楚卡在哪 —— 「没权限」经常是误判。"""

    def test_nested_tree_deleted(self):
        import uuid

        from core.registry import delete_key_tree

        name = f"_dch_d_{uuid.uuid4().hex[:6]}"
        root = rf"SOFTWARE\{name}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as key:
            with winreg.CreateKey(key, "a") as a:
                winreg.CreateKey(a, "a1")
        ok, error = delete_key_tree(rf"HKEY_CURRENT_USER\{root}")
        self.assertTrue(ok, error)
        with self.assertRaises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, root, 0, winreg.KEY_READ)

    def test_missing_key_says_gone(self):
        import uuid

        from core.registry import delete_key_tree

        path = rf"HKEY_CURRENT_USER\SOFTWARE\_dch_gone_{uuid.uuid4().hex[:6]}"
        ok, error = delete_key_tree(path)
        self.assertFalse(ok)
        self.assertIn("已经不在了", error)

    def test_unreadable_child_still_attempted(self):
        """打不开的子键也要试删 —— 不能一读不到就放弃整棵树。"""
        import uuid

        from core.registry import _delete_tree

        name = f"_dch_u_{uuid.uuid4().hex[:6]}"
        root = rf"SOFTWARE\{name}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as key:
            winreg.CreateKey(key, "child")
        # 正常路径不该抛
        _delete_tree(winreg.HKEY_CURRENT_USER, root)
        with self.assertRaises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, root, 0, winreg.KEY_READ)

    def test_error_mentions_which_key(self):
        """失败文案要指出是哪个键 —— 只说「没权限」用户没法处理。"""
        import uuid
        from unittest import mock

        from core import registry

        name = f"_dch_e_{uuid.uuid4().hex[:6]}"
        root = rf"SOFTWARE\{name}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as key:
            winreg.CreateKey(key, "locked_child")

        original = registry._delete_tree

        def fake_delete(hive, sub):
            if sub.endswith("locked_child"):
                raise OSError(5, "拒绝访问。")
            return original(hive, sub)

        with mock.patch.object(registry, "_delete_tree", fake_delete):
            ok, error = registry.delete_key_tree(rf"HKEY_CURRENT_USER\{root}")
        self.assertFalse(ok)
        self.assertIn("locked_child", error, "错误里没说是哪个子键")

        import subprocess

        subprocess.run(["reg", "delete", rf"HKCU\{root}", "/f"], capture_output=True)


class SystemOnlyKeyTests(unittest.TestCase):
    """有些键只给 SYSTEM，管理员也读不到 —— 文案要说清楚，别让用户以为权限不够。"""

    def test_security_is_system_only(self):
        from core.registry import _system_only

        self.assertTrue(_system_only(r"HKEY_LOCAL_MACHINE\SECURITY"))
        self.assertTrue(_system_only(r"HKEY_LOCAL_MACHINE\SAM"))
        self.assertTrue(_system_only(r"HKEY_LOCAL_MACHINE\BCD00000000"))

    def test_normal_key_not_system_only(self):
        from core.registry import _system_only

        self.assertFalse(_system_only(r"HKEY_LOCAL_MACHINE\SOFTWARE"))
        self.assertFalse(_system_only(r"HKEY_CURRENT_USER\Software\Tencent"))

    def test_reason_mentions_system_account(self):
        from core.registry import _lock_reason

        text = _lock_reason(r"HKEY_LOCAL_MACHINE\SECURITY")
        self.assertIn("SYSTEM", text)
        self.assertIn("管理员", text)

    def test_reason_for_ordinary_denied(self):
        from core.registry import _lock_reason

        text = _lock_reason(r"HKEY_LOCAL_MACHINE\SOME\THING")
        self.assertIn("没有权限", text)
        self.assertNotIn("SYSTEM 账户", text)

    def test_locked_key_shows_reason(self):
        from core.registry import RegistryKey

        key = RegistryKey(full_path=r"HKEY_LOCAL_MACHINE\SECURITY", short_root="HKLM",
                          name="SECURITY", locked=True,
                          lock_reason="Windows 只允许 SYSTEM 账户读")
        self.assertEqual(key.detail, "Windows 只允许 SYSTEM 账户读")
        self.assertEqual(key.type_label, "打不开")

    def test_locked_without_reason_falls_back(self):
        from core.registry import RegistryKey

        key = RegistryKey(full_path=r"HKEY_LOCAL_MACHINE\X", short_root="HKLM",
                          name="X", locked=True)
        self.assertEqual(key.detail, "读不到")

    def test_real_hklm_children_explain_locks(self):
        """列 HKLM 时，锁住的键要带上具体原因。"""
        from core.registry import list_children

        children, error = list_children(r"HKEY_LOCAL_MACHINE")
        if children is None:
            self.skipTest("这台机器列不了 HKLM：%s" % error)
        for child in children:
            if child.locked and child.name.upper() in ("SECURITY", "BCD00000000"):
                self.assertIn("SYSTEM", child.lock_reason,
                              "%s 锁住但没说清原因" % child.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
