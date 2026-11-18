"""文件浏览用的小部件：勾选框、三态表头、面包屑。

从 `ui/main_window.py` 拆出来，主窗口只负责把模块和业务串起来。
"""
from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QStyle,
    QStyleOptionButton,
    QStyleOptionHeader,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QToolButton,
    QWidget,
)

from ui.table_model import COL_CHECK

DRIVE_KIND_ICON = {
    "fixed": "💽",
    "removable": "💾",
    "remote": "🌐",
    "cdrom": "📀",
    "ram": "🧠",
    "unknown": "❓",
}


class CheckBoxDelegate(QStyledItemDelegate):
    """把「是否删除」的勾选框画在格子正中间。"""

    def paint(self, painter, option, index) -> None:  # noqa: D102
        if index.column() != COL_CHECK:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        # 关掉样式自带的勾选标记，勾选框交给下面自己画（居中那个）
        opt.features = opt.features & ~QStyleOptionViewItem.HasCheckIndicator
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)
        box = QStyleOptionButton()
        box.rect = QRect(0, 0, 16, 16)
        box.rect.moveCenter(option.rect.center())
        if index.flags() & Qt.ItemIsUserCheckable:
            state = QStyle.State_On if index.data(Qt.CheckStateRole) == Qt.Checked else QStyle.State_Off
            box.state = state | QStyle.State_Enabled
        else:
            # 只有读不到的项画成灰色禁用框，让人一眼看出这里点不动
            box.state = QStyle.State_Off
        style.drawPrimitive(QStyle.PE_IndicatorCheckBox, box, painter, widget)

    def editorEvent(self, event, model, option, index) -> bool:  # noqa: D102
        if (
            index.column() == COL_CHECK
            and event.type() == QEvent.MouseButtonRelease
            and (index.flags() & Qt.ItemIsUserCheckable)
        ):
            new_state = Qt.Unchecked if index.data(Qt.CheckStateRole) == Qt.Checked else Qt.Checked
            model.setData(index, new_state, Qt.CheckStateRole)
            return True
        return super().editorEvent(event, model, option, index)


class SelectionHeader(QHeaderView):
    """「是否删除」列表头画一个三态复选框：全选 / 部分选中 / 全不选。

    点这一个框等于对当前页所有可勾选的行做全选或全不选；
    三态本身由 model.check_state() 算出来，这里只负责画和响应点击。
    """

    toggleAllRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Horizontal, parent)
        self._state = Qt.Unchecked
        self.setSectionsClickable(True)
        self.setHighlightSections(False)

    def set_check_state(self, state: Qt.CheckState) -> None:
        if state == self._state:
            return
        self._state = state
        self.updateSection(COL_CHECK)

    @staticmethod
    def _indicator_rect(section_rect: QRect) -> QRect:
        box = QRect(0, 0, 14, 14)
        box.moveCenter(QPoint(section_rect.left() + 16, section_rect.center().y()))
        return box

    def paintSection(self, painter, rect, logicalIndex) -> None:  # noqa: D102, N802
        if logicalIndex != COL_CHECK:
            super().paintSection(painter, rect, logicalIndex)
            return
        style = self.style()
        # PySide6 的 initStyleOption 只接受 option 一个参数，这里直接自己填
        base = QStyleOptionHeader()
        base.rect = rect
        base.section = logicalIndex
        base.orientation = Qt.Horizontal
        base.selectedPosition = QStyleOptionHeader.NotAdjacent
        base.state = QStyle.State_Enabled | QStyle.State_Horizontal
        if self.count() == 1:
            base.position = QStyleOptionHeader.OnlyOneSection
        elif logicalIndex <= 0:
            base.position = QStyleOptionHeader.Beginning
        elif logicalIndex >= self.count() - 1:
            base.position = QStyleOptionHeader.End
        else:
            base.position = QStyleOptionHeader.Middle

        background = QStyleOptionHeader(base)
        background.text = ""
        style.drawControl(QStyle.CE_Header, background, painter, self)

        text_option = QStyleOptionHeader(base)
        text_option.rect = rect.adjusted(30, 0, -4, 0)
        text_option.text = "是否删除"
        text_option.textAlignment = Qt.AlignLeft | Qt.AlignVCenter
        style.drawControl(QStyle.CE_HeaderLabel, text_option, painter, self)

        box = QStyleOptionButton()
        box.rect = self._indicator_rect(rect)
        box.state = QStyle.State_Enabled
        if self._state == Qt.Checked:
            box.state |= QStyle.State_On
        elif self._state == Qt.PartiallyChecked:
            box.state |= QStyle.State_NoChange
        else:
            box.state |= QStyle.State_Off
        style.drawPrimitive(QStyle.PE_IndicatorCheckBox, box, painter, self)

    def mousePressEvent(self, event) -> None:  # noqa: D102, N802
        if self.logicalIndexAt(event.pos()) == COL_CHECK:
            self.toggleAllRequested.emit()
            return
        super().mousePressEvent(event)


class BreadcrumbBar(QWidget):
    """路径面包屑，每一段都能点。

    第一段（盘符）点开是下拉菜单 —— 左侧盘符栏去掉之后，这是切盘的入口。
    """

    pathActivated = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(2, 0, 2, 0)
        self._layout.setSpacing(0)
        self._layout.addStretch(1)
        self._path = ""
        self._roots_provider = None

    def set_roots_provider(self, provider) -> None:
        """回调返回 DriveInfo 列表；有了它，第一段就能点出盘符菜单。"""
        self._roots_provider = provider

    def _show_roots(self, button: QToolButton) -> None:
        drives = []
        if callable(self._roots_provider):
            try:
                drives = list(self._roots_provider() or [])
            except Exception:  # noqa: BLE001
                drives = []
        if not drives:
            return
        menu = QMenu(self)
        for drive in drives:
            label = f"{DRIVE_KIND_ICON.get(drive.kind, '❓')}  {drive.root}  {drive.label}"
            if drive.state == "probing":
                label += "  连接中…"
            elif drive.state == "unavailable":
                label += "  连不上"
            action = menu.addAction(label)
            action.setEnabled(drive.state != "unavailable")
            action.triggered.connect(lambda _checked=False, root=drive.root: self.pathActivated.emit(root))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def crumbs(self, path: str) -> list[tuple[str, str]]:
        if not path:
            return [("", "这台电脑")]
        crumb_list: list[tuple[str, str]] = []
        p = os.path.normpath(path)
        drive, rest = os.path.splitdrive(p)
        if not rest.strip("\\/"):
            root = (drive + "\\") if drive else "\\"
            return [(root, root)]
        current = drive + "\\"
        crumb_list.append((current, drive + "\\"))
        for part in rest.split("\\"):
            if not part:
                continue
            current = os.path.join(current, part)
            crumb_list.append((current, part))
        return crumb_list

    def set_path(self, path: str) -> None:
        self._path = path
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        crumb_list = self.crumbs(path)
        for index, (target, label) in enumerate(crumb_list):
            if index:
                sep = QLabel("›")
                sep.setStyleSheet("color:#8b949e;padding:0 2px;")
                self._layout.addWidget(sep)
            button = QToolButton()
            button.setText(label)
            button.setAutoRaise(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(target)
            if index == len(crumb_list) - 1:
                button.setStyleSheet("font-weight:bold;color:#1f2328;")
            if index == 0 and self._roots_provider is not None:
                # 第一段是盘符：点开选盘，而不是单纯回根目录
                button.setToolTip(target + "（点一下可以换盘）")
                button.clicked.connect(lambda _checked=False, b=button: self._show_roots(b))
            else:
                button.clicked.connect(lambda _checked=False, t=target: self.pathActivated.emit(t))
            self._layout.addWidget(button)
        self._layout.addStretch(1)
