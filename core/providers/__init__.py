"""把各来源合起来。

合并规则：**注册表优先**。同一个名字如果在注册表里有，就不再用 winget 那条 ——
注册表那条带着卸载命令和安装位置，信息更全。
"""
from __future__ import annotations

from core.providers import orphan_source, registry_source, winget_source
from core.providers.base import ProviderResult


def _norm(name: str) -> str:
    return " ".join(str(name or "").lower().split())


def collect_all(include_winget: bool = True, include_orphans: bool = True) -> ProviderResult:
    merged = ProviderResult(source="all")
    seen: set[str] = set()

    registry = registry_source.collect()
    merged.items.extend(registry.items)
    merged.warnings.extend(registry.warnings)
    seen.update(_norm(item.name) for item in registry.items)

    if include_winget:
        winget = winget_source.collect()
        for item in winget.items:
            key = _norm(item.name)
            if key in seen:
                continue          # 注册表里已经有了，跳过
            merged.items.append(item)
            seen.add(key)
        merged.warnings.extend(winget.warnings)

    if include_orphans:
        orphans = orphan_source.collect()
        for item in orphans.items:
            key = _norm(item.name)
            if key in seen:
                continue
            merged.items.append(item)
            seen.add(key)
        merged.warnings.extend(orphans.warnings)

    return merged
