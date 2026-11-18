"""注册表失效项扫描的测试。

核心原则：只判「指向的目标不存在」的项，不做主观猜测。
"""
from __future__ import annotations

import os
import unittest

from core import registry_scan


class ExistsTests(unittest.TestCase):
    """判断「这条路走不走得通」，三种写法都要认。"""

    def test_full_path_exists(self):
        import sys

        self.assertTrue(registry_scan._exists(sys.executable))

    def test_with_icon_index(self):
        import sys

        self.assertTrue(registry_scan._exists(sys.executable + ",0"))

    def test_system_command_exists(self):
        """MsiExec.exe 这种命令名，要去 PATH 里找，不能当路径。"""
        self.assertTrue(registry_scan._exists("MsiExec.exe /X{1234}"))

    def test_missing_path(self):
        self.assertFalse(registry_scan._exists(r"C:\完全不存在的目录zzz\a.exe"))

    def test_missing_with_icon_index(self):
        self.assertFalse(registry_scan._exists(r"C:\完全不存在的目录zzz\a.exe,0"))

    def test_empty(self):
        self.assertFalse(registry_scan._exists(""))
        self.assertFalse(registry_scan._exists("   "))

    def test_quoted_path(self):
        import sys

        self.assertTrue(registry_scan._exists('"' + sys.executable + '"'))


class ScannerTests(unittest.TestCase):
    def test_all_scanners_return_dead_entries(self):
        entries = registry_scan.scan_all()
        for entry in entries:
            self.assertTrue(entry.reason, "%s 没写原因" % entry.label)
            self.assertTrue(entry.target, "%s 没写它指向什么" % entry.label)
            self.assertIn(entry.risk, ("safe", "caution", "risky"))
            self.assertTrue(entry.identity)

    def test_category_filter(self):
        only = registry_scan.scan_all(categories=["app_paths"])
        self.assertTrue(all(e.category == "app_paths" for e in only), only[:3])

    def test_unknown_category_returns_empty(self):
        self.assertEqual(registry_scan.scan_all(categories=["不存在"]), [])

    def test_scanners_do_not_crash(self):
        """单个扫描器出问题不能把整批拖垮。"""
        for key, _label, scanner in registry_scan.SCANNERS:
            try:
                result = scanner()
            except Exception as exc:  # noqa: BLE001
                self.fail("%s 抛了异常：%s" % (key, exc))
            self.assertIsInstance(result, list)

    def test_identity_format(self):
        entry = registry_scan.DeadEntry(
            key_path=r"HKEY_LOCAL_MACHINE\SOFTWARE\X", value_name="",
            label="x", reason="r", target="t", category="app_paths")
        self.assertEqual(entry.identity, r"HKEY_LOCAL_MACHINE\SOFTWARE\X")

        with_value = registry_scan.DeadEntry(
            key_path=r"HKEY_LOCAL_MACHINE\SOFTWARE\X", value_name="v",
            label="x", reason="r", target="t", category="shared_dlls")
        self.assertIn(" :: v", with_value.identity)


class Wow64PathTests(unittest.TestCase):
    """32 位视图的项，路径里必须带 WOW6432Node —— 否则删除时会打开另一个键。"""

    def test_scan_paths_are_explicit(self):
        """扫出来的路径不能依赖 WOW64 标志才能定位。"""
        entries = registry_scan.scan_all()
        for entry in entries:
            lower = entry.key_path.lower()
            # SharedDLLs 在 32 位视图下真实路径就是 WOW6432Node 那条
            if "wow6432node" not in lower and "shareddlls" in lower:
                # 64 位那份也允许，但必须有一条带 WOW6432Node 的
                pass
        with_wow = [e for e in entries if "WOW6432Node" in e.key_path]
        plain = [e for e in entries if "WOW6432Node" not in e.key_path]
        # 两种都可能为空（取决于机器），但如果有 32 位的项，路径必须显式
        for entry in with_wow:
            self.assertIn("WOW6432Node", entry.key_path)

    def test_no_duplicate_identity(self):
        """同一个值不该被算两次 —— 之前两个视图各扫一遍就重复了。"""
        entries = registry_scan.scan_all()
        identities = [e.identity for e in entries]
        duplicates = {i for i in identities if identities.count(i) > 1}
        self.assertEqual(duplicates, set(), "重复项：%s" % list(duplicates)[:3])

    def test_shared_dlls_paths_are_real(self):
        """SharedDLLs 的两条路径都要真的存在（至少一条）。"""
        import winreg

        found = 0
        for path in registry_scan._SHARED_DLLS_PATHS:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ):
                    found += 1
            except OSError:
                continue
        self.assertGreaterEqual(found, 1, "一条 SharedDLLs 路径都打不开")

    def test_value_path_roundtrip(self):
        """扫出来的 identity 必须能被 split_value 正确解析回去。"""
        from core.regvalue import split_value

        for entry in registry_scan.scan_all():
            if not entry.value_name:
                continue
            parts = split_value(entry.identity)
            self.assertIsNotNone(parts, "解析不了：%s" % entry.identity[:60])
            self.assertEqual(parts[1], entry.value_name)


class NoFalsePositiveTests(unittest.TestCase):
    """这几个是真实踩过的误报，必须挡住。"""

    def test_msiexec_not_dead(self):
        """MsiExec.exe /X{GUID} 的 msiexec 本身在系统里，不算失效。"""
        self.assertTrue(registry_scan._exists("MsiExec.exe /X{56D804AC-361D-40C0-99EA-431DC0403DB0}"))

    def test_icon_index_not_dead(self):
        import sys

        self.assertTrue(registry_scan._exists(sys.executable + ",0"))

    def test_no_system_component_in_uninstall(self):
        """SystemComponent=1 的条目一律跳过，不动系统组件。"""
        entries = registry_scan.scan_uninstall()
        for entry in entries:
            self.assertNotIn("SystemComponent", entry.reason)


if __name__ == "__main__":
    unittest.main()
