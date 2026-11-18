"""impact 必填的兼容与降级行为。"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.knowledge import KnowledgeBase, load_from_files
from core.risk import Risk, evaluate

ROOT = Path(__file__).resolve().parent.parent


def rule(rule_id, risk, **extra):
    data = {
        "id": rule_id, "layer": 1, "pattern": "d:\\test\\" + rule_id + "\\",
        "match": "path_contains", "risk": risk, "effect": "说明",
    }
    data.update(extra)
    return data


def build(rules):
    return KnowledgeBase.build({"schema": 3, "version": "t", "rules": rules})


classDowngrade = None


class DowngradeTests(unittest.TestCase):
    def test_forbidden_without_impact_becomes_unlisted(self):
        kb = build([rule("n", "forbidden")])
        result = kb.match(r"D:\test\n\x.txt", False)
        self.assertEqual(result[1], "unlisted", "禁止级缺 impact 必须降灰")

    def test_forbidden_with_impact_stays(self):
        kb = build([rule("n", "forbidden", impact="删了起不来")])
        result = kb.match(r"D:\test\n\x.txt", False)
        self.assertEqual(result[1], "forbidden")

    def test_caution_without_impact_stays_caution(self):
        kb = build([rule("n", "caution")])
        result = kb.match(r"D:\test\n\x.txt", False)
        self.assertEqual(result[1], "caution", "黄色缺 impact 只提示，不降级")

    def test_safe_without_impact_stays(self):
        kb = build([rule("n", "safe")])
        self.assertEqual(kb.match(r"D:\test\n\x.txt", False)[1], "safe")

    def test_downgrade_is_recorded(self):
        kb = build([rule("n", "forbidden")])
        issues = kb.impact_issues()
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0]["downgraded"])
        self.assertEqual(issues[0]["was"], "forbidden")
        self.assertEqual(issues[0]["risk"], "unlisted")

    def test_caution_missing_not_marked_downgraded(self):
        kb = build([rule("n", "caution")])
        issues = kb.impact_issues()
        self.assertEqual(len(issues), 1)
        self.assertFalse(issues[0]["downgraded"])

    def test_warning_mentions_rule_id(self):
        kb = build([rule("bad-rule", "forbidden")])
        self.assertTrue(any("bad-rule" in warning for warning in kb.warnings))

    def test_impact_flows_into_match_result(self):
        kb = build([rule("n", "forbidden", impact="删了会坏")])
        result = kb.match_detail(r"D:\test\n\x.txt", False)
        self.assertEqual(result.impact, "删了会坏")
        self.assertFalse(result.impact_missing)

    def test_missing_impact_flag(self):
        kb = build([rule("n", "caution")])
        result = kb.match_detail(r"D:\test\n\x.txt", False)
        self.assertEqual(result.impact, "")
        self.assertTrue(result.impact_missing)


class OldLibraryTests(unittest.TestCase):
    """v1.3 的知识库没有 impact 字段，升级后必须能加载。"""

    def setUp(self):
        self.doc = json.loads((ROOT / "resources" / "default_knowledge.json").read_text(encoding="utf-8"))
        for entry in self.doc["rules"]:
            entry.pop("impact", None)      # 变回旧格式

    def test_old_library_loads(self):
        kb = KnowledgeBase.build(self.doc)
        self.assertEqual(kb.rule_count, len(self.doc["rules"]))
        self.assertEqual(kb.impact_missing_count, kb.rule_count)

    def test_old_forbidden_become_grey_not_dropped(self):
        kb = KnowledgeBase.build(self.doc)
        # 规则还在（没被跳过），只是等级降了
        result = kb.match(r"C:\Windows\boot\BCD", False)
        self.assertEqual(result[1], "unlisted")
        self.assertGreater(kb.rule_count, 0)

    def test_old_safe_rules_unaffected(self):
        kb = KnowledgeBase.build(self.doc)
        result = kb.match(r"C:\Windows\Temp\x.tmp", False)
        self.assertEqual(result[1], "safe")

    def test_issues_list_actionable(self):
        kb = KnowledgeBase.build(self.doc)
        issues = kb.impact_issues()
        self.assertEqual(len(issues), kb.rule_count)
        sample = issues[0]
        for key in ("id", "kind", "match", "pattern", "risk", "downgraded"):
            self.assertIn(key, sample)


class ShippedLibraryTests(unittest.TestCase):
    """随程序发布的这一份必须是完整的。"""

    def setUp(self):
        self.kb = load_from_files(ROOT / "resources" / "default_knowledge.json", ROOT / "知识库.json")

    def test_every_rule_has_impact(self):
        self.assertEqual(self.kb.impact_missing_count, 0, "自带规则应该全部写了 impact")

    def test_no_downgrade_warnings(self):
        self.assertEqual([w for w in self.kb.warnings if "impact" in w], [])

    def test_all_four_kinds_expose_impact(self):
        cases = [
            ("file", lambda: self.kb.match_detail(r"C:\Windows\boot", True)),
            ("software", lambda: self.kb.match_software("Microsoft Visual C++ 2015 Redistributable", "Microsoft")),
            ("registry", lambda: self.kb.match_registry(r"HKEY_LOCAL_MACHINE\SYSTEM")),
            ("process", lambda: self.kb.match_process("lsass.exe")),
        ]
        for kind, getter in cases:
            result = getter()
            self.assertTrue(result.impact, f"{kind} 类规则应该有 impact")
            self.assertEqual(result.risk, "forbidden", f"{kind} 类这条应该是红的")

    def test_verdict_carries_impact(self):
        verdict = evaluate(r"C:\Windows\boot", True, self.kb)
        self.assertEqual(verdict.level, Risk.FORBIDDEN)
        self.assertTrue(verdict.impact)


class ConfirmColumnTests(unittest.TestCase):
    """确认清单里的「删了会怎样」一列，取的就是 impact。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def item(name, impact="", effect=""):
        from dataclasses import dataclass

        @dataclass
        class Fake:
            name: str
            risk: str = "forbidden"
            impact: str = ""
            effect: str = ""
            size_bytes: int | None = None

        return Fake(name=name, impact=impact, effect=effect)

    def column_texts(self, items):
        from ui.confirm_flow import ConfirmDialog

        dialog = ConfirmDialog(items, "删除", "提示")
        table = dialog.table
        return (table.horizontalHeaderItem(dialog.COL_IMPACT).text(),
                [table.item(row, dialog.COL_IMPACT).text() for row in range(table.rowCount())])

    def test_column_is_about_consequence(self):
        header, _rows = self.column_texts([self.item("甲", impact="删了会坏")])
        self.assertEqual(header, "删了会怎样")

    def test_shows_impact_verbatim(self):
        _header, rows = self.column_texts([
            self.item("Windows 引导文件", impact="删了电脑开机直接失败", effect="开机引导用的文件"),
        ])
        self.assertEqual(rows[0], "删了电脑开机直接失败")

    def test_missing_impact_says_so_instead_of_borrowing_effect(self):
        _header, rows = self.column_texts([self.item("旧规则", impact="", effect="这条只有作用")])
        self.assertNotIn("这条只有作用", rows[0])
        self.assertIn("没写后果", rows[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
