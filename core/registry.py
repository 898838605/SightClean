"""注册表浏览、备份、删除、还原。

几条不能破的规矩：
1. **懒加载**：进哪一级就只枚举哪一级的子键，绝不预先扫整棵树。
2. **删之前先备份**：整批导出成 .reg，备份失败就地中止，绝不在没有退路的情况下动手。
3. **只删空的键**：下面还有子键就拒绝 —— 绝不递归带走用户没勾选的东西。
4. 权限不够就如实说「打不开」，不尝试提权绕过。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app_paths import to_long

# (完整根名, 缩写, 根键句柄)
ROOTS = (
    ("HKEY_LOCAL_MACHINE", "HKLM", winreg.HKEY_LOCAL_MACHINE),
    ("HKEY_CURRENT_USER", "HKCU", winreg.HKEY_CURRENT_USER),
    ("HKEY_CLASSES_ROOT", "HKCR", winreg.HKEY_CLASSES_ROOT),
    ("HKEY_USERS", "HKU", winreg.HKEY_USERS),
    ("HKEY_CURRENT_CONFIG", "HKCC", winreg.HKEY_CURRENT_CONFIG),
)
BACKUP_DIR_NAME = "backups"
LOG_PREFIX = "注册表清理-"
LOG_SUFFIX = ".txt"
REG_HEADER = "Windows Registry Editor Version 5.00"

# 这些位一旦删掉，系统基本就起不来了。只是用来标红，不拦着用户
CRITICAL_HINTS = (
    "\\system", "\\sam", "\\security", "\\bcd00000000", "controlset", "\\services",
)

ERR_NO_PERMISSION = "没有权限动这个键（可能需要管理员）"
ERR_HAS_CHILDREN = "这个键下面还有子键，先去里面处理掉"
ERR_NOT_FOUND = "这个键已经不在了"
ERR_BACKUP_FAILED = "备份没成功，这次一步都没动"

class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_advapi32.RegQueryInfoKeyW.argtypes = [
    wintypes.HKEY, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(FILETIME),
]
_advapi32.RegQueryInfoKeyW.restype = wintypes.LONG

def _last_write_text(key) -> str:
    """取键的最后修改时间。winreg 不直接给，得走 RegQueryInfoKeyW。"""
    try:
        stamp = FILETIME()
        code = _advapi32.RegQueryInfoKeyW(
            wintypes.HKEY(key.handle), None, None, None,
            None, None, None, None, None, None, None, ctypes.byref(stamp),
        )
        if code != 0:
            return "—"
        ticks = (stamp.dwHighDateTime << 32) | stamp.dwLowDateTime
        if not ticks:
            return "—"
        seconds = ticks / 10_000_000 - 11644473600
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))
    except (OSError, ValueError, OverflowError, AttributeError):
        return "—"

@dataclass(frozen=True)
class RegistryKey:
    full_path: str        # HKEY_LOCAL_MACHINE\SOFTWARE\...（reg export 要用全名）
    short_root: str       # HKLM
    name: str             # 最后一段
    subkey_count: int = 0
    value_count: int = 0
    last_write: str = "—"
    locked: bool = False  # 读不到（权限不足）
    lock_reason: str = ""  # 为什么读不到（有些键只给 SYSTEM）

    @property
    def identity(self) -> str:
        return self.full_path

    @property
    def detail(self) -> str:
        if self.locked:
            return self.lock_reason or "读不到"
        return f"{self.subkey_count} 子键 · {self.value_count} 值"

    @property
    def type_label(self) -> str:
        if self.locked:
            return "打不开"
        if not self.subkey_count and not self.value_count:
            return "空键"
        if self.subkey_count:
            return f"键 · {self.subkey_count} 子键"
        return "键"

def is_critical(key_path: str) -> bool:
    """系统关键位。只用来标红，不阻止用户勾选。"""
    lowered = str(key_path or "").lower()
    return any(hint in lowered for hint in CRITICAL_HINTS)

def _split(full_path: str) -> tuple[int, str, str]:
    """HKEY_LOCAL_MACHINE\SOFTWARE\X → (句柄, HKEY_LOCAL_MACHINE, SOFTWARE\X)"""
    text = str(full_path or "").strip()
    for full_name, short_name, hive in ROOTS:
        if text.lower().startswith(full_name.lower()):
            rest = text[len(full_name):].lstrip("\\")
            return hive, full_name, rest
        if text.lower().startswith(short_name.lower()):
            rest = text[len(short_name):].lstrip("\\")
            return hive, full_name, rest
    raise ValueError(f"认不出的注册表根：{full_path}")

def list_root_keys() -> list[RegistryKey]:
    """只列 5 个根，不进任何一层。"""
    keys = []
    for full_name, short_name, _hive in ROOTS:
        keys.append(RegistryKey(full_path=full_name, short_root=short_name, name=short_name,
                                subkey_count=0, value_count=0, last_write="—"))
    return keys

# 这几个键的 ACL 只授给 SYSTEM，管理员也打不开。
# 想读必须改所有权（SeTakeOwnershipPrivilege），那会破坏系统安全，我们不做。
SYSTEM_ONLY = ("security", "sam", "bcd00000000")


def _system_only(full_path: str) -> bool:
    lowered = str(full_path or "").lower().rstrip("\\")
    return any(lowered.endswith("\\" + name) or lowered == name for name in SYSTEM_ONLY)


def _lock_reason(full_path: str) -> str:
    if _system_only(full_path):
        return ("Windows 只允许 SYSTEM 账户读这个键，管理员也打不开。"
                "想读得改所有权，那会破坏系统安全，本程序不做")
    return "这个位置打不开（没有权限）"


def list_children(full_path: str) -> tuple[list[RegistryKey] | None, str | None]:
    """列一层子键。返回 (子键列表, 人话错误)。懒加载就靠这一个函数。"""
    try:
        hive, full_name, rest = _split(full_path)
    except ValueError:
        return None, "这个位置看不懂"
    try:
        parent = winreg.OpenKey(hive, rest, 0, winreg.KEY_READ)
    except PermissionError:
        return None, _lock_reason(full_path)
    except FileNotFoundError:
        return None, ERR_NOT_FOUND
    except OSError:
        return None, "这个位置打不开"

    children: list[RegistryKey] = []
    with parent:
        index = 0
        while True:
            try:
                sub_name = winreg.EnumKey(parent, index)
            except OSError:
                break
            index += 1
            child_full = f"{full_name}\\{rest}\\{sub_name}" if rest else f"{full_name}\\{sub_name}"
            try:
                with winreg.OpenKey(parent, sub_name, 0, winreg.KEY_READ) as child:
                    sub_count, value_count, _ = winreg.QueryInfoKey(child)
                    last_write = _last_write_text(child)
                children.append(RegistryKey(
                    full_path=child_full,
                    short_root=next(short for _f, short, _h in ROOTS if _f == full_name),
                    name=sub_name,
                    subkey_count=sub_count,
                    value_count=value_count,
                    last_write=last_write,
                ))
            except PermissionError:
                children.append(RegistryKey(
                    full_path=child_full,
                    short_root=next(short for _f, short, _h in ROOTS if _f == full_name),
                    name=sub_name,
                    locked=True,
                    lock_reason=_lock_reason(child_full),
                ))
            except OSError:
                continue
    children.sort(key=lambda item: item.name.lower())
    return children, None

@dataclass
class RegistryResult:
    identity: str
    name: str
    ok: bool
    error: str | None = None
    at: float = 0.0

def delete_key(full_path: str) -> RegistryResult:
    """删一个键。下面还有子键就拒绝 —— 不递归带走用户没勾选的东西。"""
    now = time.time()
    name = str(full_path).rsplit("\\", 1)[-1]
    try:
        hive, _full, rest = _split(full_path)
    except ValueError:
        return RegistryResult(full_path, name, False, "这个位置看不懂", now)
    if not rest:
        return RegistryResult(full_path, name, False, "根键不能删", now)
    try:
        with winreg.OpenKey(hive, rest, 0, winreg.KEY_READ) as key:
            sub_count, _values, _ = winreg.QueryInfoKey(key)
        if sub_count:
            return RegistryResult(full_path, name, False, ERR_HAS_CHILDREN, now)
        winreg.DeleteKey(hive, rest)
        return RegistryResult(full_path, name, True, None, now)
    except PermissionError:
        return RegistryResult(full_path, name, False, ERR_NO_PERMISSION, now)
    except FileNotFoundError:
        return RegistryResult(full_path, name, False, ERR_NOT_FOUND, now)
    except OSError:
        return RegistryResult(full_path, name, False, ERR_NO_PERMISSION, now)

def delete_key_tree(full_path: str) -> tuple[bool, str | None]:
    """连子键一起删掉整棵子树。

    **只给「卸载残留清理」用**：那里用户是明确勾了一整棵残留的键。
    注册表模块的快照清理仍然走 delete_key（只删空键），两边规矩不一样。
    """
    try:
        hive, _full, rest = _split(full_path)
    except ValueError:
        return False, "这个位置看不懂"
    if not rest:
        return False, "根键不能删"

    # 先确认顶层这个键在不在 —— 内部递归里 winerror 2 是「有特殊 ACL」，
    # 顶层 winerror 2 才是真的不在了，两者要分开报
    try:
        with winreg.OpenKey(hive, rest, 0, winreg.KEY_READ):
            pass
    except FileNotFoundError:
        return False, ERR_NOT_FOUND
    except OSError:
        pass      # 打不开不代表删不掉，继续试

    try:
        _delete_tree(hive, rest)
        return True, None
    except _TreeDeleteError as exc:
        return False, _diagnose(hive, exc.path, exc.original)
    except FileNotFoundError:
        return False, ERR_NOT_FOUND
    except PermissionError:
        return False, (ERR_NO_PERMISSION + "（先用「工具 → 以管理员身份重新启动」提权再试）")
    except OSError as exc:
        return False, f"删除失败：{exc.strerror or exc}"


class _TreeDeleteError(Exception):
    """删子树时卡在某一段。带上路径，才知道是哪一步不行。"""

    def __init__(self, path: str, original: OSError) -> None:
        super().__init__(str(original))
        self.path = path
        self.original = original


def _diagnose(hive, sub: str, exc: OSError) -> str:
    """把删除失败翻译成**真实原因**。

    Windows 给注册表用的错误码很含糊：winerror 5（拒绝访问）
    既可能是「真没权限」，也可能是「下面还有子键」。
    所以必须回头看一眼这个键现在的状态，才能说准。
    """
    code = getattr(exc, "winerror", 0) or getattr(exc, "errno", 0)
    leaf = str(sub).rsplit("\\", 1)[-1]

    # 先看它现在什么样：还在吗？能读吗？有几个子键？
    readable = False
    sub_count = -1
    try:
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as key:
            sub_count, _values, _ = winreg.QueryInfoKey(key)
            readable = True
    except FileNotFoundError:
        return ERR_NOT_FOUND
    except PermissionError:
        readable = False
    except OSError:
        readable = False

    if code == 5:
        if sub_count > 0:
            return (f"「{leaf}」下面还有 {sub_count} 个子键没删掉 "
                    "(其中某个被系统或别的账户保护着)")
        if readable:
            return (f"「{leaf}」能读却删不掉：这个键的权限里没给「删除」。"
                    "得先改这个键的权限所有者")
        return (f"「{leaf}」读不到也删不掉：对你的账户设了拒绝访问。"
                "这类键通常归 SYSTEM 或受保护的组件所有")
    if code == 87:
        return f"「{leaf}」名字里有系统不认的字符"
    if code == 1018:
        return f"「{leaf}」已经被标记删除了，等系统回收即可"
    if code == 1450:
        return "系统资源不够，稍后再试（通常是有程序在疯狂读写注册表）"
    return f"删不掉「{leaf}」：{exc.strerror or exc}（错误码 {code}）"


def _delete_tree(hive, sub: str) -> None:
    """先枚举、再递归、最后删自己 —— 不能边枚举边删。

    打不开的子键照样要试删：有些键对当前账户设了拒绝读的 ACL，
    OpenKey 报的是「找不到」（winerror 2），但它本身可能是空的、能删掉。
    直接放弃就会把一个本来删得掉的键报成失败。
    """
    children: list[str] = []
    try:
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    children.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
    except OSError:
        children = []
    for child in reversed(children):
        try:
            _delete_tree(hive, f"{sub}\\{child}")
        except _TreeDeleteError:
            raise
        except OSError as exc:
            raise _TreeDeleteError(f"{sub}\\{child}", exc) from exc
    try:
        winreg.DeleteKey(hive, sub)
    except OSError as exc:
        raise _TreeDeleteError(sub, exc) from exc


def backup_dir() -> Path:
    from app_paths import resolve

    return resolve().logs.parent / BACKUP_DIR_NAME

def export_keys(paths: Sequence[str], target: Path | None = None) -> tuple[Path | None, str | None]:
    """把选中的键导出成一份 .reg。任何一个失败都算整批失败。"""
    paths = [str(path) for path in paths if path]
    if not paths:
        return None, "没有要备份的键"
    directory = backup_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None, "备份目录建不出来"
    if target is None:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = directory / f"reg_{stamp}.reg"

    chunks: list[str] = [REG_HEADER, ""]
    for path in paths:
        with tempfile.NamedTemporaryFile(suffix=".reg", delete=False) as handle:
            temp_path = handle.name
        try:
            completed = subprocess.run(
                ["reg", "export", path, temp_path, "/y"],
                capture_output=True, text=True, timeout=60,
            )
            if completed.returncode != 0:
                return None, f"导出 {path} 失败"
            try:
                text = Path(to_long(temp_path)).read_text(encoding="utf-16")
            except (UnicodeError, OSError):
                text = Path(to_long(temp_path)).read_text(encoding="utf-8", errors="replace")
            body = text.splitlines()
            # 去掉 "Windows Registry Editor Version 5.00" 那行和紧随的空行
            if body and body[0].startswith("Windows Registry Editor"):
                body = body[2:] if len(body) > 1 and not body[1].strip() else body[1:]
            chunks.extend(body)
            chunks.append("")
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    try:
        Path(to_long(target)).write_text("\n".join(chunks) + "\n", encoding="utf-16")
    except OSError:
        return None, "备份文件写不进去"
    return target, None

def import_backup(reg_file: str | Path) -> tuple[bool, str]:
    """用 reg import 还原。需要写权限的键在普通权限下会失败，如实返回。"""
    path = str(reg_file)
    if not os.path.isfile(path):
        return False, "找不到这个备份文件"
    try:
        completed = subprocess.run(["reg", "import", path], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False, "还原没成功，可能是权限不够"
    if completed.returncode == 0:
        return True, "已经还原"
    return False, "还原没成功，这条备份里可能有需要管理员的键"

@dataclass
class RegistryReport:
    items: list[RegistryResult]
    started: float
    finished: float
    backup_path: Path | None = None
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
            return "这次没有选中要清理的键"
        parts = [f"清掉 {self.ok_count} 个键"]
        if self.fail_count:
            parts.append(f"{self.fail_count} 个没动")
        if self.backup_path:
            parts.append(f"备份在 {self.backup_path.name}")
        return "；".join(parts)

    def failure_reasons(self) -> list[tuple[str, str]]:
        return [(item.name, item.error or "原因不明") for item in self.items if not item.ok]

    def as_log_text(self) -> str:
        lines = [
            "SightClean · 注册表清理记录",
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started))}",
            f"备份：{self.backup_path or '（这次没有备份）'}",
            f"共 {len(self.items)} 项：成功 {self.ok_count}，失败 {self.fail_count}",
            "",
        ]
        for result in self.items:
            state = "成功" if result.ok else "失败"
            lines.append(f"{state} | {result.identity} | {result.error or '无'}")
        return "\n".join(lines) + "\n"

def write_log(report: RegistryReport, log_dir: Path) -> Path | None:
    try:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(report.finished or time.time()))
        path = directory / f"{LOG_PREFIX}{stamp}{LOG_SUFFIX}"
        Path(to_long(path)).write_text(report.as_log_text(), encoding="utf-8")
        return path
    except (OSError, ValueError):
        return None

def count_tree(full_path: str) -> int:
    """这个键下面一共有多少个子键（递归数）。确认框用它说话。"""
    try:
        hive, _full, rest = _split(full_path)
    except ValueError:
        return 0

    total = 0
    stack = [rest]
    while stack:
        current = stack.pop()
        try:
            with winreg.OpenKey(hive, current, 0, winreg.KEY_READ) as key:
                index = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    total += 1
                    stack.append(f"{current}\\{sub}")
                    if total > 5000:
                        return total
        except OSError:
            continue
    return total


def clean_keys(paths: Sequence[str], log_dir: Path) -> RegistryReport:
    """整批清理：先备份、再删，最后写记录。

    **连子键一起删**。用户勾的是一个「已卸载软件的残留键」，
    要求他先进去把子键一个个删光是没道理的；备份用的是 reg export，
    它本来就导出整棵子树，删了能原样还原。
    """
    started = time.time()
    paths = [str(path) for path in paths if path]
    report = RegistryReport(items=[], started=started, finished=started)
    if not paths:
        report.finished = time.time()
        return report

    backup_path, error = export_keys(paths)
    if backup_path is None:
        report.items = [RegistryResult(path, path.rsplit("\\", 1)[-1], False, error or ERR_BACKUP_FAILED, started)
                        for path in paths]
        report.finished = time.time()
        report.log_path = write_log(report, log_dir)
        return report

    report.backup_path = backup_path
    # 深度浅的先删：父键连子树一起走，后面再遇到它的子孙就直接算成功
    ordered = sorted(paths, key=lambda path: path.count("\\"))
    removed: list[str] = []
    for path in ordered:
        name = path.rsplit("\\", 1)[-1]
        lowered = path.lower()
        if any(lowered == done or lowered.startswith(done + "\\") for done in removed):
            # 这一项已经在删父键的时候一起带走了
            report.items.append(RegistryResult(path, name, True, None, time.time()))
            continue
        ok, error = delete_key_tree(path)
        if ok:
            removed.append(lowered)
        report.items.append(RegistryResult(path, name, ok, error, time.time()))
    report.finished = time.time()
    report.log_path = write_log(report, log_dir)
    return report

def make_worker(paths: Sequence[str], log_dir: Path):
    """返回一个可直接丢进 QThreadPool 的 QRunnable：先备份，再清理。"""
    from core.tasks import Worker

    class _Task(Worker):
        def work(self):
            total = len(paths)
            self.signals.progress.emit(0, total, "正在备份…")
            report = clean_keys(paths, log_dir)
            self.signals.progress.emit(total, total, "")
            return report

    return _Task()
