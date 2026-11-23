"""知识库：三层规则匹配。

规则格式（JSON）：
    {
      "rules": [
        {"id": "...", "layer": 1, "pattern": "Windows\\\\SoftwareDistribution\\\\Download",
         "match": "path_contains", "effect": "Windows 更新缓存，可安全清理",
         "risk": "safe", "source": "builtin"}
      ]
    }

- layer：1=系统目录库　2=软件目录库　3=扩展名兜底
- match：path_contains（路径包含）／path_regex（正则）／ext（扩展名，仅文件）
- risk：safe／caution／forbidden
- source：builtin／user

匹配顺序：用户规则整体优先于内置规则；同来源内按 layer 1 → 2 → 3，
同层按数组顺序，先命中先用（first match wins）。

两条硬约束：
1. 全程只做字符串比较，风险判定不允许读磁盘上的任何条目。
2. 任何一条规则写坏只丢这一条，并留一句人话提示；文件整体坏掉则回退到上一层。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from core.config import read_json

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
LAYER_SYSTEM = 1
LAYER_SOFTWARE = 2
LAYER_EXTENSION = 3
LAYERS = (LAYER_SYSTEM, LAYER_SOFTWARE, LAYER_EXTENSION)
LAYER_LABEL = {LAYER_SYSTEM: "系统目录", LAYER_SOFTWARE: "软件目录", LAYER_EXTENSION: "扩展名"}

MATCH_CONTAINS = "path_contains"
MATCH_REGEX = "path_regex"
MATCH_EXT = "ext"
MATCHES = (MATCH_CONTAINS, MATCH_REGEX, MATCH_EXT)

# 四类规则。kind 不写就是 file，所以 v1.2 的 63 条规则原样能用。
KIND_FILE = "file"
KIND_SOFTWARE = "software"
KIND_REGISTRY = "registry"
KIND_PROCESS = "process"
KINDS = (KIND_FILE, KIND_SOFTWARE, KIND_REGISTRY, KIND_PROCESS)

MATCH_NAME_CONTAINS = "name_contains"
MATCH_NAME_REGEX = "name_regex"
MATCH_PUBLISHER_CONTAINS = "publisher_contains"
# 注册表根键的缩写，规则里写 hklm 就能匹配 HKEY_LOCAL_MACHINE
ROOT_ALIASES = {
    "hkey_local_machine": "hklm",
    "hkey_current_user": "hkcu",
    "hkey_classes_root": "hkcr",
    "hkey_users": "hku",
    "hkey_current_config": "hkcc",
}

MATCH_NAME_EXACT = "name_exact"
MATCH_PATH_CONTAINS = "path_contains"

MATCH_KEY_PREFIX = "key_prefix"
MATCH_KEY_CONTAINS = "key_contains"
MATCH_KEY_REGEX = "key_regex"

# 每类规则允许的匹配方式；跨类写错会被跳过并给提示
MATCH_BY_KIND = {
    KIND_FILE: (MATCH_CONTAINS, MATCH_REGEX, MATCH_EXT),
    KIND_SOFTWARE: (MATCH_NAME_CONTAINS, MATCH_NAME_REGEX, MATCH_PUBLISHER_CONTAINS),
    KIND_REGISTRY: (MATCH_KEY_PREFIX, MATCH_KEY_CONTAINS, MATCH_KEY_REGEX),
    KIND_PROCESS: (MATCH_NAME_EXACT, MATCH_NAME_CONTAINS, MATCH_PATH_CONTAINS),
}

RISK_SAFE = "safe"
RISK_CAUTION = "caution"
RISK_FORBIDDEN = "forbidden"
# 未收录：知识库没覆盖，或「禁止级规则缺 impact」被降级。
# 它不进 RISKS：用户自己写规则时只能写前三档，不能把规则标成「未收录」。
RISK_UNLISTED = "unlisted"
RISKS = (RISK_SAFE, RISK_CAUTION, RISK_FORBIDDEN)
# 越往后越严格，folder_risk 取最严格一级；未收录排最前，不参与升级
RISK_RANK = {RISK_UNLISTED: 0, RISK_SAFE: 1, RISK_CAUTION: 2, RISK_FORBIDDEN: 3}

SOURCE_BUILTIN = "builtin"
SOURCE_USER = "user"

UNLISTED_EFFECT = "未收录，建议确认后再删"
# 数据层和展示层统一用 unlisted。调用方靠 known 判断是否收录，
# 但即使只看 risk 字符串，也不会把「不知道」误当成黄色「看情况」。
UNLISTED_RISK = RISK_UNLISTED

_ENV_RE = re.compile(r"%([^%]+)%")


# --------------------------------------------------------------------------- #
# 路径工具
# --------------------------------------------------------------------------- #
def normalize_path(path: str | os.PathLike[str]) -> str:
    """统一成小写 + 反斜杠 + 去掉尾部多余分隔符，用于字符串比较。"""
    if not path:
        return ""
    p = os.fspath(path).replace("/", "\\").strip()
    if len(p) > 3 and p.endswith("\\"):
        p = p.rstrip("\\")
    return p.lower()


def _haystack(path: str | os.PathLike[str]) -> str:
    """比较用。末尾补一个反斜杠，这样 pattern 写成 \\windows\\ 也能命中根目录。"""
    p = normalize_path(path)
    if not p:
        return "\\"
    return p if p.endswith("\\") else p + "\\"


def extension_of(path: str | os.PathLike[str]) -> str:
    """取扩展名，带点、小写。.gitignore 这种点开头的整名不算扩展名。"""
    name = normalize_path(path).rsplit("\\", 1)[-1]
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return ""
    return name[dot:]


def _env_lookup(name: str) -> str | None:
    low = name.lower()
    for key, value in os.environ.items():
        if key.lower() == low:
            return value
    return None


def _expand_env(text: str, rule_id: str, warnings: list[str]) -> str | None:
    """展开 %TEMP% 这类占位符。缺变量就把这条规则跳过并说一声。"""
    missing: list[str] = []

    def repl(match: re.Match[str]) -> str:
        value = _env_lookup(match.group(1))
        if value is None:
            missing.append(match.group(1))
            return ""
        return value

    expanded = _ENV_RE.sub(repl, text)
    if missing:
        warnings.append(f"规则「{rule_id}」用到这台电脑上没有的 {missing[0]}，已跳过这一条")
        return None
    return expanded


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    layer: int
    pattern: str                # 已展开环境变量、已归一化
    match: str                  # path_contains / path_regex / ext
    effect: str
    risk: str
    source: str                 # builtin / user
    order: int                  # 原始数组下标，同层内先命中先用
    locate: str = ""            # 用于「子孙预判」的定位串（path_contains 的 pattern / 正则的字面前缀）
    regex: Any = None           # 编译好的正则（仅 path_regex）
    impact: str = ""            # 一句人话后果（删了会怎样）；三道坎直接取这个
    impact_missing: bool = False   # 规则没写 impact
    original_risk: str = ""     # 被降级前的等级；只有「禁止级缺 impact 被降灰」才非空

    @property
    def layer_label(self) -> str:
        return LAYER_LABEL.get(self.layer, str(self.layer))

    @property
    def scope_label(self) -> str:
        return "我的规则" if self.source == SOURCE_USER else "自带规则"


def _regex_prefix(pattern: str) -> str:
    """从正则里抠出开头的字面路径，用于子孙预判。抠不出来就返回空串。"""
    text = pattern.lstrip("^")
    picked: list[str] = []
    for char in text:
        if char.isalnum() or char in "\\/:-._ ":
            picked.append(char)
        else:
            break
    prefix = "".join(picked).replace("/", "\\").strip("\\").lower()
    return prefix


def _build_rule(raw: dict, source_hint: str, order: int, warnings: list[str]) -> Rule | None:
    rule_id = str(raw.get("id") or f"rule-{order}").strip()
    if not rule_id:
        rule_id = f"rule-{order}"

    match_raw = raw.get("match")
    if isinstance(match_raw, dict):
        # 旧版格式（{"type": "path", "pattern": "..."}），认不出来就给一句明确的提示
        warnings.append(
            f"规则「{rule_id}」还是上一版的写法（match 是个对象），已经被跳过；"
            "新写法是 \"match\": \"path_contains\" 或 \"path_regex\" 或 \"ext\""
        )
        return None
    kind = str(raw.get("kind") or KIND_FILE).strip().lower()
    if kind not in KINDS:
        warnings.append(f"规则「{rule_id}」的类型「{kind}」看不懂，已跳过（只认 file/software/registry/process）")
        return None

    match = str(match_raw or "").strip().lower()
    allowed = MATCH_BY_KIND.get(kind, ())
    if match not in allowed:
        if match in MATCHES:
            warnings.append(
                f"规则「{rule_id}」是 {kind} 类，却用了 {match}；{kind} 只能写 {'、'.join(allowed)}，已跳过"
            )
        else:
            warnings.append(f"规则「{rule_id}」的匹配方式「{match or '空'}」看不懂，已跳过")
        return None

    pattern_raw = str(raw.get("pattern") or "").strip()
    if not pattern_raw:
        warnings.append(f"规则「{rule_id}」没写 pattern，已跳过")
        return None
    expanded = _expand_env(pattern_raw, rule_id, warnings)
    if expanded is None:
        return None

    effect = str(raw.get("effect") or "").strip()
    if not effect:
        warnings.append(f"规则「{rule_id}」没写说明文字，已跳过")
        return None

    risk = str(raw.get("risk") or "").strip().lower()
    if risk not in RISKS:
        warnings.append(f"规则「{rule_id}」的风险等级「{risk or '空'}」看不懂，已跳过（只认 safe/caution/forbidden）")
        return None

    # impact = 一句人话后果。禁止级没有它，用户就是对着一个「说不出后果的红项」做决定，
    # 那这红色没有意义 —— 降成未收录（灰），补上就能变回红色。
    impact = str(raw.get("impact") or "").strip()
    impact_missing = not impact
    original_risk = ""
    if impact_missing:
        if risk == RISK_FORBIDDEN:
            warnings.append(
                f"规则「{rule_id}」是禁止级但没写 impact（后果），已降级为未收录；补上 impact 就恢复红色"
            )
            original_risk = risk
            risk = RISK_UNLISTED
        elif risk == RISK_CAUTION:
            warnings.append(f"规则「{rule_id}」没写 impact（后果），建议补上（黄色不受影响）")

    try:
        layer = int(raw.get("layer"))
    except (TypeError, ValueError):
        warnings.append(f"规则「{rule_id}」的 layer 写得不对，已跳过（只认 1/2/3）")
        return None
    if layer not in LAYERS:
        warnings.append(f"规则「{rule_id}」的 layer 是 {layer}，只认 1/2/3，已跳过")
        return None

    source = str(raw.get("source") or source_hint).strip().lower()
    if source not in (SOURCE_BUILTIN, SOURCE_USER):
        source = source_hint

    regex = None
    locate = ""
    if kind == KIND_SOFTWARE:
        # 软件类匹配的是「显示名 / 发布者」这种短文本，不是路径，不做路径归一化
        if match == MATCH_NAME_REGEX:
            try:
                regex = re.compile(expanded, re.IGNORECASE)
            except re.error as exc:
                warnings.append(f"规则「{rule_id}」的正则写错了（{exc}），已跳过")
                return None
            pattern = expanded
        else:
            pattern = expanded.strip().lower()
            if not pattern:
                warnings.append(f"规则「{rule_id}」的关键词是空的，已跳过")
                return None
    elif kind == KIND_REGISTRY:
        # 注册表键路径统一小写比较，反斜杠分隔，不带盘符
        if match == MATCH_KEY_REGEX:
            try:
                regex = re.compile(expanded, re.IGNORECASE)
            except re.error as exc:
                warnings.append(f"规则「{rule_id}」的正则写错了（{exc}），已跳过")
                return None
            pattern = expanded
        else:
            pattern = expanded.replace("/", "\\").strip("\\").lower()
            if not pattern:
                warnings.append(f"规则「{rule_id}」的键路径是空的，已跳过")
                return None
        locate = pattern
    elif kind == KIND_PROCESS:
        pattern = expanded.replace("/", "\\").strip().lower()
        locate = ""
        if not pattern:
            warnings.append(f"规则「{rule_id}」的关键词是空的，已跳过")
            return None
    elif match == MATCH_CONTAINS:
        # 保留首部反斜杠：pattern 写成 \windows\ 才能精确匹配目录边界，
        # 不写成 strip 掉的话 "windows" 会误命中 "c:\mywindows\..."
        pattern = normalize_path(expanded)
        if not pattern.strip("\\"):
            warnings.append(f"规则「{rule_id}」的 pattern 是空的，已跳过")
            return None
        locate = pattern.strip("\\")
    elif match == MATCH_REGEX:
        try:
            regex = re.compile(expanded, re.IGNORECASE)
        except re.error as exc:
            warnings.append(f"规则「{rule_id}」的正则写错了（{exc}），已跳过")
            return None
        pattern = expanded
        locate = _regex_prefix(expanded)
    else:  # ext
        pattern = extended = expanded.lower()
        if not extended.startswith("."):
            extended = "." + extended
        pattern = extended
        locate = ""

    return Rule(
        id=rule_id,
        kind=kind,
        layer=layer,
        pattern=pattern,
        match=match,
        effect=effect,
        risk=risk,
        source=source,
        order=order,
        locate=locate,
        regex=regex,
        impact=impact,
        impact_missing=impact_missing,
        original_risk=original_risk,
    )


# --------------------------------------------------------------------------- #
# 匹配结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MatchResult:
    effect: str
    risk: str
    source: str
    rule_id: str | None = None
    known: bool = False
    layer: int | None = None
    impact: str = ""            # 一句人话后果；空表示规则没写
    impact_missing: bool = False

    def as_tuple(self) -> tuple[str, str, str]:
        return self.effect, self.risk, self.source


def _unlisted() -> MatchResult:
    # source 为空串 = 没有命中任何规则（未收录）。UI 靠这个判断是否显示灰色。
    return MatchResult(UNLISTED_EFFECT, UNLISTED_RISK, "", None, False, None)


# --------------------------------------------------------------------------- #
# 知识库
# --------------------------------------------------------------------------- #
class KnowledgeBase:
    def __init__(self) -> None:
        self.rules: list[Rule] = []
        self.warnings: list[str] = []
        self.version: str = ""
        self._desc_index: dict[str, list[Rule]] = {}
        self._by_kind: dict[str, list[Rule]] = {}
        self.counts: dict[str, int] = {}

    # -- 构建 --------------------------------------------------------------
    @classmethod
    def build(
        cls,
        builtin: dict | None = None,
        external: dict | None = None,
        user: dict | None = None,
        inline_user_rules: Iterable[dict] | None = None,
        warnings: list[str] | None = None,
    ) -> "KnowledgeBase":
        """三层来源合并。

        内置默认 → 外置 知识库.json → 用户规则（我的规则.json 与 config.json 里的 user_rules）。
        用户规则整体优先；同一 id 后出现的覆盖先出现的。
        """
        kb = cls()
        if warnings:
            kb.warnings = warnings

        buckets: list[tuple[str, str, dict | None]] = [
            ("内置默认规则", SOURCE_BUILTIN, builtin),
            ("知识库.json", SOURCE_BUILTIN, external),
            ("我的规则.json", SOURCE_USER, user),
        ]

        collected: dict[str, tuple[str, dict, int]] = {}
        order = 0
        for label, source_hint, doc in buckets:
            if doc is None:
                continue
            if not isinstance(doc, dict):
                kb.warnings.append(f"{label} 的格式不对，这一层已经跳过")
                continue
            rules_raw = doc.get("rules")
            if rules_raw is None:
                continue
            if not isinstance(rules_raw, list):
                kb.warnings.append(f"{label} 里的 rules 不是列表，这一层已经跳过")
                continue
            for raw in rules_raw:
                if not isinstance(raw, dict):
                    continue
                rule_id = str(raw.get("id") or "").strip()
                if rule_id and rule_id in collected:
                    # 后来的覆盖先前的：沿用原来的顺序位，内容用新的
                    _, _, kept_order = collected[rule_id]
                    collected[rule_id] = (source_hint, raw, kept_order)
                else:
                    collected[rule_id or f"anon-{order}"] = (source_hint, raw, order)
                order += 1
            version = doc.get("version")
            if isinstance(version, str) and version.strip():
                kb.version = version.strip()

        # config.json 里的用户规则（数组），强制按用户规则处理
        if inline_user_rules:
            for raw in inline_user_rules:
                if not isinstance(raw, dict):
                    kb.warnings.append("config.json 里的用户规则有一条不是对象，已跳过")
                    continue
                rule_id = str(raw.get("id") or "").strip() or f"config-{order}"
                copied = dict(raw)
                copied["source"] = SOURCE_USER
                collected[rule_id] = (SOURCE_USER, copied, order)
                order += 1

        built: list[Rule] = []
        for rule_id, (source_hint, raw, kept_order) in collected.items():
            rule = _build_rule(raw, source_hint, kept_order, kb.warnings)
            if rule is not None:
                built.append(rule)

        # 用户规则整体优先；同来源内 layer 1→2→3，同层按数组顺序
        built.sort(key=lambda r: (0 if r.source == SOURCE_USER else 1, r.layer, r.order))
        kb.rules = built
        kb._index()
        return kb

    def _index(self) -> None:
        """给「子孙预判」建一张按首段分组的索引，查的时候不用遍历全部规则。"""
        by_kind: dict[str, list[Rule]] = {}
        for rule in self.rules:
            by_kind.setdefault(rule.kind, []).append(rule)
        self._by_kind = by_kind

        index: dict[str, list[Rule]] = {}
        counts: dict[str, int] = {}
        for rule in self.rules:
            counts[f"layer{rule.layer}"] = counts.get(f"layer{rule.layer}", 0) + 1
            counts[rule.source] = counts.get(rule.source, 0) + 1
            counts[rule.kind] = counts.get(rule.kind, 0) + 1
            if rule.match == MATCH_EXT or not rule.locate:
                continue
            head = rule.locate.split("\\")[0]
            index.setdefault(head, []).append(rule)
        self._desc_index = index
        self.counts = counts

    # -- 匹配 --------------------------------------------------------------
    def impact_issues(self) -> list[dict]:
        """列出没写 impact 的规则，给设置页的「知识库健康检查」用。

        只报告、不改别的：禁止级的已经在加载时降成灰色了，其余照旧生效。
        """
        items: list[dict] = []
        for rule in self.rules:
            if not rule.impact_missing:
                continue
            items.append({
                "id": rule.id,
                "kind": rule.kind,
                "match": rule.match,
                "pattern": rule.pattern,
                "risk": rule.risk,
                "was": rule.original_risk,          # 被降级前的等级；空表示没降
                "downgraded": bool(rule.original_risk),
                "source": rule.source,
                "scope": rule.scope_label,
                "effect": rule.effect,
            })
        return items

    @property
    def impact_missing_count(self) -> int:
        return sum(1 for rule in self.rules if rule.impact_missing)

    def match_detail(self, path: str | os.PathLike[str], is_dir: bool = False) -> MatchResult:
        if not path:
            return _unlisted()
        hay = _haystack(path)
        ext = "" if is_dir else extension_of(path)
        for rule in self.rules:
            if rule.match == MATCH_EXT:
                if is_dir or not ext:
                    continue
                if rule.pattern == ext:
                    return MatchResult(rule.effect, rule.risk, rule.source, rule.id, True, rule.layer,
                                   rule.impact, rule.impact_missing)
            elif rule.match == MATCH_CONTAINS:
                if rule.pattern in hay:
                    return MatchResult(rule.effect, rule.risk, rule.source, rule.id, True, rule.layer,
                                   rule.impact, rule.impact_missing)
            else:  # path_regex
                if rule.regex is not None and rule.regex.search(hay):
                    return MatchResult(rule.effect, rule.risk, rule.source, rule.id, True, rule.layer,
                                   rule.impact, rule.impact_missing)
        return _unlisted()

    def match(self, path: str | os.PathLike[str], is_dir: bool = False) -> tuple[str, str, str]:
        """契约函数：返回 (effect, risk, source)。未收录时 source 为空串。"""
        return self.match_detail(path, is_dir).as_tuple()

    # -- 子孙预判 ----------------------------------------------------------
    def descendant_rules(self, path: str | os.PathLike[str]) -> list[Rule]:
        """找出「落在当前目录里面」的规则。纯字符串前缀比较，不碰磁盘。"""
        normalized = normalize_path(path).rstrip("\\")
        if not normalized:
            return []
        segments = [part for part in normalized.split("\\") if part]
        found: list[Rule] = []
        seen: set[str] = set()
        for index in range(len(segments)):
            suffix = "\\".join(segments[index:])
            for rule in self._desc_index.get(segments[index], ()):
                if rule.id in seen:
                    continue
                target = rule.locate
                if not target:
                    continue
                # 整条路径自己命中的规则由 match_detail 负责，这里只找「在它里面」的
                if index == 0 and target == normalized:
                    continue
                if target == suffix or target.startswith(suffix + "\\"):
                    found.append(rule)
                    seen.add(rule.id)
        return found

    def match_software(self, name: str, publisher: str = "") -> MatchResult:
        """按软件显示名 / 发布者匹配 software 类规则。

        和文件不一样：软件没有「目录」概念，不排序子孙，只是名字对不对得上。
        """
        software_name = str(name or "").strip().lower()
        publisher_name = str(publisher or "").strip().lower()
        if not software_name:
            return _unlisted()
        for rule in self._by_kind.get(KIND_SOFTWARE, ()):
            hit = False
            if rule.match == MATCH_NAME_CONTAINS:
                hit = bool(rule.pattern) and rule.pattern in software_name
            elif rule.match == MATCH_NAME_REGEX:
                hit = rule.regex is not None and bool(rule.regex.search(software_name))
            elif rule.match == MATCH_PUBLISHER_CONTAINS:
                hit = bool(rule.pattern) and bool(publisher_name) and rule.pattern in publisher_name
            if hit:
                return MatchResult(rule.effect, rule.risk, rule.source, rule.id, True, rule.layer,
                                   rule.impact, rule.impact_missing)
        return _unlisted()

    def match_registry(self, key_path: str) -> MatchResult:
        r"""按注册表键路径匹配 registry 类规则。

        比较前统一成小写 + 反斜杠；HKEY_LOCAL_MACHINE 这种长根名会先缩成 HKLM，
        这样规则里写 hklm\system 就够了，不用写全称。
        """
        normalized = str(key_path or "").replace("/", "\\").strip().lower()
        if not normalized:
            return _unlisted()
        for full_name, short_name in ROOT_ALIASES.items():
            if normalized.startswith(full_name):
                normalized = short_name + normalized[len(full_name):]
                break
        for rule in self._by_kind.get(KIND_REGISTRY, ()):
            hit = False
            if rule.match == MATCH_KEY_PREFIX:
                hit = normalized == rule.pattern or normalized.startswith(rule.pattern + "\\")
            elif rule.match == MATCH_KEY_CONTAINS:
                hit = bool(rule.pattern) and rule.pattern in normalized
            elif rule.match == MATCH_KEY_REGEX:
                hit = rule.regex is not None and bool(rule.regex.search(normalized))
            if hit:
                return MatchResult(rule.effect, rule.risk, rule.source, rule.id, True, rule.layer,
                                   rule.impact, rule.impact_missing)
        return _unlisted()

    def match_process(self, name: str, exe_path: str = "") -> MatchResult:
        """按进程名 / 可执行文件路径匹配 process 类规则。

        进程名的比较忽略大小写，但**不忽略扩展名**：system 和 system.exe 是两回事。
        """
        process_name = str(name or "").strip().lower()
        executable = str(exe_path or "").replace("/", "\\").strip().lower()
        if not process_name and not executable:
            return _unlisted()
        for rule in self._by_kind.get(KIND_PROCESS, ()):
            hit = False
            if rule.match == MATCH_NAME_EXACT:
                hit = process_name == rule.pattern
            elif rule.match == MATCH_NAME_CONTAINS:
                hit = bool(rule.pattern) and rule.pattern in process_name
            elif rule.match == MATCH_PATH_CONTAINS:
                hit = bool(rule.pattern) and bool(executable) and rule.pattern in executable
            if hit:
                return MatchResult(rule.effect, rule.risk, rule.source, rule.id, True, rule.layer,
                                   rule.impact, rule.impact_missing)
        return _unlisted()

    def folder_risk_detail(self, path: str | os.PathLike[str]) -> tuple[str, bool, tuple[tuple[str, str], ...]]:
        """文件夹风险 = 自身规则与所有子孙规则里最严格的一级。

        返回 (risk, known, hints)。known=False 表示完全没有规则覆盖（未收录）。
        """
        own = self.match_detail(path, True)
        descendants = self.descendant_rules(path)
        candidate_risks: list[str] = []
        if own.known:
            candidate_risks.append(own.risk)
        candidate_risks.extend(rule.risk for rule in descendants)
        if not candidate_risks:
            return UNLISTED_RISK, False, ()
        worst = max(candidate_risks, key=lambda value: RISK_RANK.get(value, 0))
        if own.known and own.risk == worst:
            # 自身就已经是最严的一档，说它自己的用途比罗列子孙更实在
            hints = ((own.effect[:24], own.effect),)
        else:
            hints = tuple(
                (rule.locate.rsplit("\\", 1)[-1] or rule.pattern, rule.effect)
                for rule in descendants
                if rule.risk == worst
            )[:3]
        return worst, True, hints

    def folder_risk(self, path: str | os.PathLike[str]) -> str:
        """契约函数：基于规则预判的文件夹风险等级（safe / caution / forbidden / unlisted）。"""
        return self.folder_risk_detail(path)[0]

    @property
    def rule_count(self) -> int:
        return len(self.rules)


# --------------------------------------------------------------------------- #
# 从文件加载
# --------------------------------------------------------------------------- #
def load_from_files(
    default_path: Path,
    external_path: Path,
    user_rules_path: Path | None = None,
    inline_user_rules: Sequence[dict] | None = None,
) -> KnowledgeBase:
    """内置默认 → 外置 知识库.json → 用户规则（我的规则.json + config.json 里的 user_rules）。"""
    warnings: list[str] = []

    builtin, problem = read_json(default_path)
    if problem:
        warnings.append("自带的默认规则读不出来：" + problem)
    if not isinstance(builtin, dict):
        builtin = {"version": "fallback", "rules": []}

    external, problem = read_json(external_path)
    if problem:
        warnings.append("「知识库.json」" + problem)
    elif external is not None and not isinstance(external, dict):
        warnings.append("「知识库.json」格式不对，已跳过这一层")

    user = None
    if user_rules_path is not None:
        user, problem = read_json(user_rules_path)
        if problem:
            warnings.append("「我的规则.json」" + problem)
        elif user is not None and not isinstance(user, dict):
            warnings.append("「我的规则.json」格式不对，已跳过这一层")

    return KnowledgeBase.build(
        builtin=builtin,
        external=external,
        user=user,
        inline_user_rules=inline_user_rules,
        warnings=warnings,
    )
