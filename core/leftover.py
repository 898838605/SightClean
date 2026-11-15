"""卸载之后找残余：文件夹 + 注册表。

扫描**只读**，绝不在扫的时候改任何东西。清理是单独的动作，
注册表必须先备份，而且走三道坎。

保守原则：
- 注册表只查三个明确的位置（卸载登记、Software\<发布者>、Software\<软件名>）
- 不碰 SYSTEM / Services / Classes 这类关键位置
- 名字对不上就不列 —— 宁可不报，也不误删
"""
from __future__ import annotations

import os
import string
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path

from core.delete import DeleteItem, Mode, delete_one
from core.registry import delete_key_tree, export_keys

KIND_FOLDER = "folder"
KIND_REGISTRY = "registry"

KIND_LABELS = {KIND_FOLDER: "文件夹", KIND_REGISTRY: "注册表"}

# 卸载登记键，四个视图都要看
_UNINSTALL_PATHS = (
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU"),
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
)


@dataclass
class Leftover:
    kind: str
    label: str          # 给人看的来源：安装位置 / 卸载登记 / 软件配置
    path: str           # 文件夹路径 或 注册表全路径
    name: str = ""
    risk: str = "caution"
    detail: str = ""    # 补充说明
    reason: str = ""    # 为什么会把它列出来（检测原因）
    size: int | None = None   # 文件夹大小，后台算完回填

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def identity(self) -> str:
        return f"{self.kind}:{self.path}"


@dataclass
class CleanResult:
    leftover: Leftover
    ok: bool
    error: str | None = None
    at: float = field(default_factory=time.time)


@dataclass
class CleanReport:
    items: list[CleanResult] = field(default_factory=list)
    backup: Path | None = None
    started: float = field(default_factory=time.time)
    finished: float = 0.0

    @property
    def ok_count(self) -> int:
        return sum(1 for item in self.items if item.ok)

    @property
    def fail_count(self) -> int:
        return sum(1 for item in self.items if not item.ok)

    @property
    def summary(self) -> str:
        if not self.items:
            return "这次没有要清理的残余"
        bits = [f"清掉 {self.ok_count} 项"]
        if self.fail_count:
            bits.append(f"{self.fail_count} 项没动")
        if self.backup:
            bits.append(f"注册表已备份到 {self.backup.name}")
        return "；".join(bits)

    def failure_reasons(self) -> list[tuple[str, str]]:
        return [(item.leftover.path, item.error or "原因不明")
                for item in self.items if not item.ok]

    def as_log_text(self) -> str:
        lines = [
            "SightClean · 卸载残余清理",
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started))}",
            f"备份：{self.backup or '（这次没有注册表项）'}",
            f"共 {len(self.items)} 项：成功 {self.ok_count}，失败 {self.fail_count}",
            "",
        ]
        for result in self.items:
            state = "成功" if result.ok else "失败"
            lines.append(f"{state} | {result.leftover.kind_label} | "
                         f"{result.leftover.path} | {result.error or '无'}")
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 扫描（只读）
# --------------------------------------------------------------------------- #
def _name_candidates(name: str, publisher: str) -> list[str]:
    """可能是目录名的几个候选：显示名、显示名去版本号、发布者、发布者第一段。"""
    seen: list[str] = []

    def push(value: str) -> None:
        text = (value or "").strip()
        if text and text.lower() not in [item.lower() for item in seen]:
            seen.append(text)

    push(name)
    # 「7-Zip 24.09 (x64)」→「7-Zip」
    base = (name or "").split("(")[0].strip()
    if base and base.lower() != (name or "").lower():
        push(base)
    stripped = " ".join(
        chunk for chunk in base.split()
        if not any(ch.isdigit() for ch in chunk)
    ).strip()
    if stripped and stripped.lower() != base.lower():
        push(stripped)
    push(publisher)
    if publisher:
        push(publisher.strip().split()[0])
    return seen


def _software_key_exists(dir_name: str) -> bool:
    """HKLM/HKCU 的 SOFTWARE 下有没有跟目录同名的键。

    有就说明这软件是正经装过的（只是显示名跟目录名对不上，比如火绒 = Huorong）。
    """
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for base in (r"SOFTWARE", r"SOFTWARE\WOW6432Node"):
            try:
                with winreg.OpenKey(hive, rf"{base}\{dir_name}", 0, winreg.KEY_READ):
                    return True
            except OSError:
                continue
    return False


def scan_shortcuts(name: str, publisher: str) -> list[Leftover]:
    """桌面和开始菜单的快捷方式。软件卸了但图标还在，用户会以为没卸掉。"""
    found: list[Leftover] = []
    candidates = _name_candidates(name, publisher)
    if not candidates:
        return found
    home = os.environ.get("USERPROFILE", "")
    appdata = os.environ.get("APPDATA", "")
    programdata = os.environ.get("PROGRAMDATA", "")
    # 公共桌面和公共开始菜单不能写死 C 盘 —— 系统可能装在别的盘。
    # %PUBLIC% 就是公共用户目录，%PROGRAMDATA% 跟着系统盘走。
    public = os.environ.get("PUBLIC", "")
    if not public and home:
        public = os.path.join(os.path.dirname(home.rstrip("\\/")), "Public")
    roots = [
        ("桌面", os.path.join(home, "Desktop") if home else ""),
        ("公共桌面", os.path.join(public, "Desktop") if public else ""),
        ("开始菜单", os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")
         if appdata else ""),
        ("公共开始菜单",
         os.path.join(programdata, "Microsoft", "Windows", "Start Menu", "Programs")
         if programdata else ""),
    ]
    seen: set[str] = set()
    for label, root in roots:
        if not root or not os.path.isdir(root):
            continue
        for entry in _iter_lnk(root):
            key = os.path.normcase(entry)
            if key in seen:
                continue
            base = os.path.splitext(os.path.basename(entry))[0].lower()
            # 只认「完全相等」或「候选名 + 空格开头的变体」——
            # 用包含匹配的话，「微信」会把「微信开发者工具」也当成残留，误伤太常见。
            if not any(base == cand.lower() or base.startswith(cand.lower() + " ")
                       for cand in candidates):
                continue
            seen.add(key)
            found.append(Leftover(
                KIND_FOLDER, label, entry, name=os.path.basename(entry),
                reason=f"{label}上还留着这个软件的快捷方式，软件本身已经不在了",
            ))
    return found


def _iter_lnk(root: str, depth: int = 3):
    """在桌面上找一层，开始菜单里找三层（开始菜单目录结构深）。"""
    stack = [(root, 0)]
    while stack:
        current, level = stack.pop()
        try:
            with os.scandir(current) as scandir:
                for entry in scandir:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if level + 1 < depth:
                                stack.append((entry.path, level + 1))
                        elif entry.name.lower().endswith(".lnk"):
                            yield entry.path
                    except OSError:
                        continue
        except OSError:
            continue


def scan_folders(install_location: str, publisher: str, name: str) -> list[Leftover]:
    """安装位置 + 三个常见用户数据目录。只 stat 固定几个点，不扫盘。"""
    found: list[Leftover] = []
    seen: set[str] = set()

    def add(label: str, path: str, display: str, reason: str) -> None:
        key = os.path.normcase(path)
        if key in seen or os.path.normcase(path) == os.path.normcase(install_location or ""):
            return
        seen.add(key)
        found.append(Leftover(KIND_FOLDER, label, path, name=display, reason=reason))

    if install_location and os.path.isdir(install_location):
        found.append(Leftover(
            KIND_FOLDER, "安装位置", install_location,
            name=os.path.basename(install_location.rstrip("\\/")),
            detail="卸载程序没删干净的原安装目录",
            reason="卸载登记里记着的安装目录，卸载完它还在",
        ))
        seen.add(os.path.normcase(install_location))

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    # LocalLow 是 IE/低完整性进程的地盘，不少国产软件的把缓存扔这儿
    local_low = os.path.join(local_appdata, "Low") if local_appdata else ""
    for label, base in (
        ("%APPDATA%", os.environ.get("APPDATA", "")),
        ("%LOCALAPPDATA%", local_appdata),
        ("%LOCALAPPDATA%\\Low", local_low),
        ("%PROGRAMDATA%", os.environ.get("PROGRAMDATA", "")),
    ):
        if not base:
            continue
        for candidate in _name_candidates(name, publisher):
            path = os.path.join(base, candidate)
            if os.path.isdir(path):
                add(f"{label}\\{candidate}", path, candidate,
                    f"在 {label} 下找到了跟软件名/发布者同名的目录")
    return found


def _iter_subkeys(hive, base: str):
    try:
        root = winreg.OpenKey(hive, base, 0, winreg.KEY_READ)
    except OSError:
        return
    with root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as key:
                    yield sub, key
            except OSError:
                continue


def _read_str(key, name: str) -> str:
    try:
        value, _kind = winreg.QueryValueEx(key, name)
        return str(value or "").strip()
    except OSError:
        return ""


def scan_registry(name: str, publisher: str, install_location: str = "") -> list[Leftover]:
    """只查两个位置：卸载登记、Software\\<候选名>。候选名包括软件名和发布者。"""
    found: list[Leftover] = []
    software = (name or "").strip()
    brand = (publisher or "").strip()
    if not software and not brand:
        return found
    lowered_name = software.lower()
    candidates = _name_candidates(software, brand)

    # 1) 卸载登记：DisplayName 对得上，或 InstallLocation 正好是刚卸载的那个目录
    seen: set[str] = set()
    for hive, base, short in _UNINSTALL_PATHS:
        for sub, key in _iter_subkeys(hive, base):
            display = _read_str(key, "DisplayName")
            location = _read_str(key, "InstallLocation")
            matched = False
            if lowered_name and display.lower() == lowered_name:
                matched = True
            elif install_location and location and \
                    os.path.normcase(location.rstrip("\\/")) == os.path.normcase(install_location.rstrip("\\/")):
                matched = True
            if not matched:
                continue
            full = f"{'HKEY_LOCAL_MACHINE' if hive == winreg.HKEY_LOCAL_MACHINE else 'HKEY_CURRENT_USER'}\\{base}\\{sub}"
            if full.lower() in seen:
                continue
            seen.add(full.lower())
            found.append(Leftover(
                KIND_REGISTRY, "卸载登记", full, name=sub,
                detail="软件已经不在了，登记项还留着",
                reason="卸载登记里的 DisplayName 跟这个软件对得上",
            ))

    # 2) Software\ 下的厂商目录 / 软件目录
    for short_name, hive_name, hive in (
        ("HKLM", "HKEY_LOCAL_MACHINE", winreg.HKEY_LOCAL_MACHINE),
        ("HKCU", "HKEY_CURRENT_USER", winreg.HKEY_CURRENT_USER),
    ):
        for base in (r"SOFTWARE", r"SOFTWARE\WOW6432Node"):
            for candidate in candidates:
                if not candidate:
                    continue
                key_path = rf"{base}\{candidate}"
                try:
                    with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ):
                        pass
                except OSError:
                    continue
                full = f"{hive_name}\\{key_path}"
                if full.lower() in seen:
                    continue
                seen.add(full.lower())
                found.append(Leftover(
                    KIND_REGISTRY, "软件配置", full, name=candidate,
                    detail="软件自己的设置存这儿",
                    reason=f"{short_name} 下有个键叫「{candidate}」，跟软件名/发布者对得上",
                ))
    return found


def _program_files_roots() -> tuple[str, ...]:
    """所有盘上的 Program Files —— 不能只认 C 盘。"""
    roots: list[str] = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        value = os.environ.get(env)
        if value:
            roots.append(value)
    for letter in string.ascii_uppercase:
        for name in ("Program Files", "Program Files (x86)"):
            path = f"{letter}:\\{name}"
            if os.path.isdir(path) and path not in roots:
                roots.append(path)
    return tuple(roots)


def scan_orphans(roots=None, limit: int = 400) -> list[Leftover]:
    """Program Files 下那些在卸载列表里找不到对应登记的目录。

    「只删登记、不删文件」的软件（国产工具里很常见）会变成孤儿：
    卸载列表里看不见，文件却还在占地方、程序还能跑。
    这里把它们翻出来，交给用户自己判断。
    """
    known_names: set[str] = set()
    known_paths: set[str] = set()
    for hive, base, _short in _UNINSTALL_PATHS:
        for _sub, key in _iter_subkeys(hive, base):
            display = _read_str(key, "DisplayName").lower().strip()
            location = _read_str(key, "InstallLocation").lower().rstrip("\\/").strip()
            if display:
                known_names.add(display)
            if location:
                known_paths.add(location)

    # 这些是系统自己的地盘，不是「软件」
    skip = {
        "common files", "internet explorer", "msbuild", "reference assemblies",
        "uninstall information", "modifiablewindowsapps", "dotnet",
        "microsoft office", "microsoft.net", "desktop.ini", "temp", "tmp",
        "hyper-v", "wsl", "difx", "windows sidebar", "windowsapps",
        "installshield installation information",
    }
    skip_prefix = ("windows", "microsoft")

    found: list[Leftover] = []
    for root in (roots or _program_files_roots()):
        if not os.path.isdir(root):
            continue
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in sorted(entries, key=lambda item: item.name.lower()):
            if len(found) >= limit:
                return found
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            name = entry.name.lower()
            path = entry.path.lower().rstrip("\\/")
            if name in skip or name.startswith(skip_prefix) or path in known_paths:
                continue
            # 登记里写着「HZVPN 1.2」而目录叫「HZVPN」，得算同一个
            if any(name in known or (len(known) > 3 and known in name)
                   for known in known_names):
                continue
            # 登记里的安装路径包含这个目录名（Google → ...\Google\Chrome）
            if any(f"\\{name}\\" in known_path for known_path in known_paths):
                continue
            # SOFTWARE 下有同名键 → 这软件是登记过的，只是显示名跟目录名不一样
            if _software_key_exists(entry.name):
                continue
            try:
                inner = len(os.listdir(entry.path))
            except OSError:
                inner = -1
            if inner == 0:
                continue          # 空壳目录不算
            found.append(Leftover(
                KIND_FOLDER, "Program Files", entry.path, name=entry.name,
                reason="这个目录在 Program Files 下，但卸载列表里找不到对应的登记"
                       "（软件可能只删了登记没删文件）",
            ))
    return found


def scan(install_location: str, publisher: str, name: str,
         exe_names=None) -> list[Leftover]:
    """全方位扫：安装目录、用户数据、快捷方式、注册表，加上系统各处的痕迹。

    exe_names 是这个软件的 exe 名（不带扩展名）—— Prefetch、崩溃报告、
    兼容性标记都是按 exe 名记的，没有它这三样就扫不到。
    """
    items: list[Leftover] = []
    names = [str(n) for n in (exe_names or []) if n]
    try:
        items.extend(scan_folders(install_location, publisher, name))
    except OSError:
        pass
    try:
        items.extend(scan_shortcuts(name, publisher))
    except OSError:
        pass
    if names:
        from core import cleaners

        for scanner in (cleaners.scan_prefetch, cleaners.scan_wer,
                        cleaners.scan_appcompat, cleaners.scan_temp,
                        cleaners.scan_startup):
            try:
                items.extend(scanner(names))
            except Exception:  # noqa: BLE001
                continue
    try:
        items.extend(cleaners.scan_registered_apps(name, names))
    except Exception:  # noqa: BLE001
        pass
    try:
        items.extend(scan_registry(name, publisher, install_location))
    except OSError:
        pass
    return items


# --------------------------------------------------------------------------- #
# 清理
# --------------------------------------------------------------------------- #
def clean(leftovers, log_dir: Path | str | None = None, progress=None) -> CleanReport:
    """一键清理：注册表先整体备份，再逐项处理。备份失败就一项都不动。

    progress(done, total, 当前路径) 每项回调一次，给界面画进度用。
    """
    report = CleanReport(items=[])
    targets = list(leftovers)
    total = len(targets)
    if not targets:
        report.finished = time.time()
        return report

    def _tick(index: int, path: str) -> None:
        if progress is not None:
            try:
                progress(index, total, path)
            except Exception:  # noqa: BLE001 - 回调出问题不能影响清理
                pass

    # 注册表残留分两种：整个键、单个值（AppCompatFlags 那种）
    reg_keys = [item.path for item in targets
                if item.kind == KIND_REGISTRY and " :: " not in item.path]
    reg_values = [item.path for item in targets
                  if item.kind == KIND_REGISTRY and " :: " in item.path]
    if reg_keys:
        _tick(0, "正在备份注册表…")
        backup, error = export_keys(reg_keys)
        if backup is None:
            for item in targets:
                report.items.append(CleanResult(item, False, error or "备份没成功，这次一项都没动"))
            report.finished = time.time()
            if progress is not None:
                try:
                    progress(total, total, "")
                except Exception:  # noqa: BLE001
                    pass
            return report
        report.backup = backup

    if reg_values:
        _tick(0, "正在备份注册表值…")
        from core.regvalue import export_values

        value_backup, value_error = export_values(reg_values)
        if value_backup is None:
            for item in targets:
                if item.kind == KIND_REGISTRY and " :: " in item.path:
                    report.items.append(CleanResult(
                        item, False, value_error or "备份没成功，这些值一项都没动"))
            # 值失败不影响文件，文件照删
            targets = [item for item in targets
                       if not (item.kind == KIND_REGISTRY and " :: " in item.path)]
        elif report.backup is None:
            report.backup = value_backup

    for index, item in enumerate(targets):
        _tick(index, item.path)
        if item.kind == KIND_REGISTRY and " :: " in item.path:
            from core.regvalue import delete_value

            ok, error = delete_value(item.path)
            report.items.append(CleanResult(item, ok, error))
        elif item.kind == KIND_REGISTRY:
            # 残留是一整棵子树，连子键一起删（已经备份过了）
            ok, error = delete_key_tree(item.path)
            report.items.append(CleanResult(item, ok, error))
        else:
            delete_item = DeleteItem(
                human_path=item.path, display=item.name or item.path,
                is_dir=os.path.isdir(item.path),      # 快捷方式是文件，不能当目录删
                risk="caution", effect=item.detail,
            )
            outcome = delete_one(delete_item, Mode.RECYCLE)
            report.items.append(CleanResult(item, outcome.ok, outcome.error))

    report.finished = time.time()
    _tick(total, "")
    return report


def measure_folders(items, progress=None) -> dict[str, int]:
    """给文件夹类的残余算大小。注册表项跳过。返回 {identity: 字节数}。"""
    sizes: dict[str, int] = {}
    folders = [item for item in items if item.kind == KIND_FOLDER]
    total = len(folders)
    for index, item in enumerate(folders):
        if progress is not None:
            try:
                progress(index, total, item.path)
            except Exception:  # noqa: BLE001
                pass
        try:
            # Prefetch 那种是单个文件，不是目录
            if os.path.isdir(item.path):
                sizes[item.identity] = _dir_size(item.path)
            elif os.path.isfile(item.path):
                sizes[item.identity] = os.path.getsize(item.path)
            else:
                sizes[item.identity] = 0
        except OSError:
            sizes[item.identity] = 0
    if progress is not None:
        try:
            progress(total, total, "")
        except Exception:  # noqa: BLE001
            pass
    return sizes


def _dir_size(path: str, limit: int = 200_000) -> int:
    """自己数一遍，碰到链接就跳过（绝不顺着链接钻出去）。"""
    total = 0
    count = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as scandir:
                for entry in scandir:
                    count += 1
                    if count > limit:
                        return total
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def make_measure_worker(items):
    """丢线程池算大小，界面先显示「计算中…」。"""
    from PySide6.QtCore import QObject, QRunnable, Signal

    class Signals(QObject):
        progress = Signal(int, int)
        finished = Signal(object)

    class _Task(QRunnable):
        def __init__(self) -> None:
            super().__init__()
            self.setAutoDelete(True)
            self.signals = Signals()

        def run(self) -> None:
            try:
                sizes = measure_folders(
                    items, progress=lambda done, total, _path: self.signals.progress.emit(done, total)
                )
            except Exception:  # noqa: BLE001
                sizes = {}
            try:
                self.signals.finished.emit(sizes)
            except RuntimeError:
                pass

    return _Task()


def make_clean_worker(leftovers, log_dir: Path | str | None = None):
    """丢进线程池跑，界面靠信号刷进度。"""
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
            try:
                report = clean(leftovers, log_dir,
                               progress=lambda done, total, path: self.signals.progress.emit(done, total, path))
            except Exception as exc:  # noqa: BLE001
                report = CleanReport(items=[
                    CleanResult(item, False, f"清理时出错：{exc}") for item in leftovers
                ])
                report.finished = time.time()
            try:
                self.signals.finished.emit(report)
            except RuntimeError:
                pass

    return _Task()
