r"""让 AI 帮忙找「名字对不上」的残留。

我们的扫描器有个根本盲区：只会拿「已知软件的名字」去找同名目录。
可现实里目录名经常跟软件名对不上 ——

    HZVPN            → 目录叫 HZVPN，登记录里根本没这个名字
    某国产加速器      → 目录叫 SomeVendor，软件叫「XX加速器」

这类东西我们的规则永远扫不到，因为**我们不知道该找什么名字**。

这个模块反过来做：先把「不属于任何已装软件」的目录和注册表键**列出来**，
再让 AI 判断哪些像已卸载软件的垃圾。

AI 只做判断，不做决定：
- 输出 leftover / keep / unsure 三级，外加理由
- 默认一个都不勾，用户看着理由自己决定
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
import winreg
from dataclasses import dataclass

SYSTEM_WORDS = (
    "microsoft", "windows", "intel", "nvidia", "amd", "realtek", "dell", "hp inc",
    "lenovo", "asus", "acer", "google", "mozilla", "adobe", "common files",
    "package cache", "temp", "tmp", "desktop", "documents", "downloads",
    "microsoft office", "windowsapps", "packages", "connecteddevicesplatform",
    # 这些是系统/厂商的容器键，本身就是分类目录，不是某个软件的残留
    "classes", "clients", "policies", "registeredapplications", "wow6432node",
    "microsoft.net", "dotnet", "com3", "odbc", "speech", "system",
)

# COM 组件、安装器、更新包都用 GUID 命名 —— 那不是「软件」，别丢给 AI
def _classes_noise(name: str) -> bool:
    r"""SOFTWARE\Classes 下一大半是文件关联和 COM 注册，跟「哪个软件的残留」无关。

    只留像软件名的（厂商名、应用名）；扩展名关联（.txt）、CLSID、TypeLib、
    SystemFileAssociations 这些整片跳过 —— 不然候选会被它们淹掉。
    """
    lowered = str(name or "").strip().lower()
    return (
        lowered.startswith(".")
        or lowered.startswith(("clsid", "typelib", "interface", "wow6432node",
                               "applications", "systemfileassociations",
                               "appid", "directory", "drive", "folder",
                               "installer", "local settings", "microsoft"))
    )


# (注册表基路径, 额外的过滤函数)
_REGISTRY_BASES = (
    (r"SOFTWARE", None),
    (r"SOFTWARE\WOW6432Node", None),
    (r"SOFTWARE\Clients", None),
    # 文件关联：里面既有 .txt 这种噪音，也有厂商注册的 progid
    (r"SOFTWARE\Classes", _classes_noise),
    # 注册过的 exe —— 软件卸了这里常常还留着
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths", None),
)


_GUID = re.compile(
    r"^\{?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}?$"
)


def _systemish(name: str) -> bool:
    lowered = str(name or "").strip().lower()
    if not lowered:
        return True
    if lowered in SYSTEM_WORDS:
        return True
    if _GUID.match(str(name or "").strip()):
        return True
    # 纯十六进制串（安装器缓存那种）也不是软件名
    if len(lowered) >= 16 and all(ch in "0123456789abcdef-_" for ch in lowered):
        return True
    return False

MAX_SUSPECTS = 150
BATCH_SIZE = 25

JUDGE_SYSTEM = (
    "你是 Windows 系统清理助手。用户给你一批目录名和注册表键名，"
    "以及这台电脑上**仍然装着**的软件清单。"
    "请判断每一项是不是「已经卸载的软件」遗留的垃圾。\n"
    "判断依据：名字像不像某个软件/厂商的数据目录；"
    "是不是通用系统组件；是不是某个仍然装着的软件（哪怕名字不完全一样）。\n"
    "只输出 JSON，不要解释。格式：\n"
    '{"items":[{"index":1,"verdict":"leftover","confidence":"high","reason":"一句话"}]}\n'
    "verdict 只能是 leftover（像已卸载软件的残留）/ keep（像还装着的东西或系统组件）"
    "/ unsure（拿不准）。confidence 用 high/medium/low。"
)


@dataclass
class Suspect:
    kind: str          # folder / registry
    path: str
    name: str
    source: str = ""   # 从哪扫出来的（%APPDATA% 之类）
    verdict: str = ""  # leftover / keep / unsure
    reason: str = ""
    confidence: str = ""

    @property
    def identity(self) -> str:
        return f"{self.kind}:{self.path}"


def _installed_corpus(installed) -> tuple[set[str], list[str]]:
    """已装软件的名字碎片 + 安装路径。"""
    words: set[str] = set()
    locations: list[str] = []
    for item in installed:
        for text in (getattr(item, "name", ""), getattr(item, "publisher", "")):
            for token in str(text or "").lower().replace("_", " ").split():
                token = token.strip(".,()[]")
                if len(token) >= 4:
                    words.add(token)
        location = str(getattr(item, "install_location", "") or "").strip()
        if location:
            locations.append(os.path.normcase(location.rstrip("\\/")))
    return words, locations


def _is_known(name: str, words: set[str], locations: list[str], path: str) -> bool:
    lowered = str(name or "").lower()
    if not lowered:
        return True
    if any(word and word in lowered for word in words):
        return True
    normed = os.path.normcase(str(path or "").rstrip("\\/"))
    return any(location and normed.startswith(location) for location in locations)


def _user_dirs(scope_keys=None) -> list[tuple[str, str]]:
    """按用户勾选的位置返回 (显示名, 真实路径)。

    一个选项可能对应多个目录（比如「所有盘的 Program Files」），
    expand() 会把它拆成一条条真实路径。
    """
    from core.scan_scopes import default_keys, expand

    keys = list(scope_keys) if scope_keys else default_keys()
    return expand(keys)


def _focus_tokens(focus) -> tuple[set[str], str]:
    """把「当前这个软件」拆成可用于匹配的碎片 + 它的安装目录。"""
    tokens: set[str] = set()
    for text in (getattr(focus, "name", ""), getattr(focus, "publisher", "")):
        for token in str(text or "").lower().replace("_", " ").split():
            token = token.strip(".,()[]{}")
            if len(token) >= 3:
                tokens.add(token)
    for exe in getattr(focus, "exe_names", []) or []:
        lowered = str(exe or "").lower()
        if len(lowered) >= 3:
            tokens.add(lowered)
    folder = str(getattr(focus, "install_location", "") or "").rstrip("\\/")
    return tokens, os.path.normcase(folder) if folder else ""


def _focus_brief(focus, mask_username: bool = True) -> str:
    """把「当前这个软件」写成一段话，交给 AI 当判断依据。"""
    if focus is None:
        return ""
    name = str(getattr(focus, "name", "") or "").strip()
    publisher = str(getattr(focus, "publisher", "") or "").strip()
    location = str(getattr(focus, "install_location", "") or "").strip()
    if location and mask_username:
        from core.ai_client import mask_path

        location = mask_path(location)
    exes = [str(x) for x in (getattr(focus, "exe_names", []) or []) if x]
    lines = [f"这次是在卸载「{name}」之后找它留下的残余。",
             f"- 软件名：{name}"]
    if publisher:
        lines.append(f"- 发布者：{publisher}")
    if location:
        lines.append(f"- 安装目录：{location}")
    if exes:
        lines.append("- 它的可执行文件：" + "、".join(exes[:8]))
    lines.append(
        "**只挑出属于这个软件的残余。** 跟它无关的候选，哪怕看起来也像残留，"
        "一律判 keep —— 别的软件的账不该算在它头上。"
    )
    return chr(10).join(lines)


def _related(suspect: Suspect, tokens: set[str], folder: str) -> bool:
    """这一项跟当前软件有关吗？"""
    lowered = str(suspect.name or "").lower()
    if any(token in lowered for token in tokens):
        return True
    if folder:
        normed = os.path.normcase(str(suspect.path or "").rstrip("\\/"))
        if normed == folder or normed.startswith(folder + os.sep):
            return True
    return False


def collect_suspects(installed, limit: int = MAX_SUSPECTS, focus=None,
                     kinds=("folder", "registry"), scopes=None) -> list[Suspect]:
    """列出「不属于任何已装软件」的目录和注册表键。

    kinds 控制只找哪一类：软件卸载页只要文件夹（"folder"），
    注册表页只要键（"registry"）。

    名额按 2:1 分给文件和注册表 —— 不然文件夹一多，注册表就挤不进去了。
    """
    wanted = set(kinds or ("folder", "registry"))
    scope_keys = list(scopes) if scopes else None
    words, locations = _installed_corpus(installed)
    found: list[Suspect] = []
    seen: set[str] = set()
    focus_tokens, focus_folder = _focus_tokens(focus) if focus is not None else (set(), "")
    # 注意：focus **不缩减**候选范围。
    # 之前只留「名字对得上的」，结果一个软件往往只剩一两条，AI 等于没扫。
    # 现在照样扫满，只在排序上把跟这个软件有关的往前放。
    if wanted == {"folder"}:
        folder_quota, registry_quota = limit, 0
    elif wanted == {"registry"}:
        folder_quota, registry_quota = 0, limit
    else:
        folder_quota = limit * 2 // 3
        registry_quota = limit - folder_quota

    for label, root in (_user_dirs(scope_keys) if folder_quota else ()):
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            if len([x for x in found if x.kind == "folder"]) >= folder_quota:
                break
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            name = entry.name
            if _systemish(name):
                continue
            if _is_known(name, words, locations, entry.path):
                continue
            key = os.path.normcase(entry.path)
            if key in seen:
                continue
            seen.add(key)
            found.append(Suspect("folder", entry.path, name, source=label))

    # 注册表这几处最容易留东西：厂商键、文件关联、注册过的 exe、32 位视图
    for hive, hive_name in (((winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE"),
                            (winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER"))
                            if registry_quota else ()):
        # 每个基路径各有一个名额上限 —— 不然 Classes 那种大目录会把配额吃光，
        # 其他位置一个都轮不上
        per_base_cap = max(8, registry_quota // max(1, len(_REGISTRY_BASES)))
        for base, filter_fn in _REGISTRY_BASES:
            try:
                key = winreg.OpenKey(hive, base, 0, winreg.KEY_READ)
            except OSError:
                continue
            taken_from_base = 0
            with key:
                index = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    if len([x for x in found if x.kind == "registry"]) >= registry_quota:
                        break
                    if taken_from_base >= per_base_cap:
                        break
                    if _systemish(sub):
                        continue
                    if filter_fn is not None and filter_fn(sub):
                        continue
                    full = rf"{hive_name}\{base}\{sub}"
                    if _is_known(sub, words, locations, full):
                        continue
                    key_id = full.lower()
                    if key_id in seen:
                        continue
                    seen.add(key_id)
                    found.append(Suspect("registry", full, sub,
                                         source=f"{hive_name}\\{base}"))
                    taken_from_base += 1
    if focus_tokens or focus_folder:
        # 相关的排前面：用户先看到「就是它留下的」，再往下才是别处的可疑项
        found.sort(key=lambda item: 0 if _related(item, focus_tokens, focus_folder) else 1)
    return found[:limit]


def _ai_conf(settings):
    """settings 可能是 Settings（模型在 .ai 里）也可能是 AISettings 本身。"""
    return getattr(settings, "ai", settings)


def _ask(batch: list[Suspect], software_names: list[str], settings,
         focus=None) -> list[Suspect]:
    """问一次 AI，把判断写回 batch。失败抛异常交给上层。"""
    lines = []
    for index, suspect in enumerate(batch, 1):
        lines.append(f"{index}. [{suspect.kind}] {suspect.name}　（来路：{suspect.source}）")
    conf = _ai_conf(settings)
    masked = bool(getattr(conf, "mask_username", True))
    brief = _focus_brief(focus, mask_username=masked)
    user_text = (
        (brief + "\n\n" if brief else "")
        + f"这台电脑上**仍然装着**的软件，共 {len(software_names)} 个：\n"
        + "、".join(software_names) + "\n\n"
        "候选（判断时请逐个对照上面的清单，清单里已有的就是还装着的，不该判成残留）：\n"
        + "\n".join(lines)
    )
    body = {
        "model": getattr(conf, "model", "") or "",
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": user_text},
        ],
        "temperature": float(getattr(conf, "temperature", 0.1) or 0.1),
        "max_tokens": 1200,
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    # 和 ai_client 用同一套地址/超时规则：用户填了 /v1 也不能拼成 /v1/v1。
    from core.ai_client import normalize_base_url

    base = normalize_base_url(str(getattr(conf, "base_url", "") or ""))
    try:
        timeout = max(3.0, float(getattr(conf, "timeout_seconds", 15) or 15))
    except (TypeError, ValueError):
        timeout = 15.0
    request = urllib.request.Request(f"{base}/v1/chat/completions", data=payload, method="POST")
    request.add_header("Authorization", f"Bearer {getattr(conf, 'api_key', '') or ''}")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    data = json.loads(raw.decode("utf-8", "replace"))
    content = data["choices"][0]["message"]["content"]
    parsed = _parse(content)
    for entry in parsed:
        position = entry.get("index", 0)
        if not isinstance(position, int) or not (1 <= position <= len(batch)):
            continue
        target = batch[position - 1]
        verdict = str(entry.get("verdict", "")).lower()
        if verdict not in ("leftover", "keep", "unsure"):
            verdict = "unsure"
        target.verdict = verdict
        target.reason = str(entry.get("reason", ""))[:200]
        target.confidence = str(entry.get("confidence", "")).lower()[:10]
    return batch


def make_judge_worker(suspects, installed, settings, focus=None, cancel=None):
    """丢线程池跑，别卡界面。"""
    from PySide6.QtCore import QObject, QRunnable, Signal

    class Signals(QObject):
        progress = Signal(int, int)
        finished = Signal(object, object)   # (带判断的候选, 错误)

    class _Task(QRunnable):
        def __init__(self) -> None:
            super().__init__()
            self.setAutoDelete(True)
            self.signals = Signals()

        def run(self) -> None:
            try:
                judged, error = judge(
                    suspects, installed, settings,
                    progress=lambda done, total: self.signals.progress.emit(done, total),
                    focus=focus, cancel=cancel,
                )
            except Exception as exc:  # noqa: BLE001
                judged, error = [], str(exc)
            try:
                self.signals.finished.emit(judged, error)
            except RuntimeError:
                pass

    return _Task()


def _parse(text: str) -> list[dict]:
    raw = str(text or "").strip()
    if raw.startswith("" + chr(96) * 3):
        raw = raw.split(chr(10), 1)[-1]
        raw = raw.rsplit(chr(96) * 3, 1)[0]
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


NAME_LIMIT = 500       # 超过这个数才截断（正常机器不会）


def installed_names(installed) -> list[str]:
    """已装软件的全量名字。

    必须传全量 —— 少传一个，「还装着的软件」就会被 AI 判成残留。
    这台机器 168 个软件，全量大约 3000 字，几批请求多花不了多少。
    """
    seen: set[str] = set()
    names: list[str] = []
    for item in installed:
        name = str(getattr(item, "name", "") or "").strip()
        if not name:
            continue
        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        names.append(name)
    names.sort(key=str.lower)
    return names[:NAME_LIMIT]


def judge(suspects: list[Suspect], installed, settings,
          progress=None, focus=None, cancel=None) -> tuple[list[Suspect], str | None]:
    """分批问 AI。返回 (带判断的候选, 错误信息)。"""
    if not suspects:
        return [], None
    conf = _ai_conf(settings)
    try:
        batch_size = max(1, int(getattr(conf, "max_batch", BATCH_SIZE) or BATCH_SIZE))
    except (TypeError, ValueError):
        batch_size = BATCH_SIZE
    names = installed_names(installed)
    judged: list[Suspect] = []
    for index in range(0, len(suspects), batch_size):
        if cancel is not None and cancel.is_set():
            return judged, "已停止"     # 用户点了停止，剩下的批次不再发
        batch = suspects[index:index + batch_size]
        if progress is not None:
            try:
                progress(len(judged), len(suspects))
            except Exception:  # noqa: BLE001
                pass
        try:
            judged.extend(_ask(batch, names, settings, focus=focus))
        except Exception as exc:  # noqa: BLE001
            return judged, f"AI 判断的时候出了点问题：{exc}"
    if progress is not None:
        try:
            progress(len(suspects), len(suspects))
        except Exception:  # noqa: BLE001
            pass
    return judged, None
