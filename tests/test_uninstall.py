"""软件卸载：解析、去重、系统组件判定、命令选择、报告结构、页面勾选规则。"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt, QTimer

from core.config import Settings
from core.uninstall import (
    ERR_NO_COMMAND,
    SoftwareItem,
    UninstallReport,
    UninstallResult,
    _build_item,
    dedupe,
    looks_system,
    start_uninstall,
)

def make_item(name="某个软件", version="1.0", publisher="某个公司", **kwargs) -> SoftwareItem:
    return SoftwareItem(
        name=name,
        version=version,
        publisher=publisher,
        install_date=kwargs.get("install_date", ""),
        size_bytes=kwargs.get("size_bytes"),
        install_location=kwargs.get("install_location", ""),
        uninstall_string=kwargs.get("uninstall_string", "uninst.exe"),
        quiet_uninstall_string=kwargs.get("quiet_uninstall_string", ""),
        key_path=kwargs.get("key_path", f"HKLM64\\{name}"),
        hive="HKLM64",
    )

class BuildItemTests(unittest.TestCase):
    def test_estimated_size_is_kb(self):
        item = _build_item({"DisplayName": "甲", "EstimatedSize": 2048}, "HKLM64", "k")
        self.assertEqual(item.size_bytes, 2048 * 1024)

    def test_bad_size_becomes_none(self):
        item = _build_item({"DisplayName": "甲", "EstimatedSize": "不是数字"}, "HKLM64", "k")
        self.assertIsNone(item.size_bytes)

    def test_install_date_formatted(self):
        item = _build_item({"DisplayName": "甲", "InstallDate": "20240115"}, "HKLM64", "k")
        self.assertEqual(item.install_date, "2024-01-15")

    def test_missing_display_name_skipped(self):
        self.assertIsNone(_build_item({}, "HKLM64", "k"))
        self.assertIsNone(_build_item({"DisplayName": "   "}, "HKLM64", "k"))

    def test_quiet_wins_over_normal(self):
        item = _build_item(
            {"DisplayName": "甲", "UninstallString": "slow.exe", "QuietUninstallString": "quick.exe /S"},
            "HKLM64", "k",
        )
        self.assertEqual(item.command, "quick.exe /S")

    def test_no_command_means_cannot_uninstall(self):
        item = _build_item({"DisplayName": "甲"}, "HKLM64", "k")
        self.assertFalse(item.can_uninstall)
        self.assertEqual(item.command, "")

class DedupeTests(unittest.TestCase):
    def test_same_name_and_version_collapsed(self):
        a = make_item(name="同一个", version="1.0", key_path="HKLM64\\a")
        b = make_item(name="同一个", version="1.0", key_path="HKLM32\\b")
        self.assertEqual(len(dedupe([a, b])), 1)

    def test_different_version_kept(self):
        a = make_item(name="同名", version="1.0")
        b = make_item(name="同名", version="2.0")
        self.assertEqual(len(dedupe([a, b])), 2)

    def test_uninstallable_copy_replaced_by_usable_one(self):
        dead = make_item(name="同一个", version="1.0", uninstall_string="", quiet_uninstall_string="")
        alive = make_item(name="同一个", version="1.0", uninstall_string="uninst.exe")
        result = dedupe([dead, alive])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].can_uninstall)

class SystemComponentTests(unittest.TestCase):
    def test_vcredist(self):
        self.assertTrue(looks_system("Microsoft Visual C++ 2015 Redistributable"))

    def test_dotnet(self):
        self.assertTrue(looks_system("Microsoft .NET Framework 4.8"))

    def test_driver(self):
        self.assertTrue(looks_system("Intel Graphics Driver"))

    def test_kb_patch(self):
        self.assertTrue(looks_system("KB5031224"))

    def test_system_component_flag(self):
        self.assertTrue(looks_system("随便什么", system_component=True))

    def test_normal_app_not_system(self):
        self.assertFalse(looks_system("7-Zip"))
        self.assertFalse(looks_system("WeChat", "Tencent"))

class ReportTests(unittest.TestCase):
    def test_counts_and_text(self):
        now = time.time()
        report = UninstallReport(
            items=[
                UninstallResult("k1", "甲", True, "uninst.exe", None, now),
                UninstallResult("k2", "乙", False, "", ERR_NO_COMMAND, now),
            ],
            started=now - 1, finished=now,
        )
        self.assertEqual(report.ok_count, 1)
        self.assertEqual(report.fail_count, 1)
        self.assertIn("已交给卸载程序 1 个", report.summary)
        self.assertIn("没起来", report.summary)
        text = report.as_log_text()
        self.assertIn("已调起 | 甲", text)
        self.assertIn("失败 | 乙", text)
        self.assertIn("不删文件", text)

    def test_start_without_command_fails_cleanly(self):
        item = make_item(uninstall_string="", quiet_uninstall_string="")
        result = start_uninstall(item)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, ERR_NO_COMMAND)

class SoftwareKnowledgeTests(unittest.TestCase):
    def setUp(self):
        from core.knowledge import load_from_files

        root = Path(__file__).resolve().parent.parent
        self.kb = load_from_files(root / "resources" / "default_knowledge.json", root / "知识库.json")

    def test_vcredist_is_forbidden(self):
        result = self.kb.match_software("Microsoft Visual C++ 2015 Redistributable", "Microsoft")
        self.assertTrue(result.known)
        self.assertEqual(result.risk, "forbidden")

    def test_normal_app_unlisted(self):
        result = self.kb.match_software("7-Zip 24.09", "Igor Pavlov")
        self.assertFalse(result.known)
        self.assertEqual(result.risk, "unlisted")

    def test_file_rules_do_not_leak_into_software(self):
        # 文件规则里的 \windows\ 不该出现在软件匹配里
        result = self.kb.match_software("Windows 系统文件", "")
        self.assertFalse(result.known)

    def test_kind_counts(self):
        self.assertEqual(self.kb.counts.get("software"), 6)

class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def build(self):
        from ui.uninstall_view import SoftwareModel, SoftwareRow

        model = SoftwareModel()
        model.set_rows([
            SoftwareRow(item=make_item(name="正常软件"), effect="随便一个软件", risk="safe"),
            SoftwareRow(item=make_item(name="危险组件"), effect="运行库", risk="forbidden"),
            SoftwareRow(item=make_item(name="没命令的", uninstall_string="", quiet_uninstall_string=""),
                        effect="", risk="unlisted", locked=True),
        ])
        return model

    def test_columns(self):
        from ui.uninstall_view import HEADERS

        model = self.build()
        self.assertEqual(model.columnCount(), 8)
        self.assertEqual(HEADERS[0], "软件名")
        self.assertEqual(HEADERS[-1], "是否卸载")

    def test_red_still_checkable(self):
        from ui.uninstall_view import COL_CHECK

        model = self.build()
        flags = model.flags(model.index(1, COL_CHECK))
        self.assertTrue(flags & Qt.ItemIsUserCheckable, "红色软件也该能勾")

    def test_no_command_still_checkable(self):
        """没有卸载命令的也能勾 —— 勾了走「扫残留」那条路。"""
        from ui.uninstall_view import COL_CHECK

        model = self.build()
        flags = model.flags(model.index(2, COL_CHECK))
        self.assertTrue(flags & Qt.ItemIsUserCheckable, "没卸载命令的也要能勾")
        model.setData(model.index(2, COL_CHECK), Qt.Checked, Qt.CheckStateRole)
        self.assertEqual(len(model.checked_rows()), 1)

    def test_check_state(self):
        from ui.uninstall_view import COL_CHECK

        model = self.build()
        model.setData(model.index(0, COL_CHECK), Qt.Checked, Qt.CheckStateRole)
        self.assertEqual(model.check_state(), Qt.PartiallyChecked)
        model.set_all_checked(True)
        self.assertEqual(model.check_state(), Qt.Checked)
        self.assertEqual(len(model.checked_rows()), 3, "三项都能勾了")

    def test_ai_can_only_raise_risk(self):
        model = self.build()
        model.update_ai("HKLM64\\危险组件", "AI 说是普通软件", "", "safe", "AI 判断，请确认")
        row = model.rows()[1]
        self.assertEqual(row.risk, "forbidden", "AI 不能把红的降级")
        model.update_ai("HKLM64\\正常软件", "AI 说别卸", "", "forbidden", "AI 判断，请确认")
        self.assertEqual(model.rows()[0].risk, "forbidden", "AI 可以升级")

class MsiCommandTests(unittest.TestCase):
    """MsiExec 的 /I 是「修改安装」，不是卸载；没命令但有产品码的要能自己拼。"""

    def make(self, **kwargs):
        from core.uninstall import SoftwareItem

        base = dict(
            name="测试", version="1", publisher="", install_date="",
            size_bytes=None, install_location="", uninstall_string="",
            quiet_uninstall_string="", key_path="HKLM64\\x", hive="HKLM64",
        )
        base.update(kwargs)
        return SoftwareItem(**base)

    def test_modify_becomes_uninstall(self):
        item = self.make(
            uninstall_string="MsiExec.exe /I{56D804AC-361D-40C0-99EA-431DC0403DB0}")
        self.assertEqual(item.command,
                         "MsiExec.exe /X{56D804AC-361D-40C0-99EA-431DC0403DB0}")

    def test_lowercase_i_too(self):
        item = self.make(uninstall_string="msiexec.exe /i {ABC}")
        self.assertIn("/X", item.command)
        self.assertNotIn("/i ", item.command.lower())

    def test_already_uninstall_untouched(self):
        raw = "MsiExec.exe /X{56D804AC-361D-40C0-99EA-431DC0403DB0}"
        item = self.make(uninstall_string=raw)
        self.assertEqual(item.command, raw)

    def test_non_msi_untouched(self):
        raw = r'"C:\App\uninst.exe" /S'
        item = self.make(uninstall_string=raw)
        self.assertEqual(item.command, raw)

    def test_quiet_string_wins(self):
        item = self.make(
            uninstall_string="MsiExec.exe /I{AAA}",
            quiet_uninstall_string="MsiExec.exe /X{AAA} /qn")
        self.assertIn("/X{AAA}", item.command)
        self.assertIn("/qn", item.command)

    def test_synthesize_from_product_code(self):
        """没有 UninstallString，但键名是产品码 —— 要能拼出卸载命令。"""
        item = self.make(product_code="{9CCFFF7D-DDA2-4123-B6D2-27A6FE6A4026}")
        self.assertTrue(item.can_uninstall)
        self.assertEqual(item.command,
                         "MsiExec.exe /X{9CCFFF7D-DDA2-4123-B6D2-27A6FE6A4026}")

    def test_product_code_beats_nothing(self):
        """字段里没产品码时，从 UninstallString 里抠一个。"""
        item = self.make(uninstall_string="MsiExec.exe /I{11111111-2222-3333-4444-555555555555}")
        self.assertEqual(item.msi_code, "{11111111-2222-3333-4444-555555555555}")

    def test_no_command_no_code(self):
        item = self.make()
        self.assertFalse(item.can_uninstall)
        self.assertEqual(item.command, "")

    def test_winget_still_works(self):
        item = self.make(source="winget", key_path="winget:7zip.7zip")
        self.assertIn("winget uninstall", item.command)


class RefreshTests(unittest.TestCase):
    """「刷新」必须每次都能用，而且卸载完要自己刷。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def build(self):
        from core.knowledge import load_from_files
        from ui.uninstall_view import UninstallView
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        kb = load_from_files(root / "resources" / "default_knowledge.json", root / "知识库.json")
        settings = Settings()
        return UninstallView(kb, settings)

    def test_loading_flag_resets(self):
        """这条守的是真 bug：_loading 没复位，第二次刷新直接被挡住。"""
        page = self.build()
        page.refresh()
        self.assertFalse(page._loading, "刷新完必须复位，否则以后再也刷不动")
        page.refresh()
        self.assertFalse(page._loading)
        self.assertGreater(page.model.rowCount(), 0, "第二次刷新也该读到软件")

    def test_refresh_twice_same_result(self):
        page = self.build()
        page.refresh()
        first = page.model.rowCount()
        page.refresh()
        second = page.model.rowCount()
        self.assertGreater(first, 0)
        self.assertEqual(first, second, "连续刷两次结果应该一致")

    def test_filter_cleared_on_refresh(self):
        page = self.build()
        page.refresh()
        page.filter_bar.edit.setText("绝对不存在的软件名zzz")
        self.app.processEvents()
        page.refresh()
        self.assertEqual(page.filter_bar.text(), "", "刷新该把过滤词清掉")

    def test_poll_stops_when_all_gone(self):
        """轮询：软件全消失后自动停，并给出提示。"""
        from unittest import mock

        page = self.build()
        page.refresh()
        page._poll_keys = {"已经没了的键"}
        page._poll_left = 5
        page._poll_timer.start()
        messages = []
        page.statusMessage.connect(messages.append)
        with mock.patch.object(page, "refresh"):
            page._poll_uninstalled()
        self.assertFalse(page._poll_timer.isActive())
        self.assertTrue(any("已从列表消失" in m for m in messages), messages)

    def test_poll_gives_up_after_timeout(self):
        from unittest import mock

        page = self.build()
        page.refresh()
        page._poll_keys = {"还在的键"}
        page._poll_left = 1
        page._poll_timer.start()
        messages = []
        page.statusMessage.connect(messages.append)
        with mock.patch.object(page, "refresh"), \
             mock.patch.object(page.model, "row_of", return_value=0):
            page._poll_uninstalled()
        self.assertFalse(page._poll_timer.isActive())
        self.assertTrue(any("扫一遍" in m for m in messages), messages)

    def test_scan_deferred_until_uninstall_completes(self):
        """关键：调起卸载程序后不能马上扫，否则扫到的是没删完的完整目录。"""
        from unittest import mock

        page = self.build()
        page.refresh()
        page._pending_scan = ["占位"]
        page._poll_keys = {"还剩的键"}
        page._poll_left = 5
        page._poll_timer.start()

        with mock.patch.object(page, "refresh"), \
             mock.patch.object(page.model, "row_of", return_value=0), \
             mock.patch.object(page, "_scan_and_show") as scan:
            page._poll_uninstalled()
        self.assertFalse(scan.called, "卸载还没完成，不该现在扫")

    def test_scan_runs_when_software_gone(self):
        from unittest import mock

        page = self.build()
        page.refresh()
        page._pending_scan = ["占位"]
        page._poll_keys = {"已经没了的键"}
        page._poll_left = 5
        page._poll_timer.start()

        captured = []
        with mock.patch.object(page, "refresh"), \
             mock.patch.object(QTimer, "singleShot",
                               side_effect=lambda ms, fn: captured.append((ms, fn))):
            page._poll_uninstalled()
        self.assertTrue(captured, "卸载完成了就该安排扫描")
        self.assertEqual(captured[0][0], 1500, "再等 1.5 秒让卸载程序收尾")

    def test_orphan_dirs_merged_into_list(self):
        """没有卸载登记的软件（只删登记不删文件那种）也要出现在列表里。"""
        from unittest import mock

        page = self.build()
        fake = [type("L", (), {
            "kind": "folder", "name": "假孤儿", "path": r"C:\Program Files\假孤儿",
        })()]
        with mock.patch("core.providers.orphan_source.scan_orphans", return_value=fake):
            page.refresh()
        names = [row.name for row in page.model.rows()]
        self.assertIn("假孤儿", names)
        row = next(r for r in page.model.rows() if r.name == "假孤儿")
        self.assertEqual(row.item.source, "orphan")
        self.assertIn("未登记", row.effect)


    def test_refresh_clears_model_filter_too(self):
        """刷新时必须把**模型里的**过滤词也清掉。

        踩过的坑：filter_bar.set_pending("") 屏蔽了信号，只清了输入框，
        模型还留着旧过滤词 —— 表现就是「清空搜索框后列表还是空的」。
        """
        page = self.build()
        page.refresh()
        total = page.model.rowCount()
        self.assertGreater(total, 0)

        page.filter_bar.edit.setText("绝对不存在的名字zzz")
        self.app.processEvents()
        self.assertEqual(page.model.rowCount(), 0, "搜索应该把列表过滤空")

        page.refresh()
        self.app.processEvents()
        self.assertEqual(page.filter_bar.text(), "", "输入框该清空")
        self.assertEqual(page.model.filter_key(), "", "模型里的过滤词也该清空")
        self.assertEqual(page.model.rowCount(), total, "列表应该恢复全量")

    def test_clearing_filter_restores_list(self):
        page = self.build()
        page.refresh()
        total = page.model.rowCount()

        page.filter_bar.edit.setText("绝对不存在的名字zzz")
        self.app.processEvents()
        self.assertEqual(page.model.rowCount(), 0)

        page.filter_bar.edit.clear()
        self.app.processEvents()
        self.assertEqual(page.model.rowCount(), total, "清空输入框后列表要回来")

    def test_poll_skipped_without_keys(self):
        page = self.build()
        page._start_uninstall_poll([])
        self.assertFalse(page._poll_timer.isActive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
