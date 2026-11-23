"""Regression tests for the source-review findings."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication, QPushButton

QT_APP = QApplication.instance() or QApplication([])


class JunkReparsePointTests(unittest.TestCase):
    def test_clean_does_not_follow_junction(self):
        from core import junk

        with tempfile.TemporaryDirectory(prefix="sightclean-junk-link-") as tmp:
            root = Path(tmp) / "temp"
            target = Path(tmp) / "important"
            root.mkdir()
            target.mkdir()
            keep = target / "keep.txt"
            keep.write_text("important", encoding="utf-8")
            link = root / "linked"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
            )
            if created.returncode != 0:
                self.skipTest("这台机器不能创建 junction")

            category = junk.JunkCategory(
                id="temp", label="临时文件", purpose="测试", detail="",
                risk="safe", mode="contents", paths=[str(root)],
            )
            scanned = junk.scan([category])
            self.assertEqual(scanned.total_size, 0, "扫描 junction 时统计了目标目录")
            outcome = junk.clean([junk.CategoryResult(category=category)])

            self.assertFalse(link.exists(), outcome.errors)
            self.assertTrue(keep.exists(), "清理 junction 时误删了目标目录里的文件")


class UninstallValidationTests(unittest.TestCase):
    def test_missing_uninstaller_is_not_reported_started(self):
        from core.uninstall import ERR_START_FAILED, SoftwareItem, start_uninstall

        with tempfile.TemporaryDirectory(prefix="sightclean-uninstall-") as tmp:
            missing = Path(tmp) / "not-there.exe"
            item = SoftwareItem(
                name="坏掉的应用", version="1", publisher="", install_date="",
                size_bytes=None, install_location=tmp,
                uninstall_string=f'"{missing}" /quiet',
                quiet_uninstall_string="", key_path="HKLM64\\bad", hive="HKLM64",
            )
            result = start_uninstall(item)
            self.assertFalse(result.ok)
            self.assertEqual(result.error, ERR_START_FAILED)

    def test_normal_uninstall_uses_normal_command(self):
        from core.uninstall import SoftwareItem

        item = SoftwareItem(
            name="应用", version="1", publisher="", install_date="",
            size_bytes=None, install_location="",
            uninstall_string="normal.exe",
            quiet_uninstall_string="quiet.exe /S",
            key_path="HKLM64\\app", hive="HKLM64",
        )
        self.assertEqual(item.command, "normal.exe")
        self.assertEqual(item.quiet_command, "quiet.exe /S")


class CacheCorruptionTests(unittest.TestCase):
    def test_bad_timestamp_does_not_break_size_lookup(self):
        from core.config import CacheStore

        with tempfile.TemporaryDirectory(prefix="sightclean-cache-") as tmp:
            cache = CacheStore(Path(tmp) / "cache.json")
            key = cache.size_key(r"C:\x")
            cache._sizes[key] = {"bytes": 1024, "at": "坏时间", "state": "ok"}
            self.assertIsNone(cache.get_size(r"C:\x", 24))


class ProcessIdentityTests(unittest.TestCase):
    def test_wrong_start_time_does_not_kill_reused_pid(self):
        import psutil

        from core.processes import kill_process

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            time.sleep(0.2)
            proc = psutil.Process(child.pid)
            result = kill_process(
                child.pid, "python.exe",
                expected_create_time=proc.create_time() + 30,
                expected_exe=sys.executable,
            )
            self.assertFalse(result.ok)
            self.assertIsNone(child.poll(), "PID 身份不匹配时仍然结束了进程")
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def test_cpu_refresh_preserves_process_metadata(self):
        app = QCoreApplication.instance() or QCoreApplication([])
        from core.processes import ProcessItem
        from ui.process_view import ProcessModel, ProcessRow

        original = ProcessItem(
            123, "demo.exe", "user", 1000, 0.1, r"D:\Demo\demo.exe", "",
            ppid=42, create_time=1000.5, foreground=True, session=2,
        )
        model = ProcessModel()
        model.set_rows([ProcessRow(item=original)])
        model.update_cpu({123: 3.5})
        item = model.rows()[0].item
        self.assertEqual(item.cpu, 3.5)
        self.assertEqual(item.ppid, 42)
        self.assertEqual(item.create_time, 1000.5)
        self.assertTrue(item.foreground)
        self.assertEqual(item.session, 2)
        self.assertIs(app, QCoreApplication.instance())


class ForceProtectionTests(unittest.TestCase):
    def test_windows_subdirectory_is_protected(self):
        from core.force import is_protected

        item = SimpleNamespace(
            name="普通应用", publisher="厂商", install_location=r"C:\Windows\Temp\bad",
            key_path=r"HKLM64\SOFTWARE\Vendor\App",
        )
        allowed, _reason = is_protected(item)
        self.assertFalse(allowed)

    def test_normal_vendor_directory_is_allowed(self):
        from core.force import is_protected

        item = SimpleNamespace(
            name="普通应用", publisher="厂商",
            install_location=r"C:\Program Files\Vendor\App",
            key_path=(r"HKLM64\SOFTWARE\Microsoft\Windows\CurrentVersion"
                      r"\Uninstall\VendorApp"),
        )
        allowed, reason = is_protected(item)
        self.assertTrue(allowed, reason)


class AiBatchStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QT_APP

    def setUp(self):
        from core.ai_client import AIClient
        from core.config import CacheStore, Settings

        self.tmp = tempfile.TemporaryDirectory(prefix="sightclean-ai-fix-")
        self.cache = CacheStore(Path(self.tmp.name) / "cache.json")
        self.settings = Settings()
        self.settings.ai.enabled = True
        self.settings.ai.base_url = "https://example.com"
        self.settings.ai.model = "test"
        self.settings.ai.api_key = "key"
        self.client = AIClient(self.settings, self.cache)
        self.finished = []
        self.client.batchFinished.connect(lambda ok, failed: self.finished.append((ok, failed)))

    def tearDown(self):
        self.client.shutdown()
        self.tmp.cleanup()

    def pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            if until is not None and until():
                return
            time.sleep(0.01)

    def test_all_cached_batch_finishes(self):
        from core.config import CacheStore

        path = r"C:\测试\cached.txt"
        key = CacheStore.ai_key_item("file", path, "cached.txt")
        self.cache.put_ai_json(
            key, {"purpose": "缓存", "impact": "", "risk": "safe"}, "test",
        )
        self.client.explain_many([(path, "cached.txt")])
        self.pump(1, until=lambda: bool(self.finished))
        self.assertEqual(self.finished, [(1, 0)])
        self.assertFalse(self.client._busy)

    def test_mixed_cached_and_network_batch_finishes(self):
        from core.ai_client import AIResult
        from core.config import CacheStore

        cached_path = r"C:\测试\cached.txt"
        key = CacheStore.ai_key_item("file", cached_path, "cached.txt")
        self.cache.put_ai_json(
            key, {"purpose": "缓存", "impact": "", "risk": "safe"}, "test",
        )
        pairs = [(cached_path, "cached.txt"), (r"C:\测试\network.txt", "network.txt")]

        def fake(task):
            return AIResult(
                human_path=task.human_path, name=task.name,
                purpose="网络", impact="", risk="safe",
            )

        with mock.patch("core.ai_client._ask_service", side_effect=fake):
            self.client.explain_many(pairs)
            self.pump(5, until=lambda: bool(self.finished))
        self.assertEqual(self.finished, [(2, 0)])
        self.assertFalse(self.client._busy)

    def test_network_runner_is_daemon_thread(self):
        def slow(task):
            time.sleep(0.2)
            from core.ai_client import AIResult

            return AIResult(task.human_path, task.name, "x", "", "safe")

        with mock.patch("core.ai_client._ask_service", side_effect=slow):
            self.client.explain_many([(r"C:\测试\slow.txt", "slow.txt")])
            self.pump(0.05)
            runner = self.client._thread
            self.assertIsInstance(runner, threading.Thread)
            self.assertTrue(runner.daemon)
            self.pump(1, until=lambda: not runner.is_alive())


class InstalledListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QT_APP

    def test_all_items_include_filtered_rows(self):
        from core.uninstall import SoftwareItem
        from ui.uninstall_view import SoftwareModel, SoftwareRow

        def row(name):
            item = SoftwareItem(
                name=name, version="1", publisher="", install_date="",
                size_bytes=None, install_location="", uninstall_string="unins.exe",
                quiet_uninstall_string="", key_path="HKLM64\\" + name, hive="HKLM64",
            )
            return SoftwareRow(item=item)

        model = SoftwareModel()
        model.set_rows([row("Alpha"), row("Beta")])
        model.set_filter("Alpha")
        self.assertEqual(len(model.rows()), 1)
        self.assertEqual([item.name for item in model.all_items()], ["Alpha", "Beta"])


class AsyncPageLoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QT_APP

    def test_software_activate_does_not_block_on_enumeration(self):
        from core.config import Settings
        from core.knowledge import KnowledgeBase
        from ui.uninstall_view import UninstallView

        started = threading.Event()
        release = threading.Event()

        def slow_enumerate(*_args, **_kwargs):
            started.set()
            release.wait(2)
            return [], []

        page = UninstallView(KnowledgeBase.build({"rules": []}), Settings())
        with mock.patch("ui.uninstall_view.enumerate_installed", side_effect=slow_enumerate):
            before = time.time()
            page.activate()
            elapsed = time.time() - before
            self.assertLess(elapsed, 0.5, "页面激活仍在 UI 线程执行慢扫描")
            self.assertTrue(started.wait(0.5))
            release.set()
            end = time.time() + 2
            while page._load_worker is not None and time.time() < end:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertIsNone(page._load_worker)


class SelectionTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QT_APP

    def test_hidden_checked_items_are_described_as_executed(self):
        from core.fs_browser import FsEntry
        from ui.main_window import MainWindow

        entry = FsEntry(
            human_path=r"C:\x\hidden.tmp", long_path=r"\\?\C:\x\hidden.tmp",
            name="hidden.tmp", is_dir=False, size=1, mtime=0.0,
            kind_label="文件", checked=True,
        )
        model = mock.Mock()
        model.checked_entries.return_value = [entry]
        model.check_state.return_value = Qt.Unchecked
        model.is_filtered.return_value = True
        model.hidden_checked_count.return_value = 1
        owner = SimpleNamespace(
            model=model, header=mock.Mock(), act_delete=QPushButton(),
            lbl_selection=SimpleNamespace(setText=mock.Mock()), _busy=False,
        )
        MainWindow._on_checked_changed(owner)
        text = owner.lbl_selection.setText.call_args.args[0]
        self.assertIn("隐藏项也会执行", text)
        self.assertNotIn("仅对可见项生效", text)


class RecalculateScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QT_APP

    def test_drive_rows_are_not_scanned(self):
        from core.fs_browser import FsEntry
        from ui.main_window import MainWindow

        drive = FsEntry(
            human_path="D:\\", long_path="\\\\?\\D:\\", name="D:\\",
            is_dir=True, size=None, mtime=0.0, kind_label="磁盘",
            deletable=False,
        )
        folder = FsEntry(
            human_path="D:\\cache", long_path=r"\\?\D:\cache", name="cache",
            is_dir=True, size=None, mtime=0.0, kind_label="文件夹",
        )
        model = mock.Mock()
        model.entries.return_value = [drive, folder]
        engine = mock.Mock()
        owner = SimpleNamespace(
            model=model, size_engine=engine, _generation=0,
            _pending_sizes=set(), act_stop=SimpleNamespace(setEnabled=mock.Mock()),
            _toast=mock.Mock(),
        )
        MainWindow._recalc_sizes(owner)
        engine.enqueue.assert_called_once()
        self.assertEqual(engine.enqueue.call_args.args[0], [folder.human_path])


class RecycleGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QT_APP

    def test_size_warning_is_used_for_recycle_mode(self):
        from core.delete import DeleteItem, Mode
        from ui.main_window import MainWindow
        from PySide6.QtWidgets import QMessageBox

        settings = SimpleNamespace(confirm_recycle=False, recycle_warn_over_gb=1)
        owner = SimpleNamespace(settings=settings)
        items = [DeleteItem(
            human_path=r"D:\large.bin", display="large.bin", is_dir=False,
            size=2 * 1024 ** 3,
        )]
        with mock.patch(
            "ui.main_window.QMessageBox.question", return_value=QMessageBox.Yes,
        ) as question:
            result = MainWindow._confirm_extra_recycle(owner, items, Mode.RECYCLE)
        self.assertTrue(result)
        self.assertIn("2.0 GB", question.call_args.args[2])

    def test_confirm_recycle_setting_adds_one_more_ask(self):
        from core.delete import DeleteItem, Mode
        from ui.main_window import MainWindow
        from PySide6.QtWidgets import QMessageBox

        settings = SimpleNamespace(confirm_recycle=True, recycle_warn_over_gb=100)
        owner = SimpleNamespace(settings=settings)
        items = [DeleteItem(
            human_path=r"D:\small.bin", display="small.bin", is_dir=False, size=10,
        )]
        with mock.patch(
            "ui.main_window.QMessageBox.question", return_value=QMessageBox.Yes,
        ) as question:
            result = MainWindow._confirm_extra_recycle(owner, items, Mode.RECYCLE)
        self.assertTrue(result)
        question.assert_called_once()


class AppPathsCacheTests(unittest.TestCase):
    def test_resolve_is_cached_after_first_probe(self):
        import app_paths

        with tempfile.TemporaryDirectory(prefix="sightclean-paths-") as tmp:
            old = getattr(app_paths, "_RESOLVE_CACHE", None)
            app_paths._RESOLVE_CACHE = None
            try:
                with mock.patch.object(app_paths, "exe_dir", return_value=Path(tmp)), \
                     mock.patch.object(app_paths, "resource_dir", return_value=Path(tmp) / "resources"), \
                     mock.patch.object(app_paths, "_writable", return_value=True) as writable:
                    first = app_paths.resolve()
                    second = app_paths.resolve()
                self.assertIs(first, second)
                self.assertEqual(writable.call_count, 1)
            finally:
                app_paths._RESOLVE_CACHE = old


class CloseSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QT_APP

    def test_close_is_ignored_while_destructive_task_runs(self):
        from ui.main_window import MainWindow

        owner = SimpleNamespace(
            _busy=True, _background_workers=set(), _toast=mock.Mock(),
        )
        event = mock.Mock()
        MainWindow.closeEvent(owner, event)
        event.ignore.assert_called_once()
        event.accept.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
