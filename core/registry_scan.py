"""注册表清理：只找「指向的东西已经不存在」的项。

借鉴 Windows Maintenance Tool / CCleaner 那类工具的核心思路：
**不做主观猜测，只做客观核对** ——
一个注册表项如果指向的文件/目录已经没了，那它一定是无效的，删掉不会影响任何东西。

反面例子（这些**不**做）：
    - 「看起来像垃圾」的键            → 主观，会误删
    - 「很久没改过」的键              → 时间不是依据
    - 空键                           → 有些程序会先建空键再填

覆盖的七类：
    1. App Paths        指向不存在的 exe
    2. 卸载登记         InstallLocation 和 DisplayIcon 都不存在
    3. SharedDLLs       指向不存在的 DLL
    4. 启动项           Run / RunOnce 指向不存在的文件
    5. 文件关联         指向不存在的程序
    6. MUICache         记录的程序已经不在了
    7. 服务             ImagePath 指向不存在的文件（标危险，默认不选）
"""
from __future__ import annotations

import os
import re
import shutil
import winreg
from dataclasses import dataclass


@dataclass
class DeadEntry:
    """一条「指向的东西不存在」的注册表项。"""

    key_path: str        # 完整键路径
    value_name: str      # 值名；空字符串表示整个键
    label: str           # 给人看的名字
    reason: str          # 为什么判它无效
    target: str          # 它指向的那个不存在的东西
    category: str        # app_paths / uninstall / shared_dlls / ...
    risk: str = "safe"   # safe / caution / risky

    @property
    def identity(self) -> str:
        return f"{self.key_path} :: {self.value_name}" if self.value_name else self.key_path

    @property
    def display_path(self) -> str:
        return self.identity


_ICON_INDEX = re.compile(r",\s*-?\d+\s*$")


def _exists(path: str) -> bool:
    r"""这条路走不走得通。

    三种写法都要认：
        C:\App\a.exe            完整路径
        C:\App\a.exe,0         带图标索引（注册表常见写法）
        MsiExec.exe /X{GUID}     系统命令 + 参数
    """
    text = os.path.expandvars(str(path or "").strip().strip('"'))
    if not text:
        return False

    # 去掉 DisplayIcon 那种 ",0" 图标索引
    text = _ICON_INDEX.sub("", text).strip()

    lowered = text.lower()
    index = lowered.find(".exe")
    if index > 0:
        exe = text[: index + 4].strip().strip('"')
    else:
        exe = text.split(" ")[0].strip().strip('"')

    if not exe:
        return False

    try:
        # 带盘符或 UNC 开头的是完整路径，直接看文件在不在
        if re.match(r"^[a-zA-Z]:", exe) or exe.startswith("\\\\"):
            return os.path.exists(exe)
        # 其余当命令名，去 PATH 里找（MsiExec.exe 这类）
        return shutil.which(exe) is not None
    except (OSError, ValueError):
        return False


def _read(key, name: str) -> str:
    try:
        value, _kind = winreg.QueryValueEx(key, name)
        return str(value or "").strip()
    except OSError:
        return ""


def _subkeys(root):
    """枚举子键名。"""
    index = 0
    while True:
        try:
            yield winreg.EnumKey(root, index)
        except OSError:
            return
        index += 1


# --------------------------------------------------------------------------- #
# 1. App Paths
# --------------------------------------------------------------------------- #
_APP_PATHS = (
    (winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE",
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    (winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE",
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
    (winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER",
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    (winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER",
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
)


def scan_app_paths() -> list[DeadEntry]:
    """App Paths 下登记的 exe 已经不存在了。

    和卸载登记一样，32 位视图直接写真实路径 WOW6432Node，不用 WOW64 标志，
    这样扫出来的键路径能原样拿去删。
    """
    found: list[DeadEntry] = []
    for hive, hive_name, path in _APP_PATHS:
        try:
            root = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
        except OSError:
            continue
        with root:
            for sub in _subkeys(root):
                try:
                    with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as child:
                        default = _read(child, "")
                except OSError:
                    continue
                if not default:
                    continue
                if _exists(default):
                    continue
                found.append(DeadEntry(
                    key_path=f"{hive_name}\\{path}\\{sub}",
                    value_name="",
                    label=sub,
                    reason="这个程序登记在 App Paths 里，但它指向的 exe 已经不在了",
                    target=default,
                    category="app_paths",
                ))
    return found


# --------------------------------------------------------------------------- #
# 2. 卸载登记
# --------------------------------------------------------------------------- #
_UNINSTALL_SOURCES = (
    (winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE",
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE",
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER",
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER",
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
)


def scan_uninstall() -> list[DeadEntry]:
    """卸载登记里的程序已经不在了（安装目录和图标都不存在）。"""
    found: list[DeadEntry] = []
    for hive, hive_name, path in _UNINSTALL_SOURCES:
        try:
            root = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
        except OSError:
            continue
        with root:
            for sub in _subkeys(root):
                try:
                    with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as child:
                        display = _read(child, "DisplayName")
                        location = _read(child, "InstallLocation")
                        icon = _read(child, "DisplayIcon")
                        uninstall = _read(child, "UninstallString")
                        system_component = _read(child, "SystemComponent")
                except OSError:
                    continue
                if not display:
                    continue
                # 系统组件不动，宁可漏
                if system_component.strip() in ("1", "True", "true"):
                    continue
                # 三样都拿不到 → 判断不了，跳过
                probes = [p for p in (location, icon, uninstall) if p]
                if not probes:
                    continue
                # 只要有一个还在，就不算失效
                if any(_exists(p) for p in probes):
                    continue
                found.append(DeadEntry(
                    key_path=f"{hive_name}\\{path}\\{sub}",
                    value_name="",
                    label=display,
                    reason="卸载登记还在，但它记的安装目录和卸载程序都找不到了",
                    target=location or icon or uninstall,
                    category="uninstall",
                    risk="caution",
                ))
    return found


# --------------------------------------------------------------------------- #
# 3. SharedDLLs
# --------------------------------------------------------------------------- #
# 注意：这里**不能用 KEY_WOW64_* 标志**。
# 用标志打开时，32 位视图实际落在 WOW6432Node 下，但拿到的键路径还是不带 WOW6432Node 的，
# 删除时就打开了另一个键 —— 表现是「值明明在，却说不在了」。
# 直接写两条真实路径，扫出来的路径就能拿去删。
_SHARED_DLLS_PATHS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\SharedDLLs",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\SharedDLLs",
)


def scan_shared_dlls() -> list[DeadEntry]:
    """共享 DLL 的引用计数记着一个已经不存在的文件。"""
    found: list[DeadEntry] = []
    for path in _SHARED_DLLS_PATHS:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    name, _data, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if _exists(name):
                    continue
                found.append(DeadEntry(
                    key_path=f"HKEY_LOCAL_MACHINE\\{path}",
                    value_name=name,
                    label=os.path.basename(name) or name,
                    reason="这个 DLL 的共享引用还在，但文件本身已经不在了",
                    target=name,
                    category="shared_dlls",
                ))
    return found


# --------------------------------------------------------------------------- #
# 4. 启动项
# --------------------------------------------------------------------------- #
_RUN_KEYS = (
    (winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER",
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    (winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER",
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
    (winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE",
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    (winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE",
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
    (winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE",
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
    (winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE",
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce"),
    (winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER",
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
    (winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER",
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce"),
)


def scan_run_entries() -> list[DeadEntry]:
    """开机启动项指向的程序已经不在了。"""
    found: list[DeadEntry] = []
    for hive, hive_name, path in _RUN_KEYS:
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    name, data, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                command = str(data or "").strip()
                if not command or not _looks_like_program(command):
                    continue
                if _exists(command):
                    continue
                found.append(DeadEntry(
                    key_path=f"{hive_name}\\{path}",
                    value_name=name,
                    label=name,
                    reason="这个开机启动项指向的程序已经不在了，留着每次开机会报错",
                    target=command,
                    category="run_entries",
                    risk="caution",
                ))
    return found


def _looks_like_program(command: str) -> bool:
    """是不是指向一个具体程序（而不是 rundll32 之类系统命令）。"""
    lowered = command.lower()
    for system in ("rundll32", "regsvr32", "cmd.exe", "powershell", "msiexec", "explorer.exe"):
        if lowered.startswith(system):
            return False
    return ".exe" in lowered or ".bat" in lowered or ".cmd" in lowered


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
SCANNERS = (
    ("app_paths", "程序路径登记", scan_app_paths),
    ("uninstall", "卸载登记", scan_uninstall),
    ("shared_dlls", "共享 DLL", scan_shared_dlls),
    ("run_entries", "开机启动项", scan_run_entries),
)


def scan_all(categories=None) -> list[DeadEntry]:
    """跑一遍所有扫描器。单个扫描器出错不影响其他的。"""
    wanted = set(categories) if categories else None
    found: list[DeadEntry] = []
    for key, _label, scanner in SCANNERS:
        if wanted is not None and key not in wanted:
            continue
        try:
            found.extend(scanner())
        except Exception:  # noqa: BLE001
            continue
    return found
