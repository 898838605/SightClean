"""对话框：删除确认清单、删除结果报告、设置、使用说明。

删除确认这一块是整个程序最容易出事的地方，所以刻意做成：
1. 清单里每一行都能单独取消勾选，不是「点了就全删」；
2. 「彻底删除」是一个需要单独点亮、并且还要再勾一次「我知道」的选项；
3. 红色项（别删）照样能勾，但整行标红提醒你别手滑。
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QDoubleSpinBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
)

from core.ai_client import PROVIDERS
from core.delete import DeleteReport, Mode
from core.fs_browser import human_size

DANGER_BG = QColor("#fff0f0")
DANGER_FG = QColor("#c62828")
MUTED_FG = QColor("#57606a")

def _bold(item: QTableWidgetItem) -> QTableWidgetItem:
    font = QFont(item.font())
    font.setBold(True)
    item.setFont(font)
    return item

class DeleteReportDialog(QDialog):
    """删除结果：成功几项、失败几项、为什么失败，并且能复制或另存。"""

    def __init__(
        self,
        report: DeleteReport,
        log_path: Path | None = None,
        parent=None,
        skipped=None,
        on_retry=None,
        op_type: str = "",
        on_scan=None,
    ) -> None:
        super().__init__(parent)
        from core import result_report

        self._report = report
        self._log_path = log_path or getattr(report, "log_path", None)
        self._op_type = op_type or self._guess_op(report)
        self._rows = result_report.rows_from_report(report, self._op_type)
        self._skipped = result_report.skipped_rows(skipped or [])
        self._on_retry = on_retry
        self._on_scan = on_scan

        self.setWindowTitle("执行结果")
        self.setMinimumSize(860, 520)

        ok = report.ok_count
        failed = report.fail_count
        mode = getattr(report, "mode", None)
        # 卸载和删除有本质区别：删除是我们自己动手，做完就是做完了；
        # 卸载只是把人家自己的卸载程序拉起来，它跑没跑完、用户会不会点取消，
        # 我们一概不知道。所以这里只能说「已启动」，不能说「成功」。
        self._is_uninstall = self._op_type == "uninstall"
        if self._is_uninstall:
            action = "启动"
            headline = (f"{ok} 个卸载程序已启动" if failed == 0
                        else f"{ok} 个卸载程序已启动，{failed} 个没启动")
        else:
            action = "删" if mode is not None else "处理"
            headline = f"{action}完了" if failed == 0 else f"有 {failed} 项没{action}成"
        title = QLabel(headline)
        title_font = QFont(title.font())
        title_font.setPointSize(title_font.pointSize() + 3)
        title_font.setBold(True)
        title.setFont(title_font)

        if self._is_uninstall:
            summary_parts = [f"已启动 {ok} 个", f"没启动 {failed} 个",
                             "接下来要由它们自己的界面负责，这里看不到进度"]
        else:
            summary_parts = [f"成功 {ok} 项", f"失败 {failed} 项"]
        freed = getattr(report, "freed", 0) or 0
        if freed and getattr(report, "mode", None) is not None:
            if report.mode is Mode.RECYCLE:
                summary_parts.append(f"约 {human_size(freed)} 已移入回收站（清空后才真正释放）")
            else:
                summary_parts.append(f"释放 {human_size(freed)}")
        summary = QLabel("　·　".join(summary_parts))
        summary.setStyleSheet(f"color:{MUTED_FG.name()};")

        # 逐项列表：一行一项，状态和原因分开写清楚，不再是「成功 X 失败 Y」一句话
        all_rows = self._rows + self._skipped
        self.table = QTableWidget(len(all_rows), 3)
        self.table.setObjectName("report_table")
        self.table.setHorizontalHeaderLabels(["名称 / 路径", "状态", "原因"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 84)
        for row, item in enumerate(all_rows):
            label = item.name if item.name == item.path or not item.path else f"{item.name}　{item.path}"
            name_cell = QTableWidgetItem(label)
            name_cell.setToolTip(item.path or item.name)
            # 卸载的「成功」其实是「卸载程序被拉起来了」，不能写成成功
            label = item.status_label
            if getattr(self, "_is_uninstall", False) and item.status == "ok":
                label = "已启动"
            elif getattr(self, "_is_uninstall", False) and item.status == "fail":
                label = "没启动"
            state_cell = QTableWidgetItem(label)
            reason_cell = QTableWidgetItem(item.reason_text or "—")
            for cell in (name_cell, state_cell, reason_cell):
                cell.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            color = {"ok": QColor("#1a7f37"), "fail": QColor("#cf222e"),
                     "skipped": QColor("#9a6700")}.get(item.status)
            if color is not None:
                state_cell.setForeground(QBrush(color))
            self.table.setItem(row, 0, name_cell)
            self.table.setItem(row, 1, state_cell)
            self.table.setItem(row, 2, reason_cell)

        browser = QTextBrowser()
        browser.setObjectName("report_browser")
        browser.setPlainText(self._build_body())
        browser.setVisible(False)      # 留作文本版，复制/另存走它

        hint = QLabel(
            f"这份记录已经存在：{self._log_path}" if self._log_path
            else "这次没写成记录文件（可能没有写权限），但删除结果以上面为准。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{MUTED_FG.name()};")

        retryable = [row for row in self._rows if row.retryable]
        self.btn_retry = QPushButton(f"重试失败的 {len(retryable)} 项")
        self.btn_retry.setEnabled(bool(retryable) and on_retry is not None)
        self.btn_retry.setToolTip("只重试现在还在的项；已经不存在的不再尝试")
        self.btn_retry.clicked.connect(self._do_retry)
        self._retryable = retryable

        row = QHBoxLayout()
        row.addWidget(self.btn_retry)
        row.addSpacing(12)
        btn_copy = QPushButton("复制报告")
        btn_save = QPushButton("另存为 txt")
        btn_open = QPushButton("打开记录所在文件夹")
        btn_copy.clicked.connect(self._copy)
        btn_save.clicked.connect(self._save_as)
        btn_open.clicked.connect(self._open_folder)
        btn_open.setEnabled(bool(self._log_path))
        for button in (btn_copy, btn_save, btn_open):
            row.addWidget(button)
        row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        ok_button = buttons.button(QDialogButtonBox.Ok)
        if self._is_uninstall and on_scan is not None:
            # 卸载时「知道了」没意义 —— 用户真正想做的是看看留下了什么
            ok_button.setText("扫描卸载残余")
            ok_button.setToolTip("卸载程序可能还在跑，按现在的情况扫一遍它留下的东西")
            ok_button.clicked.connect(self._do_scan)
        else:
            ok_button.setText("知道了")
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(summary)
        backup = getattr(report, "backup_path", None)
        if backup:
            backup_label = QLabel(f"动手之前已经把选中的内容备份到：{backup}")
            backup_label.setWordWrap(True)
            backup_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            backup_label.setStyleSheet(f"color:{MUTED_FG.name()};")
            layout.addWidget(backup_label)
        denied = [row for row in self._rows if row.category == "denied"]
        if denied:
            admin_hint = QLabel(
                f"有 {len(denied)} 项因为权限不够没动成（比如 Program Files 下面的东西）。\n"
                "用「工具 → 以管理员身份重新启动」提权后，这些项就能正常处理了。"
            )
            admin_hint.setWordWrap(True)
            admin_hint.setStyleSheet("color:#9a6700;")
            layout.addWidget(admin_hint)

        if self._skipped:
            skipped_hint = QLabel(
                f"另有 {len(self._skipped)} 项你在最后一步选了「仅执行普通项」，这次先放着了（见列表里的「已暂缓」）。"
            )
            skipped_hint.setWordWrap(True)
            skipped_hint.setStyleSheet("color:#9a6700;")
            layout.addWidget(skipped_hint)
        layout.addWidget(self.table, 1)
        layout.addWidget(browser)
        layout.addWidget(hint)
        layout.addLayout(row)
        layout.addWidget(buttons)

        self._browser = browser

    # -- 内部 --------------------------------------------------------------
    @staticmethod
    def _guess_op(report) -> str:
        if getattr(report, "mode", None) is not None:
            return "delete"
        if hasattr(report, "backup_path"):
            return "clean"
        items = getattr(report, "items", []) or []
        if items and hasattr(items[0], "pid"):
            return "kill"
        return "uninstall"

    def _do_scan(self) -> None:
        """关掉报告，让主窗口去扫残留。"""
        self.accept()
        if self._on_scan is not None:
            self._on_scan()

    def _do_retry(self) -> None:
        if self._on_retry is None or not self._retryable:
            return
        rows = list(self._retryable)
        self.accept()
        self._on_retry(rows)

    def _build_body(self) -> str:
        """文本版记录。四个模块的 report 字段名各不相同，
        所以一律走 result_report 的统一视图，不直接读 item 的属性。"""
        report = self._report
        lines: list[str] = []
        lines.append(f"时间：{time_text(getattr(report, 'finished', 0.0))}")
        mode = getattr(report, "mode", None)
        if mode is Mode.PERMANENT:
            lines.append("方式：彻底删除（找不回来）")
        elif mode is Mode.RECYCLE:
            lines.append("方式：移入回收站（能还原）")
        else:
            backup = getattr(report, "backup_path", None)
            lines.append(f"方式：直接执行{'（已生成备份：' + str(backup) + '）' if backup else ''}")
        lines.append("")

        verb = {"delete": "删", "uninstall": "卸", "clean": "清理", "kill": "结束"}.get(
            self._op_type, "处理"
        )
        ok_rows = [row for row in self._rows if row.status == "ok"]
        fail_rows = [row for row in self._rows if row.status == "fail"]
        if ok_rows:
            heading = "已启动的" if getattr(self, "_is_uninstall", False) else "成功的"
            lines.append(f"—— {heading} {len(ok_rows)} 项 ——")
            for row in ok_rows:
                size_text = human_size(row.size) if row.size else "—"
                lines.append(f"  ✓ {size_text:>10}  {row.name or row.path}")
            lines.append("")
        if fail_rows:
            if getattr(self, "_is_uninstall", False):
                lines.append(f"—— 没启动的 {len(fail_rows)} 项 ——")
            else:
                lines.append(f"—— 没{verb}成的 {len(fail_rows)} 项 ——")
            for row in fail_rows:
                lines.append(f"  ✗ {row.reason_text or '原因不明'}")
                lines.append(f"    {row.path or row.name}")
        if self._skipped:
            lines.append("")
            lines.append(f"—— 已暂缓的 {len(self._skipped)} 项（你选了「仅执行普通项」）——")
            for row in self._skipped:
                lines.append(f"  · {row.name or row.path}")
        if lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def _report_text(self) -> str:
        text = getattr(self._report, "as_log_text", None)
        if callable(text):
            try:
                return text()
            except Exception:  # noqa: BLE001
                pass
        return self._browser.toPlainText()

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._report_text())

    def _save_as(self) -> None:
        default = str(self._log_path) if self._log_path else str(Path.cwd() / "删除结果.txt")
        path, _filter = QFileDialog.getSaveFileName(self, "另存为", default, "文本文件 (*.txt)")
        if not path:
            return
        try:
            Path(path).write_text(self._report_text(), encoding="utf-8")
        except OSError:
            pass

    def _open_folder(self) -> None:
        if not self._log_path:
            return
        try:
            os.startfile(str(Path(self._log_path).parent))  # noqa: S606
        except Exception:  # noqa: BLE001
            pass

def time_text(stamp: float) -> str:
    import time

    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
    except (OSError, ValueError):
        return "—"

class KnowledgeHealthDialog(QDialog):
    """知识库健康检查：列出没写 impact（后果）的规则。

    只报告，不改行为 —— 红色规则在加载时已经降成灰色，这里只是把「该补哪几条」摊开。
    """

    HEADERS = ("类型", "规则 ID", "匹配方式", "pattern", "当前风险", "状态")

    def __init__(self, kb, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("知识库健康检查")
        self.setMinimumSize(900, 500)
        self._kb = kb
        self._issues = list(kb.impact_issues())
        self._total = getattr(kb, "rule_count", 0)

        downgraded = sum(1 for item in self._issues if item["downgraded"])
        if not self._issues:
            headline = f"全部 {self._total} 条规则都写了 impact，没问题"
        else:
            headline = f"{self._total} 条规则里有 {len(self._issues)} 条没写 impact"
            if downgraded:
                headline += f"，其中 {downgraded} 条是禁止级、已自动降为灰色"
        title = QLabel(headline)
        font = QFont(title.font())
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        title.setFont(font)
        title.setWordWrap(True)

        intro = QLabel(
            "impact 是「删了会怎样」那一句，三道坎的最后一关会原样念给你看。\n"
            "禁止级缺 impact 会被降成灰色（因为说不出后果的红色没有意义）；黄色照常生效。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{MUTED_FG.name()};")

        table = QTableWidget(len(self._issues), len(self.HEADERS))
        table.setObjectName("health_table")
        table.setHorizontalHeaderLabels(list(self.HEADERS))
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(26)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        header.setStretchLastSection(True)
        table.setColumnWidth(0, 84)
        table.setColumnWidth(1, 180)
        table.setColumnWidth(2, 140)
        table.setColumnWidth(3, 260)
        table.setColumnWidth(4, 92)
        for row, item in enumerate(self._issues):
            cells = (
                item["kind"],
                item["id"],
                item["match"],
                item["pattern"],
                item["risk"],
                "已降为灰色（原本是红色）" if item["downgraded"] else "黄色，照常生效",
            )
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(str(text))
                cell.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                cell.setToolTip(str(item.get("effect") or ""))
                if item["downgraded"]:
                    cell.setForeground(QBrush(QColor("#8b949e")))
                table.setItem(row, column, cell)
        if not self._issues:
            table.setVisible(False)

        btn_export = QPushButton("导出待补清单…")
        btn_export.setToolTip("导成 CSV，一列 id 一列空的 impact，填完贴回知识库就行")
        btn_export.clicked.connect(self._export)
        btn_export.setEnabled(bool(self._issues))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        ok_button = buttons.button(QDialogButtonBox.Ok)
        ok_button.setText("知道了")
        buttons.accepted.connect(self.accept)

        row = QHBoxLayout()
        row.addWidget(btn_export)
        row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addWidget(table, 1)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def _export(self) -> None:
        default = str(Path.home() / "知识库待补清单.csv")
        path, _filter = QFileDialog.getSaveFileName(self, "导出待补清单", default, "CSV 文件 (*.csv)")
        if not path:
            return
        import csv

        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["id", "kind", "match", "pattern", "risk", "effect", "impact"])
                for item in self._issues:
                    writer.writerow([
                        item["id"], item["kind"], item["match"], item["pattern"],
                        item["risk"], item.get("effect", ""), "",
                    ])
        except OSError:
            QMessageBox.warning(self, "没导成", "文件可能被占用着，换个位置再试。")
            return
        QMessageBox.information(self, "导出好了", f"待补清单已经存到：\n{path}\n\n把最后一列填上就行。")

class FeedbackDialog(QDialog):
    """「这条说明不对」：记一句就走，不联网、不改知识库。"""

    def __init__(self, path: str, name: str, purpose: str, module: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("这条说明不对")
        self.setMinimumWidth(520)
        self._path = path
        self._name = name
        self._purpose = purpose
        self._module = module

        title = QLabel(f"「{name or path}」")
        font = QFont(title.font())
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        title.setFont(font)
        title.setWordWrap(True)

        shown = QLabel(f"现在显示的是：{purpose or '（没有说明）'}")
        shown.setWordWrap(True)
        shown.setStyleSheet(f"color:{MUTED_FG.name()};")

        self.edit = QTextEdit()
        self.edit.setPlaceholderText("说说哪里不对（可以不填，直接保存也行）")
        self.edit.setMinimumHeight(90)

        note = QLabel(
            "这条反馈只存在本机的 feedback.json 里，不会上传、不会联网，"
            "也不会自动改动知识库。要不要采纳，你自己说了算。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED_FG.name()};")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("记下来")
        buttons.button(QDialogButtonBox.Cancel).setText("算了")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(shown)
        layout.addWidget(self.edit)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def _save(self) -> None:
        from core import feedback

        ok, message = feedback.add(
            path=self._path, name=self._name, purpose=self._purpose,
            note=self.edit.toPlainText().strip(), module=self._module,
        )
        if ok:
            QMessageBox.information(self, "记下了", "反馈已经存到本机的 feedback.json 里。\n在「设置」里可以查看和导出。")
            self.accept()
        else:
            QMessageBox.warning(self, "没记下来", message)

class FeedbackListDialog(QDialog):
    """查看 / 导出已经记下的反馈。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("已记下的反馈")
        self.setMinimumSize(880, 520)

        from core import feedback

        items = feedback.load_all()

        title = QLabel(f"一共 {len(items)} 条反馈")
        font = QFont(title.font())
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        title.setFont(font)

        hint = QLabel(
            "这些只存在本机 feedback.json 里。程序不会上传、也不会自动改知识库 —— "
            "想改哪条，去「知识库.json」里自己动手。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{MUTED_FG.name()};")

        headers = ("时间", "模块", "名称", "当时显示的说明", "我的反馈")
        table = QTableWidget(len(items), len(headers))
        table.setObjectName("feedback_table")
        table.setHorizontalHeaderLabels(list(headers))
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(26)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        table.setColumnWidth(0, 132)
        table.setColumnWidth(1, 92)
        for row, item in enumerate(items):
            cells = (
                item.time.replace("T", " "),
                feedback.MODULE_LABELS.get(item.module, item.module),
                item.name or item.path,
                item.purpose,
                item.note or "（没填）",
            )
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(str(text))
                cell.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                cell.setToolTip(str(text))
                table.setItem(row, column, cell)

        btn_export = QPushButton("导出 CSV…")
        btn_export.setEnabled(bool(items))
        btn_export.clicked.connect(self._export)
        btn_clear = QPushButton("清空…")
        btn_clear.setEnabled(bool(items))
        btn_clear.clicked.connect(self._clear)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addWidget(btn_export)
        row.addWidget(btn_clear)
        row.addStretch(1)
        row.addWidget(btn_close)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(table, 1)
        layout.addLayout(row)

    def _export(self) -> None:
        from core import feedback

        default = str(Path.home() / "我的反馈.csv")
        path, _filter = QFileDialog.getSaveFileName(self, "导出反馈", default, "CSV 文件 (*.csv)")
        if not path:
            return
        ok, message = feedback.export(path)
        if ok:
            QMessageBox.information(self, "导出好了", f"已经存到：\n{message}")
        else:
            QMessageBox.warning(self, "没导成", message)

    def _clear(self) -> None:
        from core import feedback

        answer = QMessageBox.question(
            self, "清空反馈",
            "把记下的反馈全部删掉吗？这个动作没法撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        feedback.clear()
        self.accept()

class SettingsDialog(QDialog):
    """只有本地设置与联网解释开关，没有任何注册表/卸载相关的入口。"""

    def __init__(self, settings, parent=None, cache=None, kb=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(540)
        self._settings = settings
        self._cache = cache
        self._kb = kb

        from ui.theme import SCALE_LABELS, THEME_LABELS

        self.theme_box = QComboBox()
        for label, value in THEME_LABELS:
            self.theme_box.addItem(label, value)
        theme_index = self.theme_box.findData(getattr(settings, "theme", "system"))
        self.theme_box.setCurrentIndex(theme_index if theme_index >= 0 else 0)
        self.theme_box.currentIndexChanged.connect(self._preview_appearance)

        self.scale_box = QComboBox()
        for label, value in SCALE_LABELS:
            self.scale_box.addItem(label, value)
        scale_index = self.scale_box.findData(getattr(settings, "font_scale", "medium"))
        self.scale_box.setCurrentIndex(scale_index if scale_index >= 0 else 1)
        self.scale_box.currentIndexChanged.connect(self._preview_appearance)

        self.auto_size = QCheckBox("进入文件夹后自动算一下文件夹有多大")
        self.auto_size.setChecked(settings.auto_size)
        self.confirm_recycle = QCheckBox("放进回收站之前也确认一下")
        self.confirm_recycle.setChecked(settings.confirm_recycle)
        self.show_hidden = QCheckBox("显示隐藏的文件和文件夹")
        self.show_hidden.setChecked(settings.show_hidden)

        self.ai_enabled = QCheckBox("允许手动联网识别（默认关闭，只有你点按钮才会联网）")
        self.ai_enabled.setChecked(settings.ai.enabled)
        self.provider = QComboBox()
        for key, (label, _url, _model) in PROVIDERS.items():
            self.provider.addItem(label, key)
        index = self.provider.findData(settings.ai.provider)
        self.provider.setCurrentIndex(index if index >= 0 else 0)
        self.provider.currentIndexChanged.connect(self._apply_preset)
        self.mask_username = QCheckBox("发送时把用户名换成 %USER%（推荐开着，不会把真实用户名发出去）")
        self.mask_username.setChecked(settings.ai.mask_username)
        self.base_url = QLineEdit(settings.ai.base_url)
        self.base_url.setPlaceholderText("例如 https://api.example.com")
        self.model = QLineEdit(settings.ai.model)
        self.model.setPlaceholderText("服务方给的模型名")
        self.api_key = QLineEdit(settings.ai.api_key)
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("密钥是明文存在本机设置文件里的")
        self.timeout = QSpinBox()
        self.timeout.setRange(3, 120)
        self.timeout.setValue(int(settings.ai.timeout_seconds))
        self.timeout.setSuffix(" 秒")

        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.1)
        self.temperature.setDecimals(2)
        self.temperature.setValue(float(getattr(settings.ai, "temperature", 0.1)))
        self.temperature.setToolTip(
            "控制 AI 回答的「发散程度」。\n"
            "0 = 最死板：同样的输入每次都给同样的答案，适合判断类任务（找残留、判风险）\n"
            "0.1 = 默认值，基本稳定\n"
            "0.5 以上 = 每次说法都不一样，只适合写文案这类需要变化的事\n"
            "填错或超出 0~2 会自动退回 0.1"
        )
        self.temperature_hint = QLabel(
            "0 最死板（同一问题答案一致）· 越高越发散 · 判断类任务建议 0~0.3"
        )
        self.temperature_hint.setWordWrap(True)
        self.temperature_hint.setStyleSheet("color:#57606a;")

        self.btn_clear_ai = QPushButton("清空 AI 识别缓存")
        self.btn_clear_ai.setToolTip("删掉已经存下来的 AI 解释，下次进目录会重新联网问")
        self.btn_clear_ai.clicked.connect(self._clear_ai_cache)

        from core import snapshot as snapshots

        snap_bytes, snap_count = snapshots.usage()
        self.lbl_snapshots = QLabel(
            f"操作快照：占 {human_size(snap_bytes)} / 共 {snap_count} 条（上限 200 条或 50 MB，超了先清最旧的）"
        )
        self.lbl_snapshots.setStyleSheet(f"color:{MUTED_FG.name()};")
        self.btn_clear_snapshots = QPushButton("清空操作快照")
        self.btn_clear_snapshots.setToolTip("只删快照记录，不动回收站，也不动 backups 里的备份")
        self.btn_clear_snapshots.clicked.connect(self._clear_snapshots)
        self.btn_undo = QPushButton("打开撤销中心…")
        self.btn_undo.setToolTip("看看做过哪些操作，能还原的在那里还原")
        self.btn_undo.clicked.connect(self._open_undo)

        from ui.widgets import export_csv

        exp_bytes, exp_count = export_csv.usage()
        self.lbl_exports = QLabel(
            f"导出的 CSV：占 {human_size(exp_bytes)} / 共 {exp_count} 份"
            f"（上限 {export_csv.MAX_COUNT} 份或 {human_size(export_csv.MAX_BYTES)}，超了先清最旧的）"
        )
        self.lbl_exports.setStyleSheet(f"color:{MUTED_FG.name()};")
        self.btn_clear_exports = QPushButton("清空导出的 CSV")
        self.btn_clear_exports.clicked.connect(self._clear_exports)

        from core import feedback as feedback_store

        self.lbl_feedback = QLabel(
            f"说明反馈：已记 {feedback_store.count()} 条（只存本机，不上传、不改知识库）"
        )
        self.lbl_feedback.setStyleSheet(f"color:{MUTED_FG.name()};")
        self.btn_feedback = QPushButton("查看 / 导出反馈…")
        self.btn_feedback.clicked.connect(self._open_feedback)


        self.btn_health = QPushButton("知识库健康检查…")
        self.btn_health.setToolTip("看看哪些规则还没写「删了会怎样」，可以把待补清单导出")
        self.btn_health.clicked.connect(self._check_knowledge)
        self.btn_health.setEnabled(kb is not None)
        if kb is not None:
            missing = getattr(kb, "impact_missing_count", 0)
            if missing:
                self.btn_health.setText(f"知识库健康检查…（{missing} 条待补）")

        note = QLabel(
            "四个模块共用这一份联网识别设置：填一次，文件、软件、注册表、进程都能用。\n"
            "这款工具只会动你自己勾选的东西，动手之前你自己看清楚每一行写的作用。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED_FG.name()};")

        form = QFormLayout()
        form.addRow("服务商", self.provider)
        form.addRow("接口地址", self.base_url)
        form.addRow("模型名", self.model)
        form.addRow("密钥", self.api_key)
        form.addRow("最多等", self.timeout)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.auto_size)
        layout.addWidget(self.confirm_recycle)
        layout.addWidget(self.show_hidden)
        layout.addSpacing(8)
        layout.addWidget(self.ai_enabled)
        layout.addLayout(form)
        temp_row = QHBoxLayout()
        temp_row.addWidget(QLabel("temperature"))
        temp_row.addWidget(self.temperature)
        temp_row.addStretch(1)
        layout.addLayout(temp_row)
        layout.addWidget(self.temperature_hint)
        layout.addWidget(self.mask_username)
        layout.addWidget(self.btn_clear_ai, 0, Qt.AlignLeft)
        layout.addWidget(self.btn_health, 0, Qt.AlignLeft)
        layout.addSpacing(8)
        layout.addWidget(self.lbl_snapshots)
        snap_row = QHBoxLayout()
        snap_row.addWidget(self.btn_clear_snapshots)
        snap_row.addWidget(self.btn_undo)
        snap_row.addStretch(1)
        layout.addLayout(snap_row)
        layout.addWidget(self.lbl_exports)
        layout.addWidget(self.btn_clear_exports, 0, Qt.AlignLeft)
        layout.addSpacing(8)
        layout.addWidget(self.lbl_feedback)
        layout.addWidget(self.btn_feedback, 0, Qt.AlignLeft)
        layout.addSpacing(8)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def _apply_preset(self) -> None:
        key = self.provider.currentData()
        if key in PROVIDERS and key != "custom":
            _label, url, model = PROVIDERS[key]
            if url:
                self.base_url.setText(url)
            if model:
                self.model.setText(model)

    def _clear_snapshots(self) -> None:
        from core import snapshot as snapshots

        answer = QMessageBox.question(
            self,
            "清空操作快照",
            "把全部操作快照删掉吗？\n\n"
            "只删这些记录文件。回收站里的东西不动，backups 里的 .reg 备份也不动。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        removed, freed = snapshots.clear()
        self.lbl_snapshots.setText(
            f"操作快照：占 0 B / 共 0 条（刚删了 {removed} 条，腾出 {human_size(freed)}）"
        )

    def _preview_appearance(self) -> None:
        """选完立刻能看到效果，不用保存也不用重启。"""
        from PySide6.QtWidgets import QApplication

        from ui.theme import apply

        try:
            apply(QApplication.instance(), str(self.theme_box.currentData()),
                  str(self.scale_box.currentData()))
        except Exception:  # noqa: BLE001
            pass

    def reject(self) -> None:
        """点「取消」就把预览撤回去，别让人带着没保存的样式离开。"""
        super().reject()
        from PySide6.QtWidgets import QApplication

        from ui.theme import apply

        try:
            apply(QApplication.instance(), getattr(self._settings, "theme", "system"),
                  getattr(self._settings, "font_scale", "medium"))
        except Exception:  # noqa: BLE001
            pass


    def _open_feedback(self) -> None:
        FeedbackListDialog(self).exec()
        from core import feedback as feedback_store

        self.lbl_feedback.setText(
            f"说明反馈：已记 {feedback_store.count()} 条（只存本机，不上传、不改知识库）"
        )

    def _clear_exports(self) -> None:
        from ui.widgets import export_csv

        answer = QMessageBox.question(
            self, "清空导出的 CSV",
            "把 logs/exports 里导出的表格全删掉吗？\n\n只删这些导出文件，不影响回收站、备份和快照。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        removed, freed = export_csv.clear()
        self.lbl_exports.setText(f"导出的 CSV：占 0 B / 共 0 份（刚删了 {removed} 份，腾出 {human_size(freed)}）")

    def _open_undo(self) -> None:
        from ui.undo_center import UndoCenterDialog

        UndoCenterDialog(self.parent(), None).exec()
        from core import snapshot as snapshots

        snap_bytes, snap_count = snapshots.usage()
        self.lbl_snapshots.setText(
            f"操作快照：占 {human_size(snap_bytes)} / 共 {snap_count} 条（上限 200 条或 50 MB，超了先清最旧的）"
        )

    def _check_knowledge(self) -> None:
        if self._kb is None:
            return
        KnowledgeHealthDialog(self._kb, self).exec()

    def _clear_ai_cache(self) -> None:
        if self._cache is None:
            QMessageBox.information(self, "没有可清的东西", "现在缓存是空的。")
            return
        answer = QMessageBox.question(
            self,
            "清空 AI 缓存",
            "把已经存下来的 AI 解释全部删掉吗？\n\n"
            "删掉之后，那些位置会重新显示「未收录」，要再问一次才会回来。\n"
            "知识库自带的规则不受影响。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self._cache.drop_ai()
        except Exception:  # noqa: BLE001
            QMessageBox.warning(self, "没清成", "缓存文件可能被占用着，等会儿再试。")
            return
        QMessageBox.information(self, "已经清空", "AI 识别结果都清掉了。")

    def apply_to(self, settings) -> None:
        settings.theme = str(self.theme_box.currentData() or "system")
        settings.font_scale = str(self.scale_box.currentData() or "medium")
        settings.auto_size = self.auto_size.isChecked()
        settings.confirm_recycle = self.confirm_recycle.isChecked()
        settings.show_hidden = self.show_hidden.isChecked()
        settings.ai.enabled = self.ai_enabled.isChecked()
        settings.ai.provider = str(self.provider.currentData() or "custom")
        settings.ai.mask_username = self.mask_username.isChecked()
        settings.ai.base_url = self.base_url.text().strip()
        settings.ai.model = self.model.text().strip()
        settings.ai.api_key = self.api_key.text().strip()
        settings.ai.timeout_seconds = float(self.timeout.value())
        settings.ai.temperature = float(self.temperature.value())

class AISetupDialog(QDialog):
    """第一次点联网识别时的引导：填好就存进 config.json（明文）。"""

    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("开启联网识别")
        self.setMinimumWidth(560)
        self._settings = settings

        title = QLabel("先填一下联网识别的账号")
        title_font = QFont(title.font())
        title_font.setPointSize(title_font.pointSize() + 2)
        title_font.setBold(True)
        title.setFont(title_font)

        intro = QLabel(
            "这项功能会把你选中的「名称 + 路径」发给服务商，换回一句说明和风险判断。\n"
            "不会发送任何文件内容。默认会把用户名换成 %USER%，不留真实用户名。\n"
            "默认是关着的，只有你点按钮时才会联网。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{MUTED_FG.name()};")

        self.provider = QComboBox()
        for key, (label, _url, _model) in PROVIDERS.items():
            self.provider.addItem(label, key)
        index = self.provider.findData(getattr(settings.ai, "provider", "custom"))
        self.provider.setCurrentIndex(index if index >= 0 else 0)
        self.provider.currentIndexChanged.connect(self._apply_preset)

        self.base_url = QLineEdit(settings.ai.base_url)
        self.base_url.setPlaceholderText("例如 https://api.deepseek.com")
        self.model = QLineEdit(settings.ai.model)
        self.model.setPlaceholderText("服务方给的模型名")
        self.api_key = QLineEdit(settings.ai.api_key)
        self.api_key.setPlaceholderText("在这一项里粘贴你的密钥")
        self.mask_username = QCheckBox("发送时把用户名换成 %USER%")
        self.mask_username.setChecked(getattr(settings.ai, "mask_username", True))

        form = QFormLayout()
        form.addRow("服务商", self.provider)
        form.addRow("接口地址", self.base_url)
        form.addRow("模型名", self.model)
        form.addRow("密钥", self.api_key)

        note = QLabel("密钥会明文保存在程序旁边的 config.json 里。别把这台电脑借给不放心的人。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED_FG.name()};")

        buttons = QDialogButtonBox()
        self._cancel = QPushButton("以后再说")
        self._ok = QPushButton("保存并开启")
        buttons.addButton(self._cancel, QDialogButtonBox.RejectRole)
        buttons.addButton(self._ok, QDialogButtonBox.AcceptRole)
        buttons.rejected.connect(self.reject)
        self._ok.clicked.connect(self._on_ok)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.mask_username)
        layout.addWidget(note)
        layout.addWidget(buttons)

        self._apply_preset()

    def _apply_preset(self) -> None:
        key = self.provider.currentData()
        if key in PROVIDERS and key != "custom":
            _label, url, model = PROVIDERS[key]
            if url:
                self.base_url.setText(url)
            if model:
                self.model.setText(model)

    def _on_ok(self) -> None:
        if not self.base_url.text().strip() or not self.model.text().strip() or not self.api_key.text().strip():
            QMessageBox.information(self, "还差一点", "接口地址、模型名、密钥这三样都要填上才能用。")
            return
        self.accept()

    def apply_to(self, settings) -> None:
        settings.ai.enabled = True
        settings.ai.provider = str(self.provider.currentData() or "custom")
        settings.ai.base_url = self.base_url.text().strip()
        settings.ai.model = self.model.text().strip()
        settings.ai.api_key = self.api_key.text().strip()
        settings.ai.mask_username = self.mask_username.isChecked()

class HelpDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("怎么用")
        self.setMinimumSize(620, 520)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setHtml(HELP_HTML)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("知道了")
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(browser)
        layout.addWidget(buttons)

HELP_HTML = """
<h2>这就是一个能看清每样东西是干什么用的资源管理器</h2>
<p>它不会自己扫你的硬盘，也不会替你决定删什么。你看到什么、勾什么，它就删什么。</p>
<h3>三步用完</h3>
<ol>
  <li>左边点一个盘，右边点进你想看的文件夹；</li>
  <li>每一行都写着 <b>作用</b> 和 <b>风险</b>，看清楚再决定；</li>
  <li>在最后一列打勾，然后点上面的「删除选中」。</li>
</ol>
<h3>关于风险标签</h3>
<ul>
  <li><span style="color:#1a7f37">● 放心删</span>：删了基本没影响，比如缓存和临时文件；</li>
  <li><span style="color:#9a6700">● 看情况</span>：能不能删取决于你自己用不用它；</li>
  <li><span style="color:#cf222e">● 别删</span>：删了大概率找不回来或者会坏东西。
      这些行<b>仍然是红的</b>，勾之前多看一眼；</li>
  <li><span style="color:#8b949e">● 未收录</span>：规则里没有这条路径，删之前请自己看一眼。</li>
</ul>
<h3>删错了怎么办</h3>
<p>默认是 <b>放进回收站</b>，去桌面上的回收站右键还原就行。
只有在确认清单里选了「彻底删除」，并且又勾了「我知道删了之后找不回来」，才是真的找不回来。</p>
<h3>它不做什么</h3>
<p>不清理注册表、不卸载软件、不自动联网、不自动替你勾选任何东西。联网解释只有你点按钮时才会发生。</p>
"""
