"""提权入口：🔒 项有出口、点击才走重启、不自动骚扰。"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QMessageBox


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class ArgumentTests(unittest.TestCase):
    def test_module_argument(self):
        import main as entry

        self.assertEqual(entry._module_argument(["--module", "2"]), 2)
        self.assertEqual(entry._module_argument(["--module=3"]), 3)
        self.assertIsNone(entry._module_argument([]))
        self.assertIsNone(entry._module_argument(["--module", "abc"]))

    def test_dir_still_works(self):
        import main as entry

        self.assertEqual(entry._dir_argument(["--dir", "C:\\x"]), "C:\\x")


class LockedEntryTests(Base):
    def test_locked_menu_entry_exists(self):
        """源码级检查：🔒 项的菜单入口在，旧的死路已经换掉。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        main_src = (root / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn("需要管理员权限，点此重启", main_src)
        self.assertNotIn("这一项打不开，不能删", main_src, "旧的禁用项要换掉")

        for name in ("registry_view.py", "process_view.py", "uninstall_view.py"):
            src = (root / "ui" / name).read_text(encoding="utf-8")
            self.assertIn("需要管理员权限，点此重启", src, name)

    def test_double_click_offers_restart(self):
        """双击 🔒 项：问一句，选「是」才重启。"""
        from ui.main_window import MainWindow

        window = mock.Mock(spec=MainWindow)
        window._restart_as_admin = mock.Mock()
        entry = mock.Mock()
        entry.name = "System Volume Information"

        with mock.patch("ui.main_window._is_admin", return_value=False), \
             mock.patch("ui.main_window.QMessageBox.question", return_value=QMessageBox.Yes):
            MainWindow._offer_admin_for_locked(window, entry)
        window._restart_as_admin.assert_called_once()

    def test_double_click_no_keeps_running(self):
        from ui.main_window import MainWindow

        window = mock.Mock(spec=MainWindow)
        window._restart_as_admin = mock.Mock()
        entry = mock.Mock()
        entry.name = "x"

        with mock.patch("ui.main_window._is_admin", return_value=False), \
             mock.patch("ui.main_window.QMessageBox.question", return_value=QMessageBox.No):
            MainWindow._offer_admin_for_locked(window, entry)
        window._restart_as_admin.assert_not_called()

    def test_already_admin_says_so(self):
        from ui.main_window import MainWindow

        with mock.patch("ui.main_window._is_admin", return_value=True):
            window = mock.Mock(spec=MainWindow)
            entry = mock.Mock()
            entry.name = "x"
            window._toast = mock.Mock()
            MainWindow._offer_admin_for_locked(window, entry)
            window._toast.assert_called_once()
            self.assertIn("管理员", window._toast.call_args[0][0])


class NoAutoPromptTests(Base):
    def test_nothing_pops_without_click(self):
        """读目录时不该自己弹任何提权框。"""
        import tempfile
        from pathlib import Path

        import app_paths
        from core.config import CacheStore, SettingsStore
        from core.knowledge import load_from_files
        from ui.main_window import MainWindow

        paths = app_paths.resolve()
        tmp = Path(tempfile.mkdtemp(prefix="dch-admin-"))
        store = SettingsStore(tmp / "config.json", paths.resources / "default_config.json")
        settings = store.load()
        settings.last_module = 0
        settings.auto_size = False
        cache = CacheStore(paths.cache)
        cache.load()
        kb = load_from_files(paths.resources / "default_knowledge.json", paths.knowledge, paths.user_rules)

        with mock.patch("ui.main_window._is_admin", return_value=False), \
             mock.patch("PySide6.QtWidgets.QMessageBox.question") as question, \
             mock.patch("PySide6.QtWidgets.QMessageBox.information") as info:
            window = MainWindow(paths, settings, store, kb, cache)
            window.show()
            window.bootstrap()
            for _ in range(40):
                self.app.processEvents()
            window.navigate(os.environ.get("WINDIR", r"C:\Windows"))
            for _ in range(80):
                self.app.processEvents()
            window.close()

        self.assertFalse(question.called, "不该自动弹提权询问")
        self.assertFalse(info.called, "不该自动弹提示")


if __name__ == "__main__":
    unittest.main(verbosity=2)
