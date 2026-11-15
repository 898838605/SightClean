"""BCU 路子上的全局残留扫描器。

我们的老扫描只查「跟软件同名的目录」，扫不到这些散布在系统各处的东西：

| 位置 | 长什么样 | 为什么会出现 |
|---|---|---|
| C:\Windows\Prefetch | STEAM++.EXE-1C19C8D5.pf | 系统记录每个跑过的 exe 的预读数据 |
| C:\ProgramData\...\WER\ReportArchive | AppCrash_Steam++.exe_xxxx | 程序崩过一次就留一个 |
| HKLM/HKCU\...\AppCompatFlags | 以 exe 路径为名的值 | 兼容性疑难解答改过设置 |
| %TEMP% | 同名目录/文件 | 安装和升级时的临时文件 |

这几个位置**按 exe 名匹配**，不是按软件名 —— 所以先把 exe 名找出来。
"""
from __future__ import annotations

import os
import re
import winreg

from core.leftover import KIND_FOLDER, KIND_REGISTRY, Leftover

WINDIR = os.environ.get("WINDIR", r"C:\Windows")
PROGRAMDATA = os.environ.get("PROGRAMDATA", r"C:\ProgramData")

_ICON_SUFFIX = re.compile(r",\s*-?\d+$")


def exe_names(*sources: str) -> list[str]:
    """从 DisplayIcon / UninstallString / 安装路径里挖出 exe 名（不带扩展名、小写去重）。"""
    found: list[str] = []

    def push(path: str) -> None:
        text = _ICON_SUFFIX.sub("", str(path or "").strip().strip('"'))
        if not text:
            return
        base = os.path.basename(text.replace("/", "\\"))
        if not base.lower().endswith(".exe"):
            return
        name = base[:-4].strip()
        if name and name.lower() not in [item.lower() for item in found]:
            found.append(name)

    for source in sources:
        text = str(source or "").strip()
        if not text:
            continue
        if text.startswith('"'):
            end = text.find('"', 1)
            push(text[1:end] if end > 0 else text)
            continue
        index = text.lower().find(".exe")
        push(text[: index + 4] if index > 0 else text)
    return found


def exe_names_from_dir(install_location: str, limit: int = 300, max_depth: int = 4) -> list[str]:
    """把安装目录里所有 exe 的名字收出来。

    这是 BCU 能扫到多个痕迹的关键：一个软件往往带好几个 exe
    （Steam++.exe / Steam++.Accelerator.exe / SteamHelper.exe / uninst.exe），
    Prefetch 和兼容性标记是按**每个 exe** 各记一份的。
    """
    folder = str(install_location or "").strip()
    if not folder or not os.path.isdir(folder):
        return []
    found: list[str] = []
    base_depth = folder.rstrip("\\/").count("\\")
    try:
        for root, dirs, files in os.walk(folder):
            if root.count("\\") - base_depth >= max_depth:
                dirs[:] = []
            for name in files:
                if not name.lower().endswith(".exe"):
                    continue
                stem = name[:-4].strip()
                if stem and stem.lower() not in [item.lower() for item in found]:
                    found.append(stem)
                    if len(found) >= limit:
                        return found
    except OSError:
        pass
    return found


def scan_userassist(names: list[str], install_location: str = "") -> list[str]:
    """从 UserAssist 里挖出**曾经在这个目录里运行过**的 exe 名。

    Windows 会在这里记下每个跑过的程序路径（ROT13 编码），
    所以就算旧版本的 exe 已经被删了，也能顺着它找到对应的 Prefetch 痕迹。
    返回额外的 exe 名（不含扩展名）。
    """
    import codecs

    guid_keys = (
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
        r"\{CEBFF5CD-ACE2-4F4F-9178-9926F41749EA}\Count",
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
        r"\{F4E57C4B-2036-45F0-A9AB-443BCFE33D9F}\Count",
    )
    wanted = {name.lower() for name in names}
    folder = str(install_location or "").rstrip("\\/").lower()
    extra: list[str] = []

    for path in guid_keys:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    value_name, _data, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                try:
                    decoded = codecs.decode(str(value_name), "rot13")
                except Exception:  # noqa: BLE001
                    continue
                if not decoded.lower().endswith(".exe"):
                    continue
                base = os.path.basename(decoded.replace("/", "\\"))
                stem = base[:-4].strip()
                if not stem:
                    continue
                lowered_folder = decoded.lower().replace("/", "\\")
                related = bool(folder) and lowered_folder.startswith(folder)
                if not related and stem.lower() not in wanted:
                    continue
                if stem.lower() not in [item.lower() for item in extra]:
                    extra.append(stem)
    return extra


def scan_prefetch(names: list[str]) -> list[Leftover]:
    """C:/Windows/Prefetch/NAME-XXXXXXXX.pf"""
    if not names:
        return []
    folder = os.path.join(WINDIR, "Prefetch")
    if not os.path.isdir(folder):
        return []
    wanted = {name.upper() for name in names}
    found: list[Leftover] = []
    try:
        entries = os.scandir(folder)
    except OSError:
        return []
    with entries as iterator:
        for entry in iterator:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            stem = entry.name.split("-")[0].upper()
            if not stem.endswith(".EXE"):
                continue
            if stem[:-4] in wanted:
                found.append(Leftover(
                    KIND_FOLDER, "预读取数据", entry.path, name=entry.name,
                    reason=f"系统预读取记录里还留着 {stem}，是这个软件的启动痕迹（删了只影响下次启动速度）",
                ))
    return found


def scan_wer(names: list[str]) -> list[Leftover]:
    """WER 崩溃报告：ReportArchive / ReportQueue 下 AppCrash_<exe>_*"""
    if not names:
        return []
    if not PROGRAMDATA:
        return []
    base = os.path.join(PROGRAMDATA, "Microsoft", "Windows", "WER")
    wanted = {name.lower() for name in names}
    found: list[Leftover] = []
    for sub in ("ReportArchive", "ReportQueue", "Temp"):
        folder = os.path.join(base, sub)
        if not os.path.isdir(folder):
            continue
        try:
            with os.scandir(folder) as iterator:
                for entry in iterator:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    lowered = entry.name.lower()
                    if not lowered.startswith(("appcrash_", "apphang_")):
                        continue
                    tail = lowered.split("_", 1)[1] if "_" in lowered else ""
                    stem = tail.split(".")[0].split("_")[0]
                    if stem in wanted:
                        found.append(Leftover(
                            KIND_FOLDER, "崩溃报告", entry.path, name=entry.name,
                            reason=f"Windows 错误报告里存着 {stem} 的崩溃记录，卸载后没清",
                        ))
        except OSError:
            continue
    return found


def scan_appcompat(names: list[str]) -> list[Leftover]:
    """AppCompatFlags：兼容性设置，值名就是 exe 完整路径。"""
    if not names:
        return []
    wanted = {name.lower() for name in names}
    targets = (
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Compatibility Assistant\Store"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"),
    )
    found: list[Leftover] = []
    for hive, path in targets:
        hive_name = "HKEY_CURRENT_USER" if hive == winreg.HKEY_CURRENT_USER else "HKEY_LOCAL_MACHINE"
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    value_name, _data, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                base = os.path.basename(str(value_name).replace("/", "\\")).lower()
                if not base.endswith(".exe"):
                    continue
                if base[:-4] in wanted:
                    found.append(Leftover(
                        KIND_REGISTRY, "兼容性设置",
                        f"{hive_name}\\{path} :: {value_name}",
                        name=os.path.basename(value_name),
                        reason=f"兼容性标志里记着 {base}，是这个软件留下的（删了只影响它的兼容性设置）",
                    ))
    return found


def scan_startup(names: list[str]) -> list[Leftover]:
    """开机启动项：Run 注册表 + 启动文件夹。

    软件卸了但启动项还留着，下次开机就会报「找不到文件」。
    """
    if not names:
        return []
    wanted = {name.lower() for name in names}
    found: list[Leftover] = []

    run_keys = (
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
    )
    for hive, path in run_keys:
        hive_name = ("HKEY_CURRENT_USER" if hive == winreg.HKEY_CURRENT_USER
                     else "HKEY_LOCAL_MACHINE")
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    value_name, data, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                command = str(data or "")
                exe = os.path.basename(
                    command.strip().strip('"').split('"')[0].split(" ")[0].replace("/", "\\")
                ).lower()
                hit = exe.endswith(".exe") and exe[:-4] in wanted
                if not hit:
                    # 值名本身可能就是软件名
                    hit = str(value_name).lower().replace(" ", "") in {
                        name.replace(" ", "") for name in wanted}
                if hit:
                    found.append(Leftover(
                        KIND_REGISTRY, "开机启动项", f"{hive_name}\\{path} :: {value_name}",
                        name=str(value_name),
                        reason="软件没了但开机启动项还在，下次开机会报「找不到文件」",
                    ))

    # 启动文件夹里的快捷方式
    appdata = os.environ.get("APPDATA", "")
    programdata = os.environ.get("PROGRAMDATA", "")
    for label, folder in (
        ("启动文件夹", os.path.join(appdata, "Microsoft", "Windows",
                                    "Start Menu", "Programs", "Startup") if appdata else ""),
        ("公共启动文件夹", os.path.join(programdata, "Microsoft", "Windows",
                                        "Start Menu", "Programs", "StartUp") if programdata else ""),
    ):
        if not folder or not os.path.isdir(folder):
            continue
        try:
            with os.scandir(folder) as iterator:
                for entry in iterator:
                    stem = os.path.splitext(entry.name)[0].lower()
                    if any(stem == n or stem.startswith(n + " ") for n in wanted):
                        found.append(Leftover(
                            KIND_FOLDER, label, entry.path, name=entry.name,
                            reason=f"{label}里还留着这个软件的启动快捷方式",
                        ))
        except OSError:
            pass
    return found


def scan_registered_apps(name: str, exe_names_list: list[str]) -> list[Leftover]:
    """RegisteredApplications：软件往系统里注册的「默认程序」入口。"""
    wanted = {str(name or "").lower().strip()}
    wanted.update(str(n).lower() for n in exe_names_list)
    wanted.discard("")
    if not wanted:
        return []
    found: list[Leftover] = []
    for hive, hive_name in ((winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE"),
                            (winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER")):
        for path in (r"SOFTWARE\RegisteredApplications",
                     r"SOFTWARE\WOW6432Node\RegisteredApplications"):
            try:
                key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
            except OSError:
                continue
            with key:
                index = 0
                while True:
                    try:
                        value_name, _data, _kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    lowered = str(value_name).lower().strip()
                    if lowered in wanted or any(
                            w and (lowered == w or lowered.startswith(w + " ")) for w in wanted):
                        found.append(Leftover(
                            KIND_REGISTRY, "已注册应用",
                            f"{hive_name}\\{path} :: {value_name}",
                            name=str(value_name),
                            reason="系统「默认程序」里还挂着这个软件的注册项，卸载后没摘掉",
                        ))
    return found


def scan_temp(names: list[str]) -> list[Leftover]:
    """%TEMP% 下的同名目录/文件。"""
    if not names:
        return []
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    if not temp or not os.path.isdir(temp):
        return []
    wanted = {name.lower() for name in names}
    found: list[Leftover] = []
    try:
        with os.scandir(temp) as iterator:
            for entry in iterator:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if entry.name.lower() in wanted:
                    found.append(Leftover(
                        KIND_FOLDER, "临时文件", entry.path, name=entry.name,
                        reason="临时目录里还留着同名文件夹，多半是安装或升级时没清掉的",
                    ))
    except OSError:
        pass
    return found
