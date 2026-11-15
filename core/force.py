"""强制卸载：不走卸载程序，直接删目录 + 删注册表。

这是 BCUninstaller 的看家本领 —— 那些卸载程序坏掉的、只删登记不删文件的、
根本没提供卸载命令的软件，靠这个清掉。

**它不讲情面**，所以规矩比普通卸载更严：

1. 只删两样东西：InstallLocation 指向的目录 + 这个软件自己的卸载登记键
2. 注册表先备份，备份失败一项都不动
3. 受保护的东西直接拒绝（系统目录、Windows 自己的键）
"""
from __future__ import annotations

import os
from pathlib import Path

from core.leftover import (
    KIND_FOLDER,
    KIND_REGISTRY,
    CleanReport,
    Leftover,
    clean,
)

# 这些位置绝不「强制卸载」
_PROTECTED_DIRS = (
    r"C:\windows",
    r"C:\program files",
    r"C:\program files (x86)",
    r"C:\programdata",
    r"C:\users",
    r"C:\users\public",
)

# 这些注册表位置绝不「强制卸载」
_PROTECTED_KEYS = (
    r"hkey_local_machine\software",
    r"hkey_local_machine\system",
    r"hkey_current_user\software",
    r"hkey_current_user\software\microsoft",
    r"hkey_local_machine\software\microsoft",
)


def _norm(path: str) -> str:
    return os.path.normcase(str(path or "").rstrip("\\/"))


def _to_full_key(key_path: str) -> str:
    """把 HKLM64\SOFTWARE\... 这种内部分类转成注册表全名。"""
    text = str(key_path or "").strip()
    for short, full in (
        ("HKLM64", "HKEY_LOCAL_MACHINE"),
        ("HKLM32", "HKEY_LOCAL_MACHINE"),
        ("HKCU64", "HKEY_CURRENT_USER"),
        ("HKCU32", "HKEY_CURRENT_USER"),
        ("HKLM", "HKEY_LOCAL_MACHINE"),
        ("HKCU", "HKEY_CURRENT_USER"),
    ):
        if text.upper().startswith(short + "\\"):
            return full + text[len(short):]
    return text


def is_protected(item) -> tuple[bool, str]:
    """能不能对这项做强制卸载。返回 (能不能, 原因)。"""
    from core.uninstall import looks_system

    if looks_system(item.name, item.publisher):
        return False, "这是运行库 / 驱动 / 系统组件一类的，强删会连累别的软件"

    location = _norm(item.install_location)
    if location:
        for guarded in _PROTECTED_DIRS:
            if location == _norm(guarded):
                return False, f"「{item.install_location}」本身是系统目录，不能整个删"

    key = _to_full_key(item.key_path).lower()
    if key.startswith("hkey_"):
        for guarded in _PROTECTED_KEYS:
            if key.rstrip("\\") == guarded:
                return False, "这个键是系统/厂商的容器，不能整个删"

    if not location and not key.startswith("hkey_"):
        return False, "既没有安装目录，也没有注册表键，没东西可删"
    return True, ""


def build_targets(item) -> list[Leftover]:
    """列出强制卸载要动的东西。"""
    targets: list[Leftover] = []

    location = str(item.install_location or "").strip()
    if location and os.path.isdir(location):
        targets.append(Leftover(
            KIND_FOLDER, "安装目录", location, name=item.name,
            reason=f"强制卸载：删掉「{item.name}」的整个安装目录",
        ))

    key = _to_full_key(item.key_path)
    if key.lower().startswith("hkey_"):
        targets.append(Leftover(
            KIND_REGISTRY, "卸载登记", key, name=item.name,
            reason=f"强制卸载：删掉「{item.name}」的卸载登记键",
        ))
    return targets


def force_uninstall(item, log_dir: Path | str | None = None, progress=None) -> CleanReport:
    """强制卸载。删目录（进回收站）+ 删注册表键（先备份）。"""
    allowed, reason = is_protected(item)
    if not allowed:
        from core.leftover import CleanResult

        report = CleanReport(items=[])
        report.items.append(CleanResult(
            Leftover(KIND_FOLDER, "强制卸载", item.install_location or item.key_path,
                     name=item.name), False, reason,
        ))
        return report

    targets = build_targets(item)
    if not targets:
        from core.leftover import CleanResult

        report = CleanReport(items=[])
        report.items.append(CleanResult(
            Leftover(KIND_FOLDER, "强制卸载", item.name, name=item.name),
            False, "没找到可删的东西（目录和注册表都不在）",
        ))
        return report
    return clean(targets, log_dir, progress=progress)
