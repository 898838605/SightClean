"""四个模块页面的公共底座。

只负责四件跨模块一样的事，别的一概不管：
1. 勾选状态（勾了什么、清空、文案）
2. 红色项的解锁集合（会话内有效，切走就清）
3. 切走时该停的后台任务
4. 状态栏那一行字

具体怎么列条目、怎么执行，由各页面自己实现。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QWidget

def build_empty_state(title: str, hint: str) -> QLabel:
    """空态提示。四个模块长得一样，别再各写一个。"""
    label = QLabel(f"<h3 style='color:#1f2328'>{title}</h3><p style='color:#57606a'>{hint}</p>")
    label.setAlignment(Qt.AlignCenter)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label

class ModulePage(QWidget):
    """一个模块页。子类最少要实现 refresh / clear_checks / checked_items。"""

    checkedChanged = Signal()
    statusMessage = Signal(str)
    aiSetupRequested = Signal()   # 想让主窗口弹「填密钥」引导（四个模块共用一个入口）
    adminRestartRequested = Signal()   # 用户点了「需要管理员权限，点此重启」

    def __init__(self, nav_key: str, title: str, parent=None) -> None:
        super().__init__(parent)
        self.nav_key = nav_key
        self.title = title

        # 记录本页自己那一行状态栏文案，主窗口切回来时不用重算
        self._last_status = "已选 0 项"

    # -- 子类要实现的 ------------------------------------------------------
    def refresh(self) -> None:
        """重新读一遍列表。主窗口的「刷新」按钮和切到本页时都会调。"""

    def clear_checks(self) -> None:
        """清空勾选。"""

    def checked_items(self) -> list:
        """当前勾选、可以执行的那些条目。"""
        return []

    def cancel_running(self) -> None:
        """停掉本页正在跑的后台活（加载、执行、AI 批量）。"""

    # -- 公共行为 ----------------------------------------------------------
    def selection_text(self) -> str:
        """状态栏右边那句「已选 X 项」。子类可以覆盖来加自己的后缀。"""
        return self._last_status

    # -- 导出 --------------------------------------------------------------
    def export_headers(self) -> tuple[str, ...]:
        """CSV 表头。子类覆盖后才有导出能力。"""
        return ()

    def export_rows(self) -> list[tuple]:
        """只返回**当前可见**的元数据行：不含文件内容、键值、命令行。"""
        return []

    def status_line(self) -> str:
        """状态栏左边那句。子类覆盖来报自己的规模，比如「共 89 个软件」。"""
        return self.title

    def set_selection_text(self, text: str) -> None:
        self._last_status = text
        self.checkedChanged.emit()

    def is_red(self, item) -> bool:
        return str(getattr(item, "risk", "")) == "forbidden"

    def activate(self) -> None:
        """被切到前台。默认就把自己的列表读一遍。

        这一点必须默认做：用户切过来是要看东西的，不该还要再点一次「刷新」。
        数据特别重的页面（比如进程）可以覆盖它做取舍，但**默认必须读**。
        """
        self.refresh()

    def deactivate(self) -> None:
        """要切走了：停后台、清勾选、清解锁。

        解锁状态一定不能跨模块带 —— 在软件页确认过的东西，不该成为注册表页的通行证。
        """
        self.cancel_running()
        self.clear_checks()

        self._last_status = "已选 0 项"

