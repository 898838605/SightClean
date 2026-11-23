"""主窗口：盘符栏 + 面包屑 + 文件列表 + 工具栏 + 状态栏。

这个程序不扫盘、不猜用户想删什么。它只做三件事：
把当前位置的东西列出来、写清楚每一行是干什么用的、把用户勾的东西删掉。
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Sequence

from PySide6.QtCore import QEvent, QSize, Qt, QThreadPool, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app_paths import to_long

# 「帮助」菜单里指向的两个地方
GITHUB_URL = "https://github.com/898838605/SightClean"
TUTORIAL_URL = "https://mp.weixin.qq.com/s/JKYaUpnryQ0Rn97V0So6aw"

# 注意：core.ai_client（带 urllib/ssl）和 ui.dialogs 都不在启动路径上，
# 一律在用到的地方再 import，别拖慢冷启动。
from core.config import CacheStore, Settings, SettingsStore
# core.delete 会连带加载 send2trash 和 ctypes，只在真要删东西时才值得付这个成本
from core.fs_browser import DirLister, DriveEnumerator, DriveInfo, FsEntry, human_size
from core.knowledge import KnowledgeBase
from core.risk import Risk, evaluate
from ui.nav import PAGE_FILES, PAGE_PROCESS, PAGE_REGISTRY, PAGE_SOFTWARE, NavBar
from ui.widgets.browser_widgets import BreadcrumbBar, CheckBoxDelegate, SelectionHeader
from ui.widgets.filter_bar import FilterBar
from ui.page_base import ModulePage
from ui.process_view import ProcessView
from ui.registry_view import RegistryView
from ui.uninstall_view import UninstallView
# 对话框同理：只在用户真的点开时才加载
from ui.table_model import (
    COL_CHECK,
    COL_KIND,
    COL_MTIME,
    COL_NAME,
    COL_PURPOSE,
    COL_SIZE,
    FsTableModel,
)

DRIVE_KIND_LABEL = {
    "fixed": "本地磁盘",
    "removable": "移动磁盘",
    "remote": "网络位置",
    "cdrom": "光驱",
    "ram": "内存盘",
    "unknown": "磁盘",
}

def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False

class MainWindow(QMainWindow):
    def __init__(
        self,
        paths,
        settings: Settings,
        settings_store: SettingsStore,
        kb: KnowledgeBase,
        cache: CacheStore,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.paths = paths
        self.settings = settings
        self.settings_store = settings_store
        self.kb = kb
        self.cache = cache

        self.model = FsTableModel(self)
        self.size_engine = None
        self.drive_enum = None
        self.dir_lister = None
        self.ai = None

        self._generation = 0
        self._current_dir = ""
        self._pending_dir = ""
        self._history: list[str] = []
        self._pending_sizes: set[str] = set()
        self._worker = None          # DeleteWorker 是懒加载的，注解别引用它
        self._busy = False
        self._background_workers: set = set()
        # 以管理员身份重启之后，之前那些「打不开」的文件夹现在可能能读了，
        # 所以第一次进目录时不看缓存，全部重算一遍。
        self.force_recalc = False
        self._current_module = PAGE_FILES
        self._drives: list[DriveInfo] = []
        # 已解锁的红色项（存 identity）。只在本会话有效，切模块/切目录就清空。
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)

        self.setWindowTitle("SightClean")
        self.resize(settings.window_width, settings.window_height)
        self._build_ui()
        self._build_menu()
        self.setAcceptDrops(False)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.toolbar = QToolBar("工具栏", self)
        self.toolbar.setIconSize(QSize(18, 18))
        self.toolbar.setMovable(False)
        self.toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(self.toolbar)

        self.act_back = QAction("← 返回", self)
        self.act_back.setToolTip("回到刚才那个文件夹")
        self.act_back.triggered.connect(self._go_back)
        self.act_up = QAction("↑ 上一级", self)
        self.act_up.setToolTip("回到上一层文件夹")
        self.act_up.triggered.connect(self._go_up)
        self.act_refresh = QAction("⟳ 刷新", self)
        self.act_refresh.setShortcut(QKeySequence.Refresh)
        self.act_refresh.triggered.connect(self._refresh_current)
        self.act_size = QAction("🧮 算一下多大", self)
        self.act_size.setToolTip("把当前这一页里文件夹的大小算出来（只算这一页，不动别的地方）")
        self.act_size.triggered.connect(self._recalc_sizes)
        self.act_stop = QAction("⏹ 停止计算", self)
        self.act_stop.setEnabled(False)
        self.act_stop.triggered.connect(self._stop_sizes)

        self.act_clean_process = QAction("🧹 一键清理进程", self)
        self.act_clean_process.setToolTip(
            "找出「开了很久、不在前台、CPU 也不动」的进程。\n"
            "系统服务、桌面、开始菜单一律排除；剩下的基本是忘了关的软件。"
        )
        self.act_clean_process.triggered.connect(self.clean_orphan_processes)

        self.act_junk = QAction("🧹 一键清理 C 盘垃圾", self)
        self.act_junk.setToolTip(
            "按规则清 C 盘的临时文件、系统日志、缩略图缓存、浏览器缓存、包管理器缓存。\n"
            "规则写在 垃圾清理规则.json 里，可以自己改。"
        )
        self.act_junk.triggered.connect(self.open_junk_cleaner)

        self.act_delete = QAction("🗑 删除选中", self)
        self.act_delete.setToolTip("先看清单，逐项确认，再决定是移入回收站还是彻底删除")
        self.act_delete.triggered.connect(self._execute_current)
        self.act_ai = QAction("✨ AI 识别此项", self)
        self.act_ai.setToolTip("只识别选中的这一项，别的都不动")
        self.act_ai.triggered.connect(self._explain_current_row)
        self.act_ai_page = QAction("✨ 识别本页未知项", self)
        self.act_ai_page.setToolTip("把这一页里知识库不认识的项目逐个问一遍（会先跟你确认）")
        self.act_ai_page.triggered.connect(self._explain_current_page)
        self.act_ai_stop = QAction("⏹ 停止识别", self)
        self.act_ai_stop.setToolTip("停在当前这一项之后，已经识别出来的结果留着")
        self.act_ai_stop.setEnabled(False)
        self.act_ai_stop.triggered.connect(self._stop_ai)
        self.act_export = QAction("⇩ 导出 CSV…", self)
        self.act_export.setToolTip("把当前列表里看得见的那些导成表格，只含名称、路径、作用、风险")
        self.act_export.triggered.connect(self.export_current)
        self.act_orphans = QAction("🔍 扫描孤儿目录…", self)
        self.act_orphans.setToolTip(
            "有些软件只删了卸载登记、文件和进程都留着，列表里看不见它。"
            "这个会去 Program Files 下找出这类目录"
        )
        self.act_orphans.triggered.connect(self.scan_orphans)
        self.act_undo = QAction("↶ 撤销中心…", self)
        self.act_undo.setToolTip("看看自己做过哪些操作，能还原的可以在这里还原")
        self.act_undo.triggered.connect(self.open_undo_center)
        self.act_global_clean = QAction("🧹 AI 扫描卸载残余文件夹", self)
        self.act_global_clean.setToolTip(
            "这个是全机器扫一遍文件夹：把不属于任何已装软件的目录列出来，交给 AI 判断。"
        )
        self.act_global_clean.triggered.connect(self.scan_global_leftovers)

        self.act_scan_scopes = QAction("📁 扫描位置…", self)
        self.act_scan_scopes.setToolTip(
            "选要扫哪些位置。默认只勾安全的那几个；\n"
            "「文档」「用户主目录」可能混着你自己的数据，「Installer」「drivers」删了会出问题。"
        )
        self.act_scan_scopes.triggered.connect(self.choose_scan_scopes)

        self.act_dead_registry = QAction("🧹 清理注册表", self)
        self.act_dead_registry.setToolTip(
            "找「指向的东西已经不存在」的注册表项：\n"
            "程序路径登记、卸载登记、共享 DLL 引用、开机启动项。\n"
            "只处理客观可验证的失效项，不做主观猜测；删除前会先备份成 .reg。"
        )
        self.act_dead_registry.triggered.connect(self.scan_dead_registry)

        self.act_ai_registry = QAction("🧹 让 AI 找注册表残留", self)
        self.act_ai_registry.setToolTip(
            "把「不属于任何已装软件」的注册表键列出来，交给 AI 判断哪些是已卸载软件的遗留。\n"
            "判断结果默认一个都不勾。"
        )
        self.act_ai_registry.triggered.connect(self.scan_registry_leftovers)

        self.act_settings = QAction("⚙ 设置", self)
        self.act_settings.triggered.connect(self._open_settings)
        self.act_help = QAction("? 怎么用", self)
        self.act_help.triggered.connect(self._open_help)

        self.act_github = QAction("🔗 项目地址", self)
        self.act_github.setToolTip(GITHUB_URL)
        self.act_github.triggered.connect(self._open_github)

        self.act_tutorial = QAction("📖 教程", self)
        self.act_tutorial.setToolTip(TUTORIAL_URL)
        self.act_tutorial.triggered.connect(self._open_tutorial)

        # 只有文件模块用得到的按钮；切到别的模块时整组隐藏
        self._file_only_actions = (self.act_back, self.act_up, self.act_size, self.act_stop,
                                   self.act_junk)
        for action in (self.act_back, self.act_up, self.act_refresh):
            self.toolbar.addAction(action)
        self._sep_after_nav = self.toolbar.addSeparator()
        for action in (self.act_size, self.act_stop):
            self.toolbar.addAction(action)
        self._sep_after_size = self.toolbar.addSeparator()
        self.toolbar.addAction(self.act_delete)
        self.toolbar.addAction(self.act_junk)
        self.toolbar.addSeparator()
        for action in (self.act_ai_page, self.act_ai_stop):
            self.toolbar.addAction(action)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.act_scan_scopes)
        self.toolbar.addAction(self.act_global_clean)
        self.toolbar.addAction(self.act_dead_registry)
        self.toolbar.addAction(self.act_clean_process)
        self.toolbar.addAction(self.act_ai_registry)
        # 「设置」挪到菜单栏了（工具和帮助之间），工具栏只留干活用的按钮。

        self.breadcrumb = BreadcrumbBar(self)
        self.breadcrumb.pathActivated.connect(lambda p: self.navigate(p))
        self.breadcrumb.set_roots_provider(self._drive_menu)

        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setItemDelegate(CheckBoxDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSortingEnabled(False)     # 三态排序自己管，交给 Qt 只有升/降两态
        self.table.sortByColumn(COL_NAME, Qt.AscendingOrder)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideRight)
        self.header = SelectionHeader(self.table)
        self.table.setHorizontalHeader(self.header)
        self.header.toggleAllRequested.connect(self._toggle_all_checked)
        self.header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.header.setStretchLastSection(False)
        # 名称列用 Interactive：Stretch 的列宽是自动算的，用户拖不动
        self.header.setSectionResizeMode(COL_NAME, QHeaderView.Interactive)
        self.table.setColumnWidth(COL_NAME, 260)
        self.header.setSectionResizeMode(COL_MTIME, QHeaderView.Interactive)
        self.header.setSectionResizeMode(COL_KIND, QHeaderView.Interactive)
        self.header.setSectionResizeMode(COL_SIZE, QHeaderView.Interactive)
        self.header.setSectionResizeMode(COL_PURPOSE, QHeaderView.Stretch)
        self.header.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_NAME, 300)
        self.table.setColumnWidth(COL_MTIME, 140)
        self.table.setColumnWidth(COL_KIND, 100)
        self.table.setColumnWidth(COL_SIZE, 130)
        self.table.setColumnWidth(COL_PURPOSE, 340)
        self.table.setColumnWidth(COL_CHECK, 86)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)

        # 左侧盘符栏去掉了：整页都给表格，切盘走面包屑第一段的下拉
        self.files_view = QWidget(self)
        right_layout = QVBoxLayout(self.files_view)
        right_layout.setContentsMargins(8, 6, 8, 6)
        right_layout.setSpacing(6)
        self.filter_bar = FilterBar("在名称 / 扩展名里找…")
        self.filter_bar.changed.connect(self._on_file_filter_changed)
        right_layout.addWidget(self.breadcrumb)
        right_layout.addWidget(self.filter_bar)
        right_layout.addWidget(self.table, 1)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.files_view)
        self.software_page = UninstallView(self.kb, self.settings, self.cache, self)
        self.software_page.openPathRequested.connect(self._open_in_files_module)
        self.registry_page = RegistryView(self.kb, self.settings, self.cache, self)
        self.process_page = ProcessView(self.kb, self.settings, self.cache, self)
        self._module_pages = (self.software_page, self.registry_page, self.process_page)
        for page in self._module_pages:
            page.checkedChanged.connect(self._on_module_checked_changed)
            if hasattr(page, "statusMessage"):
                page.statusMessage.connect(self._toast)
            if hasattr(page, "aiSetupRequested"):
                page.aiSetupRequested.connect(self._prompt_ai_setup)
            if hasattr(page, "adminRestartRequested"):
                page.adminRestartRequested.connect(self._restart_as_admin)
            self.stack.addWidget(page)

        self.nav = NavBar(self)
        self.nav.moduleChanged.connect(self._switch_module)

        body = QWidget(self)
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self.nav)
        body_layout.addWidget(self.stack, 1)
        self.setCentralWidget(body)

        self.lbl_status = QLabel("正在准备…")
        self.lbl_warning = QLabel("")
        self.lbl_warning.setStyleSheet("color:#9a6700;padding:0 8px;")
        self.lbl_warning.setVisible(False)
        self.lbl_selection = QLabel("还没有勾选")
        self.lbl_selection.setStyleSheet("color:#57606a;")
        bar = self.statusBar()
        bar.addWidget(self.lbl_status, 1)
        bar.addPermanentWidget(self.lbl_warning)
        bar.addPermanentWidget(self.lbl_selection)

        self.model.checkedChanged.connect(self._on_checked_changed)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._on_table_selection())
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self.table.installEventFilter(self)

        self._on_checked_changed()
        self._apply_module_ui(PAGE_FILES)
        self.apply_appearance()

    def apply_appearance(self) -> None:
        """按 config.json 里的主题/字号铺样式。启动时和改完设置时各调一次。"""
        from PySide6.QtWidgets import QApplication

        from ui.theme import apply

        try:
            apply(QApplication.instance(), getattr(self.settings, "theme", "system"),
                  getattr(self.settings, "font_scale", "medium"))
        except Exception:  # noqa: BLE001 - 样式出问题不能把人挡在外面
            pass

    def _installed_for_ai(self) -> list:
        """判断残留需要一份「已装软件清单」。

        没加载过就自己读一遍 —— 让用户先跑去「软件卸载」点刷新再回来，
        纯属给人添堵。读一次大概几百毫秒，带个等待光标就行。
        """
        import time as _time

        view = getattr(self, "software_page", None)
        loaded = view.model.all_items() if view is not None else []
        if loaded:
            return loaded

        # 读一遍注册表要两秒多，短时间内再问就直接给上次的结果
        cached_at, cached = getattr(self, "_installed_cache", (0.0, []))
        if cached and _time.time() - cached_at < 60:
            return list(cached)

        from core.uninstall import enumerate_installed

        from PySide6.QtCore import Qt as _Qt

        QApplication.setOverrideCursor(_Qt.WaitCursor)
        try:
            items, _warnings = enumerate_installed(include_orphans=False)
        except Exception:  # noqa: BLE001
            items = []
        finally:
            QApplication.restoreOverrideCursor()
        self._installed_cache = (_time.time(), list(items))
        return list(items)

    def clean_orphan_processes(self) -> None:
        """找出开了很久、不在前台、CPU 也不动的进程，让用户决定停不停。"""
        from core.processes import find_idle_processes

        self._run_background(
            "正在查找空闲进程…",
            lambda: find_idle_processes(),
            self._show_idle_processes,
        )

    def _show_idle_processes(self, orphans) -> None:
        from core.processes import DEFAULT_MAX_CPU, DEFAULT_MIN_AGE_MINUTES
        from ui.process_clean_dialog import ProcessCleanDialog

        if not orphans:
            QMessageBox.information(
                self, "没找到空闲进程",
                "没有「开了很久、不在前台、CPU 也不动」的进程，没什么可清的。\n\n"
                f"判据是：开了超过 {int(DEFAULT_MIN_AGE_MINUTES)} 分钟、"
                f"CPU 低于 {DEFAULT_MAX_CPU:g}%、且不是当前前台窗口。",
            )
            return

        dialog = ProcessCleanDialog(
            orphans, self,
            max_cpu=DEFAULT_MAX_CPU,
            min_age_minutes=DEFAULT_MIN_AGE_MINUTES,
            log_dir=self.paths.logs,
        )
        dialog.exec()
        if dialog.killed:
            self._toast(f"结束 {dialog.killed} 个进程")
            self.process_page.refresh_async()

    def open_junk_cleaner(self) -> None:
        """一键清理 C 盘垃圾。"""
        from ui.junk_dialog import JunkDialog

        dialog = JunkDialog(self)
        dialog.exec()
        outcome = getattr(dialog, "outcome", None)
        if outcome is not None and outcome.removed:
            self._toast(
                f"清掉 {outcome.removed} 项，释放 {human_size(outcome.freed)}"
            )

    def scan_dead_registry(self) -> None:
        """找「指向的东西已经不存在」的注册表项，交给用户勾选后清理。"""
        from core.registry_scan import scan_all

        self._run_background(
            "正在扫描失效注册表项…",
            lambda: scan_all(),
            self._show_dead_registry,
        )

    def _show_dead_registry(self, entries) -> None:
        from ui.leftover_dialog import LeftoverDialog

        if not entries:
            QMessageBox.information(
                self, "没找到失效项",
                "注册表里没有「指向的目标已经不存在」的项。\n"
                "扫的是程序路径、卸载登记、共享 DLL、开机启动项这四类。",
            )
            return

        from collections import Counter

        counts = Counter(entry.category for entry in entries)
        labels = {
            "app_paths": "程序路径登记",
            "uninstall": "卸载登记",
            "shared_dlls": "共享 DLL 引用",
            "run_entries": "开机启动项",
        }
        summary = "、".join(f"{labels.get(k, k)} {v} 个" for k, v in counts.most_common())

        answer = QMessageBox.question(
            self,
            "要清理吗",
            f"找到 {len(entries)} 个失效项：{summary}。\n\n"
            "这些项指向的文件都已经不存在了，删掉不会影响任何程序。\n"
            "动手之前会先把它们导出成一份 .reg 备份，随时能还原。\n"
            "结果默认一个都不勾，你看过理由再决定。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        from core.leftover import KIND_REGISTRY, Leftover

        leftovers = [
            Leftover(
                KIND_REGISTRY,
                labels.get(entry.category, entry.category),
                entry.identity,
                name=entry.label,
                reason=entry.reason,
            )
            for entry in entries
        ]
        dialog = LeftoverDialog(
            leftovers, self,
            title_text="注册表里的失效项",
            kinds=("registry",),
        )
        dialog.exec()
        if dialog.report is not None and dialog.report.ok_count:
            self._toast(dialog.report.summary)

    def scan_registry_leftovers(self) -> None:
        """注册表版：找「不属于任何已装软件」的键，交给 AI 判断。"""
        from core.ai_leftover import collect_suspects

        view = getattr(self, "software_page", None)
        ai = self.ai or getattr(view, "ai", None)
        if ai is None or not ai.is_configured():
            QMessageBox.information(
                self, "AI 还没配好",
                "这个功能要靠 AI 判断，先去「设置」里填上接口地址和密钥。",
            )
            return

        def collect():
            installed = view.model.all_items() if view is not None else []
            if not installed:
                from core.uninstall import enumerate_installed

                installed, _warnings = enumerate_installed(include_orphans=False)
            return installed, collect_suspects(installed, kinds=("registry",))

        self._run_background(
            "正在收集注册表候选…",
            collect,
            lambda data: self._confirm_registry_leftovers(ai, data[0], data[1]),
        )

    def _confirm_registry_leftovers(self, ai, installed, suspects) -> None:
        from core.ai_leftover import make_judge_worker
        from ui.leftover_dialog import LeftoverDialog

        if not installed:
            QMessageBox.information(
                self, "读不到已装软件",
                "判断一个注册表键是不是「已卸载软件的残留」，需要跟已装软件对照，"
                "但这次一个软件都没读到。\n\n"
                "可能是注册表读不了，试试用管理员身份重开一次。",
            )
            return

        if not suspects:
            QMessageBox.information(
                self, "没找到候选",
                "这台电脑上没发现「不属于任何已装软件」的注册表键。",
            )
            return

        batch_size = max(1, int(getattr(ai.settings.ai, "max_batch", 25) or 25))
        answer = QMessageBox.question(
            self,
            "要扫了吗",
            f"找到 {len(suspects)} 个注册表键，都不属于任何已装软件。\n\n"
            f"接下来会分 {(len(suspects) + batch_size - 1) // batch_size} 批交给 AI 判断，"
            "需要一点时间，也会消耗 token。\n"
            "判断结果只是建议，默认一个都不勾，删什么还是你说了算。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        from PySide6.QtCore import QThreadPool
        from PySide6.QtWidgets import QProgressDialog

        import threading

        cancel = threading.Event()
        progress = QProgressDialog("正在让 AI 判断…", "取消", 0, len(suspects), self)
        progress.setWindowTitle("AI 找注册表残留")
        progress.canceled.connect(cancel.set)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)

        worker = make_judge_worker(suspects, installed, ai.settings, cancel=cancel)
        worker.signals.progress.connect(
            lambda done, total: (progress.setMaximum(max(total, 1)), progress.setValue(done))
        )

        def on_done(judged, error):
            progress.close()
            if error:
                QMessageBox.warning(self, "AI 判断没成功", error)
                return
            picked = [item for item in judged if item.verdict == "leftover"]
            if not picked:
                QMessageBox.information(
                    self, "AI 没挑出东西",
                    f"看了 {len(judged)} 个键，没找到能确定是残留的。",
                )
                return
            from core.leftover import KIND_REGISTRY, Leftover

            leftovers = []
            for suspect in picked:
                leftovers.append(Leftover(
                    KIND_REGISTRY,
                    f"AI 判断（{suspect.confidence or '不确定'}）",
                    suspect.path,
                    name=suspect.name,
                    reason=f"AI：{suspect.reason or '像是已卸载软件的遗留'}",
                ))
            dialog = LeftoverDialog(
                leftovers, self,
                title_text="AI 找到的可疑注册表项",
                ai=ai, installed=installed, kinds=("registry",),
            )
            dialog.exec()
            if dialog.report is not None and dialog.report.ok_count:
                self._toast(dialog.report.summary)

        worker.signals.finished.connect(on_done)
        self._registry_worker = worker
        QThreadPool.globalInstance().start(worker)
        progress.show()

    def choose_scan_scopes(self) -> None:
        """让用户勾要扫哪些位置，存进设置。"""
        from core.scan_scopes import default_keys
        from ui.scan_scope_dialog import ScanScopeDialog

        current = list(self.settings.scan_scopes) or default_keys()
        dialog = ScanScopeDialog(current, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.chosen()
        if not chosen:
            QMessageBox.information(
                self, "一个都没选",
                "至少要勾一个位置，不然没东西可扫。",
            )
            return
        self.settings.scan_scopes = chosen
        self.settings_store.save(self.settings)
        self._toast(f"扫描位置已保存：{len(chosen)} 个")

    def scan_global_leftovers(self) -> None:
        """全机器扫一遍「不属于任何已装软件」的目录和注册表键，交给 AI 判断。

        跟卸载后那个清单不同：那边只看「这个软件留下的」，这边是全局摸底，
        用来找那些名字对不上、压根不知道属于谁的残留。
        """
        from core.ai_leftover import collect_suspects

        view = getattr(self, "software_page", None)
        # AI 客户端是 bootstrap 时注入的，初始化阶段可能还是 None
        ai = self.ai or getattr(view, "ai", None)
        if ai is None or not ai.is_configured():
            QMessageBox.information(
                self, "AI 还没配好",
                "这个功能要靠 AI 判断，先去「设置」里填上接口地址和密钥。",
            )
            return

        from core.scan_scopes import default_keys
        scope_keys = list(self.settings.scan_scopes) or default_keys()

        def collect():
            installed = view.model.all_items() if view is not None else []
            if not installed:
                from core.uninstall import enumerate_installed

                installed, _warnings = enumerate_installed(include_orphans=False)
            suspects = collect_suspects(
                installed, kinds=("folder",), scopes=scope_keys,
            )
            return installed, suspects

        self._run_background(
            "正在收集全局残留候选…",
            collect,
            lambda data: self._confirm_global_leftovers(
                ai, data[0], data[1], scope_keys,
            ),
        )

    def _confirm_global_leftovers(self, ai, installed, suspects, scope_keys) -> None:
        from core.ai_leftover import make_judge_worker
        from ui.leftover_dialog import LeftoverDialog

        if not installed:
            QMessageBox.information(self, "读不到已装软件", "软件列表是空的，无法可靠判断残留。")
            return
        if not suspects:
            QMessageBox.information(
                self, "没找到候选",
                "这台电脑上没发现「不属于任何已装软件」的目录或注册表键。",
            )
            return

        batch_size = max(1, int(getattr(ai.settings.ai, "max_batch", 25) or 25))
        answer = QMessageBox.question(
            self,
            "要扫了吗",
            f"找到 {len(suspects)} 个候选（不属于任何已装软件的**文件夹**）。\n\n"
            f"接下来会分 {(len(suspects) + batch_size - 1) // batch_size} 批交给 AI 判断，"
            "需要一点时间，也会消耗 token。\n"
            "判断结果只是建议，默认一个都不勾，最后删什么还是你说了算。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        from PySide6.QtCore import QThreadPool
        from PySide6.QtWidgets import QProgressDialog

        import threading

        cancel = threading.Event()
        progress = QProgressDialog("正在让 AI 判断…", "取消", 0, len(suspects), self)
        progress.setWindowTitle("扫描全局残留")
        progress.canceled.connect(cancel.set)     # 点取消就真的不再发请求
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)

        holder = {}

        worker = make_judge_worker(suspects, installed, ai.settings, cancel=cancel)
        worker.signals.progress.connect(
            lambda done, total: (progress.setMaximum(max(total, 1)), progress.setValue(done))
        )

        def on_done(judged, error):
            progress.close()
            worker_holder = holder.get("worker")
            if worker_holder is not None:
                worker_holder.signals.progress.disconnect()
            if error:
                QMessageBox.warning(self, "AI 判断没成功", error)
                return
            picked = [item for item in judged if item.verdict == "leftover"]
            if not picked:
                QMessageBox.information(
                    self, "AI 没挑出东西",
                    f"看了 {len(judged)} 个候选，没找到能确定是残留的。",
                )
                return
            from core.leftover import KIND_FOLDER, KIND_REGISTRY, Leftover

            leftovers = []
            for suspect in picked:
                leftovers.append(Leftover(
                    KIND_REGISTRY if suspect.kind == "registry" else KIND_FOLDER,
                    f"AI 判断（{suspect.confidence or '不确定'}）",
                    suspect.path,
                    name=suspect.name,
                    reason=f"AI：{suspect.reason or '像是已卸载软件的残留'}",
                ))
            dialog = LeftoverDialog(
                leftovers, self,
                on_open_path=self._open_in_files_module,
                title_text="全机器扫描：可疑的残留文件夹",
                ai=ai, installed=installed, kinds=("folder",), scopes=scope_keys,
            )
            dialog.exec()
            if dialog.report is not None and dialog.report.ok_count:
                self._toast(dialog.report.summary)

        worker.signals.finished.connect(on_done)
        holder["worker"] = worker
        self._global_worker = worker          # 挂住引用，别让线程池回收掉
        QThreadPool.globalInstance().start(worker)
        progress.show()

    def scan_orphans(self) -> None:
        """去 Program Files 下找那些「卸载列表里没有、但目录还在」的软件。"""
        from core.leftover import scan_orphans as find_orphans

        self._run_background(
            "正在扫描孤儿目录…",
            lambda: find_orphans(),
            self._show_orphans,
        )

    def _show_orphans(self, found) -> None:
        from ui.leftover_dialog import LeftoverDialog

        if not found:
            QMessageBox.information(
                self, "没找到孤儿目录",
                "Program Files 下的每个目录都能在卸载列表里找到对应的软件。",
            )
            return
        dialog = LeftoverDialog(
            found, self,
            on_open_path=self._open_in_files_module,
            title_text="找不到卸载登记的软件目录",
        )
        dialog.exec()
        if dialog.report is not None and dialog.report.ok_count:
            self._toast(dialog.report.summary)

    def open_undo_center(self) -> None:
        from ui.undo_center import UndoCenterDialog

        UndoCenterDialog(self, self.paths.logs).exec()

    def _build_menu(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("文件")
        file_menu.addAction(self.act_refresh)
        file_menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = menu.addMenu("查看")
        select_all = QAction("全部勾选", self)
        select_all.setShortcut(QKeySequence.SelectAll)
        select_all.triggered.connect(lambda: self.model.set_all_checked(True))
        clear_all = QAction("全部取消勾选", self)
        clear_all.triggered.connect(lambda: self.model.set_all_checked(False))
        view_menu.addAction(select_all)
        view_menu.addAction(clear_all)
        view_menu.addSeparator()
        self.act_hidden = QAction("显示隐藏的文件", self)
        self.act_hidden.setCheckable(True)
        self.act_hidden.setChecked(self.settings.show_hidden)
        self.act_hidden.toggled.connect(self._toggle_hidden)
        view_menu.addAction(self.act_hidden)

        tool_menu = menu.addMenu("工具")
        tool_menu.addAction(self.act_orphans)
        tool_menu.addSeparator()
        tool_menu.addAction(self.act_export)
        tool_menu.addAction(self.act_undo)
        # 设置挪到顶级菜单（工具和帮助之间），点一下就打开，不再套一层
        menu.addAction(self.act_settings)
        help_menu = menu.addMenu("帮助")
        help_menu.addAction(self.act_help)
        help_menu.addSeparator()
        help_menu.addAction(self.act_github)
        help_menu.addAction(self.act_tutorial)

    # --------------------------------------------------------------- 启动
    def bootstrap(self) -> None:
        """窗口已经画出来了再干活，保证一开就能看见界面。"""
        self.size_engine = self._make_size_engine()
        self.drive_enum = DriveEnumerator()
        self.drive_enum.driveUpdated.connect(self._on_drive_updated)
        self.drive_enum.finished.connect(self._on_drives_finished)
        self.dir_lister = DirLister()
        self.dir_lister.ready.connect(self._on_dir_ready)
        self.dir_lister.failed.connect(self._on_dir_failed)
        from core.ai_client import AIClient

        self.ai = AIClient(self.settings, self.cache, self)
        self.ai.resultReady.connect(self._on_ai_result)
        self.ai.failed.connect(self._on_ai_failed)
        self.ai.progress.connect(self._on_ai_progress)
        self.ai.batchFinished.connect(self._on_ai_batch_finished)
        self.ai.busyChanged.connect(self._on_ai_busy)
        for page in (self.software_page, self.registry_page, self.process_page):
            attach = getattr(page, "attach_ai", None)
            if callable(attach):
                attach(self.ai)

        self.drive_enum.start()
        self._update_ai_action()
        self._purge_logs()

        target = self.settings.last_dir or os.environ.get("USERPROFILE") or os.environ.get("SystemDrive", "C:") + "\\"
        if not os.path.isdir(to_long(target)):
            target = os.environ.get("SystemDrive", "C:") + "\\"
        self.navigate(target)

        # 上次停在哪个模块就回到哪个模块（0 是文件页，本来就在，不用切）
        saved_module = int(getattr(self.settings, "last_module", 0) or 0)
        if 0 < saved_module < self.stack.count():
            self._switch_module(saved_module)

    def _make_size_engine(self):
        from core.size_calc import SizeEngine

        engine = SizeEngine(self, self.settings.auto_size_concurrency)
        engine.sizeReady.connect(self._on_size_ready)
        engine.batchDone.connect(self._on_size_batch_done)
        engine.start()
        return engine

    def _purge_logs(self) -> None:
        try:
            from core.delete import purge_old_logs

            purge_old_logs(self.paths.logs, self.settings.log_keep_count)
        except Exception:  # noqa: BLE001 - 清理旧日志失败绝不影响启动
            pass

    def _run_background(self, label: str, func, on_done) -> None:
        """Run a slow read-only scan without blocking the event loop."""
        from core.tasks import make_call_worker

        worker = make_call_worker(func)
        self._background_workers.add(worker)
        self.lbl_status.setText(label)

        def finished(value) -> None:
            self._background_workers.discard(worker)
            if value is None:
                self._toast(f"{label.rstrip('…')}没有完成，请稍后重试")
                return
            on_done(value)

        worker.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(worker)

    # --------------------------------------------------------------- 盘符
    def _upsert_drive(self, drive: DriveInfo) -> None:
        """盘符列表只存在内存里，给面包屑的下拉菜单用。"""
        for index, existing in enumerate(self._drives):
            if existing.root == drive.root:
                self._drives[index] = drive
                return
        self._drives.append(drive)
        self._drives.sort(key=lambda item: item.root.lower())

    def _on_drive_updated(self, root: str, drive: DriveInfo) -> None:
        self._upsert_drive(drive)
        if not self._current_dir and not self._pending_dir and self._current_module == PAGE_FILES:
            self.show_drives()

    def _on_drives_finished(self, drives) -> None:
        for drive in drives:
            self._upsert_drive(drive)
        if not self._current_dir and not self._pending_dir and self._current_module == PAGE_FILES:
            self.show_drives()

    def _drive_menu(self) -> list:
        """面包屑第一段点开时给的盘符清单。"""
        return list(self._drives)

    # --------------------------------------------------------------- 导航
    def navigate(self, path: str, push_history: bool = True) -> None:
        # 换地方就把过滤词清掉，免得用户以为新目录是空的
        if getattr(self, "filter_bar", None) is not None:
            self.filter_bar.set_pending("")
            self.model.set_filter("")
        if not path:
            self.show_drives()      # 空路径 = 「这台电脑」那一层
            return
        if push_history and self._current_dir and os.path.normcase(path) != os.path.normcase(self._current_dir):
            self._history.append(self._current_dir)
        self._pending_dir = path
        self._generation += 1
        generation = self._generation
        if self.size_engine is not None:
            self.size_engine.cancel()
        self._pending_sizes.clear()
        self.model.clear_checks()
        self.model.set_entries([])
        self.lbl_status.setText(f"正在打开 {path} …")
        self.table.setEnabled(False)
        if self.dir_lister is not None:
            self.dir_lister.start(path, generation, self.settings.show_hidden)

    def _on_dir_ready(self, generation: int, entries: list[FsEntry]) -> None:
        if generation != self._generation:
            return  # 旧任务的结果，丢掉
        path = self._pending_dir
        self._current_dir = path
        for entry in entries:
            verdict = evaluate(entry.human_path, entry.is_dir, self.kb, entry.locked)
            entry.purpose = verdict.purpose or ""
            entry.purpose_source = "kb" if verdict.rule_id else "fallback"
            entry.risk = verdict.level.value
            entry.risk_reason = verdict.reason
            entry.advice = verdict.advice
            # 三道坎第三道直接念这一句；规则没写就留空，界面会如实说「未标注」
            entry.impact = verdict.impact or ""
            if entry.locked:
                entry.purpose = "无权限访问"
                entry.purpose_source = "locked"
            elif not verdict.known:
                # 知识库没有这条路径的信息时，看看以前有没有问过 AI：
                # 有就直接显示并打上 AI 标记，不必再联网。
                # 注意：知识库命中时轮不到这里，知识库永远优先。
                cached = self._cached_ai_result(entry.human_path, entry.name)
                if cached:
                    from core.ai_client import AI_NOTE

                    entry.purpose = str(cached.get("purpose") or "")
                    entry.impact = str(cached.get("impact") or "")
                    entry.purpose_source = "ai"
                    entry.ai_note = AI_NOTE
                    incoming = Risk.parse(str(cached.get("risk") or "caution"))
                    if incoming.rank > Risk.parse(entry.risk).rank:
                        entry.risk = incoming.value
            if entry.is_dir and not entry.locked:
                entry.size_state = "queued" if self.settings.auto_size else "none"
                if self.cache is not None and not self.force_recalc:
                    cached = self.cache.get_size(entry.human_path, self.settings.size_cache_ttl_hours)
                    if cached is not None:
                        entry.size, entry.size_state = cached[0], "ok"
        self.model.set_entries(entries)
        self.table.setEnabled(True)
        self.breadcrumb.set_path(path)
        if self._current_module == PAGE_FILES:
            # 目录是异步读回来的，回来时用户可能已经切到别的模块了，别覆盖人家的状态栏
            self.lbl_status.setText(f"{len(entries)} 项 · {path}")
            if not entries:
                self.lbl_status.setText(f"这个文件夹是空的 · {path}")
        self._schedule_sizes(entries, generation)
        self._on_checked_changed()
        self.force_recalc = False     # 只对重启后的第一次加载生效

    def _cached_ai_result(self, human_path: str, name: str) -> dict | None:
        if self.cache is None:
            return None
        try:
            data = self.cache.get_ai_json(CacheStore.ai_key_for(human_path, name))
        except Exception:  # noqa: BLE001 - 缓存读不出来不能影响列目录
            return None
        if isinstance(data, dict) and data.get("purpose"):
            return data
        return None

    def _on_dir_failed(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        self.table.setEnabled(True)
        self.lbl_status.setText(message)
        self._toast(message)

    def _go_back(self) -> None:
        if self._history:
            target = self._history.pop()
            self.navigate(target, push_history=False)

    def _go_up(self) -> None:
        if not self._current_dir:
            return
        stripped = self._current_dir.rstrip("\\/")
        # 盘根（C: / C:\）没有真正的上一级：Windows 上 dirname("C:") 还是 "C:"，
        # 照那个往上走等于原地打转，用户会觉得按钮坏了。这一层直接给「这台电脑」。
        if len(stripped) <= 2 and stripped[1:2] == ":":
            self.show_drives()
            return
        # UNC 路径（\\server\share）的上一层不好算，段数不够也给磁盘列表
        if stripped.startswith("\\\\") and stripped.count("\\") <= 2:
            self.show_drives()
            return
        parent = os.path.dirname(stripped)
        if not parent or os.path.normcase(parent) == os.path.normcase(stripped):
            self.show_drives()
            return
        self.navigate(parent)

    # -- 导出 --------------------------------------------------------------
    def _export_rows_for_files(self) -> tuple[tuple[str, ...], list[tuple]]:
        from core.risk import Risk
        from ui.widgets.export_csv import size_text

        headers = ("名称", "路径", "类型", "大小", "作用", "风险")
        rows = [
            (entry.name, entry.human_path, entry.kind_label, size_text(entry.size),
             entry.purpose, Risk.parse(entry.risk).label)
            for entry in self.model.entries()
        ]
        return headers, rows

    def export_current(self) -> None:
        """把当前列表里**看得见的**那些导成 CSV。只导元数据。"""
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        from ui.widgets import export_csv

        if self._current_module == PAGE_FILES:
            headers, rows = self._export_rows_for_files()
            module = "files"
        else:
            page = self.stack.widget(self._current_module)
            headers = page.export_headers() if hasattr(page, "export_headers") else ()
            rows = page.export_rows() if hasattr(page, "export_rows") else []
            module = getattr(page, "nav_key", "module")

        if not headers:
            self._toast("这个模块还不支持导出")
            return
        if not rows:
            self._toast("当前列表是空的（或者被过滤光了），没东西可导")
            return

        answer = QMessageBox.question(
            self,
            "导出 CSV",
            f"要把当前看得见的 {len(rows)} 项导成 CSV 吗？\n\n"
            "只导名称、路径、作用、风险这些信息，不含文件内容，也不含注册表键值。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        default = str(export_csv.export_dir() / export_csv.default_name(module))
        path, _filter = QFileDialog.getSaveFileName(self, "保存 CSV", default, "CSV 文件 (*.csv)")
        if not path:
            return
        ok, message = export_csv.write_csv(path, headers, rows)
        if ok:
            export_csv.prune()
            QMessageBox.information(self, "导出好了", f"已经存到：\n{message}")
        else:
            QMessageBox.warning(self, "没导成", message)

    def _on_header_clicked(self, column: int) -> None:
        """点列头循环：默认 → 升 → 降 → 默认。"""
        col, order = self.model.cycle_sort(column)
        header = self.table.horizontalHeader()
        if col < 0:
            header.setSortIndicator(-1, Qt.AscendingOrder)
        else:
            header.setSortIndicator(col, order)

    def _on_file_filter_changed(self, text: str) -> None:
        """只筛当前目录已加载的条目，绝不去读磁盘。"""
        self.model.set_filter(text)
        self.filter_bar.set_counts(self.model.rowCount(), self.model.total_count())
        self._on_checked_changed()

    def _refresh(self) -> None:
        if self._current_dir:
            self.navigate(self._current_dir, push_history=False)
        else:
            self.show_drives()

    def show_drives(self) -> None:
        """「这台电脑」这一层：列出所有盘。

        盘符栏去掉之后，用户在盘根点「上一级」就到这儿；双击某个盘再进去。
        这些条目能进不能删，所以 deletable=False（勾选框是灰的）。
        """
        self._current_dir = ""
        self._generation += 1
        if self.size_engine is not None:
            self.size_engine.cancel()
        self._pending_sizes.clear()
        self.model.clear_checks()

        entries: list[FsEntry] = []
        for drive in self._drives:
            if drive.state == "unavailable":
                purpose = drive.note or "这个盘暂时连不上"
                risk = "unlisted"
            else:
                bits = []
                if drive.total:
                    bits.append(f"共 {human_size(drive.total)}")
                if drive.free:
                    bits.append(f"可用 {human_size(drive.free)}")
                purpose = "，".join(bits) or "本地磁盘"
                risk = "caution"
            entries.append(FsEntry(
                human_path=drive.root,
                long_path=drive.root,
                name=f"{drive.root}  {drive.label}",
                is_dir=True,
                size=drive.total,
                mtime=0.0,
                kind_label=DRIVE_KIND_LABEL.get(drive.kind, "磁盘"),
                purpose=purpose,
                purpose_source="drives",
                risk=risk,
                size_state="ok" if drive.total else "none",
                deletable=False,
                impact="整个盘不能删，只能进去清理里面的东西",
            ))

        self.model.set_entries(entries)
        self.table.setEnabled(True)
        self.breadcrumb.set_path("")
        self.lbl_status.setText(
            f"共 {len(entries)} 个盘 · 双击进入" if entries else "没有读到任何磁盘"
        )
        self._on_checked_changed()

    # --------------------------------------------------------- 模块切换
    _MODULE_EXEC_LABELS = {
        PAGE_FILES: ("🗑 删除选中", "先看清单，逐项确认，再决定是移入回收站还是彻底删除"),
        PAGE_SOFTWARE: ("🗑 卸载选中", "调用软件自己的卸载程序，不是直接删文件夹"),
        PAGE_REGISTRY: ("🧹 清理选中", "删掉勾选的注册表项，动手之前再确认一次"),
        PAGE_PROCESS: ("⛔ 结束进程", "结束勾选的进程，没保存的工作会丢"),
    }

    def _refresh_current(self) -> None:
        """工具栏那个「刷新」，按当前模块分发。"""
        if self._current_module == PAGE_FILES:
            self._refresh()
            return
        page = self.stack.widget(self._current_module)
        if isinstance(page, ModulePage):
            refresh = getattr(page, "refresh_async", page.refresh)
            refresh()
            self._refresh_status_bar()

    def _execute_current(self) -> None:
        """工具栏那个执行按钮，按当前模块分发。"""
        if self._current_module == PAGE_FILES:
            self._delete_selected()
            return
        page = self.stack.widget(self._current_module)
        handler = getattr(page, "execute_checked", None)
        if callable(handler):
            handler()
        else:
            self._toast(f"{getattr(page, 'title', '这个模块')}的这一步还没接上")

    def _explain_current_page(self) -> None:
        """批量识别，按当前模块分发。

        四个模块统一在这儿先确认「填过密钥没有」——没填就弹引导，
        别让用户点了按钮只看到一句「还没配置」却不知道去哪配。
        """
        if self.ai is None:
            return
        if not self._prompt_ai_setup():
            return
        if self._current_module == PAGE_FILES:
            self._explain_unknown()
            return
        page = self.stack.widget(self._current_module)
        handler = getattr(page, "explain_unknown", None)
        if callable(handler):
            handler()
        else:
            self._toast("这个模块的联网识别还没接上")

    def _apply_module_ui(self, index: int) -> None:
        """工具栏按模块收放：文件专属的整组隐藏，执行按钮换个说法。"""
        is_files = index == PAGE_FILES
        for action in getattr(self, "_file_only_actions", ()):
            action.setVisible(is_files)
        for separator in (getattr(self, "_sep_after_nav", None), getattr(self, "_sep_after_size", None)):
            if separator is not None:
                separator.setVisible(is_files)
        label, tip = self._MODULE_EXEC_LABELS.get(index, self._MODULE_EXEC_LABELS[PAGE_FILES])
        self.act_delete.setText(label)
        self.act_delete.setToolTip(tip)
        # 「扫描全局残留」是给卸载模块用的：那里才有「已装软件清单」这个参照
        self.act_scan_scopes.setVisible(index == PAGE_SOFTWARE)
        self.act_scan_scopes.setEnabled(index == PAGE_SOFTWARE)
        self.act_global_clean.setVisible(index == PAGE_SOFTWARE)
        self.act_global_clean.setEnabled(index == PAGE_SOFTWARE)
        self.act_clean_process.setVisible(index == PAGE_PROCESS)
        self.act_clean_process.setEnabled(index == PAGE_PROCESS)
        self.act_dead_registry.setVisible(index == PAGE_REGISTRY)
        self.act_dead_registry.setEnabled(index == PAGE_REGISTRY)
        self.act_ai_registry.setVisible(index == PAGE_REGISTRY)
        self.act_ai_registry.setEnabled(index == PAGE_REGISTRY)

    def _deactivate_files_module(self) -> None:
        """离开文件模块：清勾选、停大小计算、清解锁记录。"""
        self.model.clear_checks()
        if self.size_engine is not None:
            self.size_engine.cancel()
        self._pending_sizes.clear()

    def _switch_module(self, index: int) -> None:
        """切模块。走之前先把当前页收干净，不许带着勾选或解锁状态过去。"""
        if index == self._current_module or not (0 <= index < self.stack.count()):
            self.nav.set_current(self._current_module)
            return

        if self._current_module == PAGE_FILES:
            self._deactivate_files_module()
        else:
            previous = self.stack.widget(self._current_module)
            if isinstance(previous, ModulePage):
                previous.deactivate()
        if self.ai is not None:
            self.ai.cancel_all()
        self.act_ai_stop.setEnabled(False)

        self._current_module = index
        self.stack.setCurrentIndex(index)
        self.nav.set_current(index)
        self._apply_module_ui(index)

        if index == PAGE_FILES:
            self.lbl_status.setText(f"{len(self.model.entries())} 项 · {self._current_dir}")
        else:
            page = self.stack.widget(index)
            activate = getattr(page, "activate", None)
            try:
                if callable(activate):
                    activate()      # 页面自己决定怎么读，默认就是读一遍
                else:
                    page.refresh()
            except Exception:  # noqa: BLE001 - 读不出来也不能把切页搞崩
                self._toast(f"{getattr(page, 'title', '这个模块')}加载时出了点问题")
            line = getattr(page, "status_line", None)
            self.lbl_status.setText(line() if callable(line) else page.title)
        self._refresh_status_bar()

        self.settings.last_module = index
        self.settings_store.save(self.settings)

    def _open_in_files_module(self, path: str) -> None:
        """别的模块让你「在文件模块打开某个位置」时走这里。"""
        if not path:
            return
        self._switch_module(PAGE_FILES)
        self.navigate(path)

    def _on_module_checked_changed(self) -> None:
        if self._current_module == PAGE_FILES:
            return
        self._refresh_status_bar()

    def _refresh_status_bar(self) -> None:
        """状态栏统一：左边一句状态，右边「已选 X 项」，各模块自己拼后缀。"""
        if self._current_module == PAGE_FILES:
            self._on_checked_changed()
            return
        page = self.stack.widget(self._current_module)
        if not isinstance(page, ModulePage):
            return
        self.lbl_selection.setText(page.selection_text())
        # 页面自己知道「现在停在哪一层」，状态栏左边那句跟着它走
        line = getattr(page, "status_line", None)
        if callable(line):
            self.lbl_status.setText(line())
        self.act_delete.setEnabled(bool(page.checked_items()) and not self._busy)

    def _toggle_hidden(self, checked: bool) -> None:
        self.settings.show_hidden = checked
        self.settings_store.save(self.settings)
        self._refresh()

    # ----------------------------------------------------------- 大小计算
    def _schedule_sizes(self, entries: Sequence[FsEntry], generation: int) -> None:
        if not self.settings.auto_size or self.size_engine is None:
            return
        targets = [
            entry.human_path
            for entry in entries
            # 盘符（deletable=False）绝不参与：那等于把整个盘扫一遍
            if entry.is_dir and not entry.locked and entry.deletable and entry.size_state != "ok"
        ]
        if not targets:
            return
        self._pending_sizes = set(targets)
        self.act_stop.setEnabled(True)
        self.size_engine.enqueue(targets, generation)

    def _recalc_sizes(self) -> None:
        entries = self.model.entries()
        dirs = [
            entry.human_path
            for entry in entries
            if entry.is_dir and not entry.locked and entry.deletable
        ]
        if not dirs:
            self._toast("这一页没有可以算大小的文件夹")
            return
        self._generation += 1
        generation = self._generation
        if self.size_engine is not None:
            self.size_engine.cancel()
        self._pending_sizes = set(dirs)
        self.act_stop.setEnabled(True)
        for entry in entries:
            if entry.is_dir and not entry.locked and entry.deletable:
                self.model.update_size(entry.human_path, None, "queued")
        if self.size_engine is not None:
            self.size_engine.enqueue(dirs, generation)

    def _stop_sizes(self) -> None:
        if self.size_engine is not None:
            self.size_engine.cancel()
        self._pending_sizes.clear()
        self.act_stop.setEnabled(False)
        self._toast("已经停下，没有算完的文件夹会显示成「—」")

    def _on_size_ready(self, generation: int, human_path: str, size, state: str) -> None:
        if generation != self._generation:
            return
        self.model.update_size(human_path, size, state)
        self._pending_sizes.discard(human_path)
        if state in ("ok", "partial") and size is not None and self.cache is not None:
            self.cache.put_size(human_path, int(size), state)
        if self._pending_sizes and self._current_module == PAGE_FILES:
            self.lbl_status.setText(f"{self._current_dir} · 还有 {len(self._pending_sizes)} 个文件夹在算…")

    def _on_size_batch_done(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._pending_sizes.clear()
        self.act_stop.setEnabled(False)
        entries = self.model.entries()
        if self._current_module == PAGE_FILES:
            self.lbl_status.setText(f"{len(entries)} 项 · {self._current_dir}")

    # --------------------------------------------------------------- 勾选
    def _on_checked_changed(self) -> None:
        entries = self.model.checked_entries()
        has_any = bool(entries)
        self.act_delete.setEnabled(has_any and not self._busy)
        if hasattr(self, "header"):
            self.header.set_check_state(self.model.check_state())
        if not entries:
            self.lbl_selection.setText(
                "已选 0 项（已过滤）" if self.model.is_filtered() else "已选 0 项"
            )
            return
        sizes = [entry.size for entry in entries]
        suffix = ""
        hidden = self.model.hidden_checked_count()
        if hidden:
            suffix += f" · 另有 {hidden} 项被过滤藏起来了但还勾着"
        if self.model.is_filtered():
            suffix += "（已过滤；隐藏项也会执行）"
        if any(size is None for size in sizes):
            # 只要有一项还没算出多大，就只说选了几项，不编一个数字出来
            self.lbl_selection.setText(f"已选 {len(entries)} 项{suffix}")
        else:
            total = human_size(sum(sizes))
            self.lbl_selection.setText(
                f"已选 {len(entries)} 项，约 {total}（移入回收站，清空回收站后释放）{suffix}"
            )
    def _toggle_all_checked(self) -> None:
        """表头那个框被点了：没全选就全选，已全选就清空。"""
        state = self.model.check_state()
        self.model.set_all_checked(state != Qt.Checked)

    def _on_table_selection(self) -> None:
        self._update_ai_action()

    def _update_ai_action(self) -> None:
        has_selection = bool(self.table.selectionModel().selectedRows())
        configured = self.ai is not None and self.ai.is_configured()
        # 没配置也让点：点下去会弹引导填密钥，比灰着不告诉原因好
        self.act_ai.setEnabled(bool(has_selection and not self._busy))
        self.act_ai_page.setEnabled(bool(not self._busy and self.model.rowCount() > 0))
        if not configured:
            self.act_ai.setToolTip("还没配置，点了会引导你填密钥")
            self.act_ai_page.setToolTip("还没配置，点了会引导你填密钥")
        else:
            self.act_ai.setToolTip("只识别选中的这一项")
            self.act_ai_page.setToolTip("把这一页里知识库不认识的项逐个问一遍")

    # --------------------------------------------------------------- 删除
    def _build_delete_items(self):
        """把勾选的条目转成执行用的 DeleteItem。单独抽出来，测试能直接调。"""
        from core.delete import DeleteItem

        return [
            DeleteItem(
                human_path=entry.human_path,
                display=entry.name,
                is_dir=entry.is_dir,
                risk=entry.risk,
                size=entry.size,
                effect=entry.purpose,
                impact=entry.impact,
            )
            for entry in self.model.checked_entries()
        ]

    def _delete_selected(self) -> None:
        """勾选 → 清单确认（可逐项取消、可选方式）→ 后台删 → 结果报告。"""
        from core.delete import Mode

        entries = self.model.checked_entries()
        if not entries:
            self._toast("还没有勾选要删的东西")
            return
        items = self._build_delete_items()
        from ui.confirm_flow import ConfirmFlow

        decision = ConfirmFlow.confirm(
            self,
            items,
            "删除",
            hint="下面每一项都可以单独取消。「作用」就是列表里那行说明，再确认一遍。",
            irreversible=True,
            modes=(
                ("移入回收站（之后还能还原）", Mode.RECYCLE.value),
                ("彻底删除（回收站里也不会有）", Mode.PERMANENT.value),
            ),
        )
        if decision is None or not decision.items:
            self._toast("清单里一项都没留下，那就先不删了")
            return
        mode = Mode.PERMANENT if decision.mode == Mode.PERMANENT.value else Mode.RECYCLE
        if not self._confirm_extra_recycle(decision.items, mode):
            self._toast("已经取消，这一批没有删除")
            return
        self._last_mode = mode
        self._start_delete(decision.items, mode)

    def _confirm_extra_recycle(self, items, mode):
        """Apply the configured large-recycle warning or extra confirmation."""
        from core.delete import Mode

        if mode is not Mode.RECYCLE:
            return True
        total = sum(
            int(getattr(item, "size", 0) or 0)
            for item in items
        )
        threshold = int(getattr(self.settings, "recycle_warn_over_gb", 20) or 0)
        if threshold and total >= threshold * 1024 ** 3:
            answer = QMessageBox.question(
                self,
                "回收站提醒",
                f"这一批约有 {human_size(total)} 会先进入回收站，"
                f"超过你设置的 {threshold} GB 提醒线。\n\n"
                "回收站清空之前空间不会真正释放。确定继续吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            return answer == QMessageBox.Yes
        if bool(getattr(self.settings, "confirm_recycle", False)):
            answer = QMessageBox.question(
                self,
                "再确认一次",
                f"要把这 {len(items)} 项移入回收站吗？\n\n"
                "删除后还能从回收站还原；清空回收站后才真正释放空间。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            return answer == QMessageBox.Yes
        return True

    def _start_delete(self, items, mode) -> None:
        from core import snapshot as snapshots
        from core.delete import DeleteWorker, Mode

        # 动手之前先留一份快照：只记名称、路径、风险，不复制文件
        self._snap_id = snapshots.begin(
            "delete", items, mode=mode.value if hasattr(mode, "value") else str(mode),
        )
        worker = DeleteWorker(items, mode, self.paths.logs)
        worker.signals.progress.connect(self._on_delete_progress)
        worker.signals.finished.connect(self._on_delete_finished)
        self._worker = worker
        self._set_busy(True)
        self.lbl_status.setText(
            "正在彻底删除…" if mode is Mode.PERMANENT else "正在移入回收站…"
        )
        QThreadPool.globalInstance().start(worker)

    def _on_delete_progress(self, done: int, total: int, path: str) -> None:
        name = os.path.basename(path.rstrip("\\/")) or path
        self.lbl_status.setText(f"正在删除 {done}/{total} · {name}")

    def _on_delete_finished(self, report) -> None:
        from core import snapshot as snapshots

        snapshots.finish(getattr(self, "_snap_id", None), getattr(report, "items", []), self.paths.logs)
        self._worker = None
        self._set_busy(False)
        self.model.clear_checks()
        # 删成功的那些直接从列表里拿掉，没删成的留着，用户可以当场再处理
        removed = self.model.remove_paths([item.human_path for item in report.items if item.ok])
        self._toast(report.summary)
        if self._current_module == PAGE_FILES:
            self.lbl_status.setText(
                f"{len(self.model.entries())} 项 · {self._current_dir}"
                if self.model.entries()
                else f"这个文件夹已经空了 · {self._current_dir}"
            )
        from ui.dialogs import DeleteReportDialog

        log_path = getattr(report, "log_path", None)
        dialog = DeleteReportDialog(
            report, log_path, self,
            on_retry=self._retry_delete,
            op_type="delete",
        )
        dialog.exec()
        if removed:
            self._refresh_after_delete()

    def _retry_delete(self, rows) -> None:
        """重试失败项：把还在的项按原来的方式再跑一遍。"""
        from core.delete import DeleteItem

        items = []
        for row in rows:
            path = row.path
            # 重试时尽量把原来那行的说明找回来，别让用户对着一条「未收录」重新做决定
            entry = None
            for candidate in self.model._all_entries:
                if os.path.normcase(candidate.human_path) == os.path.normcase(path):
                    entry = candidate
                    break
            items.append(DeleteItem(
                human_path=path,
                display=(entry.name if entry is not None else
                         os.path.basename(path.rstrip("\\/")) or path),
                is_dir=os.path.isdir(path),
                risk=(entry.risk if entry is not None else "unlisted"),
                size=(entry.size if entry is not None else None),
                effect=(entry.purpose if entry is not None else ""),
                impact=(entry.impact if entry is not None else ""),
            ))
        if not items:
            self._toast("这些项都已经不在了，没什么可重试的")
            return
        from core.delete import Mode

        self._start_delete(items, getattr(self, "_last_mode", Mode.RECYCLE))

    def _refresh_after_delete(self) -> None:
        """删完之后重新读一次当前目录，让大小、剩余项都跟磁盘一致。"""
        self.navigate(self._current_dir, push_history=False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.act_delete.setEnabled(not busy and bool(self.model.checked_entries()))
        self.act_ai.setEnabled(not busy and self.ai is not None and self.ai.is_configured())
        self.table.setEnabled(not busy)

    # ----------------------------------------------------- 双击 / 右键菜单
    def _send_feedback(self, path: str, name: str, purpose: str, module: str = "") -> None:
        from ui.dialogs import FeedbackDialog

        FeedbackDialog(path=path, name=name, purpose=purpose, module=module, parent=self).exec()

    def _offer_admin_for_locked(self, entry) -> None:
        """读不到的项：只有用户主动双击/右键才问，绝不自己弹。"""
        if _is_admin():
            self._toast("这一项连管理员也读不到，可能是被别的进程占着")
            return
        answer = QMessageBox.question(
            self,
            "需要管理员权限",
            f"「{entry.name}」普通权限读不到。\n\n"
            "用管理员身份重新启动一次就能看到它，重启后会回到这个目录并重算大小。\n"
            "要现在重启吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self._restart_as_admin()

    def _on_double_click(self, index) -> None:
        entry = self.model.entry_at(index.row())
        if entry is None:
            return
        if entry.locked and entry.is_dir:
            self._offer_admin_for_locked(entry)
            return
        if entry.is_dir:
            self.navigate(entry.human_path)
        else:
            self._open_path(entry.human_path)

    def _open_path(self, path: str) -> None:
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except Exception:  # noqa: BLE001
            self._toast("打不开这个文件")

    def _reveal(self, path: str) -> None:
        target = os.path.normpath(path)
        try:
            subprocess.Popen(["explorer", f"/select,{target}"], close_fds=True)  # noqa: S603,S607
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            os.startfile(os.path.dirname(target))  # noqa: S606
        except Exception:  # noqa: BLE001
            self._toast("打不开这个文件夹")

    def _show_context_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        menu = QMenu(self)
        entry = self.model.entry_at(index.row()) if index.isValid() else None
        if entry is not None:
            menu.addAction("在资源管理器中打开", lambda: self._reveal(entry.human_path))
            menu.addAction("复制完整路径", lambda: self._copy_path(entry.human_path))
            ai_action = menu.addAction("AI 识别此项", lambda: self._explain_entry(entry))
            ai_action.setEnabled(self.ai is not None and self.ai.is_configured())
            if self.ai is not None and not self.ai.is_configured():
                ai_action.setToolTip("联网解释现在是关着的，去「设置」里开一下")
            menu.addSeparator()
            if entry.locked:
                # 读不到通常是权限问题。给个出口，但不自动弹 —— 用户点了才动
                if _is_admin():
                    blocked = menu.addAction("这一项读不到（已经是管理员权限了）")
                    blocked.setEnabled(False)
                else:
                    menu.addAction(
                        "🔒 需要管理员权限，点此重启",
                        self._restart_as_admin,
                    )
            elif entry.checked:
                menu.addAction("取消勾选", lambda: self.model.set_checked(entry.human_path, False))
            else:
                menu.addAction(
                    "勾选这一项（" + ("整个文件夹" if entry.is_dir else "这个文件") + "）",
                    lambda: self.model.set_checked(entry.human_path, True),
                )
            menu.addSeparator()
            menu.addAction(
                "这条说明不对…",
                lambda: self._send_feedback(entry.human_path, entry.name, entry.purpose, "files"),
            )
            menu.addSeparator()
            menu.addAction(self.act_delete)
            menu.addAction(self.act_refresh)
        else:
            menu.addAction("全部勾选", lambda: self.model.set_all_checked(True))
            menu.addAction("全部取消勾选", lambda: self.model.set_all_checked(False))
            menu.addSeparator()
            menu.addAction(self.act_refresh)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_path(self, path: str) -> None:
        QApplication.clipboard().setText(path)
        self._toast("完整路径已经复制好了")

    def _explain_entry(self, entry: FsEntry) -> None:
        """只问当前这一项，不去碰别的行。"""
        if self.ai is None:
            return
        if not self._prompt_ai_setup():
            return
        self.ai.explain(entry.human_path, entry.name)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.table and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Delete and self.model.checked_entries():
                self._delete_selected()
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------ 联网解释
    def _prompt_ai_setup(self) -> bool:
        """没配置就先弹引导；配置好了返回 True。"""
        if self.ai is not None and self.ai.is_configured():
            return True
        from ui.dialogs import AISetupDialog

        dialog = AISetupDialog(self.settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        dialog.apply_to(self.settings)
        self.settings_store.save(self.settings)
        if self.ai is not None:
            self.ai.settings = self.settings
        self._update_ai_action()
        self._toast("联网识别已经打开")
        return self.ai is not None and self.ai.is_configured()

    def _explain_current_row(self) -> None:
        """右键那一行的「AI 识别此项」。"""
        if self.ai is None:
            return
        if not self._prompt_ai_setup():
            return
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        entry = self.model.entry_at(rows[0]) if rows else None
        if entry is None:
            self._toast("先在列表里点一行")
            return
        self.ai.explain(entry.human_path, entry.name)

    def _explain_unknown(self) -> None:
        """批量：只问这一页里知识库不认识的项。"""
        if self.ai is None:
            return
        if not self._prompt_ai_setup():
            return
        entries = [
            entry
            for entry in self.model.entries()
            if not entry.locked and entry.purpose_source not in ("kb", "ai")
        ]
        if not entries:
            self._toast("这一页没有知识库不认识的项")
            return
        answer = QMessageBox.question(
            self,
            "联网识别",
            f"{len(entries)} 个未知项将逐项调用 AI，是否继续？\n\n"
            "每一项单独发一次请求，右侧的「停止识别」随时可以停。\n"
            "发出去的只有名称和路径，不会发文件内容。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        pairs = [(entry.human_path, entry.name) for entry in entries]
        self.act_ai_page.setEnabled(False)
        self.act_ai_stop.setEnabled(True)
        self.lbl_status.setText(f"联网识别 0/{len(pairs)} …")
        queued = self.ai.explain_many(pairs)
        if queued == 0:
            self.act_ai_page.setEnabled(True)
            self.act_ai_stop.setEnabled(False)
            self._toast("没排上队，检查一下设置")

    def _stop_ai(self) -> None:
        if self.ai is not None:
            self.ai.cancel_all()
        self.act_ai_stop.setEnabled(False)
        self.act_ai_page.setEnabled(True)
        self._toast("已经停下，已经识别出来的结果都留着")

    def _on_ai_progress(self, done: int, total: int, human_path: str) -> None:
        name = os.path.basename(human_path.rstrip("\\/")) or human_path
        self.lbl_status.setText(f"联网识别 {done}/{total} · {name}")

    def _on_ai_result(self, result) -> None:
        row = self.model.row_of(result.human_path)
        if row < 0:
            return
        entry = self.model.entry_at(row)
        if entry is None:
            return
        # AI 只能把风险说得更严，不能把知识库判的「别删」说成能删
        current = Risk.parse(entry.risk)
        incoming = Risk.parse(result.risk)
        if incoming.rank > current.rank:
            entry.risk = incoming.value
        entry.impact = result.impact
        entry.ai_note = result.note
        self.model.set_purpose_only(result.human_path, result.purpose, "ai")
        if Risk.parse(entry.risk) is Risk.FORBIDDEN:
            # 只提醒，不替用户取消勾选
            self._toast("AI 认为这一项不能删，要不要留着勾由你决定")

    def _on_ai_failed(self, human_path: str, message: str) -> None:
        self._toast(f"识别没成功：{message}")

    def _on_ai_batch_finished(self, ok: int, failed: int) -> None:
        self.act_ai_page.setEnabled(True)
        self.act_ai_stop.setEnabled(False)
        self.lbl_status.setText(f"{len(self.model.entries())} 项 · {self._current_dir}")
        if failed:
            self._toast(f"识别完成：成功 {ok} 项，失败 {failed} 项（失败的那一行右键可以单独重试）")
        else:
            self._toast(f"识别完成：{ok} 项全部成功")

    def _on_ai_busy(self, busy: bool) -> None:
        if busy:
            self.act_ai.setEnabled(False)
            self.act_ai.setToolTip("正在问…")
        else:
            self._update_ai_action()
        # 停止按钮跟着「AI 忙不忙」走 —— 四个模块都从这儿拿状态。
        # 以前只有文件模块在它自己的流程里手动启用，其他三个模块按钮一直是灰的，
        # 用户点不动，看起来就是「停止识别不生效」。
        running = bool(busy)
        if not running and self.ai is not None:
            try:
                running = bool(self.ai.in_batch())
            except Exception:  # noqa: BLE001
                running = False
        self.act_ai_stop.setEnabled(running)
        self.act_ai_page.setEnabled(not running)

    # --------------------------------------------------------------- 设置
    def _open_help(self) -> None:
        from ui.dialogs import HelpDialog

        HelpDialog(self).exec()

    def _open_url(self, url: str) -> None:
        """用系统默认浏览器打开。打不开就说一声，别静默失败。"""
        try:
            ok = QDesktopServices.openUrl(QUrl(url))
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            QMessageBox.information(
                self, "打不开浏览器",
                f"没能调起系统浏览器。\n\n可以手动复制这个地址去看：\n{url}",
            )

    def _open_github(self) -> None:
        self._open_url(GITHUB_URL)

    def _open_tutorial(self) -> None:
        self._open_url(TUTORIAL_URL)

    def _open_settings(self) -> None:
        from ui.dialogs import SettingsDialog

        dialog = SettingsDialog(self.settings, self, cache=self.cache, kb=self.kb)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.apply_to(self.settings)
            self.settings_store.save(self.settings)
            self.apply_appearance()          # 主题/字号改完立即生效，不用重启
            self.act_hidden.setChecked(self.settings.show_hidden)
            if self.ai is not None:
                self.ai.settings = self.settings
            self._update_ai_action()
            self._toast("设置已经存下来了")
            self._refresh()

    def _restart_as_admin(self) -> None:
        """换个更高权限重开自己，并把当前目录带过去。

        提权之后，之前标着「打不开」的文件夹可能就读得到了，
        所以新进程会跳过大小缓存、把当前目录重算一遍。
        """
        if _is_admin():
            QMessageBox.information(
                self, "已经是管理员了", "这个窗口现在就是以管理员身份在跑的，不用再重启。"
            )
            return
        try:
            import ctypes
        except ImportError:
            self._toast("这个功能在当前环境用不了")
            return

        pieces = []
        if self._current_dir:
            pieces.append(f'--dir "{self._current_dir}"')
        # 带上模块：重启后回到刚才那一页，并且重新读一遍
        pieces.append(f"--module {self._current_module}")
        params = " ".join(pieces)
        executable = sys.executable
        if not getattr(sys, "frozen", False):
            script = os.path.abspath(sys.argv[0])
            params = f'"{script}" {params}'.strip()
        try:
            code = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
        except Exception:  # noqa: BLE001
            code = 0
        if code <= 32:
            self._toast("没有拿到管理员权限（可能是点了「否」），继续用普通权限")
            return
        self.close()

    # --------------------------------------------------------------- 杂项
    def _toast(self, message: str) -> None:
        self.statusBar().showMessage(message, 6000)

    def show_startup_notes(self, warnings: Sequence[str], notes: Sequence[str] = ()) -> None:
        """启动时把「刚生成的文件」和「哪些东西没用上默认」讲清楚。

        提示一闪而过容易被忽略，所以在状态栏留一个常驻标记，鼠标停上去能看全部。
        """
        if notes:
            self._toast(notes[0])
        if not warnings:
            return
        self.lbl_warning.setText(f"⚠ {len(warnings)} 条提示")
        self.lbl_warning.setToolTip("\n".join(warnings[:10]))
        self.lbl_warning.setVisible(True)
        if not notes:
            self._toast(warnings[0])

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._busy or self._background_workers:
            self._toast("还有任务在执行，完成后再关闭窗口")
            event.ignore()
            return
        self.settings.window_width = self.width()
        self.settings.window_height = self.height()
        self.settings.last_dir = self._current_dir
        try:
            self.settings_store.save(self.settings)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.cache is not None:
                self.cache.flush(force=True)
        except Exception:  # noqa: BLE001
            pass
        for engine in (self.size_engine,):
            try:
                if engine is not None:
                    engine.stop()
            except Exception:  # noqa: BLE001
                pass
        for maybe in (self.drive_enum, self.ai):
            try:
                if maybe is not None and hasattr(maybe, "stop"):
                    maybe.stop()
                elif maybe is not None and hasattr(maybe, "shutdown"):
                    maybe.shutdown()
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)
