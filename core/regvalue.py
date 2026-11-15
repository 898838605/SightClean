"""注册表「单个值」的删除与备份。

AppCompatFlags 这类地方的残留是**值**不是键：
    [HKCU\...\AppCompatFlags\Layers]
    "C:\\App\\main.exe"="~ WIN7RTM"

删整个 Layers 键会连累别的软件的兼容性设置，所以只能删那一条值。
reg export 导的是整个键，对不上，得自己拼 .reg。
"""
from __future__ import annotations

import winreg
from pathlib import Path

from core.registry import _split, backup_dir

ERR_NOT_FOUND = "这一项已经不在了"
ERR_NO_PERMISSION = "没有权限改这个位置"


def split_value(display: str) -> tuple[str, str] | None:
    """把 'HIVE\\KEY :: VALUE' 拆成 (键全名, 值名)。不是这个格式返回 None。"""
    text = str(display or "")
    if " :: " not in text:
        return None
    key_path, value_name = text.split(" :: ", 1)
    return key_path.strip(), value_name.strip()


def _open_for_write(key_path: str):
    hive, _full, rest = _split(key_path)
    return hive, rest


def delete_value(display: str) -> tuple[bool, str | None]:
    """删掉一个注册表值。

    顺序很重要：**先确认值还在不在**，再看有没有权限。
    反过来的话，一个根本不存在的值会被报成「没权限」，纯属误导。
    """
    parts = split_value(display)
    if parts is None:
        return False, "路径格式不对"
    key_path, value_name = parts
    try:
        hive, rest = _open_for_write(key_path)
    except ValueError:
        return False, "这个位置看不懂"

    if read_value(display) is None:
        return False, ERR_NOT_FOUND
    try:
        with winreg.OpenKey(hive, rest, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
        return True, None
    except FileNotFoundError:
        # Windows 对「没权限」的键有时报的也是「找不到」（用来隐藏键的存在）。
        # 回头用只读权限探一次，才能分清到底是哪一种。
        try:
            with winreg.OpenKey(hive, rest, 0, winreg.KEY_READ):
                pass
        except FileNotFoundError:
            return False, ERR_NOT_FOUND
        except PermissionError:
            return False, ERR_NO_PERMISSION
        except OSError:
            return False, ERR_NO_PERMISSION
        # 键能读、但不能写 —— 就是权限问题
        return False, ERR_NO_PERMISSION
    except PermissionError:
        return False, ERR_NO_PERMISSION
    except OSError as exc:
        code = getattr(exc, "winerror", 0)
        if code in (5, 448, 1018):
            return False, ERR_NO_PERMISSION
        return False, f"删不掉这个值：{exc.strerror or exc}"


def read_value(display: str) -> tuple[object, int] | None:
    """读出 (数据, 类型)。读不到返回 None。"""
    parts = split_value(display)
    if parts is None:
        return None
    key_path, value_name = parts
    try:
        hive, rest = _open_for_write(key_path)
    except ValueError:
        return None
    try:
        with winreg.OpenKey(hive, rest, 0, winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, value_name)
    except OSError:
        return None


def _reg_escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def export_values(displays, target: Path | None = None) -> tuple[Path | None, str | None]:
    """把这些值导出成 .reg（用 reg import 能还原）。"""
    entries: list[tuple[str, str, object, int]] = []
    for display in displays:
        parts = split_value(display)
        if parts is None:
            continue
        data = read_value(display)
        if data is None:
            continue
        entries.append((parts[0], parts[1], data[0], data[1]))
    if not entries:
        # 一个都读不到也没关系：没东西可备份，就当备份成功（空文件）
        entries = []

    if target is None:
        import time

        folder = backup_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"regvalue_{time.strftime('%Y%m%d_%H%M%S')}.reg"

    lines = ["Windows Registry Editor Version 5.00", ""]
    grouped: dict[str, list[tuple[str, object, int]]] = {}
    for key_path, value_name, data, kind in entries:
        grouped.setdefault(key_path, []).append((value_name, data, kind))
    for key_path, values in grouped.items():
        lines.append(f"[{key_path}]")
        for value_name, data, kind in values:
            lines.append(f'"{_reg_escape(value_name)}"={_format(data, kind)}')
        lines.append("")

    try:
        target.write_text("\r\n".join(lines), encoding="utf-16")
    except OSError as exc:
        return None, f"备份文件写不进去：{exc}"
    return target, None


def _format(data, kind: int) -> str:
    if kind == winreg.REG_DWORD:
        return f"dword:{int(data):08x}"
    if kind == winreg.REG_QWORD:
        return "hex(b):" + ",".join(f"{b:02x}" for b in int(data).to_bytes(8, "little"))
    if kind == winreg.REG_BINARY:
        return "hex:" + ",".join(f"{b:02x}" for b in bytes(data))
    text = str(data)
    if len(text.splitlines()) > 1:
        return "hex(7):" + ",".join(f"{b:02x}" for b in text.encode("utf-16-le"))
    return '"' + _reg_escape(text) + '"'
