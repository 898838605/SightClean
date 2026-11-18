"""绿色目录解析 + 长路径工具。

程序不写注册表、不装服务。所有可写数据都放在 resolve() 给的位置：
优先 exe 同目录（绿色便携），目录不可写时退到用户目录。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "SightClean"
# 回退目录用 ASCII 名，避免个别环境下的编码问题
FALLBACK_DIR_NAME = APP_NAME
# 老版本用的目录名：启动时先尝试迁移，迁不动就继续用老的，绝不丢配置
LEGACY_FALLBACK_DIR_NAME = "DiskCleanupHelper"

_LONG_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def to_long(path: str | os.PathLike[str]) -> str:
    """转成长路径形式，超过 260 个字符也能正常读写。"""
    if not path:
        return str(path)
    p = os.fspath(path)
    if p.startswith(_LONG_PREFIX):
        return p
    try:
        p = os.path.abspath(p)
    except (OSError, ValueError):
        return p
    if p.startswith("\\\\"):
        return _UNC_PREFIX + p[2:]
    return _LONG_PREFIX + p


def to_human(path: str | os.PathLike[str]) -> str:
    """去掉长路径前缀：显示、写日志、交给回收站都用这个形态。"""
    p = os.fspath(path)
    if p.startswith(_UNC_PREFIX):
        return "\\\\" + p[len(_UNC_PREFIX):]
    if p.startswith(_LONG_PREFIX):
        return p[len(_LONG_PREFIX):]
    return p


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_dir() -> Path:
    """exe 所在目录；开发运行时是项目根目录。"""
    if _is_frozen():
        try:
            return Path(sys.executable).resolve().parent
        except OSError:
            return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """内置只读资源目录（打包后在临时解包目录里）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "resources"
    return Path(__file__).resolve().parent / "resources"


def _fallback_root() -> Path:
    """程序目录写不了时，数据放哪。

    新版本统一放到 %LOCALAPPDATA%\\SightClean。老版本用的是
    %LOCALAPPDATA%\\DiskCleanupHelper —— 第一次升级时尝试改名迁移；
    改名失败（被占用、权限不够）就继续用老目录，保证配置不丢。
    """
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    current = base / FALLBACK_DIR_NAME
    legacy = base / LEGACY_FALLBACK_DIR_NAME
    if not current.exists() and legacy.is_dir():
        try:
            legacy.rename(current)
        except OSError:
            return legacy
    return current


def _writable(directory: Path) -> bool:
    """真写一次才算数：只读介质、权限被挡、路径过长都要能识别出来。"""
    probe = directory / ".write-test"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"1")
        probe.unlink()
        return True
    except (OSError, ValueError):
        return False


@dataclass(frozen=True)
class AppPaths:
    root: Path          # 数据根目录
    config: Path        # config.json
    knowledge: Path     # 知识库.json（外置）
    junk: Path          # 垃圾清理规则.json（外置）
    user_rules: Path    # 我的规则.json（用户层）
    cache: Path         # cache.json
    logs: Path          # 删除记录目录
    resources: Path     # 内置默认值
    portable: bool      # True = 就地存在 exe 旁边


def resolve() -> AppPaths:
    base = exe_dir()
    resource = resource_dir()
    if _writable(base):
        root, portable = base, True
    else:
        root, portable = _fallback_root(), False
    logs = root / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return AppPaths(
        root=root,
        config=root / "config.json",
        knowledge=root / "知识库.json",
        junk=root / "垃圾清理规则.json",
        user_rules=root / "我的规则.json",
        cache=root / "cache.json",
        logs=logs,
        resources=resource,
        portable=portable,
    )
