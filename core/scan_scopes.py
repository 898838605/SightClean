"""可扫描的位置清单。

用户自己勾要扫哪些。我们把能想到的位置全列出来，标清楚风险和该怎么用，
默认只勾安全的那几个。

**这里不出现具体盘符。** 「所有盘的 Program Files」是一个选项，
实际去哪些盘是运行时探测的 —— 换台机器、加减硬盘都不用改代码。

风险分三档：
    safe    删了只影响软件本身，放开扫
    caution 可能混着用户自己的数据（文档、聊天记录），扫之前要想清楚
    risky   系统自己的地盘（安装器缓存、驱动），删了会出问题，默认不勾
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    key: str
    label: str          # 给人看的名字
    confirm: str        # 确认时用的人话
    paths: tuple[str, ...]   # 实际要扫的路径（可能有多处）
    risk: str           # safe / caution / risky
    note: str           # 扫它会扫到什么
    default: bool       # 默认勾不勾

    @property
    def path(self) -> str:
        """显示用的一句话。多处时只列第一个加总数。"""
        if not self.paths:
            return ""
        if len(self.paths) == 1:
            return self.paths[0]
        return f"{self.paths[0]} 等 {len(self.paths)} 处"

    @property
    def available(self) -> bool:
        return any(os.path.isdir(path) for path in self.paths)


def _env(name: str) -> str:
    return os.environ.get(name, "") or ""


def _user(name: str) -> str:
    home = _env("USERPROFILE")
    return os.path.join(home, name) if home else ""


def fixed_drives() -> list[str]:
    """这台机器上真实存在的**固定磁盘**盘符。

    不能写死 C/D/E —— 插着移动硬盘、光驱、虚拟盘的机器各不相同。
    只用 GetDriveTypeW 认 DRIVE_FIXED（3），U 盘和光驱跳过。
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
        kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetDriveTypeW.restype = ctypes.c_uint
    except Exception:  # noqa: BLE001 - 非 Windows 或没有 ctypes 就退回系统盘
        return ["C"]

    found: list[str] = []
    for index in range(26):
        if not (mask & (1 << index)):
            continue
        letter = chr(ord("A") + index)
        if kernel32.GetDriveTypeW(f"{letter}:\\") == 3:      # DRIVE_FIXED
            found.append(letter)
    return found or ["C"]


def _program_files_paths() -> tuple[str, ...]:
    """所有固定盘上的 Program Files / Program Files (x86)。"""
    found: list[str] = []
    for letter in fixed_drives():
        for name in ("Program Files", "Program Files (x86)"):
            path = os.path.join(f"{letter}:\\", name)
            if os.path.isdir(path):
                found.append(path)
    return tuple(found)


def build_scopes() -> list[Scope]:
    """列出这台机器上真实存在的位置。"""
    local = _env("LOCALAPPDATA")
    windir = _env("WINDIR") or r"C:\Windows"
    programdata = _env("PROGRAMDATA")

    return [
        Scope("appdata", "%APPDATA%", "漫游配置目录",
              (os.environ.get("APPDATA", "") or "",), "safe",
              "软件的设置、账号、模板。删了软件要重新配置，但不会影响别的软件", True),
        Scope("localappdata", "%LOCALAPPDATA%", "本地数据目录", (local,), "safe",
              "缓存、本地数据库、日志。绝大多数残留在这儿", True),
        Scope("locallow", "%LOCALAPPDATA%\\Low", "低完整性数据目录",
              (os.path.join(local, "Low") if local else "",), "safe",
              "IE 控件、部分国产软件把缓存扔这儿", True),
        Scope("packages", "%LOCALAPPDATA%\\Packages", "商店应用数据",
              (os.path.join(local, "Packages") if local else "",), "safe",
              "UWP / MSIX 应用（应用商店装的）的沙箱数据", True),
        Scope("local_programs", "%LOCALAPPDATA%\\Programs", "用户级程序目录",
              (os.path.join(local, "Programs") if local else "",), "safe",
              "装在用户目录下的程序（VS Code、Chrome、Discord 这类）。"
              "卸载后目录还在就是残留", True),
        Scope("programdata", "%PROGRAMDATA%", "公共数据目录", (programdata,), "safe",
              "装给所有用户的东西：许可证、共享配置、服务数据", True),
        Scope("program_files", "所有盘的 Program Files", "各盘的程序目录",
              _program_files_paths(), "safe",
              "软件本体。卸载后目录还在就是残留", True),
        Scope("windows_temp", "%WINDIR%\\Temp", "系统临时目录",
              (os.path.join(windir, "Temp"),), "safe",
              "安装程序解压出来的临时文件，删了没影响", False),
        Scope("documents", "%USERPROFILE%\\Documents", "文档目录",
              (_user("Documents"),), "caution",
              "这里可能混着你的聊天记录、游戏存档、工程文件，扫之前想清楚", False),
        Scope("downloads", "%USERPROFILE%\\Downloads", "下载目录",
              (_user("Downloads"),), "caution",
              "安装包、解压出来的目录常留在这儿。也可能是你要用的东西", False),
        Scope("saved_games", "%USERPROFILE%\\Saved Games", "游戏存档目录",
              (_user("Saved Games"),), "caution",
              "很多游戏直接在这儿建目录。删了存档就没了", False),
        Scope("userprofile", "%USERPROFILE% 根", "用户主目录",
              (_env("USERPROFILE"),), "caution",
              "会扫到 .ssh / .gitconfig 这类你自己的配置", False),
        Scope("public", "%PUBLIC%", "公共用户目录", (_env("PUBLIC"),), "caution",
              "所有用户共享的文档和桌面", False),
        Scope("system_profile", "%WINDIR%\\System32\\config\\systemprofile\\AppData",
              "SYSTEM 账户数据",
              (os.path.join(windir, "System32", "config", "systemprofile", "AppData"),),
              "risky", "服务、SYSTEM 身份跑的程序把数据放这儿。动之前确认没有服务在用",
              False),
        Scope("windows_installer", "%WINDIR%\\Installer", "MSI 安装缓存",
              (os.path.join(windir, "Installer"),), "risky",
              "删了会导致软件无法修复安装、无法正常卸载，能占几个 G 但别乱动", False),
        Scope("package_cache", "%PROGRAMDATA%\\Package Cache", "安装器缓存",
              (os.path.join(programdata, "Package Cache") if programdata else "",),
              "risky", "同上，是已装软件的安装包缓存", False),
        Scope("drivers", "%WINDIR%\\System32\\drivers", "驱动目录",
              (os.path.join(windir, "System32", "drivers"),), "risky",
              "删错了直接蓝屏，不建议勾", False),
    ]


def available_scopes() -> list[Scope]:
    return [scope for scope in build_scopes() if scope.available]


def by_key() -> dict[str, Scope]:
    return {scope.key: scope for scope in build_scopes()}


def default_keys() -> list[str]:
    return [scope.key for scope in build_scopes() if scope.default and scope.available]


def resolve(keys) -> list[Scope]:
    """把用户选的 key 变成真实存在的 Scope。"""
    wanted = set(keys or [])
    return [scope for scope in available_scopes() if scope.key in wanted]


def expand(keys) -> list[tuple[str, str]]:
    """把 key 展开成 (显示名, 真实路径) —— 一个 scope 可能对应多个目录。"""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for scope in resolve(keys):
        for path in scope.paths:
            if not path or not os.path.isdir(path):
                continue
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((scope.label, path))
    return pairs
