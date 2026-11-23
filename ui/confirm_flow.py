"""确认清单：执行之前把要做的事列出来，让你逐项过一遍。

**风险等级本身不拦截。** 红色、黄色、灰色在列表里只是颜色；
只有「彻底删除」这种不可逆方式会额外要求确认一次。

保留的东西：
- 逐项勾选（默认全勾，可以取消）
- 汇总卡片（多少项、多大、走回收站还是彻底删）
- 方式选择、备份提示
- 「后果」一列直接取知识库的 impact
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.fs_browser import human_size
from core.risk import Risk

MUTED_FG = QColor("#57606a")

# 执行方式那一块要显眼：红边、粗体、竖着排，别让人漏看
MODE_BOX_QSS = (
    "QGroupBox{border:2px solid #c62828;border-radius:6px;"
    "margin-top:12px;padding:16px 16px 12px 16px;font-weight:bold;}"
    "QGroupBox::title{subcontrol-origin:margin;left:12px;padding:0 6px;"
    "color:#c62828;font-weight:bold;}"
    # 整行都能点：padding 给足，spacing 把圆和文字拉开
    "QRadioButton{font-weight:bold;font-size:10pt;"
    "padding:10px 8px 10px 6px;spacing:12px;border-radius:6px;}"
    "QRadioButton:hover{background:#fff1f0;}"
    # 未选中：空心圆
    "QRadioButton::indicator{width:18px;height:18px;border-radius:10px;"
    "border:2px solid #6e7781;background:#ffffff;}"
    "QRadioButton::indicator:hover{border-color:#c62828;}"
    # 选中：圆环 + 中心实心点（用径向渐变画点）
    "QRadioButton::indicator:checked{border:2px solid #c62828;background:qradialgradient("
    "cx:0.5,cy:0.5,radius:0.5,fx:0.5,fy:0.5,"
    "stop:0 #c62828,stop:0.45 #c62828,stop:0.5 #ffffff,stop:1 #ffffff);}"
)


def is_red(item) -> bool:
    """红色只是展示用的颜色，不参与任何拦截判断。"""
    return Risk.parse(getattr(item, "risk", "")) is Risk.FORBIDDEN


def _item_name(item) -> str:
    """取名字。文件模块的 DeleteItem 叫 display，其它三个模块叫 name。"""
    return str(getattr(item, "name", "") or getattr(item, "display", "") or "")


def _item_detail(item) -> str:
    """取「说明」列：名称以外的补充信息（文件模块给完整路径）。"""
    detail = str(getattr(item, "detail", "") or "")
    if detail:
        return detail
    return str(getattr(item, "human_path", "") or getattr(item, "path", "") or "")


def _item_size(item):
    """大小。DeleteItem 用 size，三个页面的 Row 用 size_bytes。"""
    size = getattr(item, "size_bytes", None)
    if size is None:
        size = getattr(item, "size", None)
    return size if isinstance(size, int) and size > 0 else None


def _item_impact(item) -> str:
    """「删了会怎样」那一句。知识库写的 impact，没写就留空。"""
    return str(getattr(item, "impact", "") or "").strip()


@dataclass
class FlowDecision:
    """确认流程的结论。"""

    items: list
    mode: str | None = None

    @property
    def skipped_red(self) -> list:
        """留着这个属性只为兼容旧调用方 —— 现在不会再暂缓任何项。"""
        return []

    @property
    def skipped_count(self) -> int:
        return 0


class ConfirmDialog(QDialog):
    """执行前的清单。所有项一视同仁，红色只是颜色。"""

    COL_CHECK, COL_NAME, COL_DETAIL, COL_EFFECT, COL_IMPACT, COL_RISK = range(6)
    HEADERS = ("执行", "名称", "说明", "作用", "删了会怎样", "风险")
    COLUMNS = len(HEADERS)

    def __init__(
        self,
        items: Sequence,
        action: str,
        hint: str,
        irreversible: bool = True,
        parent=None,
        modes: Sequence[tuple[str, str]] | None = None,
        backup_note: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"确认{action}")
        self.setMinimumSize(1020, 600)
        self.setModal(True)
        self._items = list(items)
        self._checks = [True] * len(self._items)
        self._action = action
        self._backup_note = backup_note

        title = QLabel(f"要{action}这 {len(self._items)} 项吗？")
        font = QFont(title.font())
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        title.setFont(font)

        subtitle = QLabel(hint)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{MUTED_FG.name()};")

        table = QTableWidget(len(self._items), self.COLUMNS)
        table.setObjectName("confirm_table")
        table.setHorizontalHeaderLabels(list(self.HEADERS))
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(28)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setShowGrid(False)
        # 「作用」和「删了会怎样」经常很长，宁可换行也别截断 —— 用户就是要看全
        table.setTextElideMode(Qt.ElideNone)
        table.setWordWrap(True)
        header = table.horizontalHeader()
        header.setSectionResizeMode(self.COL_CHECK, QHeaderView.Fixed)
        header.setSectionResizeMode(self.COL_NAME, QHeaderView.Interactive)
        header.setSectionResizeMode(self.COL_DETAIL, QHeaderView.Interactive)
        header.setSectionResizeMode(self.COL_EFFECT, QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_IMPACT, QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_RISK, QHeaderView.Fixed)
        table.setColumnWidth(self.COL_CHECK, 52)
        table.setColumnWidth(self.COL_NAME, 200)
        table.setColumnWidth(self.COL_DETAIL, 230)
        table.setColumnWidth(self.COL_RISK, 70)

        for row, item in enumerate(self._items):
            level = Risk.parse(getattr(item, "risk", ""))
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Checked)
            table.setItem(row, self.COL_CHECK, check)

            cells = (
                _item_name(item),
                _item_detail(item),
                str(getattr(item, "effect", "") or "未收录，建议确认后再做"),
                _item_impact(item) or "这条没写后果",
                level.label,
            )
            for index, text in enumerate(cells, start=self.COL_NAME):
                cell = QTableWidgetItem(str(text))
                cell.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                cell.setToolTip(str(text))
                if index == self.COL_RISK:
                    cell.setForeground(QBrush(level.color))
                table.setItem(row, index, cell)
        # 行高跟着内容走：换行之后一行可能要占两三行高
        vheader = table.verticalHeader()
        vheader.setSectionResizeMode(QHeaderView.ResizeToContents)
        vheader.setMinimumSectionSize(28)
        table.itemChanged.connect(self._on_item_changed)
        self.table = table

        self.summary = QLabel("")
        self.summary.setObjectName("preflight_card")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setStyleSheet(
            f"QLabel#preflight_card{{background:#f6f8fa;border:1px solid #d0d7de;"
            f"border-radius:6px;padding:8px 10px;color:{MUTED_FG.name()};}}"
        )

        self.confirm_hint = QLabel("")
        self.confirm_hint.setWordWrap(True)
        self.confirm_hint.setVisible(False)

        self._modes = list(modes or [])
        self._mode_group = None
        self._mode_box = None
        if self._modes:
            from PySide6.QtWidgets import QButtonGroup, QGroupBox, QRadioButton

            self._mode_group = QButtonGroup(self)
            self._mode_box = QGroupBox("执行方式（必须选一个）")
            mode_font = QFont(self._mode_box.font())
            mode_font.setBold(True)
            self._mode_box.setFont(mode_font)
            self._mode_box.setStyleSheet(MODE_BOX_QSS)
            mode_layout = QVBoxLayout(self._mode_box)
            mode_layout.setSpacing(4)
            for index, (label, value) in enumerate(self._modes):
                radio = QRadioButton(label)
                radio.setProperty("mode_value", value)
                radio.setCursor(Qt.PointingHandCursor)
                radio.setMinimumHeight(38)      # 点击区域放大，别让人瞄准那个小圈
                if index == 0:
                    radio.setChecked(True)
                self._mode_group.addButton(radio, index)
                self._mode_group.button(index).toggled.connect(lambda _checked: self._refresh())
                mode_layout.addWidget(radio)

        self.permanent_ack = QCheckBox(
            "我明白：彻底删除后文件不进回收站，无法撤销"
        )
        self.permanent_ack.setVisible(False)
        self.permanent_ack.toggled.connect(self._refresh)

        buttons = QDialogButtonBox()
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setDefault(True)
        self.btn_ok = QPushButton(action)
        buttons.addButton(self.btn_cancel, QDialogButtonBox.RejectRole)
        buttons.addButton(self.btn_ok, QDialogButtonBox.AcceptRole)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self.summary)
        layout.addWidget(subtitle)
        layout.addWidget(table, 1)
        if self._mode_box is not None:
            layout.addWidget(self._mode_box)
        layout.addWidget(self.permanent_ack)
        layout.addWidget(self.confirm_hint)
        layout.addWidget(buttons)
        self._refresh()

    def _on_item_changed(self, cell: QTableWidgetItem) -> None:
        if cell.column() == self.COL_CHECK:
            self._checks[cell.row()] = cell.checkState() == Qt.Checked
            self._refresh()

    def _refresh(self) -> None:
        chosen = self.chosen_items
        self.summary.setText(self._preflight_text(chosen))
        requires_ack = self.chosen_mode == "permanent"
        self.permanent_ack.setVisible(requires_ack)
        self.btn_ok.setEnabled(
            bool(chosen) and (not requires_ack or self.permanent_ack.isChecked())
        )

    def _preflight_text(self, chosen: Sequence) -> str:
        unit = {"删除": "项", "卸载": "个", "清理": "个键", "结束": "个进程"}.get(self._action, "项")
        verb = {"删除": "将删除", "卸载": "将卸载", "清理": "将清理", "结束": "将结束"}.get(
            self._action, f"将{self._action}"
        )
        if not chosen:
            return f"<b>{verb} 0 {unit}</b>　·　什么都没选，不会动任何东西"

        lines = [f"<b>{verb} {len(chosen)} {unit}</b>"]
        sizes = [_item_size(item) for item in chosen]
        known = [size for size in sizes if size]
        mode = self.chosen_mode
        impact_bits = []
        if known:
            if mode == "permanent" or (mode is None and self._action != "删除"):
                impact_bits.append(f"涉及约 {human_size(sum(known))}")
            else:
                impact_bits.append(f"约 {human_size(sum(known))} 移入回收站（清空回收站后释放）")
        elif all(size is None for size in sizes):
            impact_bits.append("有些项还没算出大小，这次不估")

        if self._action == "删除":
            if mode == "permanent":
                impact_bits.append("彻底删除，找不回来")
            elif mode == "recycle":
                impact_bits.append("可从回收站还原")
        elif self._action == "清理":
            if self._backup_note:
                impact_bits.append(self._backup_note)
            impact_bits.append("可从备份 .reg 还原")
        elif self._action == "结束":
            impact_bits.append("结束之后没法撤销，没保存的工作会丢")
        elif self._action == "卸载":
            impact_bits.append("由各软件自己的卸载程序执行，实际释放以完成情况为准")

        if impact_bits:
            lines.append("　·　".join(impact_bits))
        if self._backup_note and self._action != "清理":
            lines.append(self._backup_note)
        return "<br>".join(lines)

    @property
    def chosen_items(self) -> list:
        return [item for item, keep in zip(self._items, self._checks) if keep]

    @property
    def chosen_mode(self) -> str | None:
        if self._mode_group is None:
            return None
        button = self._mode_group.checkedButton()
        return str(button.property("mode_value")) if button is not None else None

    @property
    def needed_word(self) -> str:
        """旧版要用户输入「确认」两个字，现在不需要了。留着属性免得调用方报错。"""
        return ""


class ConfirmFlow:
    """把「确认」这一步串起来。没有解锁，也没有最终闸门。"""

    @staticmethod
    def unlock(parent, items: Sequence, action: str = "处理") -> bool:
        """旧版的第一道坎。现在恒为 True —— 红色不再拦人。
        留着这个函数只为兼容旧调用点，随时可以删。"""
        return True

    @staticmethod
    def confirm(
        parent,
        items: Sequence,
        action: str,
        hint: str = "",
        irreversible: bool = True,
        modes: Sequence[tuple[str, str]] | None = None,
        backup_note: str = "",
    ) -> FlowDecision | None:
        hint = hint or "下面每一项都可以单独取消；「作用」就是列表里那行说明，再确认一遍。"
        dialog = ConfirmDialog(items, action, hint, irreversible, parent,
                               modes=modes, backup_note=backup_note)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        chosen = dialog.chosen_items
        if not chosen:
            return None
        return FlowDecision(items=chosen, mode=dialog.chosen_mode)
