"""注册表清理页：一层一层看，勾选后先备份再删。

刻意不做「全盘扫描注册表」——那违反第一版就定下的「不遍历」原则，
也容易把不相干的东西一起扫进来。这里只有：你走到哪、看到哪、勾哪删哪。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThreadPool, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.knowledge import KnowledgeBase
from core.registry import (
    RegistryKey,
    RegistryReport,
    backup_dir,
    import_backup,
    is_critical,
    list_children,
    list_root_keys,
    make_worker,
)
from core.risk import Risk
from ui.confirm_flow import ConfirmFlow, is_red
from ui.page_base import ModulePage, build_empty_state
from ui.widgets.filter_bar import FilterBar
from ui.widgets.filter_mixin import FilterMixin

COL_NAME, COL_TIME, COL_TYPE, COL_EFFECT, COL_RISK, COL_CHECK = range(6)
HEADERS = ("键名", "最后修改", "类型", "作用", "风险", "是否清理")

MUTED = QColor("#8b949e")

def _is_root(full_path: str) -> bool:
    return "\\" not in full_path

@dataclass
class RegistryRow:
    key: RegistryKey
    effect: str = ""
    risk: str = "unlisted"
    source: str = ""
    checked: bool = False
    locked: bool = False
    impact: str = ""          # 知识库写的那句后果；空 = 规则没写

    @property
    def identity(self) -> str:
        return self.key.full_path

    @property
    def name(self) -> str:
        return self.key.name

    @property
    def detail(self) -> str:
        return self.key.detail

class RegistryModel(FilterMixin, QAbstractTableModel):
    checkedChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._all_rows: list[RegistryRow] = []      # 数据源（过滤前的全量）
        self._rows: list[RegistryRow] = []          # 当前可见
        self._row_by_id: dict[str, int] = {}
        self._filter_key = ""
        self._sort_column = -1
        self._sort_cycle = 0

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
            if section in (COL_TIME, COL_TYPE, COL_RISK, COL_CHECK):
                return int(Qt.AlignCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        return None

    @staticmethod
    def is_checkable(row: RegistryRow) -> bool:
        """根键不能删；读不到的不给勾。红色照样能勾，走三道坎。"""
        return not row.locked and not _is_root(row.identity)

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
        if role == Qt.ToolTipRole:
            return self._tooltip(row, col)
        if role == Qt.TextAlignmentRole and col in (COL_TIME, COL_TYPE, COL_RISK, COL_CHECK):
            return int(Qt.AlignCenter)
        if role == Qt.ForegroundRole:
            if col == COL_RISK:
                return QBrush(Risk.parse(row.risk).color)
            if col == COL_EFFECT and Risk.parse(row.risk) is Risk.UNLISTED:
                return QBrush(MUTED)
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

    def _display(self, row: RegistryRow, col: int) -> str:
        key = row.key
        if col == COL_NAME:
            prefix = "🔒 " if row.locked else ""
            return prefix + (key.name if not _is_root(key.full_path) else key.short_root)
        if col == COL_TIME:
            return key.last_write
        if col == COL_TYPE:
            return key.type_label
        if col == COL_EFFECT:
            if row.locked:
                return "打不开，读不到里面的东西"
            if _is_root(key.full_path):
                return "注册表的一个根，点进去看里面的子键"
            text = row.effect or "未收录，建议确认后再清理"
            return f"AI · {text}" if row.source == "ai" else text
        if col == COL_RISK:
            return Risk.parse(row.risk).label
        return ""

    def _tooltip(self, row: RegistryRow, col: int) -> str | None:
        key = row.key
        if col == COL_NAME:
            return f"{key.full_path}\n{key.detail}"
        if col == COL_EFFECT:
            return row.effect or "未收录，建议确认后再清理"
        if col == COL_CHECK:
            if _is_root(key.full_path):
                return "根键不能删"
            if row.locked:
                return "没有权限读这个键，勾不上"
            return "勾上之后会先备份、再删这个键（下面的子键必须先处理干净）"
        return None

    # -- 数据 --------------------------------------------------------------
    def set_rows(self, rows: Sequence[RegistryRow]) -> None:
        self._all_rows = list(rows)
        self.rebuild_visible()
        self.checkedChanged.emit()

    def haystack(self, row: RegistryRow) -> str:
        # 注册表：键名
        return row.key.name.lower()

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

    def rows(self) -> list[RegistryRow]:
        return self._rows

    def row_at(self, index: int) -> RegistryRow | None:
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def row_of(self, identity: str) -> int:
        return self._row_by_id.get(identity, -1)

    def checked_rows(self) -> list[RegistryRow]:
        return [row for row in self._rows if row.checked and self.is_checkable(row)]

    def set_checked(self, identity: str, checked: bool) -> None:
        index = self.row_of(identity)
        if index >= 0:
            self.setData(self.index(index, COL_CHECK), Qt.Checked if checked else Qt.Unchecked,
                         Qt.CheckStateRole)

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

    def update_ai(self, identity: str, purpose: str, risk: str, note: str) -> None:
        index = self.row_of(identity)
        if index < 0:
            return
        row = self._rows[index]
        row.effect = purpose
        row.source = "ai"
        if Risk.parse(risk).rank > Risk.parse(row.risk).rank:
            row.risk = risk
        self.dataChanged.emit(self.index(index, COL_EFFECT), self.index(index, COL_RISK),
                              [Qt.DisplayRole, Qt.ToolTipRole, Qt.ForegroundRole])

    def _sort_key(self, column: int):
        return {
            COL_NAME: lambda r: r.key.name.lower(),
            COL_TIME: lambda r: r.key.last_write,
            COL_TYPE: lambda r: r.key.subkey_count,
            COL_EFFECT: lambda r: r.effect,
            COL_RISK: lambda r: Risk.parse(r.risk).rank,
            COL_CHECK: lambda r: r.checked,
        }.get(column) or (lambda r: r.key.name.lower())

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

class PathBar(QWidget):
    """注册表路径条：每一段都能点，点哪层跳哪层。"""

    pathActivated = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addStretch(1)

    def set_path(self, full_path: str) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        def add_button(text: str, target: str, strong: bool = False) -> None:
            button = QToolButton()
            button.setText(text)
            button.setAutoRaise(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(target or "注册表根")
            if strong:
                button.setStyleSheet("font-weight:bold;color:#1f2328;")
            button.clicked.connect(lambda _checked=False, t=target: self.pathActivated.emit(t))
            self._layout.addWidget(button)

        add_button("注册表", "", strong=not full_path)
        if not full_path:
            self._layout.addStretch(1)
            return
        segments = full_path.split("\\")
        for index, segment in enumerate(segments):
            separator = QLabel("›")
            separator.setStyleSheet("color:#8b949e;padding:0 3px;")
            self._layout.addWidget(separator)
            prefix = "\\".join(segments[: index + 1])
            add_button(segment, prefix, strong=index == len(segments) - 1)
        self._layout.addStretch(1)

class RegistryView(ModulePage):
    """注册表清理页。"""

    def __init__(self, knowledge: KnowledgeBase, settings, cache=None, parent=None) -> None:
        super().__init__("registry", "注册表清理", parent)
        self.kb = knowledge
        self.settings = settings
        self.cache = cache
        self.ai = None
        self.current_path = ""
        self.model = RegistryModel(self)
        self.model.checkedChanged.connect(self._on_checked_changed)
        self._worker = None

        self.path_bar = PathBar(self)
        self.path_bar.pathActivated.connect(self.navigate)

        self.up_button = QToolButton(self)
        self.up_button.setText("↑ 上一级")
        self.up_button.setAutoRaise(True)
        self.up_button.clicked.connect(self._go_up)
        self.restore_button = QPushButton("还原备份…", self)
        self.restore_button.setToolTip("选一个 .reg 备份导回去，会先问你一次")
        self.restore_button.clicked.connect(self._restore_backup)

        bar = QHBoxLayout()
        bar.addWidget(self.up_button)
        bar.addWidget(self.path_bar, 1)
        bar.addWidget(self.restore_button)

        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setSortingEnabled(False)     # 三态排序自己管，交给 Qt 只有升/降两态
        self.table.setTextElideMode(Qt.ElideRight)
        header = self.table.horizontalHeader()
        # 名称列用 Interactive：Stretch 的列宽是自动算的，用户拖不动
        header.setSectionResizeMode(COL_NAME, QHeaderView.Interactive)
        self.table.setColumnWidth(COL_NAME, 300)
        header.setSectionResizeMode(COL_TIME, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_TYPE, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_EFFECT, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_RISK, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_TIME, 130)
        self.table.setColumnWidth(COL_TYPE, 110)
        self.table.setColumnWidth(COL_RISK, 76)
        self.table.setColumnWidth(COL_CHECK, 92)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_menu)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)

        self.empty = build_empty_state("注册表清理", "选左边一个根，一层一层往下看。")

        self.filter_bar = FilterBar("在键名里找…")
        self.filter_bar.changed.connect(self._on_filter_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(bar)
        layout.addWidget(self.filter_bar)
        layout.addWidget(self.empty, 1)
        layout.addWidget(self.table, 1)
        self.table.setVisible(False)
        self.set_selection_text("已选 0 项")
        self._refresh_path_bar()

    # -- 导航 --------------------------------------------------------------
    def navigate(self, full_path: str) -> None:
        self.current_path = str(full_path or "")
        self._refresh_path_bar()
        self.refresh()
        self.checkedChanged.emit()      # 让主窗口把状态栏那行字刷新一下

    def _go_up(self) -> None:
        if not self.current_path:
            return
        pieces = self.current_path.split("\\")
        self.navigate("\\".join(pieces[:-1]))
        self.clear_checks()

    def _refresh_path_bar(self) -> None:
        self.path_bar.set_path(self.current_path)
        self.up_button.setEnabled(bool(self.current_path))

    def _on_double_click(self, index) -> None:
        row = self.model.row_at(index.row())
        if row is not None and not row.locked:
            self.clear_checks()
            self.navigate(row.identity)

    def _on_header_clicked(self, column: int) -> None:
        """点列头循环：默认 → 升 → 降 → 默认。"""
        col, order = self.model.cycle_sort(column)
        header = self.table.horizontalHeader()
        if col < 0:
            header.setSortIndicator(-1, Qt.AscendingOrder)
        else:
            header.setSortIndicator(col, order)

    def _send_feedback(self, path: str, name: str, purpose: str, module: str = "") -> None:
        from ui.dialogs import FeedbackDialog

        FeedbackDialog(path=path, name=name, purpose=purpose, module=module, parent=self).exec()

    # -- 过滤 --------------------------------------------------------------
    def _on_filter_changed(self, text: str) -> None:
        """只筛当前这一层已经读出来的键，不重新开注册表。"""
        self.model.set_filter(text)
        self.filter_bar.set_counts(self.model.rowCount(), self.model.total_count())
        self._on_checked_changed()

    # -- 加载 --------------------------------------------------------------
    def refresh(self) -> None:
        self.filter_bar.set_pending("")
        self.model.set_filter("")      # set_pending 屏蔽了信号，模型的过滤词得手动清
        if self.current_path:
            children, error = list_children(self.current_path)
            if children is None:
                self.model.set_rows([])
                self.table.setVisible(False)
                self.empty.setVisible(True)
                self.empty.setText(
                    f"<h3 style='color:#1f2328'>{error or '打不开'}</h3>"
                    "<p style='color:#57606a'>换个位置，或者用管理员身份重新启动再试。</p>"
                )
                return
            keys = children
        else:
            keys = list_root_keys()
            error = None

        rows: list[RegistryRow] = []
        for key in keys:
            if not self.current_path:
                rows.append(RegistryRow(key=key, effect="注册表的一个根，点进去看里面的子键",
                                        risk="caution", source="kb"))
                continue
            if key.locked:
                rows.append(RegistryRow(key=key, effect="打不开，读不到里面的东西",
                                        risk="unlisted", source="fallback", locked=True))
                continue
            result = self.kb.match_registry(key.full_path)
            risk = result.risk if result.known else "unlisted"
            effect = result.effect if result.known else ""
            if is_critical(key.full_path):
                if Risk.parse(risk).rank < Risk.parse("forbidden").rank:
                    risk = "forbidden"
                if not effect:
                    effect = "系统关键位置，删了大概率起不来。"
            if not result.known:
                cached = self._cached_ai(key)
                if cached:
                    effect = str(cached.get("purpose") or "")
                    incoming = Risk.parse(str(cached.get("risk") or "caution"))
                    if incoming.rank > Risk.parse(risk).rank:
                        risk = incoming.value
                    rows.append(RegistryRow(key=key, effect=effect, risk=risk, source="ai"))
                    continue
            rows.append(RegistryRow(key=key, effect=effect, risk=risk,
                                    source="kb" if result.known else "fallback",
                                    impact=result.impact if result.known else ""))
        self.model.set_rows(rows)
        self.table.setVisible(bool(rows))
        self.empty.setVisible(not rows)
        if not rows:
            self.empty.setText("<h3 style='color:#1f2328'>这一层没有子键</h3>"
                               "<p style='color:#57606a'>要么是个空键，要么权限不够看不到。</p>")
        if error:
            self.statusMessage.emit(error)

    def _cached_ai(self, key: RegistryKey) -> dict | None:
        if self.cache is None:
            return None
        try:
            from core.config import CacheStore

            data = self.cache.get_ai_json(CacheStore.ai_key_item("registry", key.full_path, key.name))
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) and data.get("purpose") else None

    def clear_checks(self) -> None:
        self.model.clear_checks()

    def checked_items(self) -> list:
        return self.model.checked_rows()

    def cancel_running(self) -> None:
        if self.ai is not None:
            self.ai.cancel_all()

    def export_headers(self) -> tuple[str, ...]:
        return ("键名", "键路径", "最后修改", "类型", "作用", "风险")

    def export_rows(self) -> list[tuple]:
        from core.risk import Risk

        return [
            (row.key.name, row.key.full_path, row.key.last_write, row.key.type_label,
             row.effect, Risk.parse(row.risk).label)
            for row in self.model.rows()
        ]

    def status_line(self) -> str:
        count = self.model.rowCount()
        where = self.current_path.rsplit("\\", 1)[-1] if self.current_path else "根"
        return f"注册表 · {where} · {count} 个子键"

    # -- 勾选 --------------------------------------------------------------
    def _on_checked_changed(self) -> None:
        rows = self.model.checked_rows()
        text = f"已选 {len(rows)} 项"
        red = sum(1 for row in rows if is_red(row))
        if red:
            text += f" · 其中 {red} 项标着「别删」"
        if rows:
            text += "（清理前会先备份到 backups）"
        hidden = self.model.hidden_checked_count()
        if hidden:
            text += f" · 另有 {hidden} 项被过滤藏起来了但还勾着"
        if self.model.is_filtered():
            text += "（已过滤，仅对可见项生效）"
        self.set_selection_text(text)

    # -- AI ----------------------------------------------------------------
    def attach_ai(self, client) -> None:
        self.ai = client
        if client is not None:
            client.resultReady.connect(self._on_ai_result)

    def _on_ai_result(self, result) -> None:
        self.model.update_ai(result.human_path, result.purpose, result.risk, result.note)

    def explain_unknown(self) -> None:
        if self.ai is None:
            self.statusMessage.emit("联网识别还没准备好")
            return
        if not self.ai.is_configured():
            # 四个模块共用同一个引导框，由主窗口弹
            self.aiSetupRequested.emit()
            return
        rows = [row for row in self.model.rows()
                if row.source != "kb" and not row.locked and self.model.is_checkable(row)]
        if not rows:
            self.statusMessage.emit("这一层没有知识库不认识的键")
            return
        answer = QMessageBox.question(
            self,
            "联网识别",
            f"{len(rows)} 个键知识库不认识，将逐项问一遍，是否继续？\n\n"
            "发出去的只有键路径和键名，不会读注册表里的值。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.ai.explain_many([(row.identity, row.name) for row in rows], item_type="registry")

    def _explain_one(self, row: RegistryRow) -> None:
        if self.ai is None:
            return
        self.ai.explain(row.identity, row.name, item_type="registry")

    # -- 执行 --------------------------------------------------------------
    def execute_checked(self) -> None:
        rows = list(self.model.checked_rows())
        if not rows:
            self.statusMessage.emit("还没有勾选要清理的键")
            return
        decision = ConfirmFlow.confirm(
            self,
            rows,
            "清理",
            hint=(
                "下面每一项都可以单独取消。\n"
                "**勾了父键就等于勾了它下面所有子键** —— 不用进去一个个删。\n"
                "备份用的是 reg export，整棵子树都在里面，删了能原样还原。"
            ),
            irreversible=True,
            backup_note="动手前会先备份到 backups/（备份失败就一个都不动）",
        )
        if decision is None or not decision.items:
            return
        paths = [row.identity for row in decision.items]
        from core import snapshot as snapshots

        self._snap_id = snapshots.begin("clean", rows)
        worker = make_worker(paths, self._log_dir())
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)
        self._worker = worker
        self.statusMessage.emit("正在备份并清理…")
        QThreadPool.globalInstance().start(worker)

    def _log_dir(self) -> Path:
        from app_paths import resolve

        return resolve().logs

    def _on_progress(self, done: int, total: int, text: str) -> None:
        if text:
            self.statusMessage.emit(f"{text} {done}/{total}")

    def _on_finished(self, report: RegistryReport) -> None:
        from core import snapshot as snapshots
        from ui.dialogs import DeleteReportDialog

        backup = getattr(report, "backup_path", None)
        snapshots.finish(
            getattr(self, "_snap_id", None), getattr(report, "items", []), self._log_dir(),
            backup=backup.name if backup is not None else "",
        )
        self._worker = None
        self.model.clear_checks()
        self.refresh()
        self.statusMessage.emit(report.summary)
        if report.backup_path is None and report.items:
            QMessageBox.warning(
                self,
                "没有动手",
                "备份没成功，所以这一次一个键都没删。\n\n"
                "可能是 backups 目录写不进去，或者 reg 命令被挡住了。",
            )
            return
        dialog = DeleteReportDialog(
            report, report.log_path, self,
            on_retry=self._retry_clean,
            op_type="clean",
        )
        dialog.exec()

    def _restore_backup(self) -> None:
        directory = backup_dir()
        start = str(directory) if directory.exists() else ""
        path, _filter = QFileDialog.getOpenFileName(self, "选一个备份文件", start, "注册表备份 (*.reg)")
        if not path:
            return
        answer = QMessageBox.question(
            self,
            "还原备份",
            f"要把这份备份导回去吗？\n\n{path}\n\n"
            "导入会覆盖同名键当前的内容。这一步需要写注册表的权限。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        ok, message = import_backup(path)
        if ok:
            QMessageBox.information(self, "还原完成", message)
            self.refresh()
        else:
            QMessageBox.warning(self, "还原没成功", message)

    def _retry_clean(self, rows) -> None:
        """重试失败项：键还在就再删一次。"""
        from core import snapshot as snapshots

        paths = [row.path for row in rows if row.path]
        if not paths:
            self.statusMessage.emit("这些键都已经不在了，没什么可重试的")
            return
        self._snap_id = snapshots.begin("clean", rows)
        worker = make_worker(paths, self._log_dir())
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)
        self._worker = worker
        self.statusMessage.emit("正在重试…")
        QThreadPool.globalInstance().start(worker)

    # -- 右键 --------------------------------------------------------------
    def _show_menu(self, pos) -> None:
        from PySide6.QtWidgets import QApplication, QMenu

        index = self.table.indexAt(pos)
        row = self.model.row_at(index.row()) if index.isValid() else None
        menu = QMenu(self)
        if row is not None:
            if not row.locked:
                menu.addAction("进去看看", lambda: self.navigate(row.identity))
            menu.addAction("复制键路径", lambda: QApplication.clipboard().setText(row.identity))
            menu.addSeparator()
            if row.locked:
                menu.addAction("🔒 需要管理员权限，点此重启", self.adminRestartRequested.emit)
            if not self.model.is_checkable(row) and not row.locked:
                blocked = menu.addAction("这一项不能勾选")
                blocked.setEnabled(False)
            elif row.checked:
                menu.addAction("取消勾选", lambda: self.model.set_checked(row.identity, False))
            else:
                menu.addAction("勾选这一项", lambda: self.model.set_checked(row.identity, True))
            menu.addSeparator()
            menu.addAction("这条说明不对…", lambda: self._send_feedback(
                row.key.full_path, row.name, row.effect, "registry"))
            menu.addSeparator()
            ai_action = menu.addAction("AI 识别此项", lambda: self._explain_one(row))
            ai_action.setEnabled(self.ai is not None and not row.locked)
        menu.addAction("刷新", self.refresh)
        menu.exec(self.table.viewport().mapToGlobal(pos))
