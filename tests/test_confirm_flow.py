"""确认清单：所有项一视同仁，红色只是颜色。

三道坎（解锁 / 输入确认 / 仅执行普通项）已经全部去掉，
这里守的就是「不再分级拦截」这件事。
"""
from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog


@dataclass
class FakeItem:
    name: str
    risk: str = "safe"
    detail: str = ""
    size_bytes: int | None = 1024
    effect: str = ""
    impact: str = ""


RED = FakeItem("危险的东西", "forbidden", "1.0", 1024, "删了会坏", "删了系统起不来")
NORMAL = FakeItem("普通的东西", "safe", "2.0", 2048, "随便删", "删了没影响")
CAUTION = FakeItem("看情况的东西", "caution", "3.0", 4096, "自己掂量", "可能影响别的软件")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class NoGateTests(Base):
    def make(self, items, action="删除", **kwargs):
        from ui.confirm_flow import ConfirmDialog

        return ConfirmDialog(items, action, "提示", **kwargs)

    def test_no_unlock_prompt(self):
        """旧的第一道坎已经去掉，unlock 恒放行。"""
        from ui.confirm_flow import ConfirmFlow

        self.assertTrue(ConfirmFlow.unlock(None, [RED]))
        self.assertTrue(ConfirmFlow.unlock(None, [RED, NORMAL]))

    def test_no_type_to_confirm(self):
        """红色项不需要输入「确认」两个字，按钮直接可点。"""
        dialog = self.make([RED])
        self.assertTrue(dialog.btn_ok.isEnabled())
        self.assertFalse(hasattr(dialog, "confirm_input"))

    def test_red_and_normal_same_columns(self):
        dialog = self.make([RED, NORMAL])
        self.assertEqual(dialog.table.rowCount(), 2)
        for row in range(2):
            for col in range(dialog.table.columnCount()):
                self.assertIsNotNone(dialog.table.item(row, col))

    def test_no_final_gate(self):
        """第三道坎（仅执行普通项）不再存在。"""
        import ui.confirm_flow as cf

        self.assertFalse(hasattr(cf, "FinalGateDialog"))
        self.assertFalse(hasattr(cf, "UnlockDialog"))

    def test_confirm_returns_all_chosen_including_red(self):
        with unittest.mock.patch.object(
            __import__("ui.confirm_flow", fromlist=["ConfirmDialog"]).ConfirmDialog,
            "exec", return_value=QDialog.Accepted,
        ):
            from ui.confirm_flow import ConfirmFlow

            decision = ConfirmFlow.confirm(None, [RED, NORMAL], "删除")
        self.assertIsNotNone(decision)
        self.assertEqual(len(decision.items), 2, "红色项不该被剔除")
        self.assertEqual(decision.skipped_count, 0)

    def test_unchecking_still_works(self):
        dialog = self.make([RED, NORMAL])
        dialog.table.item(0, 0).setCheckState(Qt.Unchecked)
        self.assertEqual(len(dialog.chosen_items), 1)
        self.assertFalse(dialog.btn_ok.isEnabled() is False)

    def test_risk_column_still_shows_levels(self):
        """安全等级留着：风险列仍然分四级。"""
        dialog = self.make([RED, NORMAL, CAUTION])
        labels = [dialog.table.item(r, dialog.COL_RISK).text() for r in range(3)]
        self.assertEqual(labels, ["别删", "放心删", "看情况"])

    def test_impact_column_shown(self):
        dialog = self.make([RED])
        self.assertEqual(dialog.table.item(0, dialog.COL_IMPACT).text(), "删了系统起不来")

    def test_missing_impact_says_so(self):
        dialog = self.make([FakeItem("没有后果的", "caution", impact="")])
        self.assertIn("没写后果", dialog.table.item(0, dialog.COL_IMPACT).text())

    def test_summary_has_no_risk_warning(self):
        """汇总不再喊「高风险 N 项」。"""
        dialog = self.make([RED, NORMAL])
        self.assertNotIn("高风险", dialog.summary.text())
        self.assertIn("将删除 2 项", dialog.summary.text())

    def test_mode_selection_kept(self):
        dialog = self.make([NORMAL], modes=(("移入回收站", "recycle"), ("彻底删除", "permanent")))
        self.assertEqual(dialog.chosen_mode, "recycle")
        dialog._mode_group.buttons()[1].setChecked(True)
        self.assertEqual(dialog.chosen_mode, "permanent")

    def test_permanent_delete_requires_explicit_acknowledgement(self):
        dialog = self.make(
            [NORMAL],
            modes=(("移入回收站", "recycle"), ("彻底删除", "permanent")),
        )
        permanent = dialog._mode_group.buttons()[1]
        permanent.setChecked(True)
        self.assertTrue(dialog.permanent_ack.isVisibleTo(dialog))
        self.assertFalse(dialog.permanent_ack.isChecked())
        self.assertFalse(dialog.btn_ok.isEnabled())
        dialog.permanent_ack.setChecked(True)
        self.assertTrue(dialog.btn_ok.isEnabled())
        dialog._mode_group.buttons()[0].setChecked(True)
        self.assertFalse(dialog.permanent_ack.isVisibleTo(dialog))
        self.assertTrue(dialog.btn_ok.isEnabled())


class LayoutTests(Base):
    """作用/后果两列必须能看全，方式选择必须显眼。"""

    def make(self, items=None, **kwargs):
        from ui.confirm_flow import ConfirmDialog

        items = items or [FakeItem("甲", "forbidden", "1.0", 1024,
                                   "安装的软件本体，要卸载请走系统设置，别手动删。",
                                   "删了对应软件直接不能用，注册表和快捷方式还在，清理起来更麻烦")]
        return ConfirmDialog(items, "删除", "提示", **kwargs)

    def test_long_text_wraps_not_elided(self):
        dialog = self.make()
        self.assertTrue(dialog.table.wordWrap(), "长文本必须能换行")
        self.assertEqual(dialog.table.textElideMode(), Qt.ElideNone, "不能省略号截断")

    def test_full_text_kept(self):
        dialog = self.make()
        shown = dialog.table.item(0, dialog.COL_EFFECT).text()
        self.assertEqual(shown, "安装的软件本体，要卸载请走系统设置，别手动删。")
        self.assertEqual(len(dialog.table.item(0, dialog.COL_IMPACT).text()), 30)

    def test_row_height_grows_for_wrapped_rows(self):
        dialog = self.make()
        dialog.resize(1200, 700)
        dialog.show()
        for _ in range(10):
            self.app.processEvents()
        self.assertGreaterEqual(dialog.table.rowHeight(0), 28)
        dialog.close()

    def test_mode_box_is_prominent_group(self):
        from PySide6.QtWidgets import QGroupBox

        dialog = self.make(modes=(("移入回收站", "recycle"), ("彻底删除", "permanent")))
        self.assertIsInstance(dialog._mode_box, QGroupBox)
        self.assertIn("执行方式", dialog._mode_box.title())
        self.assertEqual(len(dialog._mode_group.buttons()), 2)
        self.assertEqual(dialog.chosen_mode, "recycle")

    def test_clicking_the_label_area_selects(self):
        """点文字也要能选中，不是非得瞄准那个小圆圈。"""
        from PySide6.QtCore import QPoint

        dialog = self.make(modes=(("移入回收站", "recycle"), ("彻底删除", "permanent")))
        radio = dialog._mode_group.buttons()[1]
        radio.resize(320, 38)
        # 最右侧的文字区域
        self.assertTrue(radio.hitButton(QPoint(radio.width() - 5, radio.height() // 2)),
                        "文字区域点不动，只能点圆圈就不能用了")
        self.assertTrue(radio.hitButton(QPoint(12, radio.height() // 2)),
                        "圆圈本身当然也要能点")

    def test_indicator_styled_and_big(self):
        """选择框要明显：样式里显式画了圆圈和实心点。"""
        from ui.confirm_flow import MODE_BOX_QSS as qss

        self.assertIn("QRadioButton::indicator", qss)
        self.assertIn(":checked", qss)
        self.assertIn("qradialgradient", qss, "选中态要画中心实心点")
        self.assertIn("width:18px", qss, "圈要大一点才显眼")

    def test_radio_tall_enough_to_hit(self):
        dialog = self.make(modes=(("甲", "a"), ("乙", "b")))
        for radio in dialog._mode_group.buttons():
            self.assertGreaterEqual(radio.minimumHeight(), 32)

    def test_exactly_one_selected(self):
        from PySide6.QtWidgets import QRadioButton

        dialog = self.make(modes=(("甲", "a"), ("乙", "b")))
        radios = [w for w in dialog._mode_box.findChildren(QRadioButton)]
        self.assertEqual(sum(1 for r in radios if r.isChecked()), 1, "任何时候只该有一个被选中")

    def test_no_mode_box_when_no_modes(self):
        dialog = self.make()
        self.assertIsNone(dialog._mode_box)
        self.assertIsNone(dialog.chosen_mode)


class RealItemTests(Base):
    def test_delete_item_shows_all_columns(self):
        from core.delete import DeleteItem
        from ui.confirm_flow import ConfirmDialog

        items = [DeleteItem(human_path=r"C:\x\a.txt", display="a.txt", is_dir=False,
                            risk="forbidden", size=2048, effect="文档", impact="删了找不回来")]
        dialog = ConfirmDialog(items, "删除", "提示")
        table = dialog.table
        self.assertEqual(table.item(0, dialog.COL_NAME).text(), "a.txt")
        self.assertIn("C:\\x\\a.txt", table.item(0, dialog.COL_DETAIL).text())
        self.assertEqual(table.item(0, dialog.COL_IMPACT).text(), "删了找不回来")

    def test_red_delete_item_executes_like_any(self):
        from core.delete import DeleteItem
        from ui.confirm_flow import ConfirmFlow

        items = [DeleteItem(human_path=r"C:\x\a.txt", display="a.txt", is_dir=False,
                            risk="forbidden", size=2048, effect="文档", impact="删了找不回来")]
        with unittest.mock.patch.object(
            __import__("ui.confirm_flow", fromlist=["ConfirmDialog"]).ConfirmDialog,
            "exec", return_value=QDialog.Accepted,
        ):
            decision = ConfirmFlow.confirm(None, items, "删除")
        self.assertEqual(len(decision.items), 1, "红色项照样能删")


import unittest.mock  # noqa: E402  (放最后避免和上面的 from 混在一起)

if __name__ == "__main__":
    unittest.main(verbosity=2)
