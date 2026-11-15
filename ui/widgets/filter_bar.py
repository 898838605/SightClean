"""四模块通用的过滤框。

只过滤**已经加载到内存里的**数据 —— 敲字不会触发任何 scandir / winreg / psutil 调用。
这条是硬底线：搜索框不是扫描器。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QToolButton, QWidget


class FilterBar(QWidget):
    """一个输入框 + 一个清空按钮。输入即过滤，Esc 清空。"""

    changed = Signal(str)
    cleared = Signal()

    def __init__(self, placeholder: str = "在当前列表里找…", parent=None) -> None:
        super().__init__(parent)
        self.edit = QLineEdit(self)
        self.edit.setObjectName("filter_edit")
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.edit.setMaximumWidth(320)
        self.edit.textChanged.connect(self._on_text)

        self.count = QLabel("")
        self.count.setStyleSheet("color:#57606a;padding-left:8px;")
        self.count.setVisible(False)

        self.btn_clear = QToolButton(self)
        self.btn_clear.setText("✕")
        self.btn_clear.setAutoRaise(True)
        self.btn_clear.setToolTip("清空过滤（Esc）")
        self.btn_clear.clicked.connect(self.clear)
        self.btn_clear.setVisible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        hint = QLabel("🔍")
        hint.setStyleSheet("color:#8b949e;")
        layout.addWidget(hint)
        layout.addWidget(self.edit)
        layout.addWidget(self.btn_clear)
        layout.addWidget(self.count)
        layout.addStretch(1)

    # -- 对外 --------------------------------------------------------------
    def text(self) -> str:
        return self.edit.text()

    def clear(self) -> None:
        if self.edit.text():
            self.edit.clear()          # 会触发 textChanged → changed("")
        else:
            self.cleared.emit()

    def set_pending(self, text: str) -> None:
        """由外部清空（切目录/刷新时用）。

        **注意**：这里屏蔽了信号，所以不会触发 changed。
        调用方必须自己把 model 的过滤词也清掉，否则会留下「输入框空了、
        但列表还在按旧词过滤」的怪状态。各页面的 refresh() 里都跟了一句
        self.model.set_filter("")。
        """
        if self.edit.text():
            self.edit.blockSignals(True)
            self.edit.clear()
            self.edit.blockSignals(False)
        self._reset_hint()
        self.cleared.emit()

    def set_counts(self, visible: int, total: int) -> None:
        """过滤生效时显示「找到 N / 共 M」，方便用户知道被藏了多少。"""
        if not self.text().strip():
            self.count.setVisible(False)
            self.btn_clear.setVisible(False)
            return
        self.count.setText(f"找到 {visible} / 共 {total}")
        self.count.setVisible(True)
        self.btn_clear.setVisible(True)

    # -- 内部 --------------------------------------------------------------
    def _on_text(self, text: str) -> None:
        self.btn_clear.setVisible(bool(text))
        if not text.strip():
            self.count.setVisible(False)
        self.changed.emit(text)

    def _reset_hint(self) -> None:
        self.count.setVisible(False)
        self.btn_clear.setVisible(False)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.clear()
            return
        super().keyPressEvent(event)
