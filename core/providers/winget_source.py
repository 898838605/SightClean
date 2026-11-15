"""winget 来源：Windows 包管理器管着的那批。

传统 exe 装在注册表里有登记，winget 装的包不一定 —— 补上这块。

**解析难点**：winget list 输出的是「按显示宽度对齐」的表格，中文表头里
每个汉字占 2 个字符宽度，但字符串索引只算 1。所以不能按字符位置切，
得先把「显示宽度」算出来再对齐。

输出格式随 winget 版本变化，解析失败只记一条警告，不影响其它来源。
"""
from __future__ import annotations

import subprocess
import unicodedata

from core.providers.base import ProviderResult, RawItem

WINGET_TIMEOUT = 60


def _width(char: str) -> int:
    """一个字符占几个显示宽度。汉字/全角算 2。"""
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def available() -> bool:
    try:
        completed = subprocess.run(["winget", "--version"], capture_output=True, text=True, timeout=15)
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def collect() -> ProviderResult:
    result = ProviderResult(source="winget")
    try:
        completed = subprocess.run(
            ["winget", "list", "--accept-source-agreements", "--disable-interactivity"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=WINGET_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result.warnings.append(f"调用 winget 失败：{exc}")
        return result
    if completed.returncode != 0:
        result.warnings.append("winget 返回了错误，可能没装或版本太老")
        return result

    lines = (completed.stdout or "").splitlines()
    if len(lines) < 3:
        result.warnings.append("winget 没有输出内容")
        return result

    sep_index = -1
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and set(stripped) == {"-"} and len(stripped) > 20:
            sep_index = index
            break
    if sep_index <= 0:
        result.warnings.append("winget 的输出格式认不出来（版本可能太新或太老）")
        return result

    starts = _column_starts(lines[sep_index - 1])
    if len(starts) < 3:
        result.warnings.append("winget 输出的列数不对，跳过")
        return result

    for line in lines[sep_index + 1:]:
        if not line.strip():
            continue
        cells = _split_by_width(line, starts)
        if len(cells) < 3 or not cells[0]:
            continue
        name, package_id, version = cells[0], cells[1], cells[2]
        if not package_id:
            continue
        result.items.append(RawItem(
            name=name,
            version=version,
            install_location="",
            uninstall_string="",           # 卸载走 winget uninstall，不用注册表那串
            key_path=f"winget:{package_id}",
            hive="winget",
            source="winget",
        ))
        result.items[-1].publisher = ""
    return result


def _column_starts(header: str) -> list[int]:
    """表头每列起点的**显示宽度**。"""
    starts: list[int] = []
    width = 0
    in_token = False
    for char in header:
        if char != " ":
            if not in_token:
                starts.append(width)
                in_token = True
        else:
            in_token = False
        width += _width(char)
    return starts


def _split_by_width(line: str, starts: list[int]) -> list[str]:
    """按显示宽度把一行切成几段。"""
    if not starts:
        return []
    parts: list[str] = []
    buffer: list[str] = []
    width = 0
    seg = 0
    for char in line:
        while seg + 1 < len(starts) and width >= starts[seg + 1]:
            parts.append("".join(buffer).strip())
            buffer = []
            seg += 1
        buffer.append(char)
        width += _width(char)
    parts.append("".join(buffer).strip())
    return parts
