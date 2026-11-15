"""进程管理页：现在跑着什么，结束哪一个由你决定。

不做实时刷新 —— 进页面枚举一次，想看新的就点刷新。
进程表每秒都在变，跟着刷会让人眼花，也白费 CPU。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QThreadPool, QTimer, Qt, Signal
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
from core.processes import (
    CRITICAL_HINT,
    ProcessItem,
    ProcessReport,
    enumerate_processes,
    make_cpu_sampler,
    make_worker,
)
from core.risk import Risk
from ui.confirm_flow import ConfirmFlow, is_red
from ui.page_base import ModulePage, build_empty_state
from ui.widgets.filter_bar import FilterBar
from ui.widgets.filter_mixin import FilterMixin

COL_NAME, COL_PID, COL_USER, COL_MEM, COL_CPU, COL_PATH, COL_EFFECT, COL_RISK, COL_CHECK = range(9)
HEADERS = ("进程名", "PID", "用户名", "内存", "CPU%", "路径", "作用", "风险", "是否结束")

MUTED = QColor("#8b949e")
CPU_SAMPLE_DELAY_MS = 700


@dataclass
class ProcessRow:
    item: ProcessItem
    effect: str = ""
    risk: str = "unlisted"
    source: str = ""
    checked: bool = False
    impact: str = ""          # 知识库写的那句后果；空 = 规则没写

    @property
    def identity(self) -> str:
        return str(self.item.pid)

    @property
    def name(self) -> str:
        return self.item.name

    @property
    def detail(self) -> str:
        return f"PID {self.item.pid}"


class ProcessModel(FilterMixin, QAbstractTableModel):
    checkedChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._all_rows: list[ProcessRow] = []      # 数据源（过滤前的全量）
        self._rows: list[ProcessRow] = []          # 当前可见
        self._row_by_id: dict[str, int] = {}
        self._filter_key = ""
        self._sort_column = -1
        self._sort_cycle = 0
        # AI 识别过的结果按 exe 路径记着 —— 刷新重建行时套回去。
        # 结束一个进程会重建整个列表，不记的话刚识别出来的「作用」就全空回去了。
        self._ai_memory: dict[str, tuple[str, str, str]] = {}

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
            if section in (COL_PID, COL_MEM, COL_CPU, COL_RISK, COL_CHECK):
                return int(Qt.AlignCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        return None

    @staticmethod
    def is_checkable(row: ProcessRow) -> bool:
        """读不到路径的照样能结束（可能更需要），红色也能勾 —— 走三道坎。"""
        return True

    def flags(self, index) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == COL_CHECK:
            return base | Qt.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.CheckStateRole and col == COL_CHECK:
            return Qt.Checked if row.checked else Qt.Unchecked
        if role == Qt.DisplayRole:
            return self._display(row, col)
        if role == Qt.ToolTipRole:
            return self._tooltip(row, col)
        if role == Qt.TextAlignmentRole and col in (COL_PID, COL_MEM, COL_CPU, COL_RISK, COL_CHECK):
            return int(Qt.AlignCenter)
        if role == Qt.ForegroundRole:
            if col == COL_RISK:
                return QBrush(Risk.parse(row.risk).color)
            if col == COL_EFFECT and Risk.parse(row.risk) is Risk.UNLISTED:
                return QBrush(MUTED)
            if col == COL_PATH and row.item.locked:
                return QBrush(MUTED)
        return None

    def setData(self, index, value, role=Qt.EditRole) -> bool:  # noqa: N802
        if not index.isValid() or role != Qt.CheckStateRole or index.column() != COL_CHECK:
            return False
        row = self._rows[index.row()]
        checked = value in (Qt.Checked, True, 2)
        if row.checked == checked:
            return False
        row.checked = checked
        self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        self.checkedChanged.emit()
        return True

    def _display(self, row: ProcessRow, col: int) -> str:
        item = row.item
        if col == COL_NAME:
            return ("🔒 " if item.locked else "") + item.name
        if col == COL_PID:
            return str(item.pid)
        if col == COL_USER:
            return item.username or "—"
        if col == COL_MEM:
            return human_size(item.rss) if item.rss else "—"
        if col == COL_CPU:
            return f"{item.cpu:.1f}%"
        if col == COL_PATH:
            return item.exe or "读不到"
        if col == COL_EFFECT:
            text = row.effect or "未收录，建议确认后再结束"
            return f"AI · {text}" if row.source == "ai" else text
        if col == COL_RISK:
            return Risk.parse(row.risk).label
        return ""

    def _tooltip(self, row: ProcessRow, col: int) -> str | None:
        item = row.item
        if col == COL_NAME:
            bits = [f"PID {item.pid}", item.exe or "路径读不到"]
            if item.publisher:
                bits.append(f"发布者：{item.publisher}")
            return "\n".join(bits)
        if col == COL_EFFECT:
            return row.effect or "未收录，建议确认后再结束"
        if col == COL_CHECK:
            return "勾上就会结束这个进程" + ("；它一结束电脑可能蓝屏或重启" if item.system else "")
        return None

    # -- 数据 --------------------------------------------------------------
    def set_rows(self, rows: Sequence[ProcessRow]) -> None:
        self._all_rows = list(rows)
        self.rebuild_visible()
        self.checkedChanged.emit()

    def haystack(self, row: ProcessRow) -> str:
        # 进程：名称 + PID（输数字也能定位）
        return f"{row.name} {row.item.pid}".lower()

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

    def rows(self) -> list[ProcessRow]:
        return self._rows

    def row_at(self, index: int) -> ProcessRow | None:
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def row_of(self, identity: str) -> int:
        return self._row_by_id.get(identity, -1)

    def checked_rows(self) -> list[ProcessRow]:
        return [row for row in self._rows if row.checked]

    def set_checked(self, identity: str, checked: bool) -> None:
        index = self.row_of(identity)
        if index >= 0:
            self.setData(self.index(index, COL_CHECK), Qt.Checked if checked else Qt.Unchecked,
                         Qt.CheckStateRole)

    def set_all_checked(self, checked: bool) -> None:
        if not self._rows:
            return
        for row in self._rows:
            row.checked = checked
        self.dataChanged.emit(self.index(0, COL_CHECK),
                              self.index(len(self._rows) - 1, COL_CHECK), [Qt.CheckStateRole])
        self.checkedChanged.emit()

    def clear_checks(self) -> None:
        self.set_all_checked(False)

    def check_state(self) -> Qt.CheckState:
        if not self._rows:
            return Qt.Unchecked
        checked = sum(1 for row in self._rows if row.checked)
        if checked == 0:
            return Qt.Unchecked
        if checked == len(self._rows):
            return Qt.Checked
        return Qt.PartiallyChecked

    def update_cpu(self, samples: dict[int, float]) -> None:
        if not samples:
            return
        touched = False
        for row in self._rows:
            if row.item.pid in samples:
                row.item = ProcessItem(
                    pid=row.item.pid, name=row.item.name, username=row.item.username,
                    rss=row.item.rss, cpu=samples[row.item.pid], exe=row.item.exe,
                    publisher=row.item.publisher, locked=row.item.locked, system=row.item.system,
                )
                touched = True
        if touched:
            self.dataChanged.emit(self.index(0, COL_CPU),
                                  self.index(len(self._rows) - 1, COL_CPU), [Qt.DisplayRole])

    def memory_of(self, exe: str):
        """取这个 exe 之前被 AI 判过的结论。"""
        return self._ai_memory.get(str(exe or "").strip().lower())

    def update_ai_by_exe(self, exe: str, purpose: str, risk: str, impact: str = "") -> int:
        """按 exe 路径更新，同一程序开了多个实例时一起更新。"""
        text = str(exe or "").strip()
        if not text:
            return 0
        self._ai_memory[text.lower()] = (purpose, risk, impact)
        count = 0
        for row in self._rows:
            if (row.item.exe or row.name) != text:
                continue
            self.update_ai(row.identity, purpose, risk, impact)
            count += 1
        return count

    def update_ai(self, identity: str, purpose: str, risk: str, impact: str = "") -> None:
        index = self.row_of(identity)
        if index < 0:
            return
        row = self._rows[index]
        row.effect = purpose
        row.source = "ai"
        if impact:
            row.impact = impact
        if Risk.parse(risk).rank > Risk.parse(row.risk).rank:
            row.risk = risk
        self.dataChanged.emit(self.index(index, COL_EFFECT), self.index(index, COL_RISK),
                              [Qt.DisplayRole, Qt.ToolTipRole, Qt.ForegroundRole])

    def _sort_key(self, column: int):
        return {
            COL_NAME: lambda r: r.item.name.lower(),
            COL_PID: lambda r: r.item.pid,
            COL_USER: lambda r: r.item.username.lower(),
            COL_MEM: lambda r: r.item.rss,
            COL_CPU: lambda r: r.item.cpu,
            COL_PATH: lambda r: r.item.exe.lower(),
            COL_EFFECT: lambda r: r.effect,
            COL_RISK: lambda r: Risk.parse(r.risk).rank,
            COL_CHECK: lambda r: r.checked,
        }.get(column) or (lambda r: r.item.name.lower())

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


class ProcessView(ModulePage):
    """进程管理页。"""

    def __init__(self, knowledge: KnowledgeBase, settings, cache=None, parent=None) -> None:
        super().__init__("process", "进程管理", parent)
        self.kb = knowledge
        self.settings = settings
        self.cache = cache
        self.ai = None
        self.model = ProcessModel(self)
        self.model.checkedChanged.connect(self._on_checked_changed)
        self._worker = None
        self._cpu_timer = QTimer(self)
        self._cpu_timer.setSingleShot(True)
        self._cpu_timer.timeout.connect(self._sample_cpu_now)

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
        self.table.setColumnWidth(COL_NAME, 220)
        for column in (COL_PID, COL_USER, COL_MEM, COL_CPU):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_PATH, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_EFFECT, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_RISK, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_PID, 76)
        self.table.setColumnWidth(COL_USER, 130)
        self.table.setColumnWidth(COL_MEM, 88)
        self.table.setColumnWidth(COL_CPU, 66)
        self.table.setColumnWidth(COL_PATH, 240)
        self.table.setColumnWidth(COL_RISK, 70)
        self.table.setColumnWidth(COL_CHECK, 92)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_menu)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)

        self.empty = build_empty_state("进程管理", "正在读取当前进程…")

        self.filter_bar = FilterBar("在进程名 / PID 里找…")
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

    def _send_feedback(self, path: str, name: str, purpose: str, module: str = "") -> None:
        from ui.dialogs import FeedbackDialog

        FeedbackDialog(path=path, name=name, purpose=purpose, module=module, parent=self).exec()

    # -- 过滤 --------------------------------------------------------------
    def _on_filter_changed(self, text: str) -> None:
        """只筛已经读出来的进程，不重新调 psutil。"""
        self.model.set_filter(text)
        self.filter_bar.set_counts(self.model.rowCount(), self.model.total_count())
        self._on_checked_changed()

    # -- 基类要求 ----------------------------------------------------------
    def refresh(self) -> None:
        self.filter_bar.set_pending("")
        self.model.set_filter("")      # set_pending 屏蔽了信号，模型的过滤词得手动清
        try:
            items, warnings = enumerate_processes()
        except Exception:  # noqa: BLE001
            items, warnings = [], ["读取进程时出了点问题"]
        rows: list[ProcessRow] = []
        for item in items:
            result = self.kb.match_process(item.name, item.exe)
            risk = result.risk if result.known else "unlisted"
            effect = result.effect if result.known else ""
            if item.system and Risk.parse(risk).rank < Risk.parse("forbidden").rank:
                risk = "forbidden"
                if not effect:
                    effect = CRITICAL_HINT
            row = ProcessRow(item=item, effect=effect, risk=risk,
                             source="kb" if result.known else "fallback",
                             impact=result.impact if result.known else "")
            # 之前 AI 认过这个 exe，把结论套回去
            remembered = self.model.memory_of(item.exe or item.name)
            if remembered is not None:
                ai_purpose, ai_risk, ai_impact = remembered
                row.effect = ai_purpose
                row.source = "ai"
                if ai_impact:
                    row.impact = ai_impact
                # AI 只能把风险说得更严，不能把红色的说成安全
                if Risk.parse(ai_risk).rank > Risk.parse(row.risk).rank:
                    row.risk = ai_risk
            rows.append(row)
        self.model.set_rows(rows)
        self.table.setVisible(bool(rows))
        self.empty.setVisible(not rows)
        self._last_status = "已选 0 项"
        if warnings:
            self.statusMessage.emit(warnings[0])
        # CPU 百分比第一次必然读成 0，隔一会儿再采一次
        self._cpu_timer.start(CPU_SAMPLE_DELAY_MS)

    def _sample_cpu_now(self) -> None:
        pids = [row.item.pid for row in self.model.rows()]
        if not pids:
            return
        sampler = make_cpu_sampler(pids)
        sampler.signals.ready.connect(self.model.update_cpu)
        self._cpu_task = sampler          # 持住引用，别被回收
        QThreadPool.globalInstance().start(sampler)

    def clear_checks(self) -> None:
        self.model.clear_checks()

    def checked_items(self) -> list:
        return self.model.checked_rows()

    def cancel_running(self) -> None:
        self._cpu_timer.stop()
        if self.ai is not None:
            self.ai.cancel_all()

    def export_headers(self) -> tuple[str, ...]:
        return ("进程名", "PID", "用户名", "内存", "CPU%", "路径", "作用", "风险")

    def export_rows(self) -> list[tuple]:
        from core.fs_browser import human_size
        from core.risk import Risk

        return [
            (row.name, row.item.pid, row.item.username,
             human_size(row.item.rss) if row.item.rss else "", f"{row.item.cpu:.1f}",
             row.item.exe, row.effect, Risk.parse(row.risk).label)
            for row in self.model.rows()
        ]

    def status_line(self) -> str:
        return f"进程管理 · 当前 {self.model.rowCount()} 个进程"

    def activate(self) -> None:
        """进程页的取舍：第一次进来读一次，之后切回来不重读。

        进程表每秒都在变，跟着重读既慢（260 个进程要 1 秒多）又没意义；
        想要最新状态点「刷新」。
        """
        if not self.model.rows():
            self.refresh()

    # -- 勾选 --------------------------------------------------------------
    def _on_checked_changed(self) -> None:
        rows = self.model.checked_rows()
        text = f"已选 {len(rows)} 项"
        red = sum(1 for row in rows if is_red(row))
        if red:
            text += f" · 其中 {red} 项结束会出事"
        if rows:
            text += "（结束之后没法撤销）"
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
        """AI 回来的是「按 exe 路径」的结果，要落到所有同 exe 的行上。

        进程的 identity 是 PID，但发给 AI 的是 exe 路径（PID 对它没意义）。
        以前这里直接拿 exe 路径去 row_of() 找 PID，永远找不到，
        所以点了识别「作用」列一直没反应。
        """
        self.model.update_ai_by_exe(
            result.human_path, result.purpose, result.risk,
            getattr(result, "impact", "") or "",
        )

    def _ai_key(self, row) -> str:
        """发给 AI 的标识：exe 路径，读不到就退回进程名。"""
        return row.item.exe or row.name

    def explain_unknown(self) -> None:
        if self.ai is None:
            self.statusMessage.emit("联网识别还没准备好")
            return
        if not self.ai.is_configured():
            # 四个模块共用同一个引导框，由主窗口弹
            self.aiSetupRequested.emit()
            return
        rows = [row for row in self.model.rows() if row.source != "kb"]
        if not rows:
            self.statusMessage.emit("这一页没有知识库不认识的进程")
            return
        answer = QMessageBox.question(
            self,
            "联网识别",
            f"{len(rows)} 个进程知识库不认识，将逐项问一遍，是否继续？\n\n"
            "发出去的只有进程名、可执行文件路径和发布者。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        pairs = [(self._ai_key(row), row.name) for row in rows]
        extras = [{"publisher": row.item.publisher} for row in rows]
        self.ai.explain_many(pairs, item_type="process", extras=extras)

    def _explain_one(self, row: ProcessRow) -> None:
        if self.ai is None:
            return
        self.ai.explain(row.item.exe or row.name, row.name,
                        item_type="process", extra={"publisher": row.item.publisher})

    # -- 执行 --------------------------------------------------------------
    def execute_checked(self) -> None:
        rows = list(self.model.checked_rows())
        if not rows:
            self.statusMessage.emit("还没有勾选要结束的进程")
            return
        decision = ConfirmFlow.confirm(
            self,
            rows,
            "结束",
            hint="下面每一项都可以单独取消。进程结束之后没法撤销，没保存的工作会丢。",
            irreversible=True,
        )
        if decision is None or not decision.items:
            return
        pairs = [(row.item.pid, row.name) for row in decision.items]
        from core import snapshot as snapshots

        self._snap_id = snapshots.begin("kill", rows)
        worker = make_worker(pairs, self._log_dir())
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)
        self._worker = worker
        self.statusMessage.emit("正在结束进程…")
        QThreadPool.globalInstance().start(worker)

    def _log_dir(self) -> Path:
        from app_paths import resolve

        return resolve().logs

    def _on_progress(self, done: int, total: int, name: str) -> None:
        if name:
            self.statusMessage.emit(f"正在结束 {done + 1}/{total} · {name}")

    def _on_finished(self, report: ProcessReport) -> None:
        from core import snapshot as snapshots
        from ui.dialogs import DeleteReportDialog

        snapshots.finish(getattr(self, "_snap_id", None), getattr(report, "items", []), self._log_dir())
        self._worker = None
        self.model.clear_checks()
        self.refresh()
        self.statusMessage.emit(report.summary)
        dialog = DeleteReportDialog(
            report, report.log_path, self,
            on_retry=self._retry_kill,
            op_type="kill",
        )
        dialog.exec()

    def _retry_kill(self, rows) -> None:
        """重试没结束掉的进程：进程还在就再试一次。"""
        from core import snapshot as snapshots

        pairs = []
        for row in rows:
            try:
                pairs.append((int(row.path), row.name))
            except (TypeError, ValueError):
                continue
        if not pairs:
            self.statusMessage.emit("这些进程都已经不在了，没什么可重试的")
            return
        self._snap_id = snapshots.begin("kill", rows)
        worker = make_worker(pairs, self._log_dir())
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
            menu.addAction("复制路径", lambda: QApplication.clipboard().setText(row.item.exe))
            menu.addAction("复制 PID", lambda: QApplication.clipboard().setText(str(row.item.pid)))
            menu.addSeparator()
            if row.checked:
                menu.addAction("取消勾选", lambda: self.model.set_checked(row.identity, False))
            else:
                menu.addAction("勾选这一项", lambda: self.model.set_checked(row.identity, True))
            menu.addSeparator()
            menu.addAction("这条说明不对…", lambda: self._send_feedback(
                row.item.exe or row.name, row.name, row.effect, "process"))
            menu.addSeparator()
            ai_action = menu.addAction("AI 识别此项", lambda: self._explain_one(row))
            ai_action.setEnabled(self.ai is not None)
            if row.item.locked:
                menu.addSeparator()
                menu.addAction("🔒 需要管理员权限，点此重启", self.adminRestartRequested.emit)
        menu.addAction("刷新", self.refresh)
        menu.exec(self.table.viewport().mapToGlobal(pos))
