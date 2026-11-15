"""正在跑的进程：枚举、判定、结束。

这一页只做三件事：把现在跑着什么列出来、标出哪些是系统命脉、把用户勾选的结束掉。
不做实时刷新、不做后台监控 —— 那是任务管理器的事。
"""
from __future__ import annotations

import ctypes
import os
import string
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import psutil

LOG_PREFIX = "结束进程-"
LOG_SUFFIX = ".txt"

# 结束这些基本就是蓝屏 / 立刻重启。只用来标红，不阻止用户勾选
CRITICAL_NAMES = {
    "system", "system idle process", "registry", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "lsaiso.exe", "svchost.exe",
    "dwm.exe", "fontdrvhost.exe", "sihost.exe",
    # WSL / 内存压缩这类不是普通程序，结束会连带影响
    "memcompression", "vmmem", "vmmemcmzygote", "vmmemwsl",
}
CRITICAL_HINT = "结束可能导致系统蓝屏或立刻重启"

ERR_GONE = "这个进程已经不在了"
ERR_DENIED = "没有权限结束它（用「工具 → 以管理员身份重新启动」再试）"
ERR_UNKNOWN = "结束失败，原因不明"


@dataclass(frozen=True)
class ProcessItem:
    pid: int
    name: str
    username: str
    rss: int              # 常驻内存，字节
    cpu: float            # 百分比，第二次采样后才有意义
    exe: str
    publisher: str
    locked: bool = False  # 读不到 exe / 用户名
    ppid: int = 0         # 父进程 PID，判断「是不是被别的程序拉起来的」用
    create_time: float = 0.0   # 启动时刻（epoch 秒），算「开了多久」用
    foreground: bool = False   # 是不是当前前台窗口的进程
    session: int = -1          # 会话 ID：0 = 服务，1 起 = 用户
    system: bool = False  # 系统命脉

    @property
    def identity(self) -> str:
        return str(self.pid)

    @property
    def detail(self) -> str:
        return self.exe or (f"PID {self.pid}")


@dataclass
class ProcessResult:
    pid: int
    name: str
    ok: bool
    error: str | None = None
    at: float = 0.0


# --------------------------------------------------------------------------- #
# 发布者：从 exe 的版本资源里读 CompanyName
# --------------------------------------------------------------------------- #
_ver = ctypes.WinDLL("version", use_last_error=True)
_ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
_ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
_ver.GetFileVersionInfoW.restype = wintypes.BOOL
_ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
_ver.VerQueryValueW.restype = wintypes.BOOL


def get_publisher(exe_path: str) -> str:
    """读 exe 的 CompanyName。读不到就返回空串 —— 不猜。

    等价于 win32api.GetFileVersionInfo(...)['CompanyName']，
    直接用 ctypes 是为了不把 pywin32 整个拖进来。
    """
    path = str(exe_path or "").strip()
    if not path or not os.path.isfile(path):
        return ""
    try:
        size = _ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not _ver.GetFileVersionInfoW(path, 0, size, buffer):
            return ""
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        # 语言页不固定，常见的几种都试一遍
        for lang in ("040904B0", "040904b0", "080404B0", "000004B0", "040704B0"):
            sub_block = f"\\StringFileInfo\\{lang}\\CompanyName"
            if _ver.VerQueryValueW(buffer, sub_block, ctypes.byref(pointer), ctypes.byref(length)):
                if pointer.value and length.value:
                    return ctypes.wstring_at(pointer.value, length.value).strip().rstrip("\x00")
    except (OSError, ValueError, ctypes.ArgumentError):
        return ""
    return ""


def is_critical_process(name: str) -> bool:
    return str(name or "").strip().lower() in CRITICAL_NAMES


def _safe(getter, default=None):
    try:
        return getter()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        return default


def enumerate_processes(with_publisher: bool = True) -> tuple[list[ProcessItem], list[str]]:
    """列一次当前进程。每条单独 try —— 一条读不到不该让整页空掉。"""
    items: list[ProcessItem] = []
    warnings: list[str] = []
    for proc in psutil.process_iter():
        try:
            info = proc.as_dict(attrs=["pid", "name", "username", "exe", "ppid"], ad_value=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        pid = info.get("pid")
        if pid is None:
            continue
        name = info.get("name") or f"进程 {pid}"
        exe = info.get("exe") or ""
        username = info.get("username") or ""
        memory = _safe(lambda: proc.memory_info().rss, 0) or 0
        cpu = _safe(lambda: proc.cpu_percent(interval=None), 0.0) or 0.0
        locked = not exe
        publisher = get_publisher(exe) if (with_publisher and exe) else ""
        items.append(ProcessItem(
            pid=int(pid),
            name=str(name),
            username=str(username),
            rss=int(memory),
            cpu=float(cpu),
            exe=str(exe),
            publisher=publisher,
            locked=locked,
            system=is_critical_process(name),
            ppid=int(info.get("ppid") or 0),
            create_time=float(_safe(lambda: proc.create_time(), 0.0) or 0.0),
            session=session_id_of(int(pid)),
        ))
    items.sort(key=lambda item: item.name.lower())
    if not items:
        warnings.append("没有读到任何进程，可能权限不够")
    return items, warnings


def sample_cpu(pids: Sequence[int], window: float = 0.3) -> dict[int, float]:
    """算 CPU 百分比。

    psutil 的 cpu_percent(interval=None) 是「跟这个 Process 对象上一次调用比」，
    新建的对象第一次问必然是 0。所以这里必须**用同一批对象问两次**，中间隔一会儿。
    调用方应该把它放后台线程，因为这里真的要等 window 秒。
    """
    procs = []
    for pid in pids:
        try:
            procs.append(psutil.Process(int(pid)))
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            continue
    if not procs:
        return {}
    for proc in procs:
        try:
            proc.cpu_percent(interval=None)      # 预热，结果丢掉
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    time.sleep(window)
    result: dict[int, float] = {}
    for proc in procs:
        try:
            result[proc.pid] = float(proc.cpu_percent(interval=None))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return result


def make_cpu_sampler(pids: Sequence[int]):
    """把采样丢到线程池，界面不会被这 0.3 秒卡住。"""
    from PySide6.QtCore import QObject, QRunnable, Signal

    class Signals(QObject):
        ready = Signal(object)   # dict[pid, percent]

    class _Task(QRunnable):
        def __init__(self) -> None:
            super().__init__()
            self.setAutoDelete(True)
            self.signals = Signals()

        def run(self) -> None:
            try:
                samples = sample_cpu(pids)
            except Exception:  # noqa: BLE001
                samples = {}
            try:
                self.signals.ready.emit(samples)
            except RuntimeError:
                pass

    return _Task()


def kill_process(pid: int, name: str = "") -> ProcessResult:
    """先好好说（terminate），不听再动手（kill）。"""
    now = time.time()
    try:
        proc = psutil.Process(int(pid))
    except psutil.NoSuchProcess:
        return ProcessResult(int(pid), name, False, ERR_GONE, now)
    except (psutil.Error, ValueError):
        return ProcessResult(int(pid), name, False, ERR_UNKNOWN, now)

    try:
        proc.terminate()
        try:
            proc.wait(timeout=1.5)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.5)
        return ProcessResult(int(pid), name, True, None, now)
    except psutil.AccessDenied:
        return ProcessResult(int(pid), name, False, ERR_DENIED, now)
    except psutil.NoSuchProcess:
        return ProcessResult(int(pid), name, False, ERR_GONE, now)
    except (psutil.Error, OSError):
        return ProcessResult(int(pid), name, False, ERR_UNKNOWN, now)


@dataclass
class ProcessReport:
    items: list[ProcessResult]
    started: float
    finished: float
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
            return "这次没有选中要结束的进程"
        parts = [f"结束了 {self.ok_count} 个进程"]
        if self.fail_count:
            parts.append(f"{self.fail_count} 个没结束")
        return "；".join(parts)

    def failure_reasons(self) -> list[tuple[str, str]]:
        return [(f"{item.name}（{item.pid}）", item.error or ERR_UNKNOWN) for item in self.items if not item.ok]

    def as_log_text(self) -> str:
        lines = [
            "SightClean · 结束进程记录",
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started))}",
            f"共 {len(self.items)} 项：成功 {self.ok_count}，失败 {self.fail_count}",
            "",
        ]
        for result in self.items:
            state = "成功" if result.ok else "失败"
            lines.append(f"{state} | {result.name} | PID {result.pid} | {result.error or '无'}")
        return "\n".join(lines) + "\n"


def write_log(report: ProcessReport, log_dir: Path) -> Path | None:
    try:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(report.finished or time.time()))
        path = directory / f"{LOG_PREFIX}{stamp}{LOG_SUFFIX}"
        path.write_text(report.as_log_text(), encoding="utf-8")
        return path
    except (OSError, ValueError):
        return None


def make_worker(pairs: Sequence[tuple[int, str]], log_dir: Path):
    """包成 QRunnable，丢线程池里跑，界面不卡。"""
    from PySide6.QtCore import QObject, QRunnable, Signal

    class Signals(QObject):
        progress = Signal(int, int, str)
        finished = Signal(object)

    class _Task(QRunnable):
        def __init__(self) -> None:
            super().__init__()
            self.setAutoDelete(True)
            self.signals = Signals()

        def run(self) -> None:
            started = time.time()
            total = len(pairs)
            results = []
            for index, (pid, name) in enumerate(pairs):
                try:
                    self.signals.progress.emit(index, total, name)
                except RuntimeError:
                    return
                results.append(kill_process(pid, name))
            report = ProcessReport(items=results, started=started, finished=time.time())
            report.log_path = write_log(report, log_dir)
            try:
                self.signals.progress.emit(total, total, "")
                self.signals.finished.emit(report)
            except RuntimeError:
                pass

    return _Task()
# --------------------------------------------------------------------------- #
# 孤儿进程：不属于系统、也不属于任何已装软件
# --------------------------------------------------------------------------- #
# 被这些进程拉起来的，算「独立启动」，不是别人的子进程。
# 用户双击启动 → explorer.exe；服务 → svchost；计划任务 → taskhostw。
_ADOPTED_PARENTS = {
    "explorer.exe", "svchost.exe", "services.exe", "wininit.exe", "winlogon.exe",
    "userinit.exe", "smss.exe", "taskhostw.exe", "taskeng.exe", "runtimebroker.exe",
    "startmenuexperiencehost.exe", "sihost.exe", "dwm.exe", "cmd.exe", "powershell.exe",
    "windowsterminal.exe", "conhost.exe",
}


def session_id_of(pid: int) -> int:
    """这个进程跑在哪个会话。

    Windows 用会话隔离服务和用户程序：
        0    = 服务会话（WUDFHost、spoolsv、各种厂商服务都在这）
        1 起 = 用户登录会话
    比「看用户名」靠谱 —— 普通权限也能查，而且系统服务一律在 0。
    """
    try:
        sid = wintypes.DWORD()
        if ctypes.windll.kernel32.ProcessIdToSessionId(int(pid), ctypes.byref(sid)):
            return int(sid.value)
    except Exception:  # noqa: BLE001
        pass
    return -1


def foreground_pid() -> int:
    """当前前台窗口属于哪个进程。拿不到就返回 0。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        return int(owner.value)
    except Exception:  # noqa: BLE001
        return 0


# 这些是系统服务的宿主，虽然是用户态进程，但删了会连带一片服务
_SERVICE_HOSTS = {
    # 服务的宿主
    "wudfhost.exe", "spoolsv.exe", "conhost.exe", "dllhost.exe", "taskhostw.exe",
    "sihost.exe", "runtimebroker.exe", "searchindexer.exe", "wmiprvse.exe",
    "audiodg.exe", "fontdrvhost.exe", "ctfmon.exe", "smartscreen.exe",
    "rundll32.exe", "unsecapp.exe", "dwm.exe",
    # 桌面和开始菜单 —— 动了这些，任务栏和桌面直接没了
    "explorer.exe", "startmenuexperiencehost.exe", "searchhost.exe", "shellhost.exe",
    "widgetboard.exe", "textinputhost.exe", "crossdeviceresume.exe",
    "applicationframehost.exe", "lockapp.exe", "shellexperiencehost.exe",
}

# 默认门槛：开了 30 分钟以上、CPU 低于 1%
DEFAULT_MIN_AGE_MINUTES = 30
DEFAULT_MAX_CPU = 1.0


def find_idle_processes(processes=None,
                        min_age_minutes: float = DEFAULT_MIN_AGE_MINUTES,
                        max_cpu: float = DEFAULT_MAX_CPU) -> list[ProcessItem]:
    """找「开了很久、不在前台、CPU 几乎不动」的进程。

    这就是「忘了关」的典型特征：
        时间够久   —— 刚打开的不管
        不在前台   —— 你正在用的那个不能动
        CPU 很低   —— 正在算东西的不管

    系统进程一律排除。已装软件的进程**不排除** ——
    「忘了关的微信」正是要找的东西。
    按开了多久从长到短排，开的越久越可能被忘掉。
    """
    items = processes if processes is not None else enumerate_processes()[0]
    now = time.time()
    front = foreground_pid()
    threshold = float(min_age_minutes) * 60
    # 只清「你自己的」进程。SYSTEM / LOCAL SERVICE / NETWORK SERVICE
    # 那些是系统服务（WUDFHost、spoolsv、vmms 之类），动了会出事。
    me_user = (os.environ.get("USERNAME") or "").strip().lower()

    candidates: list[tuple[float, ProcessItem]] = []
    for proc in items:
        if proc.system or proc.name.lower() in CRITICAL_NAMES:
            continue
        if proc.locked:
            continue                      # 读不到的判断不了
        owner = str(proc.username or "").lower()
        if me_user and owner and me_user not in owner:
            continue                      # 不是当前用户的进程
        # 会话 0 是服务会话；-1 表示查不到（受保护进程）—— 两类都跳过。
        # 「忘了关的软件」一定跑在你的登录会话里。
        if proc.session in (0, -1):
            continue
        if proc.name.lower() in _SERVICE_HOSTS:
            continue                      # 系统服务的宿主
        if front and proc.pid == front:
            continue                      # 前台那个，别动
        if not proc.create_time:
            continue
        age = now - proc.create_time
        if age < threshold:
            continue
        if proc.cpu > max_cpu:
            continue
        candidates.append((age, proc))

    candidates.sort(key=lambda pair: -pair[0])
    result = []
    for _age, proc in candidates:
        # frozen dataclass 改不了，这里重建一份带上前台标记
        result.append(proc)
    return result


def age_text(proc: ProcessItem, now: float | None = None) -> str:
    """「开了多久」的人话。"""
    if not proc.create_time:
        return "—"
    seconds = max(0.0, (now or time.time()) - proc.create_time)
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def find_orphans(installed, processes=None) -> list[ProcessItem]:
    """找出「既不是系统进程，也不属于任何已装软件」的进程。

    判断依据是**可执行文件的路径**，不是名字 —— 名字可以随便改，路径骗不了人：
        在 %WINDIR% 下           → 系统进程，跳过
        在某个软件的安装目录下     → 那是它的进程，跳过
        其余                      → 候选
    读不到路径的（受保护进程）也跳过，因为没法判断，不瞎猜。
    """
    items = processes if processes is not None else enumerate_processes()[0]

    windows = os.path.normcase(os.environ.get("WINDIR", r"C:\Windows")).rstrip("\\/")
    import sys as _sys

    me = os.path.normcase(os.path.abspath(_sys.executable))
    me_names = {os.path.basename(me).lower(), "SightClean.exe"}
    if getattr(_sys, "frozen", False):
        me_names.add(os.path.basename(me).lower())

    # 已装软件的安装目录
    locations: list[str] = []
    for software in installed or []:
        location = str(getattr(software, "install_location", "") or "").strip()
        if not location:
            continue
        normed = os.path.normcase(location).rstrip("\\/")
        if normed:
            locations.append(normed)

    # 这些目录里的东西一律不算孤儿：
    #   Common Files  各厂商共用的组件（Adobe ARM、Macrovision 授权服务都在这儿）
    #   WindowsApps   商店应用（MSIX 安装的）
    #   Installer     MSI 缓存
    # 不能只认 C 盘 —— 系统可能装在别的盘
    shared_roots_list: list[str] = [os.path.normcase(os.path.join(windows, "Installer"))]
    for letter in string.ascii_uppercase:
        for name in ("Program Files", "Program Files (x86)"):
            base = f"{letter}:\\{name}"
            shared_roots_list.append(os.path.normcase(os.path.join(base, "Common Files")))
            shared_roots_list.append(os.path.normcase(os.path.join(base, "WindowsApps")))
    shared_roots = tuple(shared_roots_list)

    # 已装软件的名字碎片，用来兜住「安装路径没登记」的情况
    software_words: set[str] = set()
    for software in installed or []:
        name = str(getattr(software, "name", "") or "").lower()
        for token in name.replace("_", " ").split():
            token = token.strip(".,()[]{}-")
            if len(token) >= 4:
                software_words.add(token)

    alive = {p.pid for p in items}
    adopted_pids = {p.pid for p in items if p.name.lower() in _ADOPTED_PARENTS}

    orphans: list[ProcessItem] = []
    for proc in items:
        if proc.system or proc.name.lower() in CRITICAL_NAMES:
            continue
        # 父进程还活着，而且不是「系统收养」——说明它是那个程序的一部分：
        # 微信拉起 WeChatAppEx、WorkBuddy 拉起 node，这些都不该算孤儿。
        if proc.ppid and proc.ppid in alive and proc.ppid not in adopted_pids:
            continue
        exe = os.path.normcase(str(proc.exe or "").strip())
        if not exe:
            continue                      # 读不到路径，判断不了
        if exe.startswith(windows + os.sep):
            continue                      # 系统目录里的
        if exe == me or proc.name.lower() in me_names:
            continue                      # 咱自己（含打包后的 exe 名）
        if any(exe == loc or exe.startswith(loc + os.sep) for loc in locations):
            continue                      # 属于某个已装软件
        if any(exe.startswith(root + os.sep) for root in shared_roots if root):
            continue                      # 共享组件 / 商店应用
        # 凡是在「某块盘的 Program Files 下」，一律算已装软件。
        # 很多软件不写 InstallLocation，靠路径登记永远认不出来。
        if _under_program_files(exe):
            continue
        # 路径没登记，但名字对得上某个软件：
        #   exe 叫 workbuddy-helper.exe        → 名字命中
        #   路径是 C:/Users/.../.workbuddy/... → 路径命中（软件自建的运行时目录）
        stem = os.path.splitext(os.path.basename(exe))[0].lower()
        if any(word in stem for word in software_words):
            continue
        if any(word in exe for word in software_words):
            continue
        orphans.append(proc)
    return orphans


def group_by_exe(orphans: Sequence[ProcessItem]) -> list[tuple[str, list[ProcessItem]]]:
    """同一个程序的多个实例归成一组 —— 微信能开出十几个 vmmem 一样的插件进程。

    返回 [(exe 路径, [进程...])]，按实例数从多到少排。
    """
    groups: dict[str, list[ProcessItem]] = {}
    for proc in orphans:
        key = str(proc.exe or proc.name).lower()
        groups.setdefault(key, []).append(proc)
    ordered = sorted(groups.items(), key=lambda pair: -len(pair[1]))
    return [(items[0].exe or items[0].name, items) for _key, items in ordered]


def _program_files_x86() -> str:
    return os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")


def _under_program_files(exe: str) -> bool:
    """这个 exe 是不是在某块盘的 Program Files 下。

    优先用环境变量（系统装在哪个盘就跟到哪个盘），再补上其他盘的可能性。
    """
    roots = [
        os.path.normcase(os.environ.get(env, ""))
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)")
    ]
    for letter in string.ascii_uppercase:
        for name in ("Program Files", "Program Files (x86)"):
            roots.append(os.path.normcase(f"{letter}:\\{name}"))
    return any(root and root + os.sep and exe.startswith(root + os.sep) for root in roots)
