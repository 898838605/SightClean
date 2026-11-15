"""安装器类型识别 + 对应的静默卸载命令。

借鉴 BCUninstaller 的做法：不同安装器生成的卸载程序，静默参数完全不一样。
硬套一个 /S 给所有软件，只会让一半的卸载程序弹出一个「参数错误」框。

BCU 的 UninstallerType 有 14 种，我们这边能落到地的是：

| 类型 | 静默参数 | 依据 |
|---|---|---|
| msi | msiexec /qb /X{产品码} REBOOT=ReallySuppress /norestart | 有产品码 |
| nsis | uninstaller.exe /S | 卸载程序名 uninst*.exe |
| inno | unins000.exe /VERYSILENT /NORESTART | 卸载程序名 unins*.exe |
| steam | steam://uninstall/{appid} | 安装路径含 steamapps |
| winget | winget uninstall --id {id} | winget 来源 |
| storeapp | PowerShell Remove-AppxPackage | 包全名特征 |
| unknown | 原样执行，不猜 | 认不出来 |

**认不出来就不加参数** —— 猜错参数比不加更糟。
"""
from __future__ import annotations

import os
import re

TYPE_LABELS = {
    "msi": "MSI 安装包",
    "nsis": "NSIS 安装器",
    "inno": "Inno Setup",
    "installshield": "InstallShield",
    "steam": "Steam 游戏",
    "winget": "winget 包",
    "storeapp": "商店应用",
    "squirrel": "Squirrel（Electron）",
    "unknown": "认不出安装器",
}

_INNO = re.compile(r"^unins\d*\.exe$", re.I)
_NSIS = re.compile(r"^uninst(?:all)?\.exe$", re.I)
_SQUIRREL = re.compile(r"^update\.exe$", re.I)


def _base(path: str) -> str:
    text = str(path or "").strip().strip('"')
    text = text.split(" --")[0]
    return os.path.basename(text)


def _exe_of(command: str) -> str:
    """从一整行命令里抠出 exe 路径。"""
    text = str(command or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 0 else text.strip('"')
    # 路径里可能有空格（C:\Program Files\...），所以不能按空格切。
    # 认 .exe 结尾更可靠：参数总是跟在它后面。
    index = text.lower().find(".exe")
    if index > 0:
        return text[: index + 4]
    return text.split(" ")[0]


def detect(uninstall_string: str, install_location: str = "",
           source: str = "", product_code: str = "") -> str:
    """判断这个软件是哪种安装器装的。认不出来返回 unknown。"""
    if source == "winget":
        return "winget"
    command = str(uninstall_string or "").lower()
    location = str(install_location or "").lower()

    if "steamapps" in location or "steam://" in command:
        return "steam"
    if "msiexec" in command or product_code:
        return "msi"

    exe = _base(_exe_of(uninstall_string))
    if _INNO.match(exe):
        return "inno"
    if _NSIS.match(exe):
        return "nsis"
    if _SQUIRREL.match(exe):
        return "squirrel"
    if "installshield" in command:
        return "installshield"

    return "unknown"


def quiet_command(uninstall_string: str, install_location: str = "",
                  source: str = "", product_code: str = "",
                  quiet_uninstall_string: str = "", package_id: str = "") -> str:
    """拼一条能静默跑完的卸载命令。拼不出来返回空串。"""
    if quiet_uninstall_string.strip():
        return quiet_uninstall_string.strip()

    kind = detect(uninstall_string, install_location, source, product_code)
    if kind == "msi" and product_code:
        return f"MsiExec.exe /qb /X{product_code} REBOOT=ReallySuppress /norestart"
    if kind == "winget" and package_id:
        return 'winget uninstall --id "' + package_id + '" --accept-source-agreements'
    if kind == "steam" and package_id:
        return f"steam://uninstall/{package_id}"

    base = str(uninstall_string or "").strip()
    if not base:
        return ""
    # 已经带参数的别硬塞 —— 可能是厂商特意配好的。
    # 注意：不能按空格判断，路径本身就可能带空格（C:\Program Files\...）。
    exe_path = _exe_of(base)
    rest = base[len(exe_path):].strip() if exe_path else ""
    if rest and not rest.startswith('"'):
        return ""
    quoted = base if base.startswith('"') else '"' + base + '"'
    if kind == "nsis":
        return quoted + " /S"
    if kind == "inno":
        return quoted + " /VERYSILENT /NORESTART"
    if kind == "squirrel":
        return ""          # Squirrel 的 update.exe 语法特殊，不猜
    return ""


def label(kind: str) -> str:
    return TYPE_LABELS.get(kind, kind)
