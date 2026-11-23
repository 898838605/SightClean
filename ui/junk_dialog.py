"""一键清理 C 盘垃圾的对话框。

按类别勾，不逐条列 —— 十几万条列出来既没用又卡。
每类都写清「删了会怎样」，用户看着办。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.fs_browser import human_size
from core.junk import make_clean_worker, make_scan_worker

MUTED = QColor("#57606a")
CAUTION = QColor("#9a6700")
OK = QColor("#1a7f37")


class JunkDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("一键清理 C 盘垃圾")
        self.setMinimumSize(820, 640)
        self._report = None
        self._boxes: dict[str, QCheckBox] = {}
        self._rows: dict[str, QLabel] = {}
        self._scan_worker = None
        self._clean_worker = None
        self.outcome = None
        self._busy = False

        self.title = QLabel("正在扫描…")
        font = self.title.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self.title.setFont(font)

        self.hint = QLabel(
            "只清规则里写死的那几类，不自由发挥。\n"
            "这些都是缓存和临时文件，删了会自动重建；正在被程序占用的会跳过。"
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color:{MUTED.name()};")

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.inner = QWidget()
        self.inner_layout = QVBoxLayout(self.inner)
        self.inner_layout.setContentsMargins(0, 0, 8, 0)
        self.inner_layout.addStretch(1)
        self.area.setWidget(self.inner)

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color:{MUTED.name()};")

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setMinimumHeight(22)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setVisible(False)

        self.buttons = QDialogButtonBox()
        self.btn_ok = self.buttons.addButton("开始清理", QDialogButtonBox.AcceptRole)
        self.btn_cancel = self.buttons.addButton("关闭", QDialogButtonBox.RejectRole)
        self.btn_ok.setEnabled(False)
        self.btn_ok.clicked.connect(self._start_clean)
        self.btn_cancel.clicked.connect(self._try_close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.hint)
        layout.addWidget(self.area, 1)
        layout.addWidget(self.summary)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        self._reload_empty()
        self._start_scan()

    # -- 扫描 --------------------------------------------------------------
    def _reload_empty(self) -> None:
        while self.inner_layout.count():
            item = self.inner_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._boxes.clear()
        self._rows.clear()

    def _start_scan(self) -> None:
        self.btn_ok.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)          # 不确定进度，转圈
        self.progress.setFormat("正在扫描…")
        worker = make_scan_worker()
        worker.signals.progress.connect(self._on_scan_progress)
        worker.signals.finished.connect(self._on_scanned)
        self._scan_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_scan_progress(self, done: int, total: int, label: str) -> None:
        if total:
            self.progress.setRange(0, max(total, 1))
            self.progress.setValue(done)
            self.progress.setFormat(f"正在扫描 {label} %v/%m")

    def _on_scanned(self, report) -> None:
        self._scan_worker = None
        self._report = report
        self.progress.setVisible(False)

        if report is None or not report.categories:
            self.title.setText("没扫到可清理的东西")
            self.summary.setText("")
            return

        self.title.setText(
            f"找到 {report.total_count} 项垃圾，约 {human_size(report.total_size)}"
        )
        self._reload_empty()
        for result in report.categories:
            self._add_row(result)
        self.inner_layout.addStretch(1)
        self._refresh()
        self._refresh()

    def _add_row(self, result) -> None:
        holder = QWidget()
        row = QVBoxLayout(holder)
        row.setContentsMargins(0, 6, 0, 6)
        row.setSpacing(2)

        top = QHBoxLayout()
        box = QCheckBox(result.label)
        box.setChecked(result.is_safe)      # 默认只勾安全的
        box.toggled.connect(self._refresh)
        self._boxes[result.category.id] = box
        top.addWidget(box)

        size_text = QLabel(f"{result.count} 项 · {human_size(result.size)}")
        size_text.setStyleSheet(f"color:{MUTED.name()};")
        top.addStretch(1)
        top.addWidget(size_text)
        row.addLayout(top)

        purpose = QLabel(result.category.purpose)
        purpose.setWordWrap(True)
        purpose.setStyleSheet(f"color:{MUTED.name()}; margin-left:22px;")
        row.addWidget(purpose)

        if result.category.detail:
            detail = QLabel(result.category.detail)
            detail.setWordWrap(True)
            color = CAUTION.name() if not result.is_safe else MUTED.name()
            detail.setStyleSheet(f"color:{color}; margin-left:22px;")
            row.addWidget(detail)

        # 具体扫的是哪些位置 —— 双击能跳过去看看
        shown = result.paths[:3]
        for path in shown:
            row.addWidget(self._path_link(path))
        if len(result.paths) > len(shown):
            more = QLabel(f"…还有 {len(result.paths) - len(shown)} 处")
            more.setStyleSheet(f"color:{MUTED.name()}; margin-left:22px;")
            row.addWidget(more)

        self.inner_layout.addWidget(holder)

    def _path_link(self, path: str) -> QLabel:
        """做成一个可双击的路径。"""
        link = QLabel(path)
        link.setWordWrap(True)
        link.setCursor(Qt.PointingHandCursor)
        link.setToolTip("双击打开这个位置")
        link.setStyleSheet("color:#0969da; margin-left:22px;")
        link.mouseDoubleClickEvent = lambda _event, p=path: self._open_path(p)
        return link

    def _open_path(self, path: str) -> None:
        """在资源管理器里打开。是文件就选中它。"""
        import os
        import subprocess

        if not os.path.exists(path):
            self.status.setVisible(True)
            self.status.setStyleSheet(f"color:{MUTED.name()};")
            self.status.setText("这个位置已经不在了，可能刚被清理过")
            return
        try:
            if os.path.isdir(path):
                subprocess.Popen(f'explorer.exe "{path}"')
            else:
                subprocess.Popen(f'explorer.exe /select,"{path}"')
        except OSError:
            self.status.setVisible(True)
            self.status.setStyleSheet(f"color:{MUTED.name()};")
            self.status.setText("打不开资源管理器")

    def _chosen(self) -> list:
        if self._report is None:
            return []
        return [item for item in self._report.categories
                if self._boxes.get(item.category.id) is not None
                and self._boxes[item.category.id].isChecked()]

    def _refresh(self) -> None:
        chosen = self._chosen()
        total_size = sum(item.size for item in chosen)
        total_count = sum(item.count for item in chosen)
        if not chosen:
            self.summary.setText("一类都没勾，勾上才会清")
            self.btn_ok.setEnabled(False)
            return
        self.summary.setText(
            f"已选 {len(chosen)} 类，共 {total_count} 项，约 {human_size(total_size)}"
        )
        self.btn_ok.setEnabled(not self._busy)

    # -- 清理 --------------------------------------------------------------
    def _start_clean(self) -> None:
        chosen = self._chosen()
        if not chosen:
            return
        self._busy = True
        self.btn_ok.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.area.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress.setFormat("正在清理…")
        self.status.setVisible(True)
        self.status.setStyleSheet(f"color:{MUTED.name()};")
        self.status.setText("正在删…被占用的会自动跳过")

        worker = make_clean_worker(chosen, self._log_dir())
        worker.signals.progress.connect(self._on_clean_progress)
        worker.signals.finished.connect(self._on_cleaned)
        self._clean_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_clean_progress(self, done: int, total: int, path: str) -> None:
        if total:
            self.progress.setRange(0, max(total, 1))
            self.progress.setValue(done)
            name = path.rsplit("\\", 1)[-1] or path
            self.progress.setFormat(f"正在清理 {done}/{total}　{name[:30]}")

    def _on_cleaned(self, outcome) -> None:
        self._clean_worker = None
        self._busy = False
        self.outcome = outcome
        self.progress.setValue(self.progress.maximum())
        self.progress.setFormat("完成")
        self.btn_cancel.setEnabled(True)
        self.area.setEnabled(True)

        if outcome is None:
            self.status.setText("清理没跑完")
            return

        bits = [f"清掉 {outcome.removed} 项，释放 {human_size(outcome.freed)}"]
        if outcome.skipped:
            bits.append(f"{outcome.skipped} 项被占用跳过")
        self.status.setStyleSheet(f"color:{OK.name()};")
        self.status.setText("　·　".join(bits))
        self.title.setText("清理完成")
        self.summary.setText("")
        self.btn_ok.setVisible(False)

    def _try_close(self) -> None:
        if self._busy:
            return
        self.reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._busy:
            self.status.setVisible(True)
            self.status.setText("正在清理，完成前不能关闭")
            event.ignore()
            return
        super().closeEvent(event)

    def _log_dir(self):
        parent = self.parent()
        paths = getattr(parent, "paths", None)
        if paths is not None and getattr(paths, "logs", None) is not None:
            return paths.logs
        from app_paths import resolve

        return resolve().logs
