"""导出当前列表为 CSV。

两条底线：
1. **只导当前可见的**（过滤之后剩下的那些），不导被藏起来的
2. **只导元数据**：名称、路径、作用、风险、大小。不含文件内容、注册表键值、命令行
"""
from __future__ import annotations

import csv
import time
from pathlib import Path

from core.fs_browser import human_size
from app_paths import to_long

EXPORT_DIR_NAME = "exports"
MAX_COUNT = 100
MAX_BYTES = 20 * 1024 * 1024


def export_dir() -> Path:
    from app_paths import resolve

    return resolve().logs / EXPORT_DIR_NAME


def default_name(module: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = {"files": "文件", "software": "软件", "registry": "注册表", "process": "进程"}.get(module, module)
    return f"{label}-{stamp}.csv"


def write_csv(path: str | Path, headers, rows) -> tuple[bool, str]:
    """写 UTF-8 with BOM —— 不带 BOM 的话 Excel 打开中文是乱码。"""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(to_long(target), "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(list(headers))
            for row in rows:
                writer.writerow(list(row))
    except (OSError, ValueError) as exc:
        return False, f"写不进去：{exc}"
    return True, str(target)


def usage() -> tuple[int, int]:
    directory = export_dir()
    if not directory.exists():
        return 0, 0
    total = 0
    count = 0
    for path in directory.glob("*.csv"):
        try:
            total += path.stat().st_size
            count += 1
        except OSError:
            continue
    return total, count


def clear() -> tuple[int, int]:
    directory = export_dir()
    if not directory.exists():
        return 0, 0
    removed = 0
    freed = 0
    for path in directory.glob("*.csv"):
        try:
            freed += path.stat().st_size
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed, freed


def prune() -> None:
    """导出的文件不该无限堆着：留最近 100 份、总量 20 MB 以内，先删最旧的。"""
    directory = export_dir()
    if not directory.exists():
        return
    entries = []
    for path in directory.glob("*.csv"):
        try:
            entries.append((path, path.stat().st_size, path.stat().st_mtime))
        except OSError:
            continue
    entries.sort(key=lambda item: item[2])
    total = sum(size for _p, size, _m in entries)
    count = len(entries)
    while entries and (count > MAX_COUNT or total > MAX_BYTES):
        path, size, _mtime = entries.pop(0)
        try:
            path.unlink()
        except OSError:
            continue
        count -= 1
        total -= size


def size_text(value) -> str:
    """大小列统一成人话；算不出来就留空，不编 0。"""
    if isinstance(value, int) and value > 0:
        return human_size(value)
    return ""
