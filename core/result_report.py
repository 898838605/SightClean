"""逐项结果的统一视图。

四个模块各自有 report 类（DeleteReport / UninstallReport / RegistryReport /
ProcessReport），字段名不一样。这里不重写它们，只做一层「读出来统一成一张表」
的适配 —— 报告对话框和撤销中心都吃这一份。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_SKIPPED = "skipped"
STATUS_PENDING = ""

STATUS_LABELS = {
    STATUS_OK: "成功",
    STATUS_FAIL: "失败",
    STATUS_SKIPPED: "已暂缓",
    STATUS_PENDING: "未执行",
}

# 失败原因归类：报告里用中文短语说清楚「为什么没成」
CATEGORY_LABELS = {
    "busy": "文件正被占用",
    "denied": "没有权限",
    "unsupported": "这种方式不支持",
    "gone": "项已不存在",
    "other": "其他原因",
}

_DENIED_HINTS = ("拒绝访问", "没有权限", "需要管理员", "权限", "AccessDenied", "5", "denied")
_GONE_HINTS = ("不存在", "找不到", "已经不在了", "NoSuchProcess", "FileNotFound", "已不在", "缺失")
_BUSY_HINTS = ("占用", "正在使用", "in use", "being used", "共享冲突")
_UNSUPPORTED_HINTS = ("不支持", "没有卸载命令", "找不到卸载", "不支持该请求")

@dataclass
class ResultRow:
    name: str
    path: str
    status: str = STATUS_PENDING
    error: str = ""
    category: str = ""
    retryable: bool = False
    size: int | None = None
    create_time: float = 0.0
    exe: str = ""

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, "") if self.category else ""

    @property
    def reason_text(self) -> str:
        """给人看的一句：「没有权限（拒绝访问）」。"""
        detail = (self.error or "").strip()
        if not detail:
            return self.category_label or ""
        if self.category_label and self.category_label not in detail:
            return f"{self.category_label}（{detail}）"
        return detail

def classify(error: str | None) -> str:
    """把失败原因归到四类里。认不出来就算「其他」。"""
    text = str(error or "")
    if not text:
        return "other"
    if any(hint in text for hint in _GONE_HINTS):
        return "gone"
    if any(hint in text for hint in _UNSUPPORTED_HINTS):
        return "unsupported"
    if any(hint in text for hint in _DENIED_HINTS):
        return "denied"
    if any(hint in text for hint in _BUSY_HINTS):
        return "busy"
    return "other"

def _exists_file(path: str) -> bool:
    try:
        return bool(path) and (os.path.exists(path) or os.path.exists(_strip_long(path)))
    except (OSError, ValueError):
        return False

def _strip_long(path: str) -> str:
    text = str(path or "")
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text

def _exists_registry(path: str) -> bool:
    import winreg

    try:
        from core.registry import _split      # 复用同一套根键解析

        hive, _full, rest = _split(path)
        if not rest:
            return True
        with winreg.OpenKey(hive, rest, 0, winreg.KEY_READ):
            return True
    except (OSError, ValueError, ImportError):
        return False

def _exists_process(pid, create_time: float = 0.0, exe: str = "") -> bool:
    try:
        import psutil

        if not psutil.pid_exists(int(pid)):
            return False
        proc = psutil.Process(int(pid))
        if create_time and abs(float(proc.create_time()) - float(create_time)) > 0.01:
            return False
        if exe and os.path.normcase(os.path.normpath(str(proc.exe() or ""))) != os.path.normcase(
            os.path.normpath(exe)
        ):
            return False
        return True
    except Exception:  # noqa: BLE001 - 进程在检查期间退出或拒绝访问都当作不可重试
        return False

def rows_from_report(report, op_type: str = "") -> list[ResultRow]:
    """把一个 report 的 items 摊成统一的行。

    op_type 用来判断「重试时该项还在不在」—— 已经没了的项不给重试按钮，
    因为没有意义，点了也是白点。
    """
    rows: list[ResultRow] = []
    for item in getattr(report, "items", []) or []:
        name = str(getattr(item, "name", "") or getattr(item, "display", "") or "")
        path = str(getattr(item, "path", "") or getattr(item, "human_path", "") or
                   getattr(item, "identity", "") or "")
        ok = bool(getattr(item, "ok", False))
        error = getattr(item, "error", None)
        size = getattr(item, "size_bytes", None)
        if size is None:
            size = getattr(item, "size", None)
        row = ResultRow(
            name=name or path or "（未命名）",
            path=path,
            status=STATUS_OK if ok else STATUS_FAIL,
            error=str(error or ""),
            size=size if isinstance(size, int) and size > 0 else None,
            create_time=float(getattr(item, "create_time", 0.0) or 0.0),
            exe=str(getattr(item, "exe", "") or ""),
        )
        if not ok:
            row.category = classify(row.error)
            row.retryable = _still_there(op_type, item, path)
        rows.append(row)
    return rows

def _still_there(op_type: str, item, path: str) -> bool:
    if op_type == "delete":
        return _exists_file(path)
    if op_type == "clean":
        return _exists_registry(path)
    if op_type == "kill":
        return _exists_process(
            getattr(item, "pid", "") or path,
            getattr(item, "create_time", 0.0),
            getattr(item, "exe", ""),
        )
    if op_type == "uninstall":
        # 软件还在不在没法用一次轻量调用确认，交给重试时再判断
        return True
    return _exists_file(path)

def summarize(rows) -> dict:
    """给报告头部用的一句统计。"""
    rows = list(rows)
    return {
        "total": len(rows),
        "ok": sum(1 for row in rows if row.status == STATUS_OK),
        "fail": sum(1 for row in rows if row.status == STATUS_FAIL),
        "skipped": sum(1 for row in rows if row.status == STATUS_SKIPPED),
        "retryable": sum(1 for row in rows if row.retryable),
    }

def skipped_rows(items) -> list[ResultRow]:
    """「仅执行普通项」时被暂缓的红色项：单列出来，不混进失败里。"""
    return [
        ResultRow(
            name=str(getattr(item, "name", "") or getattr(item, "display", "") or ""),
            path=str(getattr(item, "path", "") or getattr(item, "human_path", "") or
                     getattr(item, "identity", "") or ""),
            status=STATUS_SKIPPED,
            error="你选了「仅执行普通项」，这项先放着",
        )
        for item in items
    ]
