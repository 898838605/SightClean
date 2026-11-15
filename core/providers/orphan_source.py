"""孤儿来源：Program Files 下那些在卸载列表里找不到登记的目录。

国产软件里常见「卸载只删登记、文件和进程全留着」，这类软件在
「应用和功能」和注册表里都看不见，只能靠目录扫出来。
"""
from __future__ import annotations

from core.leftover import KIND_FOLDER, scan_orphans
from core.providers.base import ProviderResult, RawItem


def collect() -> ProviderResult:
    result = ProviderResult(source="orphan")
    try:
        found = scan_orphans()
    except Exception as exc:  # noqa: BLE001
        result.warnings.append(f"扫描孤儿目录失败：{exc}")
        return result
    for leftover in found:
        if leftover.kind != KIND_FOLDER:
            continue
        result.items.append(RawItem(
            name=leftover.name or leftover.path,
            install_location=leftover.path,
            uninstall_string="",
            key_path=leftover.path,          # 拿目录路径当身份
            hive="(未登记)",
            source="orphan",
        ))
    return result
