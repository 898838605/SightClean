"""一键清理 C 盘垃圾：按规则文件扫，按类别清。

跟其它模块的区别：
    别的模块是「你看着勾」，这里是「按规则批量扫」。
所以规矩更严：
    1. 只删规则里写死的那几类，不自由发挥
    2. 每类都说清「删了会怎样」，用户看着办
    3. 默认只勾风险为 safe 的
    4. 正在被占用的文件跳过，不强行删

扫描只统计「每类多少项、多大」，不保留每一条 ——
临时文件和包缓存动辄几千条，列出来既没用又卡。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app_paths import resource_dir


@dataclass
class JunkCategory:
    id: str
    label: str
    purpose: str
    detail: str
    risk: str
    mode: str          # contents / pattern / whole
    paths: list[str]

    @property
    def is_safe(self) -> bool:
        return self.risk == "safe"


@dataclass
class CategoryResult:
    """一个类别的扫描结果：多少项、多大、具体在哪些路径。"""

    category: JunkCategory
    count: int = 0
    size: int = 0
    paths: list[str] = field(default_factory=list)   # 展开后的真实路径

    @property
    def label(self) -> str:
        return self.category.label

    @property
    def is_safe(self) -> bool:
        return self.category.is_safe


@dataclass
class JunkReport:
    categories: list[CategoryResult] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.categories)

    @property
    def total_count(self) -> int:
        return sum(item.count for item in self.categories)

    def picked(self) -> list[CategoryResult]:
        return [item for item in self.categories if item.count]


@dataclass
class CleanOutcome:
    removed: int = 0
    freed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #
_RULES_CACHE: Path | None = None


def rules_path() -> Path:
    """优先用 exe 旁边那份（用户可以改），没有就用内置的。

    注意不能拿 resource_dir() 的 parent —— 打包后那是临时解包目录
    （%TEMP%\_MEIxxxx），用户改的文件在 exe 旁边，读不到。
    """
    global _RULES_CACHE
    if _RULES_CACHE is not None and _RULES_CACHE.is_file():
        return _RULES_CACHE

    from app_paths import resolve

    custom = resolve().junk
    if custom.is_file():
        _RULES_CACHE = custom
        return custom
    return Path(resource_dir()) / "default_junk.json"


def load_rules() -> list[JunkCategory]:
    path = rules_path()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}

    categories: list[JunkCategory] = []
    for raw in data.get("categories", []):
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("id", "")).strip()
        if not cid:
            continue
        mode = str(raw.get("mode", "contents")).strip().lower()
        if mode not in ("contents", "pattern", "whole"):
            mode = "contents"
        paths = [str(p) for p in raw.get("paths", []) if isinstance(p, str) and p.strip()]
        if not paths:
            continue
        categories.append(JunkCategory(
            id=cid,
            label=str(raw.get("label", cid)),
            purpose=str(raw.get("purpose", "")),
            detail=str(raw.get("detail", "")),
            risk=str(raw.get("risk", "caution")).lower(),
            mode=mode,
            paths=paths,
        ))
    if categories:
        return categories
    return _fallback_rules()


def _fallback_rules() -> list[JunkCategory]:
    """规则文件坏了也不能让按钮点了没反应。"""
    return [
        JunkCategory(
            id="temp", label="临时文件",
            purpose="系统或软件运行时产生的临时数据。",
            detail="删了会自动重建。",
            risk="safe", mode="contents",
            paths=["%TEMP%", r"%WINDIR%\Temp"],
        ),
    ]


# --------------------------------------------------------------------------- #
# 路径解析
# --------------------------------------------------------------------------- #
def _expand(pattern: str) -> list[str]:
    """把环境变量和通配符展开成真实路径。"""
    text = os.path.expandvars(str(pattern or ""))
    if not text or "%" in text:
        return []
    if "*" in text or "?" in text:
        return [p for p in glob.glob(text) if os.path.exists(p)]
    return [text] if os.path.exists(text) else []


def _size_of(path: str, limit: int = 200_000) -> int:
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
    except OSError:
        return 0
    return _dir_stats(path, limit)[1]


def _dir_stats(path: str, limit: int = 200_000) -> tuple[int, int]:
    """一次遍历同时数个数和大小。"""
    count = 0
    size = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    count += 1
                    if count > limit:
                        return count, size
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            size += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return count, size


def _sources_of(category: JunkCategory) -> list[str]:
    """这个类别扫的是哪些位置（目录或文件本身，不含子项）。

    给界面显示「在哪」用 —— 临时文件有几万个子项，
    列子项没意义，要的是那两个目录。
    """
    sources: list[str] = []
    for pattern in category.paths:
        for path in _expand(pattern):
            # 同一批 db 文件在同一个目录下，按父目录去重更清爽
            entry = path
            if os.path.isfile(entry) and category.mode != "pattern":
                entry = os.path.dirname(entry) or entry
            if entry not in sources:
                sources.append(entry)
    return sources


def _targets_of(category: JunkCategory) -> list[str]:
    """这个类别下具体要删哪些东西。"""
    targets: list[str] = []
    for pattern in category.paths:
        for path in _expand(pattern):
            if category.mode == "contents" and os.path.isdir(path):
                # 目录：删里面的东西，目录本身留着
                try:
                    with os.scandir(path) as it:
                        for entry in it:
                            targets.append(entry.path)
                except OSError:
                    continue
            else:
                targets.append(path)
    return targets


# --------------------------------------------------------------------------- #
# 扫描 / 清理
# --------------------------------------------------------------------------- #
def scan(categories: list[JunkCategory] | None = None,
         progress=None) -> JunkReport:
    categories = categories if categories is not None else load_rules()
    report = JunkReport()
    total = len(categories)
    for index, category in enumerate(categories):
        if progress is not None:
            try:
                progress(index, total, category.label)
            except Exception:  # noqa: BLE001
                pass
        count = 0
        size = 0
        for target in _targets_of(category):
            try:
                if os.path.isdir(target):
                    local_count, local_size = _dir_stats(target)
                    count += local_count
                    size += local_size
                elif os.path.isfile(target):
                    count += 1
                    size += os.path.getsize(target)
            except OSError:
                continue
        report.categories.append(CategoryResult(
            category=category, count=count, size=size,
            paths=_sources_of(category)))
    if progress is not None:
        try:
            progress(total, total, "")
        except Exception:  # noqa: BLE001
            pass
    return report


def clean(results, progress=None) -> CleanOutcome:
    """按类别清。删不掉的（正在被占用）跳过，最后如实报数。"""
    outcome = CleanOutcome()
    planned: list[str] = []
    for result in results:
        planned.extend(_targets_of(result.category))

    total = len(planned)
    for index, target in enumerate(planned):
        if progress is not None:
            try:
                progress(index, total, target)
            except Exception:  # noqa: BLE001
                pass
        try:
            size = _size_of(target)
        except OSError:
            size = 0
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            outcome.removed += 1
            outcome.freed += size
        except OSError as exc:
            outcome.skipped += 1
            if len(outcome.errors) < 20:
                outcome.errors.append(f"{os.path.basename(target) or target}：{exc}")
    if progress is not None:
        try:
            progress(total, total, "")
        except Exception:  # noqa: BLE001
            pass
    return outcome
def make_scan_worker(categories=None):
    """丢线程池扫，别卡界面。"""
    from core.tasks import Worker

    class _Task(Worker):
        def work(self):
            return scan(
                categories,
                progress=lambda done, total, label:
                    self.signals.progress.emit(done, total, label),
            )

    return _Task()


def make_clean_worker(results):
    from core.tasks import Worker

    class _Task(Worker):
        def work(self):
            return clean(
                results,
                progress=lambda done, total, path:
                    self.signals.progress.emit(done, total, path),
            )

    return _Task()
