"""软件卸载页：列出已装的软件，勾选后交给各自的卸载程序。

这一页刻意不做「智能推荐卸载」——只把每个软件是什么、谁出的、装在哪、
占多大、能不能卸，摆清楚，删不删（卸不卸）由用户自己决定。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMessageBox,
    QTableView,
    QVBoxLayout,
)

from core.fs_browser import human_size
from core.knowledge import KnowledgeBase
from core.leftover import scan
from core.risk import Risk
from core.uninstall import (
    SoftwareItem,
    UninstallReport,
    UninstallWorker,
    enumerate_installed,
    looks_system,
)
from ui.confirm_flow import ConfirmFlow, is_red
from ui.page_base import ModulePage, build_empty_state
from ui.widgets.filter_bar import FilterBar
from ui.widgets.filter_mixin import FilterMixin

COL_NAME, COL_VERSION, COL_PUBLISHER, COL_DATE, COL_SIZE, COL_EFFECT, COL_RISK, COL_CHECK = range(8)
HEADERS = ("软件名", "版本", "发布者", "安装日期", "占用大小", "作用", "风险", "是否卸载")

GRAY = QColor("#8b949e")
CAUTION = QColor("#9a6700")
SAFE = QColor("#1a7f37")

_dot_cache: dict[str, object] = {}

def _dot(color: str):
    from PySide6.QtGui import QIcon, QPainter, QPixmap

    icon = _dot_cache.get(color)
    if icon is None:
        pixmap = QPixmap(12, 12)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(color)))
        painter.drawEllipse(3, 3, 6, 6)
        painter.end()
        icon = QIcon(pixmap)
        _dot_cache[color] = icon
    return icon

def _version_key(raw: str) -> tuple:
    """版本号按数字排：2.10 要排在 2.9 后面，不能按字符串比。"""
    parts = []
    for chunk in str(raw or "").replace("-", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else -1)
    return tuple(parts) or (-1,)

@dataclass
class SoftwareRow:
    """一行：注册表里的软件 + 知识库/AI 的判断。"""

    item: SoftwareItem
    effect: str = ""
    risk: str = "unlisted"
    source: str = ""          # kb / ai / fallback
    checked: bool = False
    locked: bool = False      # 没有卸载命令 → 勾不上
    ai_note: str = ""
    impact: str = ""          # 知识库写的那句后果；空 = 规则没写

    @property
    def identity(self) -> str:
        return self.item.key_path

    @property
    def name(self) -> str:
        return self.item.name

    @property
    def detail(self) -> str:
        return self.item.version

    @property
    def size_bytes(self) -> int | None:
        return self.item.size_bytes

class SoftwareModel(FilterMixin, QAbstractTableModel):
    checkedChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._all_rows: list[SoftwareRow] = []      # 数据源（过滤前的全量）
        self._rows: list[SoftwareRow] = []          # 当前可见
        self._row_by_id: dict[str, int] = {}
        self._filter_key = ""
        self._sort_column = -1
        self._sort_cycle = 0

    # -- Qt ----------------------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation != Qt.Horizontal:
            return section + 1 if role == Qt.DisplayRole else None
        if role == Qt.DisplayRole:
            return HEADERS[section] if 0 <= section < len(HEADERS) else None
        if role == Qt.TextAlignmentRole:
            if section in (COL_VERSION, COL_DATE, COL_SIZE, COL_RISK, COL_CHECK):
                return int(Qt.AlignCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        return None

    @staticmethod
    def is_checkable(row: SoftwareRow) -> bool:
        """全都能勾。没有卸载命令的勾上之后走「扫残留」那条路。"""
        return True

    def flags(self, index) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        row = self._rows[index.row()]
        if index.column() == COL_CHECK and self.is_checkable(row):
            return base | Qt.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.CheckStateRole and col == COL_CHECK:
            if not self.is_checkable(row):
                return None
            return Qt.Checked if row.checked else Qt.Unchecked
        if role == Qt.DisplayRole:
            return self._display(row, col)
        if role == Qt.DecorationRole and col == COL_EFFECT:
            return _dot(Risk.parse(row.risk).color) if row.effect else None
        if role == Qt.ToolTipRole:
            return self._tooltip(row, col)
        if role == Qt.TextAlignmentRole and col in (COL_VERSION, COL_DATE, COL_SIZE, COL_RISK, COL_CHECK):
            return int(Qt.AlignCenter)
        if role == Qt.ForegroundRole:
            if col == COL_RISK:
                return QBrush(Risk.parse(row.risk).color)
            if col == COL_EFFECT and Risk.parse(row.risk) is Risk.UNLISTED:
                return QBrush(GRAY)
            if col == COL_SIZE and row.size_bytes is None:
                return QBrush(GRAY)
        return None

    def setData(self, index, value, role=Qt.EditRole) -> bool:  # noqa: N802
        if not index.isValid() or role != Qt.CheckStateRole or index.column() != COL_CHECK:
            return False
        row = self._rows[index.row()]
        if not self.is_checkable(row):
            return False
        checked = value in (Qt.Checked, True, 2)
        if row.checked == checked:
            return False
        row.checked = checked
        self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        self.checkedChanged.emit()
        return True

    def _display(self, row: SoftwareRow, col: int) -> str:
        item = row.item
        if col == COL_NAME:
            return ("🔒 " if row.locked else "") + item.name
        if col == COL_VERSION:
            return item.version or "—"
        if col == COL_PUBLISHER:
            return item.publisher or "—"
        if col == COL_DATE:
            return item.install_date or "—"
        if col == COL_SIZE:
            return human_size(item.size_bytes) if item.size_bytes else "—"
        if col == COL_EFFECT:
            if row.locked:
                return "没有卸载命令，程序不会自己去删它的文件"
            text = row.effect or "未收录，建议确认后再卸"
            return f"AI · {text}" if row.source == "ai" else text
        if col == COL_RISK:
            return Risk.parse(row.risk).label
        return ""

    def _tooltip(self, row: SoftwareRow, col: int) -> str | None:
        item = row.item
        if col == COL_NAME:
            return f"{item.name}\n注册表：{item.key_path}\n卸载命令：{item.command or '（没有）'}"
        if col == COL_EFFECT:
            return row.effect or "未收录，建议确认后再卸"
        if col == COL_CHECK:
            if row.locked:
                return "这个软件没提供卸载命令，勾不上"
            return "勾上就会调用它自己的卸载程序"
        return None

    # -- 数据 --------------------------------------------------------------
    def set_rows(self, rows: Sequence[SoftwareRow]) -> None:
        self._all_rows = list(rows)
        self.rebuild_visible()
        self.checkedChanged.emit()

    def haystack(self, row: SoftwareRow) -> str:
        # 软件：名称 + 发布者
        return f"{row.name} {row.item.publisher}".lower()

    def rebuild_visible(self) -> None:
        self.beginResetModel()
        if self._filter_key:
            self._rows = [row for row in self._all_rows if self._matches(row)]
        else:
            self._rows = list(self._all_rows)
        if self._sort_column >= 0:
            self._rows.sort(key=self._sort_key(self._sort_column), reverse=self._sort_cycle == 2)
        self._row_by_id = {row.identity: index for index, row in enumerate(self._rows)}
        self.endResetModel()

    def rows(self) -> list[SoftwareRow]:
        return self._rows

    def row_at(self, index: int) -> SoftwareRow | None:
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def row_of(self, identity: str) -> int:
        return self._row_by_id.get(identity, -1)

    def checked_rows(self) -> list[SoftwareRow]:
        return [row for row in self._rows if row.checked and self.is_checkable(row)]

    def set_checked(self, identity: str, checked: bool) -> None:
        index = self.row_of(identity)
        if index < 0:
            return
        self.setData(self.index(index, COL_CHECK), Qt.Checked if checked else Qt.Unchecked, Qt.CheckStateRole)

    def set_all_checked(self, checked: bool) -> None:
        if not self._rows:
            return
        for row in self._rows:
            if self.is_checkable(row):
                row.checked = checked
        self.dataChanged.emit(self.index(0, COL_CHECK),
                              self.index(len(self._rows) - 1, COL_CHECK), [Qt.CheckStateRole])
        self.checkedChanged.emit()

    def clear_checks(self) -> None:
        self.set_all_checked(False)

    def check_state(self) -> Qt.CheckState:
        selectable = [row for row in self._rows if self.is_checkable(row)]
        if not selectable:
            return Qt.Unchecked
        checked = sum(1 for row in selectable if row.checked)
        if checked == 0:
            return Qt.Unchecked
        if checked == len(selectable):
            return Qt.Checked
        return Qt.PartiallyChecked

    def update_ai(self, identity: str, purpose: str, impact: str, risk: str, note: str) -> None:
        index = self.row_of(identity)
        if index < 0:
            return
        row = self._rows[index]
        row.effect = purpose
        row.source = "ai"
        row.impact = ""          # AI 的影响单独展示，不冒充知识库的 impact
        row.ai_note = note
        # AI 只能把风险说得更严，不能把知识库判的红说成能卸
        if Risk.parse(risk).rank > Risk.parse(row.risk).rank:
            row.risk = risk
        left = self.index(index, COL_EFFECT)
        right = self.index(index, COL_RISK)
        self.dataChanged.emit(left, right, [Qt.DisplayRole, Qt.ToolTipRole, Qt.DecorationRole, Qt.ForegroundRole])

    def _sort_key(self, column: int):
        return {
            COL_NAME: lambda r: r.name.lower(),
            COL_VERSION: lambda r: _version_key(r.item.version),
            COL_PUBLISHER: lambda r: r.item.publisher.lower(),
            COL_DATE: lambda r: r.item.install_date,
            COL_SIZE: lambda r: r.item.size_bytes or -1,
            COL_EFFECT: lambda r: r.effect,
            COL_RISK: lambda r: Risk.parse(r.risk).rank,
            COL_CHECK: lambda r: r.checked,
        }.get(column) or (lambda r: r.name.lower())

    def cycle_sort(self, column: int):
        """点一次换个态：默认 → 升 → 降 → 默认。返回 (箭头列, 方向)，列 -1 表示不排。"""
        if column != self._sort_column:
            self._sort_column = column
            self._sort_cycle = 1
        else:
            self._sort_cycle = (self._sort_cycle + 1) % 3
            if self._sort_cycle == 0:
                self._sort_column = -1
        self.rebuild_visible()
        order = Qt.DescendingOrder if self._sort_cycle == 2 else Qt.AscendingOrder
        return self._sort_column, order

    def sort(self, column: int, order=Qt.AscendingOrder) -> None:  # noqa: N802
        """保留 Qt 的入口：外部直接调也能用。"""
        self._sort_column = column
        self._sort_cycle = 2 if order == Qt.DescendingOrder else 1
        self.rebuild_visible()

class UninstallView(ModulePage):
    """软件卸载页。"""

    openPathRequested = Signal(str)     # 让主窗口切到文件模块并导航过去

    def __init__(self, knowledge: KnowledgeBase, settings, cache=None, parent=None) -> None:
        super().__init__("software", "软件卸载", parent)
        self.kb = knowledge
        self.settings = settings
        self.cache = cache
        self.ai = None                  # 主窗口在 bootstrap 里注入
        self.model = SoftwareModel(self)
        self.model.checkedChanged.connect(self._on_checked_changed)
        self._worker = None
        self._loading = False
        # 卸载是异步的：调起外部程序就返回了，条目还在注册表里。
        # 所以过一会儿自动重刷，直到这些软件真的从列表消失。
        self._scan_asks: set[str] = set()     # 已经问过「要不要扫残留」的项
        self._pending_scan: list = []          # 等卸载完成后再扫的软件
        # 卸载前把每个软件的 exe 名记下来 —— 卸载程序一跑，安装目录就没了，
        # 那时候再去目录里找 exe 已经来不及。
        self._exe_cache: dict[str, list[str]] = {}
        self._poll_keys: set[str] = set()
        self._poll_left = 0
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._poll_uninstalled)

        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setSortingEnabled(False)     # 三态排序自己管，交给 Qt 只有升/降两态
        self.table.sortByColumn(COL_NAME, Qt.AscendingOrder)
        self.table.setTextElideMode(Qt.ElideRight)
        header = self.table.horizontalHeader()
        # 名称列用 Interactive：Stretch 的列宽是自动算的，用户拖不动
        header.setSectionResizeMode(COL_NAME, QHeaderView.Interactive)
        self.table.setColumnWidth(COL_NAME, 300)
        header.setSectionResizeMode(COL_VERSION, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_PUBLISHER, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_DATE, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_SIZE, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_EFFECT, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_RISK, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_VERSION, 130)
        self.table.setColumnWidth(COL_PUBLISHER, 150)
        self.table.setColumnWidth(COL_DATE, 100)
        self.table.setColumnWidth(COL_SIZE, 100)
        self.table.setColumnWidth(COL_RISK, 76)
        self.table.setColumnWidth(COL_CHECK, 92)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_menu)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)

        self.empty = build_empty_state("软件卸载", "正在读取这台电脑上装了哪些软件…")

        self.filter_bar = FilterBar("在软件名 / 发布者里找…")
        self.filter_bar.changed.connect(self._on_filter_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.filter_bar)
        layout.addWidget(self.empty, 1)
        layout.addWidget(self.table, 1)
        self.table.setVisible(False)
        self.set_selection_text("已选 0 项")

    def _on_header_clicked(self, column: int) -> None:
        """点列头循环：默认 → 升 → 降 → 默认。"""
        col, order = self.model.cycle_sort(column)
        header = self.table.horizontalHeader()
        if col < 0:
            header.setSortIndicator(-1, Qt.AscendingOrder)
        else:
            header.setSortIndicator(col, order)

    def _force_uninstall(self, row) -> None:
        """不走卸载程序，直接删目录和注册表。对着卸载程序坏掉的软件用。"""
        from core.force import build_targets, force_uninstall, is_protected

        item = row.item
        allowed, reason = is_protected(item)
        if not allowed:
            QMessageBox.warning(self, "这一项不能强制卸载", reason)
            return

        targets = build_targets(item)
        if not targets:
            QMessageBox.information(
                self, "没东西可删",
                "这个软件的安装目录和注册表键都不在了，可能已经删过了。",
            )
            return

        detail = "\n".join(f"· {t.kind_label}：{t.path}" for t in targets)
        answer = QMessageBox.question(
            self,
            "真的要强制卸载吗",
            f"不调用它自己的卸载程序，直接删掉下面这些：\n\n{detail}\n\n"
            "文件进回收站（能还原），注册表键会先备份到 backups/。\n"
            "这一步做完，软件就没了，跟官方卸载不完全是一回事。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self.statusMessage.emit(f"正在强制卸载 {item.name}…")
        report = force_uninstall(item, self._log_dir())
        if report.fail_count == 0:
            QMessageBox.information(self, "清完了", report.summary)
        else:
            reasons = "\n".join(f"· {p}：{r}" for p, r in report.failure_reasons()[:5])
            QMessageBox.warning(self, "没清干净", f"{report.summary}\n\n{reasons}")
        self.refresh()

    def _send_feedback(self, path: str, name: str, purpose: str, module: str = "") -> None:
        from ui.dialogs import FeedbackDialog

        FeedbackDialog(path=path, name=name, purpose=purpose, module=module, parent=self).exec()

    # -- 过滤 --------------------------------------------------------------
    def _on_filter_changed(self, text: str) -> None:
        """只在内存里筛，不重新读注册表。"""
        self.model.set_filter(text)
        self.filter_bar.set_counts(self.model.rowCount(), self.model.total_count())
        self._on_checked_changed()

    # -- 基类要求 ----------------------------------------------------------
    def refresh(self) -> None:
        if self._loading:
            return
        self.filter_bar.set_pending("")
        self.model.set_filter("")      # set_pending 屏蔽了信号，模型的过滤词得手动清
        self._loading = True
        try:
            self._reload()
        finally:
            # 这里必须复位：之前漏了，第一次刷新之后 _loading 永远是 True，
            # 后面所有刷新（包括卸载完那次）都被挡在门外 —— 表现就是「刷新没用」。
            self._loading = False

    def _reload(self) -> None:
        """真正读注册表并重建列表。refresh() 负责加锁和复位。"""
        try:
            items, warnings = enumerate_installed()
        except Exception:  # noqa: BLE001 - 读注册表出意外也不能把页面搞崩
            items, warnings = [], ["读取已安装软件时出了点问题"]
        rows: list[SoftwareRow] = []
        for item in items:
            result = self.kb.match_software(item.name, item.publisher)
            risk = result.risk if result.known else "unlisted"
            effect = result.effect if result.known else ""
            # 运行库/驱动/补丁这类组件哪怕是知识库没收录的，也按红色处理：
            # 卸掉它们影响的不是你，是别人。仍然可以勾，只是要说清楚。
            if item.hidden and not effect:
                effect = "系统默认不显示的组件（SystemComponent/NoDisplay）；这里照样列出来"
            if item.source == "orphan":
                # 没有卸载程序，也没有登记 —— 只能靠扫残留 + 手动清
                effect = "未登记录（卸载列表里找不到它，但目录还在）；勾选后会扫描它的文件和注册表"
                if not risk or risk == "unlisted":
                    risk = "caution"
            elif looks_system(item.name, item.publisher):
                if Risk.parse(risk).rank < Risk.parse("forbidden").rank:
                    risk = "forbidden"
                if not effect:
                    effect = "这是运行库 / 驱动 / 补丁一类的组件，卸掉可能影响别的软件。"
            if not result.known:
                cached = self._cached_ai(item)
                if cached:
                    effect = str(cached.get("purpose") or "")
                    incoming = Risk.parse(str(cached.get("risk") or "caution"))
                    if incoming.rank > Risk.parse(risk).rank:
                        risk = incoming.value
                    rows.append(SoftwareRow(item=item, effect=effect, risk=risk, source="ai",
                                            locked=not item.can_uninstall))
                    continue
            rows.append(SoftwareRow(item=item, effect=effect, risk=risk,
                                    source="kb" if result.known else "fallback",
                                    locked=not item.can_uninstall,
                                    impact=result.impact if result.known else ""))
        self.model.set_rows(rows)
        has_rows = bool(rows)
        self.table.setVisible(has_rows)
        self.empty.setVisible(not has_rows)
        if has_rows:
            self._last_status = "已选 0 项"
        else:
            self.empty.setText("<h3 style='color:#1f2328'>没有读到已安装的软件</h3>"
                               "<p style='color:#57606a'>可能是注册表读不到，或者这台电脑确实很干净。</p>")
        if warnings:
            self.statusMessage.emit(warnings[0])

    def clear_checks(self) -> None:
        self.model.clear_checks()

    def checked_items(self) -> list:
        return self.model.checked_rows()

    def cancel_running(self) -> None:
        if hasattr(self, "_poll_timer"):
            self._poll_timer.stop()
        if self.ai is not None:
            self.ai.cancel_all()

    def export_headers(self) -> tuple[str, ...]:
        return ("软件名", "版本", "发布者", "安装日期", "占用大小", "作用", "风险")

    def export_rows(self) -> list[tuple]:
        from ui.widgets.export_csv import size_text
        from core.risk import Risk

        return [
            (row.name, row.item.version, row.item.publisher, row.item.install_date,
             size_text(row.item.size_bytes), row.effect, Risk.parse(row.risk).label)
            for row in self.model.rows()
        ]

    def status_line(self) -> str:
        count = self.model.rowCount()
        return f"软件卸载 · 共 {count} 个软件" if count else "软件卸载 · 没有读到已安装的软件"

    # -- 勾选 --------------------------------------------------------------
    def _on_checked_changed(self) -> None:
        rows = self.model.checked_rows()
        self.set_selection_text(self._build_selection_text(rows))
        # 有些软件没有卸载命令（锁定项）。勾上它意味着「我要清它的残留」，
        # 所以问一句，别让用户以为点了就能卸。
        no_command = [row for row in rows
                      if not row.item.can_uninstall and row.identity not in self._scan_asks]
        if no_command and self.isVisible():
            names = "、".join(row.name for row in no_command[:3])
            answer = QMessageBox.question(
                self,
                "这个软件没有卸载命令",
                f"{names} 没有登记卸载程序，程序没法替你卸它。\n\n"
                "要不要现在扫一遍它留下的文件和注册表，让你手动清理？\n"
                "（取消的话这一项会被取消勾选）",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            for row in no_command:
                self._scan_asks.add(row.identity)
            if answer == QMessageBox.Yes:
                self._scan_and_show([row.item for row in no_command])
                for row in no_command:
                    self.model.set_checked(row.identity, False)
            else:
                for row in no_command:
                    self.model.set_checked(row.identity, False)
    def _build_selection_text(self, rows: Sequence[SoftwareRow]) -> str:
        if not rows:
            return "已选 0 项"
        sizes = [row.item.size_bytes for row in rows]
        text = f"已选 {len(rows)} 项"
        if all(size is not None for size in sizes):
            total = human_size(sum(sizes))
            text += f"，约 {total}（卸载由各自程序执行，实际释放以完成情况为准）"
        red = sum(1 for row in rows if is_red(row))
        if red:
            text += f" · 其中 {red} 项标着「别删」"
        hidden = self.model.hidden_checked_count()
        if hidden:
            text += f" · 另有 {hidden} 项被过滤藏起来了但还勾着"
        if self.model.is_filtered():
            text += "（已过滤，仅对可见项生效）"
        return text

    # -- AI ----------------------------------------------------------------
    def _cached_ai(self, item: SoftwareItem) -> dict | None:
        """知识库没收录时，看看以前有没有问过 AI。知识库命中时轮不到这里。"""
        if self.cache is None:
            return None
        try:
            from core.config import CacheStore

            data = self.cache.get_ai_json(CacheStore.ai_key_item("software", item.key_path, item.name))
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) and data.get("purpose") else None

    def attach_ai(self, client) -> None:
        """主窗口把共用的 AI client 注入进来。"""
        self.ai = client
        if client is not None:
            client.resultReady.connect(self._on_ai_result)

    def _on_ai_result(self, result) -> None:
        row_index = self.model.row_of(result.human_path)
        if row_index < 0:
            return
        self.model.update_ai(result.human_path, result.purpose, result.impact, result.risk, result.note)

    def explain_unknown(self) -> None:
        if self.ai is None:
            self.statusMessage.emit("联网识别还没准备好")
            return
        if not self.ai.is_configured():
            # 四个模块共用同一个引导框，由主窗口弹
            self.aiSetupRequested.emit()
            return
        rows = [row for row in self.model.rows()
                if row.source != "kb" and not row.locked]
        if not rows:
            self.statusMessage.emit("这一页没有知识库不认识的软件")
            return
        answer = QMessageBox.question(
            self,
            "联网识别",
            f"{len(rows)} 个软件知识库不认识，将逐项问一遍，是否继续？\n\n"
            "发出去的只有软件名、发布者、版本和安装位置，不会发任何文件内容。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        pairs = [(row.identity, row.name) for row in rows]
        extras = [
            {
                "publisher": row.item.publisher,
                "version": row.item.version,
                "location": row.item.install_location,
            }
            for row in rows
        ]
        self.ai.explain_many(pairs, item_type="software", extras=extras)

    # -- 执行 --------------------------------------------------------------
    def execute_checked(self) -> None:
        rows = list(self.model.checked_rows())
        if not rows:
            self.statusMessage.emit("还没有勾选要卸载的软件")
            return
        # 只勾了「没有卸载命令」的那种：没什么可卸的，直接扫残留
        normal = [row for row in rows if row.item.can_uninstall]
        if not normal:
            self._scan_and_show([row.item for row in rows])
            self.model.clear_checks()
            return
        rows = normal
        decision = ConfirmFlow.confirm(
            self,
            rows,
            "卸载",
            hint="下面每一项都可以单独取消。「卸载」是调用软件自带的卸载程序，不是直接删文件夹。",
            irreversible=False,
        )
        if decision is None or not decision.items:
            return
        items = [row.item for row in decision.items]
        from core import snapshot as snapshots

        self._remember_exe_names(rows)      # 卸载一跑目录就没了，先把 exe 名记下来
        self._snap_id = snapshots.begin("uninstall", rows)
        worker = UninstallWorker(items, self._log_dir())
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)
        self._worker = worker
        self.statusMessage.emit("正在调起卸载程序…")
        QThreadPool.globalInstance().start(worker)

    def _remember_exe_names(self, rows) -> None:
        """卸载前扫一遍安装目录，把 exe 名存下来。

        卸载程序一跑，安装目录就没了，那时候再去目录里找 exe 已经来不及。
        而 Prefetch / 兼容性标记是按每个 exe 各记一份的，少一个 exe 就少扫几项。
        """
        for row in rows:
            try:
                names = list(row.item.exe_names)
            except Exception:  # noqa: BLE001
                continue
            if names:
                self._exe_cache[row.item.key_path] = names

    def _exe_names_of(self, software) -> list[str]:
        cached = self._exe_cache.get(software.key_path)
        if cached:
            return list(cached)
        try:
            return list(software.exe_names)
        except Exception:  # noqa: BLE001
            return []

    def _log_dir(self) -> Path:
        from app_paths import resolve

        return resolve().logs

    def _on_progress(self, done: int, total: int, name: str) -> None:
        if name:
            self.statusMessage.emit(f"正在调起卸载程序 {done + 1}/{total} · {name}")

    def _on_finished(self, report: UninstallReport) -> None:
        from core import snapshot as snapshots

        snapshots.finish(getattr(self, "_snap_id", None), getattr(report, "items", []), self._log_dir())
        self._worker = None
        self.model.clear_checks()
        self.refresh()
        self.statusMessage.emit(report.summary)
        from ui.dialogs import DeleteReportDialog

        dialog = DeleteReportDialog(
            report, report.log_path, self,
            on_retry=self._retry_uninstall,
            op_type="uninstall",
            # 「知道了」换成「扫描卸载残余」：点它就直接扫，不等轮询
            on_scan=lambda: self._scan_after_report(report),
        )
        dialog.exec()

        # 调起只是「叫醒」卸载程序，它还在跑。现在扫出来的「残留」其实是没删完的完整目录，
        # 所以先盯着等它真的卸完，卸完那一刻再扫 —— 那时候剩下的才是真残留。
        ok_keys = [result.key_path for result in report.items if result.ok]
        pending = []
        for result in report.items:
            if not result.ok:
                continue
            row = self.model.row_of(result.key_path)
            if row >= 0:
                pending.append(self.model.rows()[row].item)
        self._pending_scan = pending
        self._start_uninstall_poll(ok_keys)

    def _after_uninstall_complete(self) -> None:
        """卸载程序跑完之后再扫。再等 1.5 秒，让它把最后几个文件也删干净。"""
        softwares = list(getattr(self, "_pending_scan", []))
        self._pending_scan = []
        if not softwares:
            return
        QTimer.singleShot(1500, lambda: self._scan_and_show(softwares))

    def _scan_after_report(self, report) -> None:
        """报告里点了「扫描卸载残余」：按当前状态立刻扫。"""
        softwares = []
        for result in report.items:
            if not result.ok:
                continue
            row = self.model.row_of(result.key_path)
            if row >= 0:
                softwares.append(self.model.rows()[row].item)
        self._pending_scan = []          # 手动扫过了，轮询那边不用再扫
        if softwares:
            self._scan_and_show(softwares)

    def _scan_and_show(self, softwares) -> None:
        """扫这几个软件的残留并弹清单。卸载完和「无命令手工勾选」共用这一条。"""
        found = []
        for software in softwares:
            try:
                found.extend(scan(
                    software.install_location, software.publisher, software.name,
                    exe_names=self._exe_names_of(software),
                ))
            except Exception:  # noqa: BLE001 - 扫不出来也不能让流程崩
                continue
        if not found:
            self.statusMessage.emit("没扫到残留，这个软件卸得挺干净")
            return
        from ui.leftover_dialog import LeftoverDialog as CleanDialog, describe

        self.statusMessage.emit(f"找到 {describe(found)}，勾选后可以清理")
        # 只卸了一个软件时，AI 就只管这一个；一次卸多个就退回全局模式
        focus = softwares[0] if len(softwares) == 1 else None
        from core.scan_scopes import default_keys

        dialog = CleanDialog(
            found, self, on_open_path=self.openPathRequested.emit,
            ai=self.ai, installed=[row.item for row in self.model.rows()],
            focus=focus,
            # AI 再扫时用的是用户勾过的那批位置，不是默认那套
            scopes=list(self.settings.scan_scopes) or default_keys(),
        )
        dialog.exec()
        if dialog.report is not None:
            self._last_leftover_report = dialog.report
            self._write_leftover_log(dialog.report)
            self.refresh()

    def _start_uninstall_poll(self, key_paths) -> None:
        """卸载调起来之后开始盯着：每 2 秒重读一次，直到条目消失或等满 60 秒。"""
        keys = {str(key) for key in key_paths if key}
        if not keys:
            return
        self._poll_keys = keys
        self._poll_left = 30          # 30 次 × 2 秒
        self._poll_timer.start()
        self.statusMessage.emit(
            f"卸载程序已启动，正在等它自己跑完…（{len(keys)} 个，最多等 60 秒）"
        )

    def _poll_uninstalled(self) -> None:
        self._poll_left -= 1
        self.refresh()
        still_there = {key for key in self._poll_keys if self.model.row_of(key) >= 0}
        gone = len(self._poll_keys) - len(still_there)
        self._poll_keys = still_there

        if not still_there:
            self._poll_timer.stop()
            self.statusMessage.emit(
                f"{gone} 个软件已从列表消失，卸载程序应该跑完了 —— 正在扫残留…"
            )
            self._after_uninstall_complete()
            return
        if self._poll_left <= 0:
            self._poll_timer.stop()
            self.statusMessage.emit(
                f"还有 {len(still_there)} 个没消失 —— 可能卸载程序点一下就没了，"
                "也可能它在等你点「下一步」。仍按现在的情况扫一遍"
            )
            self._after_uninstall_complete()
            return
        self.statusMessage.emit(
            f"卸载程序还在跑，列表里还剩 {len(still_there)} 个没消失"
            f"（{self._poll_left * 2} 秒后停止检查）"
        )

    def _write_leftover_log(self, report) -> None:
        """残余清理也写一份记录，和卸载记录放一起。"""
        import time as _time

        try:
            directory = self._log_dir()
            directory.mkdir(parents=True, exist_ok=True)
            stamp = _time.strftime("%Y%m%d-%H%M%S")
            path = directory / f"卸载残余-{stamp}.txt"
            path.write_text(report.as_log_text(), encoding="utf-8")
            try:
                report.log_path = path
            except AttributeError:
                pass
        except OSError:
            pass

    def _retry_uninstall(self, rows) -> None:
        """重试没调起来的卸载：按注册表键路径重新找一遍那条记录。"""
        from core.uninstall import enumerate_installed

        items, _warnings = enumerate_installed()
        by_key = {item.key_path: item for item in items}
        picked = [by_key[row.path] for row in rows if row.path in by_key]
        if not picked:
            self.statusMessage.emit("这些软件已经不在卸载列表里了，没什么可重试的")
            return
        from core import snapshot as snapshots

        self._snap_id = snapshots.begin("uninstall", picked)
        worker = UninstallWorker(picked, self._log_dir())
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)
        self._worker = worker
        self.statusMessage.emit("正在重试…")
        QThreadPool.globalInstance().start(worker)

    # -- 右键 --------------------------------------------------------------
    def _show_menu(self, pos) -> None:
        from PySide6.QtWidgets import QMenu

        index = self.table.indexAt(pos)
        row = self.model.row_at(index.row()) if index.isValid() else None
        menu = QMenu(self)
        if row is not None:
            kind = menu.addAction(f"安装器：{row.item.kind_label}")
            kind.setEnabled(False)
            menu.addSeparator()
            menu.addAction("复制软件名", lambda: self._copy(row.name))
            menu.addAction("复制卸载命令", lambda: self._copy(row.item.command))
            if row.item.quiet_command:
                menu.addAction("复制静默卸载命令", lambda: self._copy(row.item.quiet_command))
            menu.addSeparator()
            menu.addAction("⚡ 强制卸载（删目录 + 删注册表）",
                           lambda: self._force_uninstall(row))
            location = row.item.install_location
            open_action = menu.addAction("在文件模块里打开安装位置",
                                         lambda: self.openPathRequested.emit(location))
            open_action.setEnabled(bool(location and os.path.isdir(location)))
            menu.addSeparator()
            if row.locked:
                cannot = menu.addAction("这一项没有卸载命令，勾不上")
                cannot.setEnabled(False)
            elif row.checked:
                menu.addAction("取消勾选", lambda: self.model.set_checked(row.identity, False))
            else:
                menu.addAction("勾选这一项", lambda: self.model.set_checked(row.identity, True))
            menu.addSeparator()
            menu.addAction("这条说明不对…", lambda: self._send_feedback(
                row.item.key_path, row.name, row.effect, "software"))
            menu.addSeparator()
            ai_action = menu.addAction("AI 识别此项", lambda: self._explain_one(row))
            ai_action.setEnabled(self.ai is not None)
            if row.locked:
                # 没有卸载命令，多半是普通权限读不到 UninstallString
                menu.addSeparator()
                menu.addAction("🔒 需要管理员权限，点此重启", self.adminRestartRequested.emit)
            menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy(self, text: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text or "")

    def _explain_one(self, row: SoftwareRow) -> None:
        if self.ai is None:
            return
        self.ai.explain(
            row.identity,
            row.name,
            item_type="software",
            extra={
                "publisher": row.item.publisher,
                "version": row.item.version,
                "location": row.item.install_location,
            },
        )
