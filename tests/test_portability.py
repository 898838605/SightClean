"""通用性测试：不能假设系统装在 C 盘。

这些用例守护的是「换台机器还能用」——
用户可能把系统装在 D 盘、把程序装在 E 盘、把用户目录挪到别的盘。
任何写死 C 盘的地方都会让这里挂掉。
"""
from __future__ import annotations

import os
import re
import string
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app_paths


class FallbackDirTests(unittest.TestCase):
    """程序目录写不了时，数据目录既要跟程序同名，又不能丢老配置。"""

    def test_new_install_uses_app_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
                root = app_paths._fallback_root()
            self.assertEqual(root, Path(tmp) / app_paths.APP_NAME)

    def test_legacy_dir_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / app_paths.LEGACY_FALLBACK_DIR_NAME
            legacy.mkdir()
            (legacy / "config.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
                root = app_paths._fallback_root()
            self.assertEqual(root, Path(tmp) / app_paths.APP_NAME)
            self.assertTrue((root / "config.json").exists(), "迁移后配置不能丢")
            self.assertFalse(legacy.exists())

    def test_failed_migration_keeps_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / app_paths.LEGACY_FALLBACK_DIR_NAME
            legacy.mkdir()
            (legacy / "config.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                    mock.patch.object(Path, "rename", side_effect=OSError("被占用")):
                root = app_paths._fallback_root()
            self.assertEqual(root, legacy, "迁不动时必须继续用老目录")
            self.assertTrue((root / "config.json").exists())


class ProtectedDirsTests(unittest.TestCase):
    """强制卸载的受保护目录必须覆盖所有盘。"""

    def test_covers_all_drive_program_files(self):
        from core.force import _PROTECTED_DIRS, _norm

        for letter in ("c", "d", "e", "f"):
            for name in ("program files", "program files (x86)", "users", "programdata"):
                path = _norm(f"{letter}:\\{name}")
                self.assertIn(path, _PROTECTED_DIRS,
                              "%s 没被保护 —— 系统装在这个盘时会误删" % path)

    def test_covers_drive_roots(self):
        from core.force import _PROTECTED_DIRS, _norm

        for letter in ("c", "d", "e"):
            self.assertIn(_norm(f"{letter}:"), _PROTECTED_DIRS,
                          "%s: 盘根目录没被保护" % letter)

    def test_covers_env_paths(self):
        from core.force import _PROTECTED_DIRS, _norm

        for env in ("WINDIR", "PROGRAMFILES", "PROGRAMDATA"):
            value = os.environ.get(env)
            if not value:
                continue
            self.assertIn(_norm(value), _PROTECTED_DIRS,
                          "%s=%s 没被保护" % (env, value))

    def test_public_desktop_protected(self):
        from core.force import _PROTECTED_DIRS, _norm

        public = os.environ.get("PUBLIC")
        if not public:
            self.skipTest("没有 PUBLIC 环境变量")
        # 公共目录本身必须在保护范围内
        self.assertIn(_norm(public), _PROTECTED_DIRS)

    def test_normal_folder_not_protected(self):
        from core.force import _PROTECTED_DIRS, _norm

        self.assertNotIn(_norm(r"d:\myapps\sometool"), _PROTECTED_DIRS)


class ProgramFilesTests(unittest.TestCase):
    """识别「是不是装在 Program Files 下」不能只看 C 盘。"""

    def test_all_drives_recognized(self):
        from core.processes import _under_program_files

        for letter in ("c", "d", "e", "f"):
            path = r"%s:\program files\someapp\app.exe" % letter
            self.assertTrue(_under_program_files(path),
                            "%s 没被认成 Program Files" % path)

    def test_x86_recognized(self):
        from core.processes import _under_program_files

        self.assertTrue(_under_program_files(r"e:\program files (x86)\x\y.exe"))

    def test_other_folder_not_recognized(self):
        from core.processes import _under_program_files

        for path in (r"d:\desktop\tool.exe", r"c:\temp\a.exe", r"e:\games\g.exe"):
            self.assertFalse(_under_program_files(path), path)

    def test_roots_include_all_drives(self):
        from core.leftover import _program_files_roots

        roots = _program_files_roots()
        self.assertTrue(roots, "一个 Program Files 都没找到")
        letters = {root[0].lower() for root in roots}
        self.assertIn("c", letters)
        # 至少覆盖系统盘
        system = os.environ.get("PROGRAMFILES", "c:")
        self.assertIn(system[0].lower(), letters)


class PublicPathTests(unittest.TestCase):
    """公共桌面/开始菜单按环境变量算，不写死。"""

    def test_public_uses_env(self):
        import inspect

        from core import leftover

        source = inspect.getsource(leftover.scan_shortcuts)
        self.assertNotIn(r"C:\Users\Public", source,
                         "公共桌面写死了 C 盘")
        self.assertIn("PUBLIC", source, "应该用 %PUBLIC% 环境变量")

    def test_orphan_roots_not_hardcoded(self):
        import inspect

        from core import leftover

        source = inspect.getsource(leftover.scan_orphans)
        self.assertNotIn(r'roots or (r"C:\Program Files"', source,
                         "孤儿扫描默认路径写死了 C 盘")


class NoHardcodedUserTests(unittest.TestCase):
    """不能把某一台机器的用户目录写进代码。"""

    # 只认 C:\Users\xxx 这种绝对路径，GitHub 地址里的数字不会被误伤
    PATTERN = re.compile(r"[A-Za-z]:\\Users\\[^\\\"'\s]+")

    def test_no_hardcoded_user_profile(self):
        from pathlib import Path

        pattern = re.compile(self.PATTERN.pattern)
        offenders = []
        for folder in ("core", "ui"):
            for path in Path(folder).rglob("*.py"):
                for line_no, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines(), 1):
                    if pattern.search(line):
                        offenders.append("%s:%d" % (path, line_no))
        self.assertEqual(offenders, [], "这些地方写死了用户目录：%s" % offenders)

    def test_pattern_recognizes_user_path_only(self):
        self.assertIsNotNone(self.PATTERN.search(r"C:\Users\somebody\AppData"))
        self.assertIsNone(self.PATTERN.search("https://github.com/example/SightClean"))


if __name__ == "__main__":
    unittest.main()
