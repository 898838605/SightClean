"""卸载之后的残留清单：分类展示，勾哪项清哪项。

参考 Geek Uninstaller 的做法：
- 卸载完自动扫一遍（文件夹 + 注册表）
- 分「文件残留 / 注册表残留」两组列出来，每项写清路径、大小、为什么被判定为残留
- **默认一个都不勾** —— 要不要清由你自己决定
- 清理文件走回收站，清理注册表先备份
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.fs_browser import human_size
from core.leftover import (
    KIND_FOLDER,
    KIND_REGISTRY,
    Leftover,
    make_clean_worker,
    make_measure_worker,
)

MUTED = QColor("#57606a")
DANGER = QColor("#cf222e")
OK = QColor("#1a7f37")

COL_CHECK, COL_PATH, COL_SIZE, COL_REASON = range(4)
HEADERS = ("清理", "路径", "大小", "为什么列为残留")

GROUP_FOLDER = "文件残留"
GROUP_REGISTRY = "注册表残留"


class LeftoverDialog(QDialog):
    """残留清单。默认不勾，勾了才清。"""

    def __init__(self, leftovers, parent=None, on_open_path=None,
                 title_text="卸载后剩下的东西", ai=None, installed=None,
                 focus=None, kinds=("folder", "registry"), scopes=None,
                 log_dir=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title_text)
        self.setMinimumSize(1040, 620)
        self._items: list[Leftover] = list(leftovers)
        self._checks = {item.identity: False for item in self._items}   # 默认全不勾
        self._rows: dict[str, int] = {}
        self._on_open_path = on_open_path
        self._ai = ai
        self._installed = list(installed or [])
        self._focus = focus       # 只关心这一个软件的痕迹
        self._kinds = tuple(kinds)  # 这个入口只找文件/只找注册表/两者都找
        self._scopes = list(scopes) if scopes else None   # 扫哪些位置
        paths = getattr(parent, "paths", None)
        self._log_dir = log_dir or getattr(paths, "logs", None)
        self._ai_worker = None
        self._collect_worker = None
        self._ai_cancel = None
        self._worker = None
        self._busy = False
        self.report = None

        folders = [item for item in self._items if item.kind == KIND_FOLDER]
        regs = [item for item in self._items if item.kind == KIND_REGISTRY]

        title = QLabel(
            f"找到 {len(folders)} 个文件残留、{len(regs)} 个注册表残留"
            if regs else f"找到 {len(folders)} 个目录"
        )
        font = QFont(title.font())
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        title.setFont(font)

        hint = QLabel(
            "卸载程序不一定把东西删干净，下面这些是它可能留下的。**默认一个都没勾**，\n"
            "看一眼、确认不要了再勾。文件会进回收站（能还原），注册表项清理前会先导出备份。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{MUTED.name()};")

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setObjectName("leftover_table")
        self.table.setHorizontalHeaderLabels(list(HEADERS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_PATH, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_SIZE, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_REASON, QHeaderView.Stretch)
        self.table.setColumnWidth(COL_CHECK, 56)
        self.table.setColumnWidth(COL_SIZE, 96)

        self._build_rows(folders, regs)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.doubleClicked.connect(self._on_double_click)

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color:{MUTED.name()};")

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setTextVisible(True)
        self.progress.setMinimumHeight(22)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setVisible(False)

        self.btn_clean = QPushButton("清理选中项")
        self.btn_clean.setEnabled(False)
        self.btn_clean.clicked.connect(self._do_clean)
        self.btn_clean.setStyleSheet(
            "QPushButton{background:#c62828;color:white;padding:6px 16px;border-radius:4px;font-weight:bold;}"
            "QPushButton:disabled{background:#e6b3b3;color:#fff8f8;}"
        )
        if self._focus is not None:
            name = getattr(self._focus, "name", "") or "这个软件"
            self.btn_ai = QPushButton(f"🤖 让 AI 再扫一遍（先看「{name}」）")
            self.btn_ai.setToolTip(
                "在你勾过的那批位置里扫一遍所有可疑项，交给 AI 判断哪些是残留。\n"
                "跟这个软件有关的会排在前面。"
            )
        else:
            self.btn_ai = QPushButton("🤖 让 AI 再找一遍漏掉的")
            self.btn_ai.setToolTip(
                "规则只会按「已知软件的名字」找同名目录。名字对不上的（HZVPN 这种）永远扫不到。\n"
                "这个会把「不属于任何已装软件」的目录和注册表键列出来，交给 AI 判断哪些是残留。"
            )
        self.btn_ai.setVisible(self._ai is not None and bool(self._installed))
        self.btn_ai.clicked.connect(self._do_ai)
        self.btn_open = QPushButton("在文件模块中打开")
        self.btn_open.clicked.connect(self._open_first_folder)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self._try_close)

        row = QHBoxLayout()
        row.addWidget(self.btn_clean)
        row.addWidget(self.btn_ai)
        row.addWidget(self.btn_open)
        row.addStretch(1)
        row.addWidget(self.btn_close)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.summary)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addLayout(row)
        self._refresh()

        # 文件夹大小后台算，先显示「计算中…」
        if folders:
            self._start_measure()

    # -- 表格 --------------------------------------------------------------
    def _group_row(self, text: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        cell = QTableWidgetItem(text)
        cell.setFlags(Qt.ItemIsEnabled)
        font = QFont(cell.font())
        font.setBold(True)
        cell.setFont(font)
        cell.setBackground(QBrush(QColor("#eef1f4")))
        self.table.setItem(row, COL_PATH, cell)
        self.table.setSpan(row, COL_PATH, 1, len(HEADERS) - 1)

    def _build_rows(self, folders, regs) -> None:
        if folders:
            self._group_row(f"{GROUP_FOLDER}（{len(folders)} 项）")
            for item in folders:
                self._add_item_row(item, DANGER if False else None)
        if regs:
            self._group_row(f"{GROUP_REGISTRY}（{len(regs)} 项）")
            for item in regs:
                self._add_item_row(item, DANGER)

    def _add_item_row(self, item: Leftover, color: QColor | None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows[item.identity] = row

        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
        check.setCheckState(Qt.Unchecked)
        self.table.setItem(row, COL_CHECK, check)

        path_cell = QTableWidgetItem(item.path)
        path_cell.setToolTip(item.path)
        reason_cell = QTableWidgetItem(item.reason or item.detail or "同名或同路径")
        reason_cell.setToolTip(reason_cell.text())
        size_cell = QTableWidgetItem("计算中…" if item.kind == KIND_FOLDER else "—")
        size_cell.setTextAlignment(int(Qt.AlignRight | Qt.AlignVCenter))
        for cell in (path_cell, reason_cell, size_cell):
            cell.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if color is not None and cell is not size_cell:
                cell.setForeground(QBrush(color))
        self.table.setItem(row, COL_PATH, path_cell)
        self.table.setItem(row, COL_SIZE, size_cell)
        self.table.setItem(row, COL_REASON, reason_cell)

    def _on_double_click(self, index) -> None:
        """双击一行：在资源管理器里定位它。"""
        item = self._item_at_row(index.row())
        if item is None:
            return
        if item.kind == KIND_REGISTRY:
            self._open_regedit(item)
            return
        import os
        import subprocess

        if not os.path.exists(item.path):
            self.status.setVisible(True)
            self.status.setStyleSheet(f"color:{MUTED.name()};")
            self.status.setText("这一项已经不在了，可能刚被清理过")
            return
        try:
            if os.path.isdir(item.path):
                subprocess.Popen(f'explorer.exe "{item.path}"')
            else:
                subprocess.Popen(f'explorer.exe /select,"{item.path}"')
        except OSError:
            self.status.setVisible(True)
            self.status.setStyleSheet(f"color:{MUTED.name()};")
            self.status.setText("打不开资源管理器")

    def _open_regedit(self, item) -> None:
        """注册表项没法在资源管理器里定位，打开注册表编辑器并把位置说清楚。"""
        import subprocess

        from PySide6.QtWidgets import QApplication

        key_path = item.path.split(" :: ")[0]
        QApplication.clipboard().setText(key_path)
        self.status.setVisible(True)
        self.status.setStyleSheet(f"color:{MUTED.name()};")
        self.status.setText(f"注册表位置已复制到剪贴板，打开注册表编辑器粘贴到地址栏即可：{key_path}")
        try:
            subprocess.Popen("regedit.exe")
        except OSError:
            pass

    def _item_at_row(self, row: int):
        for identity, row_index in self._rows.items():
            if row_index == row:
                for item in self._items:
                    if item.identity == identity:
                        return item
        return None

    def _on_item_changed(self, cell: QTableWidgetItem) -> None:
        if cell.column() != COL_CHECK:
            return
        for identity, row in self._rows.items():
            if row == cell.row():
                self._checks[identity] = cell.checkState() == Qt.Checked
                break
        self._refresh()

    # -- 大小 --------------------------------------------------------------
    def _start_measure(self) -> None:
        self._measure([item for item in self._items if item.kind == KIND_FOLDER])

    def _measure(self, items) -> None:
        """给这批项算大小。可以反复调用（AI 新加的项也要算）。"""
        targets = [item for item in items if item.kind == KIND_FOLDER and item.size is None]
        if not targets:
            return
        worker = make_measure_worker(targets)
        worker.signals.finished.connect(self._on_measured)
        self._measure_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_measured(self, sizes: dict) -> None:
        for item in self._items:
            if item.kind != KIND_FOLDER:
                continue
            size = sizes.get(item.identity, 0)
            item.size = size
            row = self._rows.get(item.identity)
            if row is None:
                continue
            cell = self.table.item(row, COL_SIZE)
            if cell is not None:
                cell.setText(human_size(size) if size else "0 字节")
        self._refresh()

    # -- 状态 --------------------------------------------------------------
    def chosen(self) -> list[Leftover]:
        return [item for item in self._items if self._checks.get(item.identity)]

    def _refresh(self) -> None:
        chosen = self.chosen()
        self.btn_clean.setEnabled(bool(chosen) and not self._busy)
        if not chosen:
            self.summary.setText(f"共 {len(self._items)} 项，一个都没勾（勾了才会清）")
            return
        folders = [item for item in chosen if item.kind == KIND_FOLDER]
        regs = [item for item in chosen if item.kind == KIND_REGISTRY]
        bits = [f"选中 {len(chosen)} 项"]
        known = [item.size for item in folders if item.size]
        if known and len(known) == len(folders):
            bits.append(f"约 {human_size(sum(known))}")
        if folders:
            bits.append(f"{len(folders)} 个文件夹（进回收站，能还原）")
        if regs:
            bits.append(f"{len(regs)} 个注册表项（先备份再删）")
        self.summary.setText("　·　".join(bits))

    def _do_ai(self) -> None:
        """让 AI 判断「名字对不上的残留」。"""
        from PySide6.QtCore import QThreadPool

        from core.ai_leftover import collect_suspects
        from core.tasks import make_call_worker

        if self._ai is None or not self._ai.is_configured():
            self.status.setVisible(True)
            self.status.setStyleSheet("color:#9a6700;")
            self.status.setText("AI 还没配好，先去「设置」里填上接口地址和密钥")
            return
        self.btn_ai.setEnabled(False)
        self.btn_ai.setText("正在收集候选…")
        self.btn_close.setText("停止 AI")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress.setFormat("正在收集候选…")
        self.status.setVisible(True)
        self.status.setStyleSheet(f"color:{MUTED.name()};")
        self.status.setText("正在找出不属于已装软件的文件和键…")
        worker = make_call_worker(
            lambda: collect_suspects(
                self._installed, focus=self._focus,
                kinds=self._kinds, scopes=self._scopes,
            )
        )
        worker.signals.finished.connect(self._on_suspects_collected)
        self._collect_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_suspects_collected(self, suspects) -> None:
        from PySide6.QtCore import QThreadPool

        from core.ai_leftover import make_judge_worker

        self._collect_worker = None
        if suspects is None:
            self.btn_ai.setEnabled(True)
            self.btn_ai.setText("🤖 让 AI 再找一遍漏掉的")
            self.btn_close.setText("关闭")
            self.progress.setVisible(False)
            self.status.setText("收集候选时出了点问题，请稍后重试")
            return
        if not suspects:
            self.btn_ai.setEnabled(True)
            self.btn_ai.setText("🤖 让 AI 再找一遍漏掉的")
            self.btn_close.setText("关闭")
            self.progress.setVisible(False)
            self.status.setVisible(True)
            self.status.setStyleSheet(f"color:{MUTED.name()};")
            if self._focus is not None:
                self.status.setText("这个软件名下的目录和键都已经被规则扫过了，没有新候选")
            else:
                self.status.setText("没找到「名字对不上」的候选目录，暂时没什么可让 AI 看的")
            return

        self.btn_ai.setEnabled(False)
        self.btn_ai.setText("AI 正在看…")
        self.btn_close.setText("停止 AI")      # 跑的时候这个按钮就是停止键
        self.progress.setVisible(True)
        self.progress.setRange(0, len(suspects))
        self.progress.setValue(0)
        self.progress.setFormat("AI 判断中 %v/%m")
        self.status.setVisible(True)
        self.status.setStyleSheet(f"color:{MUTED.name()};")
        self.status.setText(f"把 {len(suspects)} 个候选交给 AI 判断…")

        import threading

        self._ai_cancel = threading.Event()
        worker = make_judge_worker(suspects, self._installed, self._ai.settings,
                                   focus=self._focus, cancel=self._ai_cancel)
        worker.signals.progress.connect(self._on_ai_progress)
        worker.signals.finished.connect(self._on_ai_done)
        self._ai_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_ai_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"AI 判断中 {done}/{total}")

    def _on_ai_done(self, judged, error) -> None:
        from core.leftover import KIND_REGISTRY

        self._ai_worker = None
        self._ai_cancel = None
        self.btn_ai.setEnabled(True)
        self.btn_ai.setText("🤖 让 AI 再找一遍漏掉的")
        self.btn_close.setText("关闭")
        self.progress.setVisible(False)

        if error:
            self.status.setStyleSheet("color:#9a6700;")
            self.status.setText(error)
            return

        picked = [item for item in judged if item.verdict == "leftover"]
        if not picked:
            self.status.setStyleSheet(f"color:{MUTED.name()};")
            self.status.setText(f"AI 看了 {len(judged)} 个候选，没找到新的残留")
            return

        added = 0
        fresh = []
        # 几十上百行逐个插进去会把界面卡死（每次 setItem 都触发重排），
        # 所以插入期间先把重绘冻住，插完再一次性放开。
        self.table.setUpdatesEnabled(False)
        try:
            for suspect in picked:
                leftovers = Leftover(
                    KIND_REGISTRY if suspect.kind == "registry" else KIND_FOLDER,
                    f"AI 判断（{suspect.confidence or '不确定'}）",
                    suspect.path,
                    name=suspect.name,
                    reason=f"AI：{suspect.reason or '名字像已卸载软件'}",
                )
                if leftovers.identity in self._rows:
                    continue
                self._items.append(leftovers)
                self._checks[leftovers.identity] = False     # AI 找到的也默认不勾
                self._add_item_row(leftovers, DANGER if suspect.kind == "registry" else None)
                fresh.append(leftovers)
                added += 1
        finally:
            self.table.setUpdatesEnabled(True)
        self._refresh()
        if fresh:
            self._measure(fresh)          # 新项也要算大小，不能一直「计算中…」
            self.table.scrollToBottom()   # 让用户看见新加的在最下面
        self.status.setStyleSheet("color:#1a7f37;")
        self.status.setText(
            f"AI 看了 {len(judged)} 个候选，挑出 {added} 个属于这个软件的残留"
            "（默认不勾，看理由再决定）"
        )

    def _open_first_folder(self) -> None:
        if self._on_open_path is None:
            return
        for item in self._items:
            if item.kind == KIND_FOLDER:
                self._on_open_path(item.path)
                return
        self.status.setVisible(True)
        self.status.setStyleSheet(f"color:{MUTED.name()};")
        self.status.setText("这次只找到注册表项，没有可打开的文件夹。")

    def _try_close(self) -> None:
        if self._collect_worker is not None:
            self.status.setVisible(True)
            self.status.setText("正在收集候选，请稍等")
            return
        if self._ai_worker is not None:
            self._cancel_ai()
            return
        if self._busy:
            return
        self.reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._busy or self._ai_worker is not None or self._collect_worker is not None:
            self.status.setVisible(True)
            self.status.setText("正在处理，完成前不能关闭")
            event.ignore()
            return
        super().closeEvent(event)

    def _cancel_ai(self) -> None:
        """停止还在跑的 AI 判断。已经判断出来的结果留着。"""
        if self._ai_cancel is not None:
            self._ai_cancel.set()
        self.status.setVisible(True)
        self.status.setStyleSheet("color:#9a6700;")
        self.status.setText("已停止，剩下不再问了。已经判断出来的结果留着")

    # -- 清理 --------------------------------------------------------------
    def _do_clean(self) -> None:
        """勾了就直接清，不再弹确认 —— 清单本身就在眼前，勾选就是确认。"""
        chosen = self.chosen()
        if not chosen:
            return

        self._busy = True
        self.btn_clean.setEnabled(False)
        self.btn_close.setEnabled(False)
        self.btn_open.setEnabled(False)
        self.table.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(chosen))
        self.progress.setValue(0)
        self.progress.setFormat("准备中… %v/%m")
        self.status.setVisible(True)
        self.status.setStyleSheet(f"color:{MUTED.name()};")
        self.status.setText("正在清理…")

        worker = make_clean_worker(chosen, self._log_dir)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_cleaned)
        self._worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_progress(self, done: int, total: int, path: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"正在清理 {done}/{total}　%p%")
        if done < total:
            name = path.rsplit("\\", 1)[-1] or path
            self.status.setText(f"正在处理：{name}")

    def _on_cleaned(self, report) -> None:
        self._worker = None
        self._busy = False
        self.report = report
        self.progress.setValue(self.progress.maximum())
        self.progress.setFormat("完成")
        self.btn_close.setEnabled(True)
        # 这里必须把表格放开：_do_clean 里把它整个禁用了，
        # 不恢复的话清理过一次之后就再也勾不动任何东西（包括 AI 后来加的项）。
        self.table.setEnabled(True)
        self.btn_open.setEnabled(True)

        failed = {item.leftover.identity: item for item in report.items if not item.ok}
        for item in self._items:
            result = failed.get(item.identity)
            row = self._rows.get(item.identity)
            if row is None:
                continue
            if result is None and self._checks.get(item.identity):
                cell = self.table.item(row, COL_CHECK)
                if cell is not None:
                    cell.setCheckState(Qt.Unchecked)
                    cell.setFlags(Qt.ItemIsEnabled)
                path_cell = self.table.item(row, COL_PATH)
                if path_cell is not None:
                    path_cell.setText(f"✓ 已清理　{path_cell.text()}")
                    path_cell.setForeground(QBrush(MUTED))
                self._checks[item.identity] = False
            elif result is not None:
                reason = self.table.item(row, COL_REASON)
                if reason is not None:
                    reason.setText(result.error or "没清掉")
                    reason.setForeground(QBrush(DANGER))

        if report.fail_count == 0:
            bits = [f"清理完成，共 {report.ok_count} 项"]
            if report.backup:
                bits.append(f"注册表备份：{report.backup.name}")
            self.status.setStyleSheet(f"color:{OK.name()};")
            self.status.setText("　·　".join(bits))
        else:
            bits = [f"清掉 {report.ok_count} 项，{report.fail_count} 项没动"]
            if report.backup:
                bits.append(f"注册表备份：{report.backup.name}")
            self.status.setStyleSheet("color:#9a6700;")
            self.status.setText("　·　".join(bits) + "（失败原因见最后一列）")
        # 按钮不藏：这一批没勾的项（或者新冒出来的）还要能再清一次
        self.btn_clean.setVisible(True)
        self.btn_open.setVisible(True)
        self._refresh()
        if not self.chosen():
            hint = "还想清剩下的，勾上再点一次「清理选中项」"
            self.status.setText(self.status.text() + "　·　" + hint)


def describe(items) -> str:
    """给状态栏/日志用的一句话。"""
    folders = sum(1 for item in items if item.kind == KIND_FOLDER)
    regs = sum(1 for item in items if item.kind == KIND_REGISTRY)
    bits = []
    if folders:
        bits.append(f"{folders} 个文件夹")
    if regs:
        bits.append(f"{regs} 个注册表项")
    return "、".join(bits) or "没有残余"
