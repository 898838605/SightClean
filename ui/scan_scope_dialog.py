"""让用户自己勾要扫哪些位置。

把所有能想到的位置列出来，标清楚风险：
    safe    删了只影响软件本身
    caution 可能混着用户自己的数据
    risky   系统地盘，删了会出问题
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QCheckBox,
)

from core.scan_scopes import available_scopes, default_keys

RISK_COLORS = {
    "safe": QColor("#1a7f37"),
    "caution": QColor("#9a6700"),
    "risky": QColor("#cf222e"),
}
RISK_LABELS = {"safe": "安全", "caution": "谨慎", "risky": "危险"}
RISK_ORDER = ("safe", "caution", "risky")
RISK_NOTES = {
    "safe": "删了只影响这个软件本身，放开扫",
    "caution": "里面可能混着你自己的数据，扫之前想清楚",
    "risky": "系统/安装器的地盘，删了会让软件装不上或修不好",
}


class ScanScopeDialog(QDialog):
    def __init__(self, selected, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("扫描哪些位置")
        self.setMinimumSize(760, 620)
        self._boxes: dict[str, QCheckBox] = {}

        chosen = set(selected or [])
        if not chosen:
            chosen = set(default_keys())

        title = QLabel("选择要扫描的位置")
        font = title.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        title.setFont(font)

        hint = QLabel(
            "勾得越多扫得越全，但候选也越多、AI 判断越慢越贵。\n"
            "标「危险」的位置默认关着 —— 那些是系统自己的地盘，删了会让软件装不上或修不好。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#57606a;")

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 8, 0)

        scopes = available_scopes()
        for risk in RISK_ORDER:
            group = [scope for scope in scopes if scope.risk == risk]
            if not group:
                continue
            head = QLabel(f"{RISK_LABELS[risk]}　·　{RISK_NOTES[risk]}")
            head_font = head.font()
            head_font.setBold(True)
            head.setFont(head_font)
            head.setStyleSheet(f"color:{RISK_COLORS[risk].name()};")
            inner_layout.addSpacing(10)
            inner_layout.addWidget(head)
            for scope in group:
                box = QCheckBox(scope.label)
                box.setChecked(scope.key in chosen)
                box.setToolTip(scope.note)
                self._boxes[scope.key] = box
                path = QLabel(scope.path)
                path.setStyleSheet("color:#57606a;")
                path.setTextInteractionFlags(Qt.TextSelectableByMouse)
                if len(scope.paths) > 1:
                    path.setToolTip("会扫这些地方：\n" + "\n".join(scope.paths))
                note = QLabel(scope.note)
                note.setStyleSheet("color:#57606a;")
                note.setWordWrap(True)
                row = QVBoxLayout()
                row.setContentsMargins(22, 0, 0, 6)
                row.setSpacing(1)
                row.addWidget(box)
                row.addWidget(path)
                row.addWidget(note)
                holder = QWidget()
                holder.setLayout(row)
                inner_layout.addWidget(holder)
        inner_layout.addStretch(1)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)

        self.btn_all = QPushButton("全选")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none = QPushButton("全不选")
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        self.btn_safe = QPushButton("只选安全的")
        self.btn_safe.setToolTip("把「危险」和「谨慎」的都取消掉")
        self.btn_safe.clicked.connect(self._pick_safe)
        self.btn_default = QPushButton("恢复默认")
        self.btn_default.clicked.connect(self._restore_default)

        self.count = QLabel("")
        self.count.setStyleSheet("color:#57606a;")

        tools = QHBoxLayout()
        for button in (self.btn_all, self.btn_none, self.btn_safe, self.btn_default):
            tools.addWidget(button)
        tools.addStretch(1)
        tools.addWidget(self.count)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("就用这些")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(area, 1)
        layout.addLayout(tools)
        layout.addWidget(buttons)

        for box in self._boxes.values():
            box.toggled.connect(self._refresh_count)
        self._refresh_count()

    def _set_all(self, state: bool) -> None:
        for box in self._boxes.values():
            box.setChecked(state)

    def _pick_safe(self) -> None:
        from core.scan_scopes import available_scopes as scopes_fn

        safe = {scope.key for scope in scopes_fn() if scope.risk == "safe"}
        for key, box in self._boxes.items():
            box.setChecked(key in safe)

    def _restore_default(self) -> None:
        defaults = set(default_keys())
        for key, box in self._boxes.items():
            box.setChecked(key in defaults)

    def _refresh_count(self) -> None:
        count = len(self.chosen())
        self.count.setText(f"已选 {count} / {len(self._boxes)} 个位置")

    def chosen(self) -> list[str]:
        return [key for key, box in self._boxes.items() if box.isChecked()]
