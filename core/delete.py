"""删除作业：放进回收站、彻底删除、结果报告与记录留存。

三条红线（破一条就会让用户丢数据）：
1. 回收站放不进去时，绝不悄悄改成彻底删除 —— 用户以为还能找回来，结果找不回来，是最坏的事故。
2. 文件夹是链接（junction / 符号链接）时只删链接这一层，绝不顺着链接往里删。
   否则删 D 盘一个软链会清空 C 盘数据。
3. 一条失败不影响后面，结果逐条记清楚，最后统一留一份记录。

长路径：所有真去动磁盘的地方都用 app_paths.to_long()；交给系统回收站、写日志、给界面看的
一律是 app_paths.to_human() 的普通路径（带 \\?\ 前缀的东西进不了回收站）。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from app_paths import to_human, to_long

try:  # 万一打包时漏了这个包，下面还有直接叫系统干活的那条路
    from send2trash import send2trash as _send_to_trash
except Exception:  # pragma: no cover - 只在环境异常时走到
    _send_to_trash = None


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
# 叫系统把东西丢进回收站时用的参数（Windows 的 SHFileOperationW）
_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400
_FOF_FLAGS = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI

_FILE_ATTRIBUTE_DIRECTORY = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x0010)
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)

LOG_PREFIX = "删除记录-"
LOG_SUFFIX = ".txt"
LOG_KEEP_COUNT = 30          # 只留最近 30 份
MAX_LOG_NAME_TRIES = 100

# 给用户看的失败原因，全是人话
ERR_DENIED = "系统不让删（可能被别的程序占用，或者需要管理员权限）"
ERR_GONE = "这一项已经不在了"
ERR_IN_USE = "正被别的程序用着，删不掉"
ERR_UNKNOWN = "删不掉，原因不明"
ERR_NO_RECYCLE = "放不进回收站，这一项没有删"
ERR_NEED_ADMIN = "需要管理员权限（这个位置普通权限动不了）"
ERR_TOO_LONG = "路径太长，系统处理不了"
ERR_IN_USE = "有程序正在使用它，先关掉那个程序"

MODE_LABEL = {
    "recycle": "放进回收站（能找回来）",
    "permanent": "彻底删除（找不回来）",
}

RISK_LABEL = {
    "safe": "放心删",
    "caution": "看情况",
    "forbidden": "别删",
    "unlisted": "未收录",
    # 旧版本的值留着兜底，老日志/老数据也能读懂
    "danger": "别删",
    "unknown": "未收录",
}


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
class Mode(str, Enum):
    """删除方式。放进回收站能找回来，彻底删除找不回来。"""

    RECYCLE = "recycle"
    PERMANENT = "permanent"


@dataclass
class DeleteItem:
    """要删的一项：界面传进来的只有这些信息，删除过程不回写它。"""

    human_path: str
    display: str          # 给人看的短名（文件名或文件夹名）
    is_dir: bool
    risk: str = "unknown"    # safe/caution/forbidden/unlisted
    size: int | None = None
    effect: str = ""         # 列表里那行「作用」的说明，确认清单要照抄给用户看
    impact: str = ""         # 知识库写的那句后果，三道坎第三道原样念给用户


@dataclass
class ItemResult:
    """一项的删除结果。display / size / risk 是给人看的备份，写记录时用。"""

    human_path: str
    ok: bool
    mode: Mode
    error: str | None = None
    at: float = 0.0
    display: str = ""
    size: int | None = None
    risk: str = "unknown"


@dataclass
class DeleteReport:
    """一次删除作业的完整结果。"""

    mode: Mode
    started: float
    finished: float
    items: list[ItemResult] = field(default_factory=list)
    freed: int = 0
    log_path: Path | None = None

    # -- 统计 ----------------------------------------------------------------
    @property
    def ok_count(self) -> int:
        return sum(1 for result in self.items if result.ok)

    @property
    def fail_count(self) -> int:
        return sum(1 for result in self.items if not result.ok)

    @property
    def summary(self) -> str:
        """一句话汇总，直接拿去放状态栏。"""
        total = len(self.items)
        if total == 0:
            return "这次没有选中要删的东西"
        ok = self.ok_count
        parts: list[str] = []
        if ok:
            if self._mode() == Mode.RECYCLE:
                # 放回收站不等于腾出空间：文件还在盘上，清空回收站才真正释放
                parts.append(f"移入回收站 {ok} 项，约 {_size_text(self._freed_bytes())}")
            else:
                parts.append(f"删了 {ok} 项，释放 {_size_text(self._freed_bytes())}")
        else:
            parts.append("一项也没删成")
        if self.fail_count:
            parts.append(f"{self.fail_count} 项没删成")
        if self._mode() == Mode.RECYCLE and ok:
            parts.append("清空回收站后才真正释放空间；想反悔可以去还原")
        return "；".join(parts)

    def failure_reasons(self) -> list[tuple[str, str]]:
        """没删成的那几项：(显示名, 原因)。"""
        out: list[tuple[str, str]] = []
        for result in self.items:
            if result.ok:
                continue
            name = result.display or _base_name(result.human_path) or result.human_path
            out.append((name, result.error or ERR_UNKNOWN))
        return out

    def as_log_text(self) -> str:
        """整份记录的文字版，写进 logs 目录。"""
        lines = [
            "SightClean · 删除记录",
            f"时间：{_time_text(self.started)}",
            f"方式：{MODE_LABEL.get(self._mode().value, '删除')}",
            f"共 {len(self.items)} 项：成功 {self.ok_count}，失败 {self.fail_count}",
            (
                f"移入回收站：约 {_size_text(self._freed_bytes())}（清空回收站后才真正释放）"
                if self._mode() == Mode.RECYCLE
                else f"释放空间：{_size_text(self._freed_bytes())}"
            ),
            "",
        ]
        for result in self.items:
            state = "成功" if result.ok else "失败"
            size = _size_text(result.size)
            risk = RISK_LABEL.get(result.risk, RISK_LABEL["unknown"])
            reason = result.error or "无"
            lines.append(f"{state} | {size} | {risk} | {result.human_path} | {reason}")
        return "\n".join(lines) + "\n"

    # -- 内部 ----------------------------------------------------------------
    def _mode(self) -> Mode:
        try:
            return Mode(self.mode)
        except (ValueError, TypeError):
            return Mode.RECYCLE

    def _freed_bytes(self) -> int:
        """省出来的空间：只算成功的、而且原本就知道大小的那几项。"""
        if self.freed:
            return int(self.freed)
        total = 0
        for result in self.items:
            if result.ok and result.size is not None:
                total += int(result.size)
        return total


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _size_text(size: int | None) -> str:
    """把字节数说成人话，比如 1.2 GB。"""
    if size is None:
        return "大小未知"
    value = float(max(0, int(size)))
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.1f} GB"
    if value >= 1024 ** 2:
        return f"{value / 1024 ** 2:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{int(value)} 字节"


def _time_text(stamp: float) -> str:
    """时间戳说成人话；时间戳不合理就用现在。"""
    moment = stamp if stamp else time.time()
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment))
    except (OSError, ValueError, OverflowError):
        return time.strftime("%Y-%m-%d %H:%M:%S")


def _base_name(human_path: str) -> str:
    """从路径里取最后一段当短名。"""
    text = (human_path or "").rstrip("\\/")
    if not text:
        return ""
    for sep in ("\\", "/"):
        if sep in text:
            text = text.rsplit(sep, 1)[-1]
    return text


def _present(long_path: str) -> bool:
    """这个位置还在不在。链接本身也算在（哪怕它指向的目标已经没了）。"""
    try:
        return os.path.lexists(long_path)
    except (OSError, ValueError):
        return False


def _human_error(exc: BaseException) -> str:
    """把系统抛出来的东西翻成一句人话。

    「正被别的程序用着」（32 / 33 / 145）要先看：系统把它归到「权限」那一类里，
    但真实原因是被占用，直接说被占用用户才看得懂。
    """
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (32, 33, 145):
        return ERR_IN_USE
    if isinstance(exc, FileNotFoundError):
        return ERR_GONE
    if isinstance(exc, PermissionError):
        return ERR_DENIED
    return ERR_UNKNOWN


# --------------------------------------------------------------------------- #
# 链接识别：整个模块最要紧的一段
# --------------------------------------------------------------------------- #
def _is_link(long_path: str) -> bool:
    """这个位置本身是不是链接（软链 / junction）。

    是链接就只能删链接这一层：顺着链接往里删，删 D 盘一个软链会清空 C 盘数据。
    """
    try:
        if os.path.islink(long_path):
            return True
    except OSError:
        pass
    try:
        info = os.lstat(long_path)
    except OSError:
        return False
    attrs = int(getattr(info, "st_file_attributes", 0) or 0)
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    return stat.S_ISLNK(int(getattr(info, "st_mode", 0) or 0))


def _remove_link(long_path: str) -> None:
    """只删链接自己：目录链接去掉挂接点，文件链接直接去掉。"""
    dir_like = False
    try:
        info = os.lstat(long_path)
    except OSError:
        info = None
    if info is not None:
        dir_like = stat.S_ISDIR(info.st_mode) or bool(
            int(getattr(info, "st_file_attributes", 0) or 0) & _FILE_ATTRIBUTE_DIRECTORY
        )
    if dir_like:
        os.rmdir(long_path)
    else:
        os.unlink(long_path)


# --------------------------------------------------------------------------- #
# 彻底删除
# --------------------------------------------------------------------------- #
def _clear_readonly(long_path: str) -> None:
    """去掉只读标记；去不掉就算了，后面的重试会照实报错。"""
    try:
        os.chmod(long_path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _retry_after_chmod(func, path, exc=None) -> None:
    """文件被标成只读时系统会拒绝，这里去掉只读再试一次；再失败就照实往上抛。

    3.10 / 3.11 的回调带的是异常三元组，3.12 起直接给异常对象 —— 两者都是三个参数，
    所以同一个函数两边都能用。绝不能在这里把异常吞掉，否则会「报成功但东西还在」。
    """
    _clear_readonly(path)
    func(path)


def _remove_tree(long_path: str) -> None:
    """递归删一整棵目录树。

    链接不会进到这里（进到这里之前已经被 _is_link 拦下去只删链接了）；
    树里面藏着的链接由 shutil 自己识别成链接、只删链接，不会顺着删目标。
    """
    try:
        shutil.rmtree(long_path, onexc=_retry_after_chmod)      # 3.12 以后的写法
    except TypeError:
        shutil.rmtree(long_path, onerror=_retry_after_chmod)    # 3.10 / 3.11 的写法


def _purge(long_path: str, is_dir: bool) -> str | None:
    """彻底删除。成功返回 None，失败返回一句人话。"""
    try:
        if _is_link(long_path):
            # 红线：只删链接本身，绝不顺着链接往里删
            _remove_link(long_path)
        else:
            try:
                dir_like = bool(is_dir) or stat.S_ISDIR(os.lstat(long_path).st_mode)
            except OSError as exc:
                return _human_error(exc)
            if dir_like:
                _remove_tree(long_path)
            else:
                _clear_readonly(long_path)
                os.unlink(long_path)
    except (OSError, ValueError) as exc:
        return _human_error(exc)
    if _present(long_path):
        # 系统说干完了、东西却还在，就照实算没删成
        return ERR_UNKNOWN
    return None


# --------------------------------------------------------------------------- #
# 放进回收站
# --------------------------------------------------------------------------- #
class _SHFileOpStructW(ctypes.Structure):
    """系统「扔进回收站」这个动作的入参结构，字段顺序与宽度必须和系统一致。"""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),                 # 窗口代号，不弹窗就给空
        ("wFunc", ctypes.c_uint),                  # 要做的事：删除
        ("pFrom", ctypes.c_wchar_p),               # 要处理的路径，两个空字符结尾
        ("pTo", ctypes.c_wchar_p),                 # 挪到哪儿，删除用不上
        ("fFlags", ctypes.c_ushort),               # 行为开关
        ("fAnyOperationsAborted", ctypes.c_int),   # 用户中途取消没有
        ("hNameMappings", ctypes.c_void_p),        # 不用，给空
        ("lpszProgressTitle", ctypes.c_wchar_p),   # 进度标题，不用，给空
    ]


def _shell_recycle(human_path: str) -> tuple[bool, int]:
    """第二条路：直接叫系统把这一项丢进回收站（不是彻底删除）。

    返回 (成功没, 系统错误码)。错误码要留着 —— 「没权限」和「路径太长」
    是两回事，给用户的提示必须分开。
    """
    if os.name != "nt":
        return False, 0
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    except (OSError, AttributeError):
        return False, 0

    op = _SHFileOpStructW()
    op.hwnd = None
    op.wFunc = _FO_DELETE
    op.pFrom = human_path + "\0"   # 系统要求末尾两个空字符
    op.pTo = None
    op.fFlags = _FOF_FLAGS
    op.fAnyOperationsAborted = 0
    op.hNameMappings = None
    op.lpszProgressTitle = None

    try:
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFileOpStructW)]
        shell32.SHFileOperationW.restype = ctypes.c_int
        code = int(shell32.SHFileOperationW(ctypes.byref(op)))
    except (OSError, TypeError, AttributeError):
        return False, 0
    return (code == 0 and not op.fAnyOperationsAborted), code


ERR_LOCKED = "这一项正被程序占用，先关掉相关的软件再试"


def _likely_locks(path: str, limit: int = 3) -> list[str]:
    """查一下谁可能占着这个路径：看哪些进程的 exe 在它底下。

    不保证准（文件被打开不等于 exe 在目录里），但比一句「可能被占用」有用得多 ——
    用户看到「chrome.exe」就知道该关什么。
    """
    # 空路径必须先挡掉：os.path.abspath("") 会返回**当前工作目录**，
    # 那样会把程序自己所在的目录当成目标，报出一个莫名其妙的占用者。
    text = str(path or "").strip()
    if not text:
        return []
    try:
        import psutil
    except ImportError:
        return []
    target = os.path.normcase(os.path.abspath(text))
    if not target:
        return []
    names: list[str] = []
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            exe = proc.info.get("exe") or ""
            if not exe:
                continue
            if os.path.normcase(exe).startswith(target + os.sep) or \
                    os.path.normcase(exe) == target:
                name = proc.info.get("name") or ""
                if name and name not in names:
                    names.append(name)
                    if len(names) >= limit:
                        break
        except Exception:  # noqa: BLE001 - 单个进程读不到不影响整体
            continue
    return names


def _lock_reason(path: str) -> str:
    """「被占用」的确切说法，能查出进程就把名字带上。"""
    names = _likely_locks(path)
    if names:
        return "这个目录里的程序正在运行：" + "、".join(names) + "。先关掉它们再试"
    return ERR_LOCKED

# 系统「扔进回收站」失败时给的原因码
_SHELL_ERRORS = {
    # DE_INVALIDFILES：名字里有系统不认的字符，不是权限问题
    0x7C: "文件名里有系统不认的字符",
    0x79: ERR_TOO_LONG,        # DE_PATHTOODEEP：目录太深
    0x81: ERR_TOO_LONG,        # DE_FILENAMETOOLONG
    0x75: "你取消了这次操作",
}

# 0x78（DE_ACCESSDENIEDSRC）不能一概而论：
# 已经是管理员时它几乎总是「文件正被占用」，只有普通权限才真叫权限不够。
_ACCESS_DENIED = 0x78


def _reason_for_access_denied(path: str = "") -> str:
    if not _is_admin():
        return ERR_NEED_ADMIN
    # 管理员下还是拒绝，基本就是被占用了 —— 把嫌疑进程列出来
    return _lock_reason(path)


def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _reason_from_trash_error(path: str, exc: BaseException) -> str | None:
    """把 send2trash 抛的异常翻成人话。认不出来就返回 None，不瞎猜。"""
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if code == 32:
        return _lock_reason(path)
    if code in (5, 13):
        # 拒绝访问：管理员下多半是被占用，普通权限下才是真没权限
        return _reason_for_access_denied(path)
    if isinstance(exc, PermissionError):
        return _reason_for_access_denied(path)
    text = str(exc)
    if "being used" in text.lower() or "正被" in text:
        return _lock_reason(path)
    if "denied" in text.lower() or "拒绝访问" in text:
        return _reason_for_access_denied(path)
    return None


def _recycle(human_path: str) -> str | None:
    """放进回收站。成功返回 None，失败返回人话原因。

    两条路都走不通就如实报「这一项没有删」，绝不自动改成彻底删除。
    """
    first_reason: str | None = None
    if _send_to_trash is not None:
        try:
            _send_to_trash(to_human(human_path))   # 不能带长路径前缀，带了必失败
            return None
        except Exception as exc:  # noqa: BLE001 - 两条路都得试，但原因要留下
            first_reason = _reason_from_trash_error(human_path, exc)

    ok, code = _shell_recycle(to_human(human_path))
    if ok:
        return None
    if code == _ACCESS_DENIED:
        return _reason_for_access_denied(human_path)
    # 系统给的原因码比 send2trash 的异常更具体，优先用它
    return _SHELL_ERRORS.get(code) or first_reason or ERR_NO_RECYCLE


# --------------------------------------------------------------------------- #
# 对外：删一项
# --------------------------------------------------------------------------- #
def delete_one(item: DeleteItem, mode: Mode) -> ItemResult:
    """删一项，永远不抛异常：结果全部写进 ItemResult。"""
    at = time.time()
    human = to_human(item.human_path)
    try:
        chosen = Mode(mode)
    except (ValueError, TypeError):
        chosen = Mode.RECYCLE
    try:
        long_path = to_long(human)
    except (OSError, ValueError):
        long_path = human

    def _done(ok: bool, error: str | None) -> ItemResult:
        return ItemResult(
            human_path=human,
            ok=ok,
            mode=chosen,
            error=error,
            at=at,
            display=item.display or _base_name(human),
            size=item.size,
            risk=item.risk,
        )

    if not _present(long_path):
        return _done(False, ERR_GONE)

    if chosen == Mode.RECYCLE:
        error = _recycle(human)
    else:
        error = _purge(long_path, item.is_dir)
    return _done(error is None, error)


# --------------------------------------------------------------------------- #
# 记录留档
# --------------------------------------------------------------------------- #
def write_log(report: DeleteReport, log_dir: Path) -> Path | None:
    """写一份删除记录；写不进去就返回 None，绝不打扰用户。"""
    try:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(report.finished or time.time()))
        path = directory / f"{LOG_PREFIX}{stamp}{LOG_SUFFIX}"
        tries = 1
        while _present(to_long(path)) and tries < MAX_LOG_NAME_TRIES:
            path = directory / f"{LOG_PREFIX}{stamp}-{tries}{LOG_SUFFIX}"
            tries += 1
        Path(to_long(path)).write_text(report.as_log_text(), encoding="utf-8")
        return path
    except (OSError, ValueError):
        return None


def purge_old_logs(log_dir: Path, keep_count: int = LOG_KEEP_COUNT) -> int:
    """只保留最近 keep_count 份记录，多出来的按时间从旧到新删掉。

    返回删了几个；出任何问题都当没发生，绝不影响启动。
    老的 .log 记录也一并纳入清理，避免升级后越堆越多。
    """
    try:
        keep = int(keep_count)
    except (TypeError, ValueError):
        keep = LOG_KEEP_COUNT
    if keep <= 0:
        return 0
    try:
        found: list[tuple[float, Path]] = []
        for entry in Path(to_long(log_dir)).iterdir():
            try:
                if not entry.is_file():
                    continue
                name = entry.name
                if not name.startswith(LOG_PREFIX):
                    continue
                if not (name.endswith(LOG_SUFFIX) or name.endswith(".log")):
                    continue
                found.append((entry.stat().st_mtime, entry))
            except (OSError, ValueError):
                continue
    except (OSError, ValueError):
        return 0

    found.sort(key=lambda pair: pair[0])
    removed = 0
    for _stamp, entry in found[: max(0, len(found) - keep)]:
        try:
            entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------- #
# 后台作业
# --------------------------------------------------------------------------- #
class DeleteWorker(QRunnable):
    """一次删除作业：逐条删、逐条报进度，最后交一份完整报告。

    log_dir 用来留一份记录；记录写不进去也不影响删除结果本身。
    """

    class Signals(QObject):
        progress = Signal(int, int, str)   # 已完成数, 总数, 当前处理中的路径
        finished = Signal(object)          # DeleteReport

    def __init__(self, items: list[DeleteItem], mode: Mode, log_dir: Path) -> None:
        super().__init__()
        self.signals = DeleteWorker.Signals()
        self.items = list(items)
        try:
            self.mode = Mode(mode)
        except (ValueError, TypeError):
            self.mode = Mode.RECYCLE   # 认不出的方式一律按能找回的处理
        self.log_dir = Path(log_dir)

    def run(self) -> None:
        started = time.time()
        total = len(self.items)
        results: list[ItemResult] = []
        for index, item in enumerate(self.items, start=1):
            human = to_human(item.human_path)
            self.signals.progress.emit(index - 1, total, human)
            try:
                result = delete_one(item, self.mode)
            except BaseException:  # delete_one 本身不抛，这里只是最后一道保险
                result = ItemResult(
                    human_path=human,
                    ok=False,
                    mode=self.mode,
                    error=ERR_UNKNOWN,
                    at=time.time(),
                    display=item.display or _base_name(human),
                    size=item.size,
                    risk=item.risk,
                )
            results.append(result)
        finished = time.time()

        report = DeleteReport(mode=self.mode, started=started, finished=finished, items=results)
        report.freed = sum(
            int(item.size)
            for item, result in zip(self.items, results)
            if result.ok and item.size is not None
        )
        try:
            report.log_path = write_log(report, self.log_dir)
        except BaseException:
            report.log_path = None

        self.signals.progress.emit(total, total, "")
        self.signals.finished.emit(report)
