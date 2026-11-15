"""注册表来源：四个 WOW64 视图里的卸载登记。"""
from __future__ import annotations

import re
import winreg

from core.providers.base import ProviderResult, RawItem

UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

_GUID_KEY = re.compile(r"^\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$")

# (标签, 根键, WOW64 视图)。64 位视图优先，32 位视图单独再扫一遍。
SOURCES = (
    ("HKLM64", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
    ("HKLM32", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    ("HKCU64", winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY),
    ("HKCU32", winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_32KEY),
)

_FIELDS = ("DisplayName", "DisplayVersion", "Publisher", "InstallLocation",
           "InstallDate", "EstimatedSize", "UninstallString",
           "QuietUninstallString", "SystemComponent", "DisplayIcon")


def _read_value(key, name: str) -> str:
    try:
        value, _kind = winreg.QueryValueEx(key, name)
        return str(value or "").strip()
    except OSError:
        return ""


def collect() -> ProviderResult:
    """扫四个视图。读不到某个视图只记一句，不影响其它。"""
    result = ProviderResult(source="registry")

    for label, hive, wow in SOURCES:
        try:
            root = winreg.OpenKey(hive, UNINSTALL_KEY, 0, winreg.KEY_READ | wow)
        except OSError:
            result.warnings.append(f"{label} 这个位置读不到，可能没有这个视图")
            continue
        with root:
            index = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, sub_name, 0, winreg.KEY_READ | wow) as sub:
                        values = {field_name: _read_value(sub, field_name) for field_name in _FIELDS}
                except OSError:
                    continue
                name = values.get("DisplayName", "").strip()
                if not name:
                    continue          # 空名的是别的组件的子项，不是独立软件
                result.items.append(RawItem(
                    name=name,
                    version=values.get("DisplayVersion", ""),
                    publisher=values.get("Publisher", ""),
                    install_date=values.get("InstallDate", ""),
                    size_bytes=_size_of(values.get("EstimatedSize", "")),
                    install_location=values.get("InstallLocation", ""),
                    uninstall_string=values.get("UninstallString", ""),
                    quiet_uninstall_string=values.get("QuietUninstallString", ""),
                    key_path=f"{label}\\{UNINSTALL_KEY}\\{sub_name}",
                    hive=label,
                    source="registry",
                    hidden=_is_hidden(values),
                    display_icon=values.get("DisplayIcon", ""),
                    # 键名本身就是产品码的，即使没写 UninstallString 也能卸
                    product_code=sub_name if _GUID_KEY.match(sub_name) else "",
                ))
    return result


def _is_hidden(values: dict) -> bool:
    """系统组件 / 不显示 的条目 —— 咱们照样列出来，只是标一下。"""
    if str(values.get("SystemComponent", "")).strip() in ("1", "True", "true"):
        return True
    if str(values.get("NoDisplay", "")).strip() in ("1", "True", "true"):
        return True
    return False


def _size_of(raw: str) -> int | None:
    """EstimatedSize 存的是 KB，要乘 1024。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value * 1024 if value > 0 else None
