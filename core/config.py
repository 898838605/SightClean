"""配置文件 / 知识库 / 缓存的读写与兜底。

三条铁律：
1. 任何 JSON 坏掉都不能让程序起不来 —— 备份坏文件、回退内置默认值、给一句人话提示。
2. 字段级兜底 —— 单个字段类型写错只损失那一个字段，不连累整份配置。
3. 写入必须原子（临时文件 + 替换），中途断电不会留下半截文件。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app_paths import to_long

SCHEMA_VERSION = 1

# 资源文件丢失时的最后一道底线（正常情况下用 resources/default_config.json）
FALLBACK_CONFIG: dict[str, Any] = {
    "schema": SCHEMA_VERSION,
    "auto_size": True,
    "auto_size_concurrency": 2,
    "confirm_recycle": False,
    "show_hidden": False,
    "size_cache_ttl_hours": 24,
    "log_keep_count": 30,
    "recycle_warn_over_gb": 20,
    "ai": {
        "enabled": False,
        "provider": "custom",
        "base_url": "",
        "model": "",
        "api_key": "",
        "mask_username": True,
        "timeout_seconds": 15,
        "max_batch": 20,
    },
    "window_width": 1200,
    "window_height": 760,
    "last_dir": "",
    "last_module": 0,
}


# --------------------------------------------------------------------------- #
# 原子读写
# --------------------------------------------------------------------------- #
def read_json(path: Path) -> tuple[Any | None, str | None]:
    """读取 JSON。返回 (数据, 问题描述)。问题是给状态栏看的人话。"""
    try:
        text = Path(to_long(path)).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "这个文件打不开，已改用默认设置"
    if not text.strip():
        return None, "这个文件是空的，已改用默认设置"
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        backup = _backup_bad_file(Path(path))
        tail = f"，原件已留作 {backup.name}" if backup else ""
        return None, f"这个文件内容有损坏，已改用默认设置{tail}"


def _backup_bad_file(path: Path) -> Path | None:
    try:
        target = path.with_name(f"{path.stem}.bad-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")
        os.replace(to_long(path), to_long(target))
        return target
    except OSError:
        return None


def write_json(path: Path, data: Any) -> bool:
    """原子写入。失败只返回 False，由调用方决定要不要提示。"""
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        Path(to_long(tmp)).write_text(payload + "\n", encoding="utf-8")
        os.replace(to_long(tmp), to_long(path))
        return True
    except (OSError, TypeError, ValueError):
        if tmp is not None:
            try:
                os.unlink(to_long(tmp))
            except OSError:
                pass
        return False


# --------------------------------------------------------------------------- #
# 字段级兜底
# --------------------------------------------------------------------------- #
class _Coercer:
    """把用户手改坏的值拉回正轨，并顺手记一句给人看的说明。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def boolean(self, data: dict, key: str, default: bool) -> bool:
        value = data.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "on", "是"):
                return True
            if low in ("false", "0", "no", "off", "否"):
                return False
        self.warnings.append(f"设置项「{key}」写得不对，已经用默认值")
        return default

    def integer(self, data: dict, key: str, default: int, low: int, high: int) -> int:
        value = data.get(key, default)
        try:
            if isinstance(value, bool):
                raise TypeError
            number = int(value)
        except (TypeError, ValueError):
            self.warnings.append(f"设置项「{key}」写得不对，已经用默认值")
            return default
        if number < low or number > high:
            self.warnings.append(f"设置项「{key}」超出了合理范围，已经拉回到 {min(max(number, low), high)}")
            return min(max(number, low), high)
        return number

    def number(self, data: dict, key: str, default: float,
               low: float = 0.0, high: float = 2.0) -> float:
        """读一个浮点设置。读不到 / 不是数 / 超范围都退回 default。"""
        value = data.get(key)
        if isinstance(value, bool) or value is None:
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number != number or number in (float("inf"), float("-inf")):
            return default
        if number < low or number > high:
            return default
        return number

    def text_list(self, data: dict, key: str) -> list[str]:
        """读一个字符串列表。不是列表就退回空。"""
        value = data.get(key)
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if isinstance(item, (str, int))]

    def text(self, data: dict, key: str, default: str) -> str:
        value = data.get(key, default)
        if isinstance(value, str):
            return value
        self.warnings.append(f"设置项「{key}」写得不对，已经用默认值")
        return default

    def section(self, data: dict, key: str) -> dict:
        value = data.get(key)
        if isinstance(value, dict):
            return value
        if value is not None:
            self.warnings.append(f"设置项「{key}」写得不对，已经用默认值")
        return {}


def _as_rule_list(value: Any, coercer: "_Coercer") -> list[dict]:
    """config.json 里的用户规则：只要对象，别的忽略并提示一句。"""
    if value is None:
        return []
    if not isinstance(value, list):
        coercer.warnings.append("设置项「user_rules」不是列表，已经当成没有自定义规则")
        return []
    rules = [item for item in value if isinstance(item, dict)]
    if len(rules) != len(value):
        coercer.warnings.append("设置项「user_rules」里有几条不是规则对象，已经跳过")
    return rules


# --------------------------------------------------------------------------- #
# 运行设置
# --------------------------------------------------------------------------- #
@dataclass
class AISettings:
    enabled: bool = False
    provider: str = "custom"        # deepseek / kimi / openai / custom
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    mask_username: bool = True      # 发出去的路径里把用户名换成 %USER%
    timeout_seconds: float = 15.0
    max_batch: int = 20
    # 0 最死板（同样输入给同样答案），越高越发散。判断类任务建议 0~0.3。
    temperature: float = 0.1


@dataclass
class Settings:
    auto_size: bool = True
    auto_size_concurrency: int = 2
    confirm_recycle: bool = False
    show_hidden: bool = False
    size_cache_ttl_hours: int = 24
    log_keep_count: int = 30
    recycle_warn_over_gb: int = 20
    theme: str = "system"            # system / light / dark
    font_scale: str = "medium"       # small / medium / large
    window_width: int = 1200
    window_height: int = 760
    last_dir: str = ""
    last_module: int = 0            # 上次停在哪个模块（0 文件 / 1 软件 / 2 注册表 / 3 进程）
    user_rules: list[dict] = field(default_factory=list)
    scan_scopes: list[str] = field(default_factory=list)   # 空表示用默认那套
    ai: AISettings = field(default_factory=AISettings)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: dict, warnings: list[str] | None = None) -> "Settings":
        c = _Coercer()
        ai_raw = c.section(data, "ai")
        ai = AISettings(
            enabled=c.boolean(ai_raw, "enabled", False),
            provider=c.text(ai_raw, "provider", "custom").strip() or "custom",
            base_url=c.text(ai_raw, "base_url", "").strip(),
            model=c.text(ai_raw, "model", "").strip(),
            api_key=c.text(ai_raw, "api_key", "").strip(),
            mask_username=c.boolean(ai_raw, "mask_username", True),
            timeout_seconds=float(c.integer(ai_raw, "timeout_seconds", 15, 3, 120)),
            max_batch=c.integer(ai_raw, "max_batch", 20, 1, 200),
            temperature=c.number(ai_raw, "temperature", 0.1, 0.0, 2.0),
        )
        obj = cls(
            auto_size=c.boolean(data, "auto_size", True),
            auto_size_concurrency=c.integer(data, "auto_size_concurrency", 2, 1, 8),
            confirm_recycle=c.boolean(data, "confirm_recycle", False),
            show_hidden=c.boolean(data, "show_hidden", False),
            size_cache_ttl_hours=c.integer(data, "size_cache_ttl_hours", 24, 0, 24 * 30),
            log_keep_count=c.integer(data, "log_keep_count", 30, 1, 500),
            recycle_warn_over_gb=c.integer(data, "recycle_warn_over_gb", 20, 0, 100000),
            window_width=c.integer(data, "window_width", 1200, 900, 10000),
            window_height=c.integer(data, "window_height", 760, 560, 10000),
            last_dir=c.text(data, "last_dir", ""),
            last_module=c.integer(data, "last_module", 0, 0, 3),
            theme=c.text(data, "theme", "system").strip().lower() or "system",
            font_scale=c.text(data, "font_scale", "medium").strip().lower() or "medium",
            user_rules=_as_rule_list(data.get("user_rules"), c),
            scan_scopes=c.text_list(data, "scan_scopes"),
            ai=ai,
        )
        for item in c.warnings:
            (warnings if warnings is not None else obj.warnings).append(item)
        return obj

    def to_mapping(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "auto_size": self.auto_size,
            "auto_size_concurrency": self.auto_size_concurrency,
            "confirm_recycle": self.confirm_recycle,
            "show_hidden": self.show_hidden,
            "size_cache_ttl_hours": self.size_cache_ttl_hours,
            "log_keep_count": self.log_keep_count,
            "recycle_warn_over_gb": self.recycle_warn_over_gb,
            "window_width": self.window_width,
            "window_height": self.window_height,
            "last_dir": self.last_dir,
            "last_module": self.last_module,
            "theme": self.theme,
            "font_scale": self.font_scale,
            "user_rules": self.user_rules,
            "scan_scopes": list(self.scan_scopes),
            "ai": {
                "enabled": self.ai.enabled,
                "provider": self.ai.provider,
                "base_url": self.ai.base_url,
                "model": self.ai.model,
                "api_key": self.ai.api_key,
                "mask_username": self.ai.mask_username,
                "timeout_seconds": int(self.ai.timeout_seconds),
                "max_batch": self.ai.max_batch,
            "temperature": self.ai.temperature,
            },
        }


def deep_merge(base: dict, overlay: dict) -> dict:
    """外置配置按字段覆盖内置，没写的字段继续用内置默认。

    必须是深合并：用户常常只在 config.json 里改一个 ai.api_key，
    浅合并会把整个 ai 段落盖掉，连带把脱敏开关之类的默认值也弄丢。
    """
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def ensure_first_run_files(
    default_config: Path,
    default_knowledge: Path,
    config_path: Path,
    knowledge_path: Path,
) -> list[str]:
    """首次运行时，把内置默认落成 exe 旁边可以编辑的文件。

    这样做有两个好处：用户能直接改，出问题时也能把这两份发出来对照。
    目录不可写就只留一句提示，不影响启动。
    """
    import shutil

    notes: list[str] = []
    pairs = (
        (default_config, config_path, "config.json"),
        (default_knowledge, knowledge_path, "知识库.json"),
    )
    for source, target, label in pairs:
        try:
            if target.exists() or not source.exists():
                continue
            shutil.copyfile(to_long(source), to_long(target))
            notes.append(f"已经在程序旁边生成了一份 {label}")
        except OSError:
            notes.append(f"{label} 没能生成（这个目录可能不允许写入）")
    return notes


class SettingsStore:
    """config.json 的读写门面：内置默认 → 外置覆盖 → 字段级兜底。"""

    def __init__(self, config_path: Path, default_path: Path) -> None:
        self.config_path = config_path
        self.default_path = default_path

    def load(self) -> Settings:
        warnings: list[str] = []
        builtin, builtin_problem = read_json(self.default_path)
        if builtin_problem:
            warnings.append("内置默认设置读不出来（文件可能损坏），已改用程序里的保底值")
        elif not isinstance(builtin, dict):
            warnings.append("内置默认设置的格式不对，已改用程序里的保底值")
        if not isinstance(builtin, dict):
            builtin = dict(FALLBACK_CONFIG)
        external, external_problem = read_json(self.config_path)
        if external_problem:
            warnings.append(external_problem)
        elif external is not None and not isinstance(external, dict):
            warnings.append("config.json 的格式不对，已改用内置默认设置")
            external = None
        data = deep_merge(builtin, external) if isinstance(external, dict) else dict(builtin)
        settings = Settings.from_mapping(data, warnings)
        settings.warnings = warnings + settings.warnings
        return settings

    def save(self, settings: Settings) -> bool:
        return write_json(self.config_path, settings.to_mapping())


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
def _digest(text: str) -> str:
    return hashlib.sha1(os.path.normcase(text).encode("utf-8", "surrogatepass")).hexdigest()[:16]


class CacheStore:
    """cache.json：大小结果、联网解释结果。

    key 一律是哈希，文件被顺手发出去也不会暴露完整路径清单。
    """

    MAX_SIZES = 5000
    MAX_AI = 500
    FLUSH_INTERVAL = 30.0

    def __init__(self, path: Path) -> None:
        self.path = path
        self._sizes: dict[str, dict] = {}
        self._ai: dict[str, dict] = {}
        self._dirty = False
        self._last_flush = 0.0
        self.warnings: list[str] = []

    # -- key ----------------------------------------------------------------
    @staticmethod
    def size_key(path: str) -> str:
        return f"v1|size|{_digest(path)}"

    @staticmethod
    def ai_key_item(item_type: str, identity: str, name: str) -> str:
        """按类型分开的 AI 缓存键。

        file 沿用 v1.2 的 v1|ain 格式，老缓存照样能命中；
        其它三类用带类型段的格式，互不串味。
        """
        kind = str(item_type or "file").strip().lower()
        if kind in ("", "file"):
            return CacheStore.ai_key_for(identity, name)
        return f"v1|ai|{kind}|{_digest(f'{identity}|{name}')}"

    @staticmethod
    def ai_key_for(path: str, name: str) -> str:
        """AI 结果的缓存键：完整路径 + 名称。

        名称也算进去是有意的：改名之后要重新识别，否则会顶着旧名字的解释。
        """
        return f"v1|ain|{_digest(f'{path}|{name}')}"

    def load(self) -> None:
        data, problem = read_json(self.path)
        if problem:
            self.warnings.append("「cache.json」" + problem)
        if not isinstance(data, dict):
            if data is not None:
                self.warnings.append("「cache.json」格式不对，已经当空的重新开始记")
            return
        sizes = data.get("sizes")
        if isinstance(sizes, dict):
            self._sizes = {k: v for k, v in sizes.items() if isinstance(v, dict)}
        ai = data.get("ai")
        if isinstance(ai, dict):
            self._ai = {k: v for k, v in ai.items() if isinstance(v, dict)}

    def get_size(self, path: str, ttl_hours: int) -> tuple[int, str] | None:
        entry = self._sizes.get(self.size_key(path))
        if not entry:
            return None
        if ttl_hours > 0 and time.time() - float(entry.get("at", 0)) > ttl_hours * 3600:
            return None
        try:
            return int(entry["bytes"]), str(entry.get("state", "ok"))
        except (KeyError, TypeError, ValueError):
            return None

    def put_size(self, path: str, size: int, state: str = "ok") -> None:
        self._sizes[self.size_key(path)] = {"bytes": int(size), "at": int(time.time()), "state": state}
        self._dirty = True
        self._prune()
        self.flush()

    def get_ai(self, key: str) -> str | None:
        entry = self._ai.get(key)
        if not entry:
            return None
        text = entry.get("text")
        return text if isinstance(text, str) and text.strip() else None

    def put_ai(self, key: str, text: str, model: str) -> None:
        self._ai[key] = {"text": text, "at": int(time.time()), "model": model}
        self._dirty = True
        self._prune()
        self.flush()

    def put_ai_json(self, key: str, payload: dict, model: str) -> None:
        try:
            text = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        self.put_ai(key, text, model)

    def get_ai_json(self, key: str) -> dict | None:
        """取结构化的 AI 结果。老版本存的是纯文本，这里顺手兼容成一句说明。"""
        text = self.get_ai(key)
        if not text:
            return None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {"purpose": text, "impact": "", "risk": "caution", "note": "旧记录"}
        if isinstance(data, dict):
            return data
        return None

    def drop_ai(self) -> None:
        """知识库更新后旧解释不再可信，整体作废。"""
        if self._ai:
            self._ai.clear()
            self._dirty = True
            self.flush(force=True)

    def _prune(self) -> None:
        if len(self._sizes) > self.MAX_SIZES:
            keep = sorted(self._sizes.items(), key=lambda kv: kv[1].get("at", 0))[-self.MAX_SIZES:]
            self._sizes = dict(keep)
        if len(self._ai) > self.MAX_AI:
            keep = sorted(self._ai.items(), key=lambda kv: kv[1].get("at", 0))[-self.MAX_AI:]
            self._ai = dict(keep)

    def flush(self, force: bool = False) -> None:
        now = time.time()
        if not self._dirty:
            return
        if not force and now - self._last_flush < self.FLUSH_INTERVAL:
            return
        self._last_flush = now
        self._dirty = not write_json(self.path, {"schema": SCHEMA_VERSION, "sizes": self._sizes, "ai": self._ai})
