"""BCU 路子上的全局残留扫描器测试。"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import uuid
import winreg
from pathlib import Path

from core import cleaners


class ExeNameTests(unittest.TestCase):
    def test_strips_icon_index(self):
        self.assertEqual(cleaners.exe_names(r"C:\App\main.exe,0"), ["main"])

    def test_handles_quoted_command(self):
        self.assertEqual(cleaners.exe_names('"C:\\Program Files\\App\\uninst.exe" /S'), ["uninst"])

    def test_handles_space_in_path(self):
        self.assertEqual(cleaners.exe_names(r"C:\Program Files\My App\Run.exe"), ["Run"])

    def test_dedupes_case_insensitively(self):
        names = cleaners.exe_names(r"C:\a\Main.exe", r"D:\b\main.exe")
        self.assertEqual(len(names), 1)

    def test_ignores_non_exe(self):
        self.assertEqual(cleaners.exe_names(r"C:\a\readme.txt"), [])


class PrefetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pf-"))
        self._old = cleaners.WINDIR
        cleaners.WINDIR = str(self.tmp)

    def tearDown(self):
        cleaners.WINDIR = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_matches_exe_stem(self):
        prefetch = self.tmp / "Prefetch"
        prefetch.mkdir()
        (prefetch / "STEAM++.EXE-1C19C8D5.pf").write_bytes(b"x")
        (prefetch / "OTHER.EXE-AAAAAAAA.pf").write_bytes(b"x")
        found = cleaners.scan_prefetch(["steam++"])
        self.assertEqual(len(found), 1)
        self.assertIn("STEAM++", found[0].path)

    def test_ignores_other_exe(self):
        prefetch = self.tmp / "Prefetch"
        prefetch.mkdir()
        (prefetch / "OTHER.EXE-AAAAAAAA.pf").write_bytes(b"x")
        self.assertEqual(cleaners.scan_prefetch(["steam++"]), [])

    def test_no_names_returns_empty(self):
        self.assertEqual(cleaners.scan_prefetch([]), [])


class WerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wer-"))
        self._old = cleaners.PROGRAMDATA
        cleaners.PROGRAMDATA = str(self.tmp)

    def tearDown(self):
        cleaners.PROGRAMDATA = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_crash_folder(self):
        archive = self.tmp / "Microsoft" / "Windows" / "WER" / "ReportArchive"
        archive.mkdir(parents=True)
        (archive / "AppCrash_Steam++.exe_dd9c5b5ef1").mkdir()
        (archive / "AppCrash_Other.exe_1234").mkdir()
        found = cleaners.scan_wer(["steam++"])
        self.assertEqual(len(found), 1)
        self.assertIn("Steam++", found[0].name)

    def test_finds_apphang(self):
        queue = self.tmp / "Microsoft" / "Windows" / "WER" / "ReportQueue"
        queue.mkdir(parents=True)
        (queue / "AppHang_MyApp.exe_abcd").mkdir()
        self.assertEqual(len(cleaners.scan_wer(["myapp"])), 1)


class TempTests(unittest.TestCase):
    def test_finds_same_name_folder(self):
        tmp = tempfile.mkdtemp(prefix="tp-")
        old = os.environ.get("TEMP")
        os.environ["TEMP"] = tmp
        try:
            (Path(tmp) / "MySetup").mkdir()
            found = cleaners.scan_temp(["mysetup"])
            self.assertEqual(len(found), 1)
        finally:
            if old is None:
                os.environ.pop("TEMP", None)
            else:
                os.environ["TEMP"] = old
            shutil.rmtree(tmp, ignore_errors=True)


class AppCompatTests(unittest.TestCase):
    """真实注册表：写一个值再扫。"""

    KEY = (r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers")

    def setUp(self):
        self.exe = rf"C:\Temp\_dch_{uuid.uuid4().hex[:6]}\main.exe"
        self.name = os.path.basename(self.exe)[:-4]
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.KEY) as key:
            winreg.SetValueEx(key, self.exe, 0, winreg.REG_SZ, "~ WIN7RTM")

    def tearDown(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.exe)
        except OSError:
            pass

    def test_finds_exe_entry(self):
        found = cleaners.scan_appcompat([self.name])
        self.assertTrue(any("兼容性" in item.label for item in found), found)

    def test_other_exe_not_matched(self):
        found = cleaners.scan_appcompat(["完全不相干的名字"])
        self.assertFalse(any(self.exe in item.path for item in found))


if __name__ == "__main__":
    unittest.main()
