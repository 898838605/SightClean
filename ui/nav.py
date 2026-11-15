"""左侧模块导航：四个页面，切一下就是换一套工具。

只是导航条，不持有任何业务状态 —— 切换时该清什么、该停什么，由主窗口那一侧负责。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

PAGE_FILES = 0
PAGE_SOFTWARE = 1
PAGE_REGISTRY = 2
PAGE_PROCESS = 3

MODULES = (
    (PAGE_FILES, "🗂", "文件清理", "按文件夹一个个看，每一行都写着作用和风险，自己决定删什么"),
    (PAGE_SOFTWARE, "📦", "软件卸载", "列出这台电脑装了什么，想卸哪个自己勾；卸载走软件自己的卸载程序"),
    (PAGE_REGISTRY, "🧾", "注册表清理", "开机启动、卸载残留、失效关联，逐条确认后再动"),
    (PAGE_PROCESS, "⚙", "进程管理", "现在正在跑的东西，结束哪一个由你决定"),
)

PAGE_WIDTH = 186


class NavBar(QWidget):
    """一排模块按钮。选中哪一项由主窗口控制，这里只管显示和发信号。"""

    moduleChanged = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(PAGE_WIDTH)
        self._list = QListWidget(self)
        self._list.setFrameShape(QListWidget.NoFrame)
        self._list.setStyleSheet(
            "QListWidget::item { padding: 9px 8px; }"
            "QListWidget::item:selected { background:#e8f0fe; color:#1a4b9c; border-radius:6px; }"
        )

        title = QLabel("模块")
        title.setStyleSheet("color:#57606a;padding:8px 0 4px 6px;")

        for index, icon, name, hint in MODULES:
            item = QListWidgetItem(f"{icon}  {name}")
            item.setData(Qt.UserRole, index)
            item.setToolTip(hint)
            self._list.addItem(item)

        self._list.currentRowChanged.connect(self._on_row_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 6)
        layout.setSpacing(2)
        layout.addWidget(title)
        layout.addWidget(self._list, 1)

    def set_current(self, index: int) -> None:
        if self._list.currentRow() != index:
            self._list.blockSignals(True)
            self._list.setCurrentRow(index)
            self._list.blockSignals(False)

    def _on_row_changed(self, row: int) -> None:
        if row >= 0:
            self.moduleChanged.emit(row)
