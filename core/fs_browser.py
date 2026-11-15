"""磁盘与文件夹浏览：只列一层，绝不递归；慢的事情全放到后台。

界面只拿这里给的结果，不在这里碰任何窗口控件。
"""
from __future__ import annotations

import concurrent.futures
import ctypes
import os
import queue
import stat
import threading
from ctypes import wintypes
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from app_paths import to_human, to_long


# --------------------------------------------------------------------------- #
# 数据结构（字段顺序与界面契约一致，不要调整）
# --------------------------------------------------------------------------- #
@dataclass
class FsEntry:
    human_path: str
    long_path: str
    name: str
    is_dir: bool
    size: int | None          # 文件=字节数；文件夹=None（等 core/size_calc 回填）
    mtime: float              # 秒级时间戳，取不到就 0.0
    kind_label: str           # 中文类型名
    hidden: bool = False
    locked: bool = False      # 无权限/读不到 → 界面显示锁，不参与大小计算、不可勾选
    purpose: str = ""
    purpose_source: str = ""   # kb / ai / fallback / locked
    impact: str = ""           # AI 给的「删除影响」
    ai_note: str = ""          # AI 结果的提示（AI 判断，请确认）
    risk: str = "unknown"
    risk_reason: str = ""
    advice: str = "maybe"
    size_state: str = "none"  # none/queued/running/ok/partial/denied
    checked: bool = False
    deletable: bool = True    # 盘符这种「能进去看、但不能删」的条目标成 False


@dataclass
class DriveInfo:
    root: str                 # 形如 "D:\\"
    label: str
    kind: str                 # fixed/removable/remote/cdrom/ram/unknown
    state: str                # ready/probing/unavailable
    total: int | None = None
    free: int | None = None
    note: str = ""


# --------------------------------------------------------------------------- #
# 中文类型名
# --------------------------------------------------------------------------- #
_KIND_BY_EXT: dict[str, str] = {}
for _label, _exts in (
    ("文本文档", ("txt", "log", "md", "ini", "cfg", "conf", "csv", "tsv", "json", "xml", "yaml", "yml", "rtf", "nfo", "diz")),
    ("图片", ("jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "ico", "tif", "tiff", "heic", "avif", "raw", "psd")),
    ("音频", ("mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "ape", "mid", "midi", "opus")),
    ("视频", ("mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "rmvb", "mpg", "mpeg", "3gp", "ts")),
    ("压缩包", ("zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "cab", "lzh", "arj")),
    ("安装包", ("msi", "msix", "appx", "msu", "pkg")),
    ("程序", ("exe", "com", "scr")),
    ("快捷方式", ("lnk", "url")),
    ("网页", ("html", "htm", "xhtml", "mht", "mhtml")),
    ("字体", ("ttf", "otf", "ttc", "fon", "woff", "woff2")),
    ("数据库", ("db", "sqlite", "sqlite3", "mdb", "accdb", "db3", "edb", "dbf")),
    ("脚本", ("py", "pyw", "js", "mjs", "ps1", "bat", "cmd", "vbs", "vbe", "sh", "lua", "pl", "rb")),
    ("虚拟磁盘", ("vhd", "vhdx", "vmdk", "vdi", "qcow2", "iso", "img")),
    ("系统文件", ("sys", "dll", "drv", "cpl", "ocx", "mui", "efi", "cat", "inf", "manifest")),
    ("其他", ("bak", "old", "tmp", "temp", "part", "partial", "crdownload")),
):
    for _ext in _exts:
        _KIND_BY_EXT[_ext] = _label
del _label, _exts, _ext

_FILE_ATTRIBUTE_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def kind_of(path: str, is_dir: bool) -> str:
    """按名字猜一个中文类型名。文件夹直接给「文件夹」。"""
    if is_dir:
        return "文件夹"
    name = os.path.basename(os.path.normpath(path)) if path else ""
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    if not ext:
        return "文件"
    return _KIND_BY_EXT.get(ext, "文件")


def human_size(n: int | None, approx: bool = False) -> str:
    """字节数换成人话。None 给「—」，0 给「0 B」，KB 以上保留一位小数。"""
    if n is None:
        return "—"
    try:
        value = int(n)
    except (TypeError, ValueError):
        return "—"
    if value < 0:
        return "—"
    prefix = "≈" if approx else ""
    if value < 1024:
        return f"{prefix}{value} B"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    index = 0
    while size >= 1024.0 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    return f"{prefix}{size:.1f} {units[index]}"


# --------------------------------------------------------------------------- #
# Windows 磁盘信息
# --------------------------------------------------------------------------- #
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetLogicalDriveStringsW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
_kernel32.GetLogicalDriveStringsW.restype = wintypes.DWORD
_kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetDriveTypeW.restype = wintypes.UINT
_kernel32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPWSTR,
    wintypes.DWORD,
]
_kernel32.GetVolumeInformationW.restype = wintypes.BOOL
_kernel32.GetDiskFreeSpaceExW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong),
]
_kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL

_TYPE_BY_NUMBER = {2: "removable", 3: "fixed", 4: "remote", 5: "cdrom", 6: "ram"}
_TYPE_FALLBACK_LABEL = {
    "fixed": "本地磁盘",
    "removable": "可移动磁盘",
    "remote": "网络位置",
    "cdrom": "光驱",
    "ram": "内存盘",
    "unknown": "磁盘",
}
_UNREACHABLE_NOTE = "这个盘暂时连不上，可能是网络盘或者光驱"


def _logical_drives() -> list[str]:
    """取所有盘符，形如 ["C:\\", "D:\\"]。这一步不碰任何文件。"""
    buffer = ctypes.create_unicode_buffer(2048)
    length = _kernel32.GetLogicalDriveStringsW(2048, buffer)
    if not length:
        return []
    raw = buffer[:length]
    return [item for item in raw.split("\x00") if item]


def _drive_kind(root: str) -> str:
    try:
        number = int(_kernel32.GetDriveTypeW(root))
    except (OSError, ValueError):
        return "unknown"
    return _TYPE_BY_NUMBER.get(number, "unknown")


def _volume_label(root: str) -> str | None:
    """取卷标。失败返回 None，由调用方给中文兜底。"""
    name = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_length = wintypes.DWORD()
    flags = wintypes.DWORD()
    file_system = ctypes.create_unicode_buffer(261)
    ok = _kernel32.GetVolumeInformationW(
        root,
        name,
        261,
        ctypes.byref(serial),
        ctypes.byref(max_length),
        ctypes.byref(flags),
        file_system,
        261,
    )
    if not ok:
        return None
    return name.value or None


def _free_space(root: str) -> tuple[int, int] | None:
    """取 (总容量, 可用容量)，失败返回 None。"""
    available = ctypes.c_ulonglong()
    total = ctypes.c_ulonglong()
    total_free = ctypes.c_ulonglong()
    ok = _kernel32.GetDiskFreeSpaceExW(
        root,
        ctypes.byref(available),
        ctypes.byref(total),
        ctypes.byref(total_free),
    )
    if not ok:
        return None
    return int(total.value), int(available.value)


def _base_info(root: str) -> DriveInfo:
    kind = _drive_kind(root)
    label = _volume_label(root) or _TYPE_FALLBACK_LABEL.get(kind, "磁盘")
    info = DriveInfo(root=root, label=label, kind=kind, state="ready")
    if kind in ("fixed", "removable", "ram"):
        space = _free_space(root)
        if space is not None:
            info.total, info.free = space
    elif kind == "remote":
        info.note = "网络位置，容量要连上以后才知道"
    elif kind == "cdrom":
        info.note = "光驱里没有盘的时候打不开"
    return info


def list_drives_sync() -> list[DriveInfo]:
    """同步列出所有盘。只调几个 Windows 系统查询，不遍历任何文件夹。"""
    drives: list[DriveInfo] = []
    for root in _logical_drives():
        try:
            drives.append(_base_info(root))
        except OSError:
            drives.append(
                DriveInfo(
                    root=root,
                    label=_TYPE_FALLBACK_LABEL["unknown"],
                    kind="unknown",
                    state="unavailable",
                    note=_UNREACHABLE_NOTE,
                )
            )
    return drives


def _probe_slow_drive(info: DriveInfo) -> DriveInfo:
    """网络盘/光驱/未知盘的探测：卷标与容量任意一个成功就算连上了。"""
    label = _volume_label(info.root)
    space = _free_space(info.root)
    if label is None and space is None:
        return replace(info, state="unavailable", note=_UNREACHABLE_NOTE)
    updated = replace(
        info,
        state="ready",
        label=label or _TYPE_FALLBACK_LABEL.get(info.kind, "磁盘"),
        note="",
    )
    if space is not None:
        updated.total, updated.free = space
    return updated


def _probe_with_timeout(info: DriveInfo, timeout: float = 3.0) -> DriveInfo:
    """把可能卡住的系统查询放进可以随时放弃的后台等待里。

    系统查询本身没法中途打断，所以让它待在一个不拦着程序退出的后台任务里；
    这边最多等到 timeout 秒，到点就按「连不上」处理，绝不拖住界面。
    """
    box: "queue.Queue[DriveInfo]" = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            box.put(_probe_slow_drive(info), timeout=0.1)
        except Exception:
            return

    thread = threading.Thread(
        target=run,
        name=f"disk-probe-{info.root[:2]}",
        daemon=True,
    )
    thread.start()
    try:
        return box.get(timeout=timeout)
    except queue.Empty:
        return replace(info, state="unavailable", note=_UNREACHABLE_NOTE)


# --------------------------------------------------------------------------- #
# 列出文件夹（只一层）
# --------------------------------------------------------------------------- #
def _entry_from_scandir(entry: os.DirEntry[str]) -> FsEntry:
    """把一条目录项变成界面要的数据；单条失败只让这一条带锁，不影响整批。"""
    long_path = entry.path
    human_path = to_human(long_path)
    name = entry.name
    st: os.stat_result | None
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        st = None

    if st is not None:
        is_dir = stat.S_ISDIR(st.st_mode)
        hidden = bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_HIDDEN)
        locked = False
        size = None if is_dir else int(st.st_size)
        mtime = float(st.st_mtime)
    else:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        hidden = False
        locked = True
        size = None
        mtime = 0.0

    return FsEntry(
        human_path=human_path,
        long_path=long_path,
        name=name,
        is_dir=is_dir,
        size=size,
        mtime=mtime,
        kind_label=kind_of(human_path, is_dir),
        hidden=hidden,
        locked=locked,
    )


def list_dir(path: str, show_hidden: bool = False) -> tuple[list[FsEntry] | None, str | None]:
    """列一层。返回 (条目, 人话问题)；整个文件夹打不开时条目为 None。"""
    if not path or not str(path).strip():
        return None, "没有指定要看的文件夹"
    long_dir = to_long(path)
    entries: list[FsEntry] = []
    try:
        with os.scandir(long_dir) as iterator:
            for raw in iterator:
                try:
                    item = _entry_from_scandir(raw)
                except OSError:
                    continue
                if item.hidden and not show_hidden:
                    continue
                entries.append(item)
    except NotADirectoryError:
        return None, "这个位置是一个文件，不是文件夹"
    except FileNotFoundError:
        return None, "这个文件夹不存在，可能刚被移走或者删掉了"
    except PermissionError:
        return None, "这个文件夹打不开（可能被系统保护着）"
    except OSError:
        return None, "这个文件夹打不开（可能被系统保护着）"

    entries.sort(key=lambda item: (0 if item.is_dir else 1, item.name.casefold()))
    return entries, None


# --------------------------------------------------------------------------- #
# 后台盘符枚举
# --------------------------------------------------------------------------- #
class _TaskBridge(QObject):
    """后台任务把结果交给主界面用的中转站；信号会自动排到主界面再处理。"""

    drive = Signal(str, object, int)     # root, DriveInfo, generation
    done = Signal(object, int)           # list[DriveInfo], generation


class _EnumerateTask(QRunnable):
    def __init__(self, bridge: _TaskBridge, generation: int, stopped: threading.Event) -> None:
        super().__init__()
        self._bridge = bridge
        self._generation = generation
        self._stopped = stopped

    def run(self) -> None:
        ordered: list[DriveInfo] = []
        slow: list[DriveInfo] = []
        try:
            drives = list_drives_sync()
            for info in drives:
                if self._stopped.is_set():
                    return
                if info.kind in ("remote", "cdrom", "unknown"):
                    waiting = replace(info, state="probing", note="连接中…")
                    ordered.append(waiting)
                    slow.append(info)
                    self._bridge.drive.emit(waiting.root, waiting, self._generation)
                else:
                    ordered.append(info)
                    self._bridge.drive.emit(info.root, info, self._generation)

            if slow and not self._stopped.is_set():
                self._probe_slow(ordered, slow)
        finally:
            if not self._stopped.is_set():
                self._bridge.done.emit(ordered, self._generation)

    def _probe_slow(self, ordered: list[DriveInfo], slow: list[DriveInfo]) -> None:
        """网络盘和光驱放到最多 8 个后台任务里探，每个最多等 3 秒。"""
        index_by_root = {info.root: index for index, info in enumerate(ordered)}
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="disk-probe")
        try:
            futures = {executor.submit(_probe_with_timeout, info): info for info in slow}
            for future, info in futures.items():
                if self._stopped.is_set():
                    return
                try:
                    updated = future.result(timeout=3.0)
                except concurrent.futures.TimeoutError:
                    updated = replace(info, state="unavailable", note=_UNREACHABLE_NOTE)
                except Exception:
                    updated = replace(info, state="unavailable", note=_UNREACHABLE_NOTE)
                ordered[index_by_root[info.root]] = updated
                self._bridge.drive.emit(updated.root, updated, self._generation)
        finally:
            executor.shutdown(wait=False)


class DriveEnumerator(QObject):
    """盘符枚举。start() 可重复调用，旧批次的迟到结果会被丢掉。"""

    driveUpdated = Signal(str, object)   # root, DriveInfo
    finished = Signal(list)              # list[DriveInfo]

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._generation = 0
        self._stopped = threading.Event()
        self._items: dict[str, DriveInfo] = {}
        self._bridge: _TaskBridge | None = None

    def start(self) -> None:
        self._generation += 1
        self._stopped = threading.Event()
        self._items.clear()
        bridge = _TaskBridge()
        bridge.drive.connect(self._on_drive)
        bridge.done.connect(self._on_done)
        self._bridge = bridge
        QThreadPool.globalInstance().start(_EnumerateTask(bridge, self._generation, self._stopped))

    def stop(self) -> None:
        self._generation += 1
        self._stopped.set()
        self._items.clear()
        self._bridge = None

    def _on_drive(self, root: str, info: object, generation: int) -> None:
        if generation != self._generation or not isinstance(info, DriveInfo):
            return
        self._items[root] = info
        self.driveUpdated.emit(root, info)

    def _on_done(self, drives: object, generation: int) -> None:
        if generation != self._generation or not isinstance(drives, list):
            return
        self._items.clear()
        for info in drives:
            if isinstance(info, DriveInfo):
                self._items[info.root] = info
        self._bridge = None
        self.finished.emit(list(self._items.values()))


# --------------------------------------------------------------------------- #
# 后台列文件夹
# --------------------------------------------------------------------------- #
class _ListTask(QRunnable):
    def __init__(self, bridge: "_ListBridge", path: str, token: int, generation: int, show_hidden: bool) -> None:
        super().__init__()
        self._bridge = bridge
        self._path = path
        self._token = token
        self._generation = generation
        self._show_hidden = show_hidden

    def run(self) -> None:
        entries, problem = list_dir(self._path, self._show_hidden)
        if entries is None:
            self._bridge.failed.emit(self._token, self._generation, problem or "这个文件夹打不开")
            return
        self._bridge.listed.emit(self._token, self._generation, entries)


class _ListBridge(QObject):
    listed = Signal(int, int, list)      # token, generation, entries
    failed = Signal(int, int, str)       # token, generation, 人话原因


class DirLister(QObject):
    """在后台列一层文件夹；上一次没结束时，旧结果按 generation 丢掉。"""

    ready = Signal(int, list)            # generation, list[FsEntry]
    failed = Signal(int, str)            # generation, 人话原因

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._token = 0
        self._bridge: _ListBridge | None = None

    def start(self, path: str, generation: int, show_hidden: bool = False) -> None:
        # 内部批次号：即使界面两次传同一个 generation，也只认最新这一次
        self._token += 1
        token = self._token
        bridge = _ListBridge()
        bridge.listed.connect(self._on_listed)
        bridge.failed.connect(self._on_failed)
        self._bridge = bridge
        QThreadPool.globalInstance().start(_ListTask(bridge, path, token, generation, show_hidden))

    def _on_listed(self, token: int, generation: int, entries: list) -> None:
        if token != self._token:
            return
        self.ready.emit(generation, entries)

    def _on_failed(self, token: int, generation: int, problem: str) -> None:
        if token != self._token:
            return
        self.failed.emit(generation, problem)
