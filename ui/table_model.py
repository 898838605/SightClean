"""文件列表模型：6 列，只做展示与勾选，不碰磁盘。

勾选状态存在行对象上，切目录换掉整批数据时自然清空（对应约束 7）。
同一时刻的勾选只属于当前这一页。
"""
from __future__ import annotations

import os
import time
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QStyle

from core.fs_browser import FsEntry, human_size
from core.risk import ADVICE_LABEL, Risk

COL_NAME, COL_MTIME, COL_KIND, COL_SIZE, COL_PURPOSE, COL_CHECK = range(6)
HEADERS = ("名称", "修改日期", "类型", "大小", "作用", "是否删除")

GRAY = QColor("#8b949e")
LOCK_GRAY = QColor("#b0b4b9")

_dot_cache: dict[str, QIcon] = {}
_icon_cache: dict[str, QIcon] = {}


def _dot(color: str) -> QIcon:
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


def _std_icon(is_dir: bool) -> QIcon:
    key = "dir" if is_dir else "file"
    icon = _icon_cache.get(key)
    if icon is None:
        style = QApplication.style() or QApplication.instance().style()
        pixmap = {"dir": QStyle.SP_DirIcon, "file": QStyle.SP_FileIcon}[key]
        icon = style.standardIcon(pixmap)
        _icon_cache[key] = icon
    return icon


def _fmt_mtime(ts: float) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (OSError, ValueError, OverflowError):
        return "—"


class FsTableModel(QAbstractTableModel):
    checkedChanged = Signal()

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._all_entries: list[FsEntry] = []      # 数据源（过滤前的全量）
        self._rows: list[FsEntry] = []             # 当前可见
        self._row_by_path: dict[str, int] = {}
        self._filter_key = ""
        self._sort_column = -1        # -1 = 默认顺序（不排）
        self._sort_cycle = 0          # 0 默认 / 1 升 / 2 降

    # -- Qt 接口 ------------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return HEADERS[section] if 0 <= section < len(HEADERS) else None
            if role == Qt.TextAlignmentRole:
                if section == COL_SIZE:
                    return int(Qt.AlignRight | Qt.AlignVCenter)
                if section in (COL_MTIME, COL_KIND, COL_CHECK):
                    return int(Qt.AlignCenter)
                return int(Qt.AlignLeft | Qt.AlignVCenter)
            return None
        if role == Qt.DisplayRole:
            return section + 1
        return None

    @staticmethod
    def is_checkable(entry: FsEntry) -> bool:
        """能不能勾选删除。

        只有读不到的项不给勾 —— 那种情况程序连它是什么都不知道，也删不动。
        红色「别删」的照样能勾：删不删是用户自己的决定，程序只负责把风险写清楚，
        替他拦着反而是另一种形式的「替用户做决定」。
        """
        return not entry.locked and entry.deletable

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        entry = self._rows[index.row()]
        if index.column() == COL_CHECK and self.is_checkable(entry):
            return base | Qt.ItemIsUserCheckable
        return base

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        entry = self._rows[index.row()]
        col = index.column()

        if role == Qt.CheckStateRole and col == COL_CHECK:
            if not self.is_checkable(entry):
                return None
            return Qt.Checked if entry.checked else Qt.Unchecked

        if role == Qt.DisplayRole:
            return self._display(entry, col)

        if role == Qt.DecorationRole:
            if col == COL_NAME:
                return _std_icon(entry.is_dir)
            if col == COL_PURPOSE:
                return _dot(Risk.parse(entry.risk).color) if entry.purpose else None
            return None

        if role == Qt.ToolTipRole:
            return self._tooltip(entry, col)

        if role == Qt.TextAlignmentRole:
            if col == COL_SIZE:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if col in (COL_CHECK, COL_KIND, COL_MTIME):
                return int(Qt.AlignCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)

        if role == Qt.ForegroundRole:
            if col == COL_NAME:
                if entry.locked:
                    return QBrush(LOCK_GRAY)
                if entry.hidden:
                    return QBrush(GRAY)
            if col == COL_SIZE and entry.size_state in ("denied", "none") and entry.is_dir:
                return QBrush(GRAY)
            return None

        return None

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.EditRole) -> bool:  # noqa: N802
        if not index.isValid() or role != Qt.CheckStateRole or index.column() != COL_CHECK:
            return False
        entry = self._rows[index.row()]
        if not self.is_checkable(entry):
            return False
        checked = value in (Qt.Checked, True, 2)
        if entry.checked == checked:
            return False
        entry.checked = checked
        self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        self.checkedChanged.emit()
        return True

    # -- 展示细节 -----------------------------------------------------------
    def _display(self, entry: FsEntry, col: int) -> str:
        if col == COL_NAME:
            name = f"🔒 {entry.name}" if entry.locked else entry.name
            return name
        if col == COL_MTIME:
            return _fmt_mtime(entry.mtime)
        if col == COL_KIND:
            return entry.kind_label or ("文件夹" if entry.is_dir else "文件")
        if col == COL_SIZE:
            if entry.locked:
                return "—"
            if entry.is_dir:
                if entry.size_state in ("queued", "running"):
                    return "…"
                if entry.size_state == "denied":
                    return "打不开"
                if entry.size is None:
                    return "—"
                return human_size(entry.size, approx=entry.size_state == "partial")
            return human_size(entry.size)
        if col == COL_PURPOSE:
            if entry.locked:
                return "无权限访问"
            text = entry.purpose or "不认识的类型，删之前自己看一眼"
            return f"AI · {text}" if entry.purpose_source == "ai" else text
        return ""

    def _tooltip(self, entry: FsEntry, col: int) -> str | None:
        if col == COL_NAME:
            return entry.human_path
        if col == COL_SIZE:
            if entry.is_dir and entry.size_state == "partial":
                return "里面有一部分打不开或者算不进去，这个数字是估算的"
            if entry.is_dir and entry.size_state == "denied":
                return "这个文件夹打不开，读不到里面有多少东西"
            return entry.human_path
        if col == COL_PURPOSE:
            level = Risk.parse(entry.risk)
            lines = [entry.purpose or "不认识的类型，删之前自己看一眼"]
            lines.append(f"风险：{level.label}")
            if entry.risk_reason:
                lines.append(entry.risk_reason)
            lines.append(f"建议：{ADVICE_LABEL.get(entry.advice, '看情况')}")
            if entry.impact:
                lines.append(f"删了会怎样：{entry.impact}")
            if entry.purpose_source == "ai":
                lines.append(entry.ai_note or "AI 判断，请确认")
            return "\n".join(lines)
        if col == COL_CHECK:
            if entry.locked:
                return "这一项打不开，不参与删除"
            if entry.risk == "forbidden":
                return "这一项标着「别删」，勾之前请确认你真的不要它了"
            return "勾上就会把这一整项删掉" + ("（连同里面的所有东西）" if entry.is_dir else "")
        return None

    # -- 数据装载 -----------------------------------------------------------
    def set_entries(self, entries: list[FsEntry]) -> None:
        # 数据源单独存一份：过滤只改「看得见的那一份」，勾选状态还挂在原对象上
        self._all_entries = list(entries)
        self._rebuild_rows()
        self.checkedChanged.emit()

    def set_filter(self, text: str) -> None:
        """只过滤已经加载的数据，不触发任何磁盘访问。"""
        key = str(text or "").strip().lower()
        if key == self._filter_key:
            return
        self._filter_key = key
        self._rebuild_rows()
        self.checkedChanged.emit()

    def filter_key(self) -> str:
        return self._filter_key

    def is_filtered(self) -> bool:
        return bool(self._filter_key)

    def _matches(self, entry: FsEntry) -> bool:
        key = self._filter_key
        name = entry.name.lower()
        if key in name:
            return True
        # 扩展名单独认：输入 "tmp" 也能命中 a.tmp
        ext = os.path.splitext(name)[1].lstrip(".")
        return bool(ext) and key == ext

    def _rebuild_rows(self) -> None:
        self.beginResetModel()
        if self._filter_key:
            self._rows = [entry for entry in self._all_entries if self._matches(entry)]
        else:
            self._rows = list(self._all_entries)
        if self._sort_column >= 0:
            self._rows.sort(key=self._sort_key(self._sort_column), reverse=self._sort_cycle == 2)
        self._row_by_path = {entry.human_path: row for row, entry in enumerate(self._rows)}
        self.endResetModel()

    def hidden_checked_count(self) -> int:
        """被过滤藏起来、但仍然勾着的项数。执行时它们也会被处理，必须让用户知道。"""
        if not self._filter_key:
            return 0
        visible = {id(entry) for entry in self._rows}
        return sum(1 for entry in self._all_entries
                   if entry.checked and self.is_checkable(entry) and id(entry) not in visible)

    def total_count(self) -> int:
        return len(self._all_entries)

    def entries(self) -> list[FsEntry]:
        return self._rows

    def entry_at(self, row: int) -> FsEntry | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def row_of(self, human_path: str) -> int:
        return self._row_by_path.get(human_path, -1)

    # -- 勾选 ---------------------------------------------------------------
    def checked_entries(self) -> list[FsEntry]:
        # 含被过滤藏起来的：用户勾过的东西不能因为搜了个词就悄悄不算数
        return [entry for entry in self._all_entries if entry.checked and self.is_checkable(entry)]

    def checked_paths(self) -> list[str]:
        return [entry.human_path for entry in self._all_entries if entry.checked and self.is_checkable(entry)]

    def clear_checks(self) -> None:
        changed = False
        for entry in self._all_entries:
            if entry.checked:
                entry.checked = False
                changed = True
        if changed and self._rows:
            top = self.index(0, COL_CHECK)
            bottom = self.index(len(self._rows) - 1, COL_CHECK)
            self.dataChanged.emit(top, bottom, [Qt.CheckStateRole])
        self.checkedChanged.emit()

    def set_all_checked(self, checked: bool) -> None:
        # 只作用于当前可见行：用户搜出 3 个再点全选，不该把藏起来的也勾上
        if not self._rows:
            return
        for entry in self._rows:
            if self.is_checkable(entry):
                entry.checked = checked
        top = self.index(0, COL_CHECK)
        bottom = self.index(len(self._rows) - 1, COL_CHECK)
        self.dataChanged.emit(top, bottom, [Qt.CheckStateRole])
        self.checkedChanged.emit()

    def check_state(self) -> Qt.CheckState:
        """当前页的勾选总状态，给表头三态复选框用。不参与勾的项不计入。"""
        selectable = [entry for entry in self._rows if self.is_checkable(entry)]
        if not selectable:
            return Qt.Unchecked
        checked = sum(1 for entry in selectable if entry.checked)
        if checked == 0:
            return Qt.Unchecked
        if checked == len(selectable):
            return Qt.Checked
        return Qt.PartiallyChecked

    def set_checked(self, human_path: str, checked: bool) -> None:
        row = self.row_of(human_path)
        if row < 0:
            return
        self.setData(self.index(row, COL_CHECK), Qt.Checked if checked else Qt.Unchecked, Qt.CheckStateRole)

    def remove_paths(self, paths: list[str]) -> int:
        """把删成功的项移除。返回移走了几行。"""
        targets = set(paths)
        if not targets or not self._all_entries:
            return 0
        kept = [entry for entry in self._all_entries if entry.human_path not in targets]
        removed = len(self._all_entries) - len(kept)
        if not removed:
            return 0
        self._all_entries = kept
        self._rebuild_rows()
        self.checkedChanged.emit()
        return removed

    # -- 局部刷新（大小 / 说明） -------------------------------------------
    def update_size(self, human_path: str, size: int | None, state: str) -> None:
        row = self.row_of(human_path)
        if row < 0:
            return
        entry = self._rows[row]
        if entry.size == size and entry.size_state == state:
            return
        entry.size = size
        entry.size_state = state
        index = self.index(row, COL_SIZE)
        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.ToolTipRole, Qt.ForegroundRole])

    def update_purpose(self, human_path: str, purpose: str, source: str, risk: str, reason: str, advice: str) -> None:
        row = self.row_of(human_path)
        if row < 0:
            return
        entry = self._rows[row]
        entry.purpose = purpose
        entry.purpose_source = source
        entry.risk = risk
        entry.risk_reason = reason
        entry.advice = advice
        top = self.index(row, COL_PURPOSE)
        bottom = self.index(row, COL_PURPOSE)
        self.dataChanged.emit(top, bottom, [Qt.DisplayRole, Qt.ToolTipRole, Qt.DecorationRole])

    def set_purpose_only(self, human_path: str, purpose: str, source: str) -> None:
        row = self.row_of(human_path)
        if row < 0:
            return
        entry = self._rows[row]
        entry.purpose = purpose
        entry.purpose_source = source
        index = self.index(row, COL_PURPOSE)
        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.ToolTipRole, Qt.DecorationRole])

    # -- 排序 ---------------------------------------------------------------
    # 三态：默认（保持加载顺序）→ 升 → 降 → 默认。只排可见行，不碰勾选。
    def _sort_key(self, column: int):
        return {
            COL_NAME: lambda e: (not e.is_dir, e.name.lower()),
            COL_MTIME: lambda e: (not e.is_dir, e.mtime),
            COL_KIND: lambda e: (not e.is_dir, e.kind_label),
            COL_SIZE: lambda e: (not e.is_dir, e.size if e.size is not None else -1),
            COL_PURPOSE: lambda e: (not e.is_dir, e.purpose),
            COL_CHECK: lambda e: (not e.is_dir, e.checked),
        }.get(column) or (lambda e: (not e.is_dir, e.name.lower()))

    def cycle_sort(self, column: int) -> tuple[int, Qt.SortOrder]:
        """点一次换个态。返回 (要显示的箭头列, 方向)；列 -1 表示恢复默认顺序。"""
        if column != self._sort_column:
            self._sort_column = column
            self._sort_cycle = 1
        else:
            self._sort_cycle = (self._sort_cycle + 1) % 3
            if self._sort_cycle == 0:
                self._sort_column = -1
        self._rebuild_rows()
        order = Qt.DescendingOrder if self._sort_cycle == 2 else Qt.AscendingOrder
        return self._sort_column, order

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:
        """保留 Qt 的入口：外部直接调也能用，按给定的方向排一次。"""
        self._sort_column = column
        self._sort_cycle = 2 if order == Qt.DescendingOrder else 1
        self._rebuild_rows()
