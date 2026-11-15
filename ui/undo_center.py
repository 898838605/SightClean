"""撤销中心：看看自己做过的操作，能还原的给按钮，不能的直接说清楚。

刻意不做成第五个模块挂到左边导航 —— 它不是「清理」的一部分，
是「回看」；放在菜单里，不打断四模块的主线。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
)

from core import snapshot as snapshots
from core import restore as restorer
from core.fs_browser import human_size
from core.snapshot import Snapshot

MUTED_FG = QColor("#57606a")
OK_FG = QColor("#1a7f37")
FAIL_FG = QColor("#cf222e")
SKIP_FG = QColor("#9a6700")

COLS = ("时间", "操作", "条目", "结果", "能不能还原")

# 只可追溯的操作，统一用这句话说死，不给假按钮
TRACE_ONLY = "此操作不可撤销，仅留记录供排查"


class UndoCenterDialog(QDialog):
    """列出历史快照，能还原的给「还原」，不能的明说。"""

    def __init__(self, parent=None, log_dir=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("撤销中心")
        self.setMinimumSize(960, 620)
        self._log_dir = log_dir
        self._snapshots: list[Snapshot] = []

        title = QLabel("我做过的操作")
        font = QFont(title.font())
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        title.setFont(font)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color:{MUTED_FG.name()};")

        self.table = QTableWidget(0, len(COLS))
        self.table.setObjectName("undo_table")
        self.table.setHorizontalHeaderLabels(list(COLS))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 168)
        self.table.setColumnWidth(1, 104)
        self.table.setColumnWidth(2, 72)
        self.table.setColumnWidth(3, 150)
        self.table.itemSelectionChanged.connect(self._on_select)

        self.detail = QTextBrowser()
        self.detail.setObjectName("undo_detail")
        self.detail.setMinimumHeight(180)

        self.btn_restore = QPushButton("还原")
        self.btn_restore.setEnabled(False)
        self.btn_restore.clicked.connect(self._do_restore)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.reload)
        self.btn_clear = QPushButton("清空记录…")
        self.btn_clear.setToolTip("只删快照记录，不动回收站，也不动 backups 里的备份")
        self.btn_clear.clicked.connect(self._clear)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addWidget(self.btn_restore)
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        row.addWidget(self.btn_close)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self.hint)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.detail)
        layout.addLayout(row)

        self.reload()

    # -- 数据 --------------------------------------------------------------
    def reload(self) -> None:
        self._snapshots = snapshots.load_all(self._log_dir)
        total_bytes, count = snapshots.usage(self._log_dir)
        if not self._snapshots:
            self.hint.setText("还没有任何操作记录。每做一次批量操作，这里会留一条快照（只记名称和路径，不存内容）。")
        else:
            self.hint.setText(
                f"共 {count} 条快照，占 {human_size(total_bytes)}。"
                "快照只记「什么时候动过什么」，不存文件内容，也不存注册表键值。"
            )
        self.table.setRowCount(len(self._snapshots))
        for row, snapshot in enumerate(self._snapshots):
            cells = (
                (self._pretty_time(snapshot.time), None),
                (snapshot.op_label, None),
                (str(len(snapshot.items)), None),
                (snapshot.summary, self._result_color(snapshot)),
                (self._capability_text(snapshot), OK_FG if snapshot.can_restore else MUTED_FG),
            )
            for column, (text, color) in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if color is not None:
                    cell.setForeground(QBrush(color))
                self.table.setItem(row, column, cell)
        self.detail.setPlainText("")
        self.btn_restore.setEnabled(False)
        self.btn_clear.setEnabled(bool(self._snapshots))
        if self._snapshots:
            self.table.selectRow(0)

    @staticmethod
    def _pretty_time(raw: str) -> str:
        text = str(raw or "")
        if "T" in text:
            body = text.split("T", 1)[1] if not text.startswith("T") else text[1:]
            date = text.split("T", 1)[0]
            return f"{date} {body[:8]}".strip()
        return text

    @staticmethod
    def _result_color(snapshot: Snapshot) -> QColor:
        if snapshot.fail_count:
            return FAIL_FG
        if snapshot.skipped_count:
            return SKIP_FG
        if snapshot.ok_count:
            return OK_FG
        return MUTED_FG

    @staticmethod
    def _capability_text(snapshot: Snapshot) -> str:
        if snapshot.can_restore:
            return f"可还原（{snapshot.restore_hint}）"
        if snapshot.op_type == "delete" and snapshot.mode != "recycle":
            return "仅可追溯（彻底删除）"
        return f"仅可追溯（{TRACE_ONLY}）"

    def current(self) -> Snapshot | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        index = rows[0].row()
        return self._snapshots[index] if 0 <= index < len(self._snapshots) else None

    def _on_select(self) -> None:
        snapshot = self.current()
        if snapshot is None:
            self.detail.setPlainText("")
            self.btn_restore.setEnabled(False)
            return
        self.detail.setPlainText(self._render_detail(snapshot))
        self.btn_restore.setEnabled(snapshot.can_restore)

    @staticmethod
    def _render_detail(snapshot: Snapshot) -> str:
        lines = [
            f"操作：{snapshot.op_label}",
            f"时间：{snapshot.time}",
            f"结果：{snapshot.summary}",
        ]
        if snapshot.mode:
            lines.append(f"方式：{'放进回收站' if snapshot.mode == 'recycle' else '彻底删除'}")
        if snapshot.backup:
            lines.append(f"备份：backups/{snapshot.backup}")
        if snapshot.can_restore:
            lines.append(f"还原方式：{snapshot.restore_hint}")
        else:
            lines.append(f"还原方式：{TRACE_ONLY}")
        lines.append("")
        lines.append("条目：")
        for item in snapshot.items:
            state = snapshots.RESULT_LABELS.get(item.result, item.result or "未记录")
            risk = f"[{item.risk}]" if item.risk else ""
            line = f"  {state} {risk} {item.name}"
            if item.path and item.path != item.name:
                line += f"  →  {item.path}"
            if item.error:
                line += f"  （{item.error}）"
            lines.append(line)
        return "\n".join(lines)

    # -- 动作 --------------------------------------------------------------
    def _do_restore(self) -> None:
        snapshot = self.current()
        if snapshot is None or not snapshot.can_restore:
            return
        if snapshot.op_type == "clean":
            what = f"把 backups/{snapshot.backup} 里的 {snapshot.ok_count} 个键导回注册表"
        else:
            what = f"把 {snapshot.ok_count} 项从回收站还原回原来的位置"
        answer = QMessageBox.question(
            self,
            "确认还原",
            f"要{what}吗？\n\n"
            "还原会覆盖当前同名内容；如果你之后又往同一个位置放了东西，请先自己确认一下。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        ok, message = restorer.restore(snapshot)
        if ok:
            QMessageBox.information(self, "还原好了", message)
        else:
            QMessageBox.warning(self, "没能还原", message)
        self.reload()

    def _clear(self) -> None:
        answer = QMessageBox.question(
            self,
            "清空快照",
            "把全部操作快照删掉吗？\n\n"
            "只删这些记录文件。回收站里的东西不动，backups 里的 .reg 备份也不动。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        removed, freed = snapshots.clear(self._log_dir)
        QMessageBox.information(self, "清空了", f"删掉 {removed} 条快照，腾出 {human_size(freed)}。")
        self.reload()
