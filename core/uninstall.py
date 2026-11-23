"""已安装软件的枚举与卸载调用。

这个模块只做两件事：
1. 从注册表把「装了什么」列出来（只读，不写）；
2. 把用户勾选的那几项，交给软件**自己的**卸载程序去执行。

它绝不自己删文件、绝不自己删注册表 —— 那是软件自带卸载程序的活儿。
绕开卸载程序直接删目录，只会留下满地残渣，还容易把系统搞坏。
"""
from __future__ import annotations

import re
import os
import shutil
import subprocess
import time
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from PySide6.QtCore import QObject, QRunnable, Signal

from app_paths import to_long

UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

# (标签, 根键, WOW64 视图)。64 位视图优先，32 位视图单独再扫一遍。
SOURCES = (
    ("HKLM64", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
    ("HKLM32", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    ("HKCU64", winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY),
    ("HKCU32", winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_32KEY),
)

LOG_PREFIX = "卸载记录-"
LOG_SUFFIX = ".txt"

# 名字里带这些的一律按「系统组件」标红：卸了会影响别的软件
SYSTEM_HINTS = (
    "visual c++", "redistributable", "vcredist", "runtime", ".net framework",
    ".net runtime", "directx", "driver", "驱动", "sdk", "运行库",
    "update for", "hotfix", "补丁",
)

ERR_NO_COMMAND = "这个软件没提供卸载命令，程序不会自己去删它的文件"
ERR_START_FAILED = "卸载程序没能启动"

@dataclass(frozen=True)
class SoftwareItem:
    name: str
    version: str
    publisher: str
    install_date: str
    size_bytes: int | None
    install_location: str
    uninstall_string: str
    quiet_uninstall_string: str
    key_path: str          # identity：注册表完整键路径 / 目录路径 / winget 包 id
    hive: str
    source: str = "registry"   # registry / winget / orphan
    installer_type: str = ""    # msi / nsis / inno / steam / winget / ...
    hidden: bool = False        # 系统默认不显示的（SystemComponent / NoDisplay）
    display_icon: str = ""      # DisplayIcon，通常是主 exe
    product_code: str = ""      # MSI 产品码（注册表键名就是 GUID 时能拿到）

    @property
    def command(self) -> str:
        """真正的卸载命令。

        三种情况要特殊处理：

        1. winget 包走 winget
        2. MsiExec.exe /I{GUID} 是「修改安装」，点了会弹「更改/修复」界面，
           得换成 /X 才是卸载
        3. 没有 UninstallString，但注册表键名就是产品码 —— 自己拼一条
        """
        if self.source == "winget":
            package_id = self.key_path.split(":", 1)[-1]
            if package_id:
                return f'winget uninstall --id "{package_id}" --accept-source-agreements'
            return ""

        raw = (self.uninstall_string or self.quiet_uninstall_string or "").strip()
        if raw:
            return _msi_uninstall_switch(raw)
        if self.msi_code:
            return f"MsiExec.exe /X{self.msi_code}"
        return ""

    @property
    def can_uninstall(self) -> bool:
        return bool(self.command)

    @property
    def msi_code(self) -> str:
        """MSI 产品码。优先用注册表键名里的，其次从卸载命令里抠。"""
        if self.product_code:
            return self.product_code
        match = re.search(
            r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}",
            self.uninstall_string or "")
        return match.group(0) if match else ""

    @property
    def exe_names(self) -> list[str]:
        """这个软件的**全部** exe 名（不带扩展名）。

        Prefetch / WER / 兼容性标记都是按每个 exe 各记一份的，
        所以不能只取 DisplayIcon 那一个 —— 一个软件常带好几个 exe。
        """
        from core.cleaners import exe_names as collect, exe_names_from_dir

        import os

        names = collect(self.display_icon, self.uninstall_string)
        # 很多软件的登记里 InstallLocation 是空的，但 DisplayIcon 指向安装目录
        folder = self.install_location or ""
        if not folder and self.display_icon:
            icon = self.display_icon.split(",")[0].strip().strip('"')
            if icon.lower().endswith(".exe"):
                folder = os.path.dirname(icon)
        for extra in exe_names_from_dir(folder):
            if extra.lower() not in [item.lower() for item in names]:
                names.append(extra)
        # 旧版本 exe 已经删了，但 UserAssist 记着它在哪跑过 —— 顺藤摸瓜
        try:
            from core.cleaners import scan_userassist

            for extra in scan_userassist(names, folder):
                if extra.lower() not in [item.lower() for item in names]:
                    names.append(extra)
        except Exception:  # noqa: BLE001
            pass
        return names

    @property
    def kind_label(self) -> str:
        from core.installers import label

        return label(self.installer_type or "unknown")

    @property
    def quiet_command(self) -> str:
        """能静默跑完的那条命令。拼不出来返回空串。"""
        from core.installers import quiet_command as build

        package_id = ""
        if self.source == "winget":
            package_id = self.key_path.split(":", 1)[-1]
        elif self.installer_type == "steam":
            package_id = self.steam_app_id
        return build(
            self.uninstall_string, self.install_location, self.source,
            self.product_code, self.quiet_uninstall_string, package_id,
        )

    @property
    def steam_app_id(self) -> str:
        r"""Steam 游戏的 appid，从安装路径里抠（...\steamapps\common\<名字> 推不出来，只能靠 manifest）。"""
        return ""

    @property
    def detail(self) -> str:
        bits = [self.version or "版本未知"]
        if self.publisher:
            bits.append(self.publisher)
        return " · ".join(bits)

@dataclass
class UninstallResult:
    key_path: str
    name: str
    ok: bool
    command: str = ""
    error: str | None = None
    at: float = 0.0

_MSI_MODIFY = re.compile(r"/I(\s*\{)", re.IGNORECASE)


def _msi_uninstall_switch(command: str) -> str:
    """把 MsiExec.exe /I{GUID} 换成 /X{GUID}。

    很多软件的注册表里写的是 /I —— 那是「修改安装」。
    照原样执行，用户看到的是「更改 / 修复 / 删除」三选一的界面，
    点了「删除」也能卸，但多一步，而且容易以为是没反应。
    """
    lowered = command.lower()
    if "msiexec" not in lowered:
        return command
    if "/x" in lowered:
        return command
    if not _MSI_MODIFY.search(command):
        return command
    return _MSI_MODIFY.sub(r"/X\1", command, count=1)


def looks_system(name: str, publisher: str = "", system_component: bool = False) -> bool:
    """看起来是不是「卸了会连累别人」的系统组件。只用来标红，不拦着用户。"""
    if system_component:
        return True
    text = f"{name} {publisher}".lower()
    if any(hint in text for hint in SYSTEM_HINTS):
        return True
    # KB 补丁单独判断，别把名字里恰好含 kb 的正常软件也算进去
    return name.strip().lower().startswith("kb")

def _read_value(key, name: str, default=""):
    try:
        value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return default
    return value

def _format_date(raw) -> str:
    text = str(raw or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text

def _build_item(values: dict, hive: str, key_path: str) -> SoftwareItem | None:
    """把一条注册表记录变成 SoftwareItem。抽成纯函数是为了能单测。"""
    name = str(values.get("DisplayName") or "").strip()
    if not name:
        return None
    version = str(values.get("DisplayVersion") or "").strip()
    publisher = str(values.get("Publisher") or "").strip()
    location = str(values.get("InstallLocation") or "").strip()
    size_raw = values.get("EstimatedSize")
    size_bytes = None
    try:
        if size_raw not in (None, ""):
            size_bytes = int(size_raw) * 1024       # 注册表里存的是 KB
    except (TypeError, ValueError):
        size_bytes = None
    return SoftwareItem(
        name=name,
        version=version,
        publisher=publisher,
        install_date=_format_date(values.get("InstallDate")),
        size_bytes=size_bytes,
        install_location=location,
        uninstall_string=str(values.get("UninstallString") or "").strip(),
        quiet_uninstall_string=str(values.get("QuietUninstallString") or "").strip(),
        key_path=key_path,
        hive=hive,
    )

def dedupe(items: Iterable[SoftwareItem]) -> list[SoftwareItem]:
    """按「显示名 + 版本」去重。

    同一份软件经常在 64 位和 32 位视图里各登记一次，不去重会看到两条一模一样的。
    """
    seen: dict[tuple[str, str], SoftwareItem] = {}
    for item in items:
        key = (item.name.lower(), (item.version or "").lower())
        existing = seen.get(key)
        if existing is None:
            seen[key] = item
            continue
        # 重复时优先留「有卸载命令」的那条
        if not existing.can_uninstall and item.can_uninstall:
            seen[key] = item
    result = list(seen.values())
    result.sort(key=lambda soft: soft.name.lower())
    return result

def enumerate_installed(include_winget: bool = True,
                        include_orphans: bool = True) -> tuple[list[SoftwareItem], list[str]]:
    """从各个 provider 收软件，合并去重后返回 (列表, 提示)。

    借鉴 WinClean 的分层做法：注册表、winget、孤儿目录各管各的，
    一个来源读失败只影响它自己。
    """
    from core.providers import collect_all

    merged = collect_all(include_winget=include_winget, include_orphans=include_orphans)
    items = [_from_raw(raw) for raw in merged.items]
    return dedupe(items), merged.warnings


def _from_raw(raw) -> SoftwareItem:
    """把 provider 的统一模型转成界面用的 SoftwareItem。"""
    return SoftwareItem(
        name=raw.name,
        version=raw.version,
        publisher=raw.publisher,
        install_date=_format_date(raw.install_date),
        size_bytes=raw.size_bytes,
        install_location=raw.install_location,
        uninstall_string=raw.uninstall_string,
        quiet_uninstall_string=raw.quiet_uninstall_string,
        key_path=raw.key_path,
        hive=raw.hive,
        source=raw.source,
        installer_type=_detect_installer(raw),
        hidden=bool(getattr(raw, "hidden", False)),
        display_icon=getattr(raw, "display_icon", "") or "",
        product_code=getattr(raw, "product_code", "") or "",
    )


def _detect_installer(raw) -> str:
    from core.installers import detect

    return detect(raw.uninstall_string, raw.install_location, raw.source)

def start_uninstall(item: SoftwareItem) -> UninstallResult:
    """把卸载程序调起来。不等它结束 —— 大多数卸载程序都有自己的界面。"""
    now = time.time()
    command = item.command
    if not command:
        return UninstallResult(item.key_path, item.name, False, "", ERR_NO_COMMAND, now)
    if not _command_is_available(command, item.install_location):
        return UninstallResult(item.key_path, item.name, False, command, ERR_START_FAILED, now)
    try:
        # 注册表里的卸载命令是「一整行带引号的命令行」，只能交给 shell 去解析。
        # 内容来自本机注册表，不是外部输入。
        subprocess.Popen(command, shell=True)  # noqa: S602
    except (OSError, subprocess.SubprocessError):
        return UninstallResult(item.key_path, item.name, False, command, ERR_START_FAILED, now)
    return UninstallResult(item.key_path, item.name, True, command, None, now)


def _command_is_available(command: str, install_location: str = "") -> bool:
    """Check the program portion before handing the command line to cmd.exe."""
    from core.installers import _exe_of

    text = str(command or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith(("steam://", "http://", "https://")):
        return True
    executable = os.path.expandvars(_exe_of(text).strip().strip('"'))
    if not executable:
        return False
    if re.match(r"^[a-zA-Z]:", executable) or executable.startswith("\\\\"):
        return os.path.isfile(executable)
    if install_location:
        local = os.path.join(os.path.expandvars(install_location), executable)
        if os.path.isfile(local):
            return True
    return shutil.which(executable) is not None

@dataclass
class UninstallReport:
    items: list[UninstallResult]
    started: float
    finished: float
    log_path: Path | None = None

    @property
    def ok_count(self) -> int:
        return sum(1 for item in self.items if item.ok)

    @property
    def fail_count(self) -> int:
        return sum(1 for item in self.items if not item.ok)

    @property
    def summary(self) -> str:
        if not self.items:
            return "这次没有选中要卸载的软件"
        parts = [f"已交给卸载程序 {self.ok_count} 个"]
        if self.fail_count:
            parts.append(f"{self.fail_count} 个没起来")
        return "；".join(parts)

    def failure_reasons(self) -> list[tuple[str, str]]:
        return [(item.name, item.error or ERR_START_FAILED) for item in self.items if not item.ok]

    def as_log_text(self) -> str:
        lines = [
            "SightClean · 卸载记录",
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started))}",
            "方式：调用软件自带的卸载程序（本程序不删文件、不删注册表）",
            f"共 {len(self.items)} 项：已调起 {self.ok_count}，没起来 {self.fail_count}",
            "",
        ]
        for result in self.items:
            state = "已调起" if result.ok else "失败"
            lines.append(f"{state} | {result.name} | {result.error or '无'} | {result.command}")
        return "\n".join(lines) + "\n"

def write_log(report: UninstallReport, log_dir: Path) -> Path | None:
    try:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(report.finished or time.time()))
        path = directory / f"{LOG_PREFIX}{stamp}{LOG_SUFFIX}"
        Path(to_long(path)).write_text(report.as_log_text(), encoding="utf-8")
        return path
    except (OSError, ValueError):
        return None

class UninstallWorker(QRunnable):
    """逐个把卸载程序调起来，一边调一边报进度，最后交一份记录。

    注意：这里的「成功」= 卸载程序被成功拉起，不是「软件已经卸干净」。
    卸载是软件自己的程序在干活，我们只负责把它叫醒。
    """

    class Signals(QObject):
        progress = Signal(int, int, str)   # 已完成, 总数, 当前软件名
        finished = Signal(object)          # UninstallReport

    def __init__(self, items: Sequence[SoftwareItem], log_dir: Path) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.signals = UninstallWorker.Signals()
        self.items = list(items)
        self.log_dir = Path(log_dir)

    def run(self) -> None:  # noqa: D102
        started = time.time()
        total = len(self.items)
        results: list[UninstallResult] = []
        for index, item in enumerate(self.items):
            try:
                self.signals.progress.emit(index, total, item.name)
            except RuntimeError:
                return
            results.append(start_uninstall(item))
        report = UninstallReport(items=results, started=started, finished=time.time())
        report.log_path = write_log(report, self.log_dir)
        try:
            self.signals.progress.emit(total, total, "")
            self.signals.finished.emit(report)
        except RuntimeError:
            pass
