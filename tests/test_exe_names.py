"""exe 名收集的测试 —— 这是「扫得全不全」的关键。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from core.cleaners import exe_names, exe_names_from_dir


class DirScanTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="exes-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def touch(self, rel: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    def test_finds_top_level(self):
        self.touch("main.exe")
        self.touch("uninst.exe")
        names = exe_names_from_dir(str(self.root))
        self.assertEqual(sorted(names), ["main", "uninst"])

    def test_finds_nested(self):
        """子目录里的 exe 也要收 —— 加速器、helper 常放在子目录。"""
        self.touch("main.exe")
        self.touch("modules/Accelerator/Accelerator.exe")
        names = exe_names_from_dir(str(self.root))
        self.assertIn("Accelerator", names)

    def test_ignores_non_exe(self):
        self.touch("readme.txt")
        self.assertEqual(exe_names_from_dir(str(self.root)), [])

    def test_missing_dir_returns_empty(self):
        self.assertEqual(exe_names_from_dir(r"C:\绝对不存在的目录zzz"), [])

    def test_respects_limit(self):
        for i in range(30):
            self.touch(f"a{i}.exe")
        names = exe_names_from_dir(str(self.root), limit=10)
        self.assertLessEqual(len(names), 10)

    def test_dedupes(self):
        self.touch("Main.exe")
        self.touch("sub/main.EXE")
        names = exe_names_from_dir(str(self.root))
        self.assertEqual(len([n for n in names if n.lower() == "main"]), 1)


class IconFallbackTests(unittest.TestCase):
    """InstallLocation 为空时，要从 DisplayIcon 推出安装目录。"""

    def test_icon_gives_directory(self):
        root = Path(tempfile.mkdtemp(prefix="icon-"))
        try:
            (root / "App.exe").write_bytes(b"x")
            (root / "Helper.exe").write_bytes(b"x")
            from core.uninstall import SoftwareItem

            item = SoftwareItem(
                name="测试软件", version="1", publisher="", install_date="",
                size_bytes=None, install_location="",           # 空的
                uninstall_string="", quiet_uninstall_string="",
                key_path="k", hive="HKLM64",
                display_icon=str(root / "App.exe"),
            )
            self.assertIn("Helper", item.exe_names)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
