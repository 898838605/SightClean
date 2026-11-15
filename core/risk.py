"""风险等级与展示文案。

等级只有四种，前三档来自知识库，最后一档表示「知识库没收录」：

    safe       放心删    绿
    caution    看情况    黄
    forbidden  别删      红
    unlisted   未收录    灰   ← 刻意不用黄色：没收录不等于有风险，用黄色会吓人

文件夹的风险按「所有子孙规则中最严格的一级」算，靠路径前缀预判，不遍历磁盘。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.knowledge import (
    RISK_CAUTION,
    RISK_FORBIDDEN,
    RISK_SAFE,
    UNLISTED_EFFECT,
    KnowledgeBase,
)

class Risk(str, Enum):
    SAFE = "safe"
    CAUTION = "caution"
    FORBIDDEN = "forbidden"
    UNLISTED = "unlisted"

    @classmethod
    def parse(cls, value: object) -> "Risk":
        """容错解析：认不得的值、旧版本的 danger/unknown 都落到 unlisted。"""
        if isinstance(value, Risk):
            return value
        text = str(value or "").strip().lower()
        if text == "danger":       # 上一版的写法，等价于 forbidden
            return cls.FORBIDDEN
        if text == "unknown":      # 上一版的写法，等价于 unlisted
            return cls.UNLISTED
        try:
            return cls(text)
        except ValueError:
            return cls.UNLISTED

    @property
    def label(self) -> str:
        return LABELS[self]

    @property
    def color(self) -> str:
        return COLORS[self]

    @property
    def rank(self) -> int:
        return RANKS[self]

    @property
    def advice(self) -> str:
        return ADVICE_OF[self]

LABELS = {
    Risk.SAFE: "放心删",
    Risk.CAUTION: "看情况",
    Risk.FORBIDDEN: "别删",
    Risk.UNLISTED: "未收录",
}

COLORS = {
    Risk.SAFE: "#1a7f37",
    Risk.CAUTION: "#9a6700",
    Risk.FORBIDDEN: "#cf222e",
    Risk.UNLISTED: "#8b949e",   # 灰：没信息，不是警告
}

def set_colors(mapping: dict) -> None:
    """换主题时由 ui.theme 调：深色下这几个颜色都要提亮才看得清。"""
    for level in (Risk.SAFE, Risk.CAUTION, Risk.FORBIDDEN, Risk.UNLISTED):
        value = mapping.get(level.value)
        if isinstance(value, str) and value.startswith("#"):
            COLORS[level] = value

RANKS = {
    Risk.UNLISTED: 0,
    Risk.SAFE: 1,
    Risk.CAUTION: 2,
    Risk.FORBIDDEN: 3,
}

ADVICE_LABEL = {"delete": "可以删", "maybe": "看情况", "keep": "建议保留"}
ADVICE_COLOR = {"delete": "#1a7f37", "maybe": "#9a6700", "keep": "#cf222e"}
ADVICE_OF = {
    Risk.SAFE: "delete",
    Risk.CAUTION: "maybe",
    Risk.FORBIDDEN: "keep",
    Risk.UNLISTED: "maybe",
}

# 从知识库的 risk 字符串换到展示枚举
_FROM_KB = {
    RISK_SAFE: Risk.SAFE,
    RISK_CAUTION: Risk.CAUTION,
    RISK_FORBIDDEN: Risk.FORBIDDEN,
    # 禁止级规则缺 impact 时会被降成这个，展示为灰色「未收录」
    "unlisted": Risk.UNLISTED,
}

@dataclass(frozen=True)
class Verdict:
    level: Risk
    effect: str                      # 给用户看的一句话
    reason: str                      # 为什么是这个等级（tooltip 用）
    source: str = ""                 # builtin / user / 空=未收录
    rule_id: str | None = None
    advice: str = "maybe"
    escalated: bool = False          # 是否因为「里面藏着更严格的东西」而升级
    hidden: tuple[tuple[str, str], ...] = ()
    impact: str = ""                 # 一句人话后果（知识库 impact；AI 结果也会填这里）
    impact_missing: bool = False     # 规则没写 impact（三道坎会如实说「未标注」）

    @property
    def purpose(self) -> str:        # 给列表「作用」列直接用
        return self.effect

    @property
    def label(self) -> str:
        return self.level.label

    @property
    def color(self) -> str:
        return self.level.color

    @property
    def advice_label(self) -> str:
        return ADVICE_LABEL.get(self.advice, "看情况")

    @property
    def known(self) -> bool:
        return self.level is not Risk.UNLISTED

def _reason_of(level: Risk, effect: str) -> str:
    if level is Risk.SAFE:
        return "这个位置知识库认得，属于可清理的东西"
    if level is Risk.CAUTION:
        return "能不能删取决于你自己用不用它，删之前看一眼"
    if level is Risk.FORBIDDEN:
        return "这是不能删的位置，删了大概率找不回来或者会坏东西"
    return "知识库不认识这个位置，删之前请自己确认"

def evaluate(human_path: str, is_dir: bool, kb: KnowledgeBase, locked: bool = False) -> Verdict:
    """把一条记录算成一个可以直接放到界面上的结论。"""
    if locked:
        return Verdict(
            Risk.UNLISTED,
            "无权限访问",
            "系统不让读这个位置，所以不算大小、也不能勾选删除",
            advice="keep",
        )

    own = kb.match_detail(human_path, is_dir)
    if not is_dir:
        level = _FROM_KB.get(own.risk, Risk.UNLISTED) if own.known else Risk.UNLISTED
        return Verdict(
            level,
            own.effect,
            _reason_of(level, own.effect),
            own.source,
            own.rule_id,
            advice=level.advice,
            impact=own.impact,
            impact_missing=own.impact_missing,
        )

    worst_risk, folder_known, hints = kb.folder_risk_detail(human_path)
    folder_level = _FROM_KB.get(worst_risk, Risk.UNLISTED) if folder_known else Risk.UNLISTED

    if own.known:
        level = _FROM_KB.get(own.risk, Risk.UNLISTED)
        escalated = folder_level.rank > level.rank
        if escalated:
            leaf = "、".join(name for name, _ in hints[:2]) or "不该删的东西"
            return Verdict(
                folder_level,
                own.effect,
                f"这个文件夹本身是「{level.label}」，但里面藏着「{leaf}」，整删之前先进去看一眼",
                own.source,
                own.rule_id,
                advice=folder_level.advice,
                escalated=True,
                hidden=hints,
                impact=own.impact,
                impact_missing=own.impact_missing,
            )
        return Verdict(
            level,
            own.effect,
            _reason_of(level, own.effect),
            own.source,
            own.rule_id,
            advice=level.advice,
            hidden=hints,
            impact=own.impact,
            impact_missing=own.impact_missing,
        )

    if folder_known:
        leaf = "、".join(name for name, _ in hints[:2])
        if leaf:
            effect = f"没直接收录，但里面有「{leaf}」这类东西，删整个之前先进去看一眼"
            detail = f"这个文件夹本身没收录，但按规则它下面藏着「{leaf}」"
        else:
            effect = UNLISTED_EFFECT
            detail = "这个文件夹本身没收录，但里面有关键位置"
        return Verdict(
            folder_level,
            effect,
            detail + "，删之前先进去看一眼",
            "",
            None,
            advice=folder_level.advice,
            escalated=True,
            hidden=hints,
        )

    return Verdict(Risk.UNLISTED, UNLISTED_EFFECT, _reason_of(Risk.UNLISTED, UNLISTED_EFFECT), advice="maybe")
