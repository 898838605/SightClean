"""清理「开了很久、不在前台、CPU 也不动」的进程。

判断三件事：
    时间     —— 开了多久，太短的不算
    前台     —— 你正在用的那个不动
    CPU      —— 正在算东西的不动

系统进程一律排除：会话 0 的服务、桌面 shell、Windows 自己的组件。
剩下的基本都是「忘了关」的软件 —— 浏览器、编辑器、聊天工具。
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.processes import age_text, group_by_exe, kill_process

MUTED = QColor("#8b949e")
OK = QColor("#1a7f37")
CAUTION = QColor("#9a6700")


class ProcessCleanDialog(QDialog):
    def __init__(self, orphans, parent=None, max_cpu: float = 1.0,
                 min_age_minutes: float = 30) -> None:
        super().__init__(parent)
        self.setWindowTitle("清理空闲进程")
        self.setMinimumSize(860, 620)
        self._now = time.time()
        self._groups = group_by_exe(orphans)
        self._boxes: dict[str, QCheckBox] = {}
        self.killed = 0
        self.failed = 0

        total = sum(len(group) for _exe, group in self._groups)

        title = QLabel(f"找出 {len(self._groups)} 组、{total} 个空闲进程")
        font = title.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        title.setFont(font)

        hint = QLabel(
            f"判据三条：开了 {int(min_age_minutes)} 分钟以上、不在前台、CPU 低于 {max_cpu:g}%。\n"
            "系统进程（服务、桌面、开始菜单）已经排除，剩下的基本是「忘了开着的软件」。\n"
            "**一个都不勾**是默认状态，你看过运行时长再决定。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{MUTED.name()};")

        inner = QWidget()
        self.inner_layout = QVBoxLayout(inner)
        self.inner_layout.setContentsMargins(0, 0, 8, 0)
        for exe, group in self._groups:
            self._add_group(exe, group)
        self.inner_layout.addStretch(1)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color:{MUTED.name()};")

        self.buttons = QDialogButtonBox()
        self.btn_kill = self.buttons.addButton("结束选中的进程", QDialogButtonBox.AcceptRole)
        self.btn_close = self.buttons.addButton("关闭", QDialogButtonBox.RejectRole)
        self.btn_kill.setEnabled(False)
        self.btn_kill.clicked.connect(self._do_kill)
        self.btn_close.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(area, 1)
        layout.addWidget(self.summary)
        layout.addWidget(self.buttons)
        self._refresh()

    def _add_group(self, exe: str, group) -> None:
        holder = QWidget()
        row = QVBoxLayout(holder)
        row.setContentsMargins(0, 6, 0, 6)
        row.setSpacing(2)

        head = QHBoxLayout()
        box = QCheckBox(f"{group[0].name}　{len(group)} 个进程")
        box.toggled.connect(self._refresh)
        self._boxes[str(exe).lower()] = box
        head.addWidget(box, 1)

        # 开了多久 + 当前 CPU —— 判断依据就摆在明面上。
        # 用短格式，别让它被右边缘截掉。
        oldest = min((p.create_time for p in group if p.create_time), default=0)
        age = age_text(group[0], self._now) if oldest else "—"
        cpu = sum(p.cpu for p in group)
        stat = QLabel(f"开了 {age}　CPU {cpu:.1f}%")
        stat.setStyleSheet(f"color:{CAUTION.name()};")
        stat.setToolTip(
            "整个组一共开了多久、当前占多少 CPU\n"
            "判断依据：开了越久、CPU 越低，越可能是忘了关的"
        )
        # 钉死宽度：给 stretch 的话右边还是会被挤扁，CPU 数值就看不见了
        stat.setMinimumWidth(210)
        stat.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        stat.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        head.addWidget(stat, 0)
        row.addLayout(head)

        path = QLabel(exe or "(读不到路径)")
        path.setWordWrap(True)
        path.setStyleSheet(f"color:{MUTED.name()}; margin-left:22px;")
        row.addWidget(path)

        pids = "、".join(str(p.pid) for p in group[:8])
        if len(group) > 8:
            pids += f" 等 {len(group)} 个"
        pid_label = QLabel(f"PID：{pids}")
        pid_label.setWordWrap(True)
        pid_label.setStyleSheet(f"color:{MUTED.name()}; margin-left:22px;")
        row.addWidget(pid_label)

        self.inner_layout.addWidget(holder)

    def _chosen(self) -> list:
        picked = []
        for exe, group in self._groups:
            box = self._boxes.get(str(exe).lower())
            if box is not None and box.isChecked():
                picked.extend(group)
        return picked

    def _refresh(self) -> None:
        picked = self._chosen()
        if not picked:
            self.summary.setText("一个都没勾，勾上才会结束")
            self.btn_kill.setEnabled(False)
            return
        self.summary.setText(f"已选 {len(picked)} 个进程，结束后没法撤销")
        self.btn_kill.setEnabled(True)

    def _do_kill(self) -> None:
        picked = self._chosen()
        if not picked:
            return
        self.killed = 0
        self.failed = 0
        for proc in picked:
            # kill_process 收的是 pid 和名字，不是整个对象
            if kill_process(proc.pid, proc.name).ok:
                self.killed += 1
            else:
                self.failed += 1

        bits = [f"结束 {self.killed} 个"]
        if self.failed:
            bits.append(f"{self.failed} 个没成功")
        self.summary.setStyleSheet(
            f"color:{OK.name() if not self.failed else CAUTION.name()};")
        self.summary.setText("　·　".join(bits))
        self.btn_kill.setVisible(False)
        for box in self._boxes.values():
            box.setEnabled(False)
