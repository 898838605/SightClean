"""注册表值清理 + 启动项扫描的测试。

这两块是踩过坑的地方：值被当成键去备份，导致整批中止、一项都清不掉。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import uuid
import winreg
from pathlib import Path

from core import regvalue
from core.cleaners import scan_registered_apps, scan_startup
from core.leftover import KIND_FOLDER, KIND_REGISTRY, Leftover, clean

LAYERS = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"


class SplitValueTests(unittest.TestCase):
    def test_splits(self):
        parts = regvalue.split_value(r"HKEY_CURRENT_USER\SOFTWARE\X :: C:\a\b.exe")
        self.assertEqual(parts[0], r"HKEY_CURRENT_USER\SOFTWARE\X")
        self.assertEqual(parts[1], r"C:\a\b.exe")

    def test_plain_key_returns_none(self):
        self.assertIsNone(regvalue.split_value(r"HKEY_CURRENT_USER\SOFTWARE\X"))


class ValueDeleteTests(unittest.TestCase):
    def setUp(self):
        self.exe = rf"C:\Temp\_dch_{uuid.uuid4().hex[:6]}\main.exe"
        self.display = rf"HKEY_CURRENT_USER\{LAYERS} :: {self.exe}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, LAYERS) as key:
            winreg.SetValueEx(key, self.exe, 0, winreg.REG_SZ, "~ WIN7RTM")

    def tearDown(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LAYERS, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.exe)
        except OSError:
            pass

    def test_read_then_delete(self):
        data = regvalue.read_value(self.display)
        self.assertEqual(data[0], "~ WIN7RTM")
        ok, error = regvalue.delete_value(self.display)
        self.assertTrue(ok, error)
        self.assertIsNone(regvalue.read_value(self.display))

    def test_export_creates_reg_file(self):
        target = Path(tempfile.mkdtemp()) / "v.reg"
        try:
            path, error = regvalue.export_values([self.display], target)
            self.assertEqual(path, target, error)
            text = target.read_text(encoding="utf-16")
            self.assertIn("Windows Registry Editor", text)
            self.assertIn("AppCompatFlags", text)
        finally:
            shutil.rmtree(target.parent, ignore_errors=True)

    def test_clean_deletes_value_not_whole_key(self):
        """关键回归：值单独处理，不能因为备份失败就连累整批。"""
        report = clean([Leftover(KIND_REGISTRY, "兼容性设置", self.display, name="main.exe")])
        self.assertEqual(report.ok_count, 1, [r.error for r in report.items])
        self.assertIsNone(regvalue.read_value(self.display))
        # 键本身应该还在（别的软件的兼容性设置不能动）
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LAYERS, 0, winreg.KEY_READ):
            pass

    def test_clean_mixes_value_and_file(self):
        folder = Path(tempfile.mkdtemp())
        target = folder / "x.bin"
        target.write_bytes(b"x" * 100)
        try:
            report = clean([
                Leftover(KIND_FOLDER, "临时文件", str(target), name="x.bin"),
                Leftover(KIND_REGISTRY, "兼容性设置", self.display, name="main.exe"),
            ])
            self.assertEqual(report.ok_count, 2, [r.error for r in report.items])
            self.assertFalse(target.exists())
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class StartupTests(unittest.TestCase):
    RUN = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"

    def setUp(self):
        self.tag = "_dch_st_" + uuid.uuid4().hex[:6]
        self.exe = rf"C:\Temp\{self.tag}\{self.tag}.exe"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.RUN) as key:
            winreg.SetValueEx(key, self.tag, 0, winreg.REG_SZ, '"' + self.exe + '" --min')

    def tearDown(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.tag)
        except OSError:
            pass

    def test_finds_run_entry(self):
        found = scan_startup([self.tag])
        self.assertTrue(any(self.tag in item.path for item in found), found)

    def test_other_name_not_matched(self):
        found = scan_startup(["完全不相干zzz"])
        self.assertFalse(any(self.tag in item.path for item in found))


class RegisteredAppTests(unittest.TestCase):
    KEY = r"SOFTWARE\RegisteredApplications"

    def setUp(self):
        self.name = "TestApp_" + uuid.uuid4().hex[:6]
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.KEY) as key:
            winreg.SetValueEx(key, self.name, 0, winreg.REG_SZ, r"Software\X\Capabilities")

    def tearDown(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.name)
        except OSError:
            pass

    def test_finds_registered_app(self):
        found = scan_registered_apps(self.name, [])
        self.assertTrue(any(self.name in item.path for item in found), found)


class ValueDeleteOrderTests(unittest.TestCase):
    """先判存在、再判权限 —— 顺序反了会把「不存在」报成「没权限」。"""

    def test_missing_value_says_gone(self):
        from core.regvalue import delete_value

        B = chr(92)
        shared = "HKEY_LOCAL_MACHINE" + B + B.join(
            ["SOFTWARE", "Microsoft", "Windows", "CurrentVersion", "SharedDLLs"])
        ok, error = delete_value(shared + " :: C:" + B + "绝对不存在的_zzz.dll")
        self.assertFalse(ok)
        self.assertIn("已经不在了", error)
        self.assertNotIn("权限", error, "值根本不存在，不该说没权限")

    def test_existing_value_deleted(self):
        import uuid

        from core.regvalue import delete_value, read_value

        B = chr(92)
        name = "_dch_order_" + uuid.uuid4().hex[:6]
        sub = B.join(["SOFTWARE", name])
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, sub) as key:
            winreg.SetValueEx(key, "v", 0, winreg.REG_SZ, "x")
        try:
            display = "HKEY_CURRENT_USER" + B + sub + " :: v"
            ok, error = delete_value(display)
            self.assertTrue(ok, error)
            self.assertIsNone(read_value(display))
        finally:
            import subprocess

            subprocess.run(["reg", "delete", "HKCU" + B + sub, "/f"], capture_output=True)

    def test_deleted_twice_says_gone(self):
        import uuid

        from core.regvalue import delete_value

        B = chr(92)
        name = "_dch_twice_" + uuid.uuid4().hex[:6]
        sub = B.join(["SOFTWARE", name])
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, sub) as key:
            winreg.SetValueEx(key, "v", 0, winreg.REG_SZ, "x")
        try:
            display = "HKEY_CURRENT_USER" + B + sub + " :: v"
            delete_value(display)
            ok, error = delete_value(display)
            self.assertFalse(ok)
            self.assertIn("已经不在了", error)
        finally:
            import subprocess

            subprocess.run(["reg", "delete", "HKCU" + B + sub, "/f"], capture_output=True)

    def test_no_write_permission_reports_permission(self):
        """值确实存在、但没有写权限 —— 这时才该说权限。"""
        from core.regvalue import delete_value, read_value

        B = chr(92)
        key_path = B.join(["SOFTWARE", "Microsoft", "Windows", "CurrentVersion", "SharedDLLs"])
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                                 winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        except OSError:
            self.skipTest("这台机器读不到 SharedDLLs")
        with key:
            try:
                name, _data, _kind = winreg.EnumValue(key, 0)
            except OSError:
                self.skipTest("SharedDLLs 是空的")

        display = "HKEY_LOCAL_MACHINE" + B + key_path + " :: " + name
        if read_value(display) is None:
            self.skipTest("读不到这个值")
        ok, error = delete_value(display)
        if ok:
            self.skipTest("当前有管理员权限，已经删掉了（不影响测试结论）")
        self.assertIn("权限", error)


if __name__ == "__main__":
    unittest.main()
