"""卸载残余：文件夹 + 注册表扫描，一键清理（注册表先备份）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
import winreg
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import leftover
from core.leftover import KIND_FOLDER, KIND_REGISTRY, Leftover, clean, scan, scan_folders
from ui.leftover_dialog import LeftoverDialog


class FolderScanTests(unittest.TestCase):
    def test_finds_install_dir(self):
        tmp = tempfile.mkdtemp(prefix="lo-")
        items = scan_folders(tmp, "", "")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, KIND_FOLDER)
        self.assertEqual(items[0].path, tmp)

    def test_missing_dir_not_listed(self):
        items = scan_folders(r"C:\definitely-not-here-zzz", "", "")
        self.assertEqual(items, [])

    def test_appdata_dirs(self):
        base = tempfile.mkdtemp(prefix="lo-app-")
        brand = "TestBrand"
        (Path(base) / brand).mkdir()
        old = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = base
        try:
            items = scan_folders("", brand, "")
        finally:
            if old is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old
        self.assertTrue(any(brand in item.path for item in items))


class OrphanTests(unittest.TestCase):
    """「只删登记不删文件」的软件：Program Files 里还在，卸载列表里没了。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="orphan-"))
        self.name = f"_dch_orphan_{uuid.uuid4().hex[:6]}"
        target = self.root / self.name
        target.mkdir()
        (target / "app.exe").write_bytes(b"x")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_finds_unregistered_dir(self):
        from core.leftover import scan_orphans

        items = scan_orphans(roots=[str(self.root)])
        self.assertTrue(any(item.name == self.name for item in items), items)

    def test_registered_dir_excluded(self):
        """卸载登记里有的目录不该报成孤儿。"""
        sub = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{self.name}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, sub) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, self.name)
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(self.root / self.name))
        try:
            from core.leftover import scan_orphans

            items = scan_orphans(roots=[str(self.root)])
            self.assertFalse(any(item.name == self.name for item in items), items)
        finally:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except OSError:
                pass

    def test_empty_dir_skipped(self):
        from core.leftover import scan_orphans

        empty = self.root / f"_dch_empty_{uuid.uuid4().hex[:6]}"
        empty.mkdir()
        items = scan_orphans(roots=[str(self.root)])
        self.assertFalse(any(item.name == empty.name for item in items))

    def test_system_dir_skipped(self):
        from core.leftover import scan_orphans

        for name in ("Windows Defender", "Microsoft", "Common Files"):
            (self.root / name).mkdir(exist_ok=True)
            (self.root / name / "x.dll").write_bytes(b"x")
        items = scan_orphans(roots=[str(self.root)])
        names = {item.name for item in items}
        self.assertNotIn("Microsoft", names)
        self.assertNotIn("Common Files", names)


class ShortcutTests(unittest.TestCase):
    """桌面/开始菜单的快捷方式也要算残留 —— 图标还在，用户会以为没卸掉。"""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="sc-home-")
        self.desktop = Path(self.home) / "Desktop"
        self.desktop.mkdir()
        self._old = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = self.home

    def tearDown(self):
        if self._old is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = self._old

    def make_lnk(self, name):
        path = self.desktop / f"{name}.lnk"
        path.write_bytes(b"fake")
        return path

    def test_finds_desktop_shortcut(self):
        from core.leftover import scan_shortcuts

        self.make_lnk("测试软件A")
        items = scan_shortcuts("测试软件A", "")
        self.assertTrue(any(item.label == "桌面" for item in items))
        self.assertTrue(any("测试软件A.lnk" in item.path for item in items))

    def test_does_not_match_longer_name(self):
        """「微信」不该把「微信开发者工具」也当成残留。"""
        from core.leftover import scan_shortcuts

        self.make_lnk("微信开发者工具")
        items = scan_shortcuts("微信", "")
        self.assertFalse(any("开发者" in item.path for item in items), items)

    def test_matches_name_with_suffix(self):
        from core.leftover import scan_shortcuts

        self.make_lnk("测试软件B - 快捷方式")
        items = scan_shortcuts("测试软件B", "")
        self.assertTrue(items)

    def test_scan_includes_shortcuts(self):
        from core.leftover import KIND_FOLDER, scan

        self.make_lnk("测试软件C")
        items = scan("", "", "测试软件C")
        self.assertTrue(any(item.kind == KIND_FOLDER and item.path.endswith(".lnk")
                            for item in items), items)

    def test_clean_handles_lnk_as_file(self):
        """快捷方式是文件，清理时不能当目录删。"""
        from unittest import mock

        from core.leftover import Leftover, clean

        lnk = self.make_lnk("测试软件D")
        item = Leftover(KIND_FOLDER, "桌面", str(lnk), name="测试软件D.lnk")
        seen = {}

        def fake_delete(delete_item, mode):
            seen["is_dir"] = delete_item.is_dir
            return type("R", (), {"ok": True, "error": None})()

        with mock.patch.object(leftover, "delete_one", fake_delete):
            clean([item])
        self.assertFalse(seen["is_dir"], "lnk 是文件，is_dir 该是 False")


class RegistryScanTests(unittest.TestCase):
    """在自己造的测试键上验证，不碰真软件。"""

    def setUp(self):
        self.name = f"_dch_lo_{uuid.uuid4().hex[:8]}"
        self.sub = rf"SOFTWARE\{self.name}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.sub):
            pass

    def tearDown(self):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.sub)
        except OSError:
            pass

    def test_finds_software_key(self):
        items = leftover.scan_registry(self.name, "", "")
        self.assertTrue(items, "应该能找到 HKCU\\SOFTWARE\\<名字>")
        self.assertEqual(items[0].kind, KIND_REGISTRY)
        self.assertIn(self.name, items[0].path)
        self.assertEqual(items[0].label, "软件配置")

    def test_unrelated_name_not_found(self):
        items = leftover.scan_registry("完全不相关的名字_zzz", "", "")
        self.assertEqual(items, [])

    def test_scan_covers_both(self):
        tmp = tempfile.mkdtemp(prefix="lo-both-")
        items = scan(tmp, "", self.name)
        kinds = {item.kind for item in items}
        self.assertIn(KIND_FOLDER, kinds)
        self.assertIn(KIND_REGISTRY, kinds)


class CleanTests(unittest.TestCase):
    def setUp(self):
        self.name = f"_dch_cl_{uuid.uuid4().hex[:8]}"
        self.sub = rf"SOFTWARE\{self.name}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.sub):
            pass
        self.full = rf"HKEY_CURRENT_USER\{self.sub}"
        self.dir = Path(tempfile.mkdtemp(prefix="lo-clean-"))

    def tearDown(self):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.sub)
        except OSError:
            pass

    def test_cleans_registry_with_backup(self):
        item = Leftover(KIND_REGISTRY, "软件配置", self.full, name=self.name)
        report = clean([item])
        self.assertEqual(report.ok_count, 1, [r.error for r in report.items])
        self.assertIsNotNone(report.backup, "清注册表必须先备份")
        with self.assertRaises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.sub, 0, winreg.KEY_READ)

    def test_backup_failure_aborts_everything(self):
        item = Leftover(KIND_REGISTRY, "软件配置", self.full, name=self.name)
        from unittest import mock

        with mock.patch.object(leftover, "export_keys", return_value=(None, "备份失败")):
            report = clean([item])
        self.assertEqual(report.ok_count, 0)
        self.assertEqual(report.fail_count, 1)
        # 键还在
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.sub, 0, winreg.KEY_READ):
            pass

    def test_empty_returns_quietly(self):
        report = clean([])
        self.assertEqual(report.items, [])
        self.assertEqual(report.summary, "这次没有要清理的残余")

    def test_summary_and_log(self):
        item = Leftover(KIND_REGISTRY, "软件配置", self.full, name=self.name)
        report = clean([item])
        self.assertIn("清掉 1 项", report.summary)
        self.assertIn("卸载残余清理", report.as_log_text())


class RepeatCleanTests(unittest.TestCase):
    """没勾的项清完之后还要能再清一次 —— 别一键就把路堵死。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def build(self, count=3):
        folder = Path(tempfile.mkdtemp(prefix="repeat-"))
        files = []
        for index in range(count):
            path = folder / f"f{index}.pf"
            path.write_bytes(b"x" * 64)
            files.append(path)
        items = [Leftover(KIND_FOLDER, "预读取数据", str(path), name=path.name, reason="测试")
                 for path in files]
        dialog = LeftoverDialog(items)
        dialog.show()          # 不 show 的话 isVisible() 恒为 False
        return dialog, files, folder

    def wait_done(self, dialog, rounds=400):
        import time

        for _ in range(rounds):
            self.app.processEvents()
            if dialog.report is not None:
                return True
            time.sleep(0.01)
        return False

    def test_buttons_stay_after_clean(self):
        from PySide6.QtCore import Qt

        dialog, _files, folder = self.build()
        try:
            for _identity, row in list(dialog._rows.items())[:2]:
                dialog.table.item(row, 0).setCheckState(Qt.Checked)
            dialog._do_clean()
            self.assertTrue(self.wait_done(dialog), "清理没跑完")
            self.assertFalse(dialog.btn_clean.isHidden(), "按钮不该被藏起来")
        finally:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)

    def test_remaining_item_can_be_cleaned_again(self):
        from PySide6.QtCore import Qt

        dialog, files, folder = self.build(2)
        try:
            first_identity, first_row = list(dialog._rows.items())[0]
            last_identity, last_row = list(dialog._rows.items())[1]
            dialog.table.item(first_row, 0).setCheckState(Qt.Checked)
            dialog._do_clean()
            self.assertTrue(self.wait_done(dialog), "第一次清理没跑完")
            self.assertEqual(dialog.report.ok_count, 1)

            # 剩下的那个还能勾
            cell = dialog.table.item(last_row, 0)
            self.assertTrue(bool(cell.flags() & Qt.ItemIsUserCheckable))
            cell.setCheckState(Qt.Checked)
            self.assertEqual(len(dialog.chosen()), 1)
            self.assertTrue(dialog.btn_clean.isEnabled())

            dialog.report = None
            dialog._do_clean()
            self.assertTrue(self.wait_done(dialog), "第一次清理没跑完")
            self.assertEqual(dialog.report.ok_count, 1)
            self.assertFalse(any(path.exists() for path in files), "两个都该删掉")
        finally:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)

    def test_table_stays_usable_after_clean(self):
        """清理会把整个表格禁用，完事必须放开 —— 否则事后什么都勾不动。"""
        from PySide6.QtCore import Qt

        dialog, _files, folder = self.build(1)
        try:
            _identity, row = list(dialog._rows.items())[0]
            dialog.table.item(row, 0).setCheckState(Qt.Checked)
            dialog._do_clean()
            self.assertTrue(self.wait_done(dialog), "清理没跑完")
            self.assertTrue(dialog.table.isEnabled(), "清理后表格该恢复可用")
        finally:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)

    def test_ai_items_checkable_after_clean(self):
        """先清理过一批，AI 后来加的项仍然要能勾。"""
        from PySide6.QtCore import Qt

        from core.ai_leftover import Suspect

        dialog, _files, folder = self.build(1)
        new_dir = Path(tempfile.mkdtemp(prefix="ai-new-"))
        try:
            _identity, row = list(dialog._rows.items())[0]
            dialog.table.item(row, 0).setCheckState(Qt.Checked)
            dialog._do_clean()
            self.assertTrue(self.wait_done(dialog))

            (new_dir / "x.bin").write_bytes(b"y" * 128)
            dialog._on_ai_done([Suspect("folder", str(new_dir), "新残留",
                                       verdict="leftover", reason="测试", confidence="high")], None)
            self.app.processEvents()

            last_identity, last_row = list(dialog._rows.items())[-1]
            cell = dialog.table.item(last_row, 0)
            self.assertTrue(bool(cell.flags() & Qt.ItemIsUserCheckable),
                            "AI 加的项要能勾")
            cell.setCheckState(Qt.Checked)
            self.assertEqual(len(dialog.chosen()), 1)
            self.assertTrue(dialog.btn_clean.isEnabled())
        finally:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)
            shutil.rmtree(new_dir, ignore_errors=True)

    def test_cleaned_item_not_checkable(self):
        from PySide6.QtCore import Qt

        dialog, _files, folder = self.build(1)
        try:
            identity, row = list(dialog._rows.items())[0]
            dialog.table.item(row, 0).setCheckState(Qt.Checked)
            dialog._do_clean()
            self.assertTrue(self.wait_done(dialog), "清理没跑完")
            cell = dialog.table.item(row, 0)
            self.assertFalse(bool(cell.flags() & Qt.ItemIsUserCheckable),
                             "清掉的项不该还能再勾")
        finally:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)


class BulkInsertTests(unittest.TestCase):
    """AI 一次加几十上百项时不能卡死，也不能一直显示「计算中…」。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="bulk-"))
        first = self.base / "seed.pf"
        first.write_bytes(b"x" * 64)
        self.dialog = LeftoverDialog([
            Leftover(KIND_FOLDER, "预读取数据", str(first), name="seed.pf", reason="测试")
        ])
        self.dialog.resize(900, 500)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        import shutil

        self.dialog.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def make_suspects(self, count):
        from core.ai_leftover import Suspect

        suspects = []
        for index in range(count):
            folder = self.base / f"app{index}"
            folder.mkdir(exist_ok=True)
            (folder / "x.bin").write_bytes(b"y" * (64 * (index + 1)))
            suspects.append(Suspect("folder", str(folder), f"app{index}",
                                    verdict="leftover", reason="测试", confidence="high"))
        return suspects

    def test_bulk_insert_is_fast(self):
        import time

        suspects = self.make_suspects(80)
        started = time.time()
        self.dialog._on_ai_done(suspects, None)
        elapsed = time.time() - started
        self.assertLess(elapsed, 2.0, f"插入 80 项花了 {elapsed:.2f} 秒，太慢")
        self.assertEqual(self.dialog.table.rowCount(), 2 + 80)

    def test_scrollbar_usable_after_bulk(self):
        self.dialog._on_ai_done(self.make_suspects(60), None)
        self.app.processEvents()
        bar = self.dialog.table.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0, "加完就没有滚动条了")
        bar.setValue(bar.maximum())
        self.assertEqual(bar.value(), bar.maximum())

    def test_new_items_get_sized(self):
        import time

        self.dialog._on_ai_done(self.make_suspects(6), None)
        for _ in range(200):
            self.app.processEvents()
            time.sleep(0.01)
        pending = 0
        for row in range(self.dialog.table.rowCount()):
            cell = self.dialog.table.item(row, 2)
            if cell and "计算中" in cell.text():
                pending += 1
        self.assertEqual(pending, 0, "还有项卡在「计算中」")

    def test_new_items_unchecked(self):
        self.dialog._on_ai_done(self.make_suspects(5), None)
        self.assertEqual(len(self.dialog.chosen()), 0, "AI 加的项也该默认不勾")

    def test_duplicate_paths_skipped(self):
        suspects = self.make_suspects(3)
        self.dialog._on_ai_done(suspects, None)
        rows_after_first = self.dialog.table.rowCount()
        self.dialog._on_ai_done(suspects, None)
        self.assertEqual(self.dialog.table.rowCount(), rows_after_first,
                         "同一批再判断一次不该重复加")


class KindScopeTests(unittest.TestCase):
    """入口限定只扫文件夹，对话框里再点 AI 也得守住这个范围。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def make_dialog(self, kinds):
        from unittest import mock as _mock

        class FakeAI:
            def __init__(self):
                from core.config import AISettings

                self.settings = AISettings(model="m", base_url="http://x", api_key="k")

            def is_configured(self):
                return True

        folder = Path(tempfile.mkdtemp(prefix="kind-"))
        target = folder / "x"
        target.mkdir()
        dialog = LeftoverDialog(
            [Leftover(KIND_FOLDER, "测试", str(target), name="x")],
            ai=FakeAI(), installed=["某软件"], kinds=kinds,
        )
        return dialog, folder

    def test_kinds_stored(self):
        dialog, folder = self.make_dialog(("folder",))
        try:
            self.assertEqual(dialog._kinds, ("folder",))
        finally:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)

    def test_ai_scan_uses_dialog_kinds(self):
        from unittest import mock

        dialog, folder = self.make_dialog(("folder",))
        try:
            with mock.patch("core.ai_leftover.collect_suspects", return_value=[]) as spy:
                dialog._do_ai()
            self.assertTrue(spy.called, "AI 扫描没被触发起")
            self.assertEqual(spy.call_args.kwargs.get("kinds"), ("folder",),
                             "对话框必须沿用入口限定的范围")
        finally:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)

    def test_registry_dialog_keeps_registry_scope(self):
        from unittest import mock

        dialog, folder = self.make_dialog(("registry",))
        try:
            with mock.patch("core.ai_leftover.collect_suspects", return_value=[]) as spy:
                dialog._do_ai()
            self.assertEqual(spy.call_args.kwargs.get("kinds"), ("registry",))
        finally:
            import shutil

            shutil.rmtree(folder, ignore_errors=True)


class RecursiveRegistryTests(unittest.TestCase):
    """残留清理要能连子键一起删 —— 用户勾的是一整棵子树。"""

    def setUp(self):
        self.name = f"_dch_rec_{uuid.uuid4().hex[:8]}"
        self.sub = rf"SOFTWARE\{self.name}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.sub) as key:
            with winreg.CreateKey(key, "child") as child:
                winreg.SetValueEx(child, "v", 0, winreg.REG_SZ, "x")
                with winreg.CreateKey(child, "deep"):
                    pass

    def tearDown(self):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.sub)
        except OSError:
            pass

    def test_tree_delete_removes_children_too(self):
        from core.registry import delete_key_tree

        ok, error = delete_key_tree(rf"HKEY_CURRENT_USER\{self.sub}")
        self.assertTrue(ok, error)
        with self.assertRaises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.sub, 0, winreg.KEY_READ)

    def test_clean_uses_tree_delete(self):
        from core.leftover import CleanReport

        item = Leftover(KIND_REGISTRY, "软件配置", rf"HKEY_CURRENT_USER\{self.sub}", name=self.name)
        report = clean([item])
        self.assertEqual(report.ok_count, 1, [r.error for r in report.items])
        with self.assertRaises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.sub, 0, winreg.KEY_READ)

    def test_snapshot_clean_still_refuses_children(self):
        """注册表模块的快照清理仍然只删空键 —— 两边规矩不一样。"""
        from core.registry import delete_key

        result = delete_key(rf"HKEY_CURRENT_USER\{self.sub}")
        self.assertFalse(result.ok)
        self.assertIn("子键", result.error or "")


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def build(self):
        from ui.leftover_dialog import LeftoverDialog

        items = [
            Leftover(KIND_FOLDER, "安装位置", r"C:\Program Files\X", name="X", detail="目录还在"),
            Leftover(KIND_REGISTRY, "卸载登记", r"HKCU\SOFTWARE\X", name="X", detail="登记项还在"),
        ]
        return LeftoverDialog(items), items

    def test_lists_both_groups(self):
        dialog, items = self.build()
        # 两个分组标题行 + 两个条目行
        self.assertEqual(dialog.table.rowCount(), 4)
        titles = [dialog.table.item(r, 1).text() for r in (0, 2)]
        self.assertIn("文件残留", titles[0])
        self.assertIn("注册表残留", titles[1])

    def test_unchecked_by_default(self):
        """参考 Geek 的做法：默认一个都不勾，勾了才清。"""
        dialog, _items = self.build()
        self.assertEqual(len(dialog.chosen()), 0)
        self.assertFalse(dialog.btn_clean.isEnabled())

    def test_check_updates_summary(self):
        from PySide6.QtCore import Qt

        dialog, _items = self.build()
        self.assertEqual(len(dialog.chosen()), 0)
        dialog.table.item(1, 0).setCheckState(Qt.Checked)     # 第一个文件残留
        self.assertEqual(len(dialog.chosen()), 1)
        self.assertIn("选中 1 项", dialog.summary.text())
        self.assertIn("文件夹", dialog.summary.text())


class InlineProgressTests(unittest.TestCase):
    """点清理后在本页跑进度条，不弹任何对话框。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def build(self):
        from ui.leftover_dialog import LeftoverDialog

        items = [
            Leftover(KIND_FOLDER, "安装位置", r"C:\Program Files\X", name="X", detail="目录"),
            Leftover(KIND_REGISTRY, "软件配置", r"HKCU\SOFTWARE\X", name="X", detail="设置"),
        ]
        return LeftoverDialog(items)

    def test_progress_hidden_until_clicked(self):
        dialog = self.build()
        self.assertFalse(dialog.progress.isVisible())

    @staticmethod
    def decision(dialog):
        from ui.confirm_flow import FlowDecision

        return FlowDecision(items=dialog.chosen())

    def check_all(self, dialog):
        from PySide6.QtCore import Qt

        for identity, row in dialog._rows.items():
            dialog.table.item(row, 0).setCheckState(Qt.Checked)

    def test_click_disables_buttons_and_shows_progress(self):
        from unittest import mock

        dialog = self.build()
        self.check_all(dialog)
        with mock.patch("ui.leftover_dialog.make_clean_worker") as factory:
            factory.return_value.signals.progress.connect = lambda *a: None
            factory.return_value.signals.finished.connect = lambda *a: None
            dialog._do_clean()
        self.assertFalse(dialog.btn_clean.isEnabled())
        self.assertFalse(dialog.btn_close.isEnabled())
        self.assertFalse(dialog.progress.isHidden(), "进度条该露出来了")
        self.assertEqual(dialog.progress.maximum(), 2)

    def test_no_dialog_pops(self):
        """全程不该弹任何 QMessageBox。"""
        from unittest import mock

        dialog = self.build()
        self.check_all(dialog)
        with mock.patch("ui.leftover_dialog.make_clean_worker") as factory:
            factory.return_value.signals.progress.connect = lambda *a: None
            factory.return_value.signals.finished.connect = lambda *a: None
            with mock.patch("PySide6.QtWidgets.QMessageBox.information") as info, \
                 mock.patch("PySide6.QtWidgets.QMessageBox.warning") as warn, \
                 mock.patch("PySide6.QtWidgets.QMessageBox.question") as question:
                dialog._do_clean()
                dialog._on_cleaned(self.report_for(dialog, {0, 1}))
        self.assertFalse(info.called)
        self.assertFalse(warn.called)
        self.assertFalse(question.called)

    @staticmethod
    def report_for(dialog, ok_indexes):
        from core.leftover import CleanReport, CleanResult

        items = []
        for index, item in enumerate(dialog._items):
            done = index in ok_indexes
            items.append(CleanResult(item, done, None if done else "没权限"))
        return CleanReport(items=items)

    @staticmethod
    def report_with(ok: int, fail: int):
        from core.leftover import CleanReport, CleanResult

        items = []
        for index in range(ok):
            items.append(CleanResult(Leftover(KIND_FOLDER, "x", f"C:\\a{index}"), True))
        for index in range(fail):
            items.append(CleanResult(Leftover(KIND_REGISTRY, "x", f"HKCU\\b{index}"), False, "没权限"))
        return CleanReport(items=items)

    def test_result_shown_in_page_not_popup(self):
        dialog = self.build()
        dialog._on_cleaned(self.report_for(dialog, {0, 1}))
        self.assertIn("清理完成", dialog.status.text())
        self.assertEqual(dialog.progress.format(), "完成")

    def test_failure_reason_goes_into_table(self):
        from PySide6.QtCore import Qt
        from unittest import mock

        dialog = self.build()
        self.check_all(dialog)
        with mock.patch("ui.leftover_dialog.make_clean_worker") as factory:
            factory.return_value.signals.progress.connect = lambda *a: None
            factory.return_value.signals.finished.connect = lambda *a: None
            dialog._do_clean()
        dialog._on_cleaned(self.report_for(dialog, set()))
        self.assertIn("没动", dialog.status.text())
        notes = []
        for identity, row in dialog._rows.items():
            cell = dialog.table.item(row, 3)      # 第 3 列是「为什么列为残留」
            notes.append(cell.text() if cell else "")
        self.assertTrue(notes and all("没权限" in note for note in notes), notes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
