"""纯逻辑单测：知识库三层匹配、风险判定、配置兜底。不依赖界面库。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.config import SettingsStore, read_json, write_json
from core.knowledge import KnowledgeBase, extension_of, normalize_path
from core.risk import Risk, evaluate

ROOT = Path(__file__).resolve().parent.parent

DEMO = "\\demo\\"
OLD = "\\old\\"
NEW = "\\new\\"

def builtin_doc() -> dict:
    return json.loads((ROOT / "resources" / "default_knowledge.json").read_text(encoding="utf-8"))

def rule(rule_id: str, layer: int, pattern: str, match: str, risk: str,
         effect: str = "说明", source: str | None = None) -> dict:
    """source 不写就交给「这份规则来自哪个文件」决定，和真实用法一致。"""
    # impact 是必填的「删了会怎样」；不写的话禁止级会被降成灰色，测试里统一给一句
    data = {"id": rule_id, "layer": layer, "pattern": pattern, "match": match,
            "risk": risk, "effect": effect, "impact": f"测试用后果：{risk}"}
    if source is not None:
        data["source"] = source
    return data

class PathToolTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_path(r"C:\Windows"), "c:\windows")
        self.assertEqual(normalize_path("C:/Windows/"), "c:\windows")
        self.assertEqual(normalize_path("C:\\"), "c:\\")

    def test_extension(self):
        self.assertEqual(extension_of(r"D:\vm\a.VMDK"), ".vmdk")
        self.assertEqual(extension_of(r"D:\a\.gitignore"), "")
        self.assertEqual(extension_of(r"D:\a\noext"), "")

class MatchTests(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase.build(builtin_doc())

    def test_contract_returns_three_values(self):
        result = self.kb.match(r"C:\Windows\System32\a.dll", False)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        effect, risk, source = result
        self.assertEqual(risk, "forbidden")
        self.assertEqual(source, "builtin")
        self.assertTrue(effect)

    def test_specific_rule_wins_over_general_in_same_layer(self):
        # \windows\temp\ 排在 \windows\ 前面，所以是 safe 而不是 forbidden
        self.assertEqual(self.kb.match(r"C:\Windows\Temp\x.tmp", False)[1], "safe")
        self.assertEqual(self.kb.match(r"C:\Windows\System32\x.dll", False)[1], "forbidden")

    def test_layer_order(self):
        kb = KnowledgeBase.build(
            {"rules": [rule("l1", 1, DEMO, "path_contains", "forbidden", "系统库说的")]},
            None,
            {"rules": [rule("l2", 2, DEMO, "path_contains", "caution", "软件库说的")]},
        )
        self.assertEqual(kb.match(r"D:\demo\x", False)[0], "软件库说的")

    def test_user_rules_beat_builtin(self):
        kb = KnowledgeBase.build(
            {"rules": [rule("b", 1, DEMO, "path_contains", "forbidden", "内置说不能删")]},
            None,
            {"rules": [rule("u", 3, DEMO, "path_contains", "safe", "我说能删", source="user")]},
        )
        self.assertEqual(kb.match(r"D:\demo\x", False)[1], "safe")

    def test_inline_user_rules_from_config(self):
        kb = KnowledgeBase.build(
            {"rules": [rule("b", 1, DEMO, "path_contains", "forbidden")]},
            None, None,
            inline_user_rules=[{"id": "cfg", "layer": 2, "pattern": DEMO,
                                "match": "path_contains", "effect": "config 里写的", "risk": "caution", "impact": "测试用后果：删了要重来"}],
        )
        effect, risk, source = kb.match(r"E:\demo\a", False)
        self.assertEqual(effect, "config 里写的")
        self.assertEqual(source, "user")

    def test_later_rule_with_same_id_overrides(self):
        kb = KnowledgeBase.build({"rules": [
            rule("same", 1, OLD, "path_contains", "safe", "旧"),
            rule("same", 1, NEW, "path_contains", "forbidden", "新"),
        ]})
        self.assertEqual(kb.rule_count, 1)
        self.assertEqual(kb.match(r"D:\new\a", False)[0], "新")

    def test_ext_rule_only_for_files(self):
        self.assertEqual(self.kb.match(r"D:\vm\disk.vmdk", False)[1], "forbidden")
        self.assertEqual(self.kb.match(r"D:\vm\disk.vmdk", True)[1], "unlisted")
        self.assertEqual(self.kb.match(r"D:\vm\disk.vmdk", True)[2], "")

    def test_regex_rule(self):
        kb = KnowledgeBase.build({"rules": [
            rule("re", 1, "^[a-z]:\\\\(alpha|beta)\\\\secret", "path_regex", "forbidden", "正则命中的"),
        ]})
        self.assertEqual(kb.match(r"D:\beta\secret\x", False)[1], "forbidden")
        self.assertEqual(kb.match(r"D:\gamma\secret\x", False)[2], "")

    def test_env_variable(self):
        kb = KnowledgeBase.build({"rules": [
            rule("e", 2, "%TEMP%\\", "path_contains", "safe", "临时目录"),
        ]})
        effect, risk, source = kb.match(os.path.join(os.environ["TEMP"], "a.tmp"), False)
        self.assertEqual(risk, "safe")

    def test_unlisted(self):
        effect, risk, source = self.kb.match(r"Z:\完全没听过\a.bin", False)
        self.assertEqual(effect, "未收录，建议确认后再删")
        self.assertEqual(risk, "unlisted")
        self.assertEqual(source, "")

    def test_prefix_boundary_not_leaking(self):
        # 规则写 \windows\ 时不该命中 c:\mywindows\
        self.assertEqual(self.kb.match(r"C:\mywindows\a.dll", False)[2], "")

class FolderRiskTests(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase.build(builtin_doc())

    def test_windows_is_forbidden(self):
        self.assertEqual(self.kb.folder_risk(r"C:\Windows"), "forbidden")

    def test_temp_is_safe(self):
        self.assertEqual(self.kb.folder_risk(os.environ["TEMP"]), "safe")

    def test_descendant_prediction_without_disk_access(self):
        kb = KnowledgeBase.build({"rules": [
            rule("deep", 1, "D:\\keep\\important\\keys\\", "path_contains", "forbidden", "密钥在这"),
            rule("outer", 1, "D:\\keep\\", "path_contains", "safe", "外层是缓存"),
        ]})
        self.assertEqual(kb.folder_risk(r"D:\keep"), "forbidden")
        self.assertEqual(kb.folder_risk("D:\\"), "forbidden")

    def test_unknown_folder(self):
        risk, known, hints = self.kb.folder_risk_detail(r"Z:\nothing")
        self.assertEqual(risk, "unlisted")
        self.assertFalse(known)
        self.assertEqual(hints, ())

class RuleValidationTests(unittest.TestCase):
    def test_bad_rules_are_skipped_with_warning(self):
        kb = KnowledgeBase.build({"rules": [
            rule("no-effect", 1, "\\a\\", "path_contains", "safe", effect=""),
            rule("bad-risk", 1, "\\b\\", "path_contains", "danger"),
            rule("bad-match", 1, "\\c\\", "fuzzy", "safe"),
            rule("bad-layer", 9, "\\d\\", "path_contains", "safe"),
        ]})
        self.assertEqual(kb.rule_count, 0)
        self.assertGreaterEqual(len(kb.warnings), 4)

    def test_old_format_gets_a_clear_hint(self):
        kb = KnowledgeBase.build({"rules": [
            {"id": "old", "match": {"type": "path", "pattern": "D:\\x\\**"},
             "purpose": "旧写法", "risk": "safe", "impact": "测试用后果：删了没影响"},
        ]})
        self.assertEqual(kb.rule_count, 0)
        self.assertTrue(any("上一版" in warning for warning in kb.warnings))

    def test_missing_env_var_skips_rule(self):
        kb = KnowledgeBase.build({"rules": [
            rule("env", 1, "%NO_SUCH_VAR_XYZ%\\", "path_contains", "safe"),
        ]})
        self.assertEqual(kb.rule_count, 0)
        self.assertTrue(kb.warnings)

class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase.build(builtin_doc())

    def test_forbidden_gets_keep_advice(self):
        verdict = evaluate(r"C:\Windows\System32\a.dll", False, self.kb)
        self.assertIs(verdict.level, Risk.FORBIDDEN)
        self.assertEqual(verdict.advice, "keep")
        self.assertTrue(verdict.known)

    def test_unlisted_is_gray_not_yellow(self):
        verdict = evaluate(r"Z:\没听过\a.bin", False, self.kb)
        self.assertIs(verdict.level, Risk.UNLISTED)
        self.assertEqual(verdict.level.color, "#8b949e")
        self.assertFalse(verdict.known)
        self.assertEqual(verdict.effect, "未收录，建议确认后再删")

    def test_folder_escalates_when_hiding_forbidden(self):
        kb = KnowledgeBase.build({"rules": [
            rule("outer", 1, "D:\\cache\\", "path_contains", "safe", "这层是缓存"),
            rule("inner", 1, "D:\\cache\\keys\\", "path_contains", "forbidden", "密钥别删"),
        ]})
        verdict = evaluate(r"D:\cache", True, kb)
        self.assertIs(verdict.level, Risk.FORBIDDEN)
        self.assertTrue(verdict.escalated)
        self.assertIn("keys", verdict.reason)

    def test_locked_is_unlisted(self):
        verdict = evaluate(r"C:\Windows", True, self.kb, locked=True)
        self.assertIs(verdict.level, Risk.UNLISTED)

    def test_risk_parse_is_tolerant(self):
        self.assertIs(Risk.parse("forbidden"), Risk.FORBIDDEN)
        self.assertIs(Risk.parse("danger"), Risk.FORBIDDEN)
        self.assertIs(Risk.parse("unknown"), Risk.UNLISTED)
        self.assertIs(Risk.parse(None), Risk.UNLISTED)

class ConfigMergeTests(unittest.TestCase):
    def test_partial_external_keeps_builtin_defaults(self):
        """外置只写一个字段时，同段落的其他字段必须还是内置默认，不能被盖没。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "default_config.json"
            builtin.write_text(json.dumps({
                "auto_size": True,
                "ai": {"provider": "deepseek", "timeout_seconds": 30, "model": "内置模型"},
            }), encoding="utf-8")
            external = root / "config.json"
            external.write_text(json.dumps({"ai": {"api_key": "我的密钥"}}), encoding="utf-8")

            settings = SettingsStore(external, builtin).load()
            self.assertEqual(settings.ai.api_key, "我的密钥")
            self.assertEqual(settings.ai.provider, "deepseek", "内置默认被浅合并盖掉了")
            self.assertEqual(settings.ai.timeout_seconds, 30.0)
            self.assertEqual(settings.ai.model, "内置模型")

    def test_external_overrides_scalar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "default_config.json"
            builtin.write_text(json.dumps({"auto_size": True}), encoding="utf-8")
            external = root / "config.json"
            external.write_text(json.dumps({"auto_size": False}), encoding="utf-8")
            self.assertFalse(SettingsStore(external, builtin).load().auto_size)

    def test_broken_builtin_still_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "default_config.json"
            builtin.write_text("这不是 json", encoding="utf-8")
            settings = SettingsStore(root / "config.json", builtin).load()
            self.assertTrue(settings.auto_size)
            self.assertTrue(any("损坏" in w for w in settings.warnings))

class FirstRunTests(unittest.TestCase):
    def test_generates_editable_files_once(self):
        from core.config import ensure_first_run_files

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            default_config = root / "default_config.json"
            default_knowledge = root / "default_knowledge.json"
            default_config.write_text('{"auto_size": true}', encoding="utf-8")
            default_knowledge.write_text('{"rules": []}', encoding="utf-8")
            config = root / "config.json"
            knowledge = root / "知识库.json"

            notes = ensure_first_run_files(default_config, default_knowledge, config, knowledge)
            self.assertEqual(len(notes), 2)
            self.assertTrue(config.exists())
            self.assertTrue(knowledge.exists())
            self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["auto_size"], True)

            # 第二次不该再动它，也不该覆盖用户改过的内容
            config.write_text('{"auto_size": false}', encoding="utf-8")
            again = ensure_first_run_files(default_config, default_knowledge, config, knowledge)
            self.assertEqual(again, [])
            self.assertFalse(json.loads(config.read_text(encoding="utf-8"))["auto_size"])

    def test_broken_cache_is_reported(self):
        from core.config import CacheStore

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text("{{{ 坏的", encoding="utf-8")
            cache = CacheStore(path)
            cache.load()
            self.assertTrue(cache.warnings)
            self.assertTrue(any("cache.json" in w for w in cache.warnings))

    def test_cache_wrong_shape_is_reported(self):
        from core.config import CacheStore

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            cache = CacheStore(path)
            cache.load()
            self.assertTrue(any("格式不对" in w for w in cache.warnings))

class ConfigTests(unittest.TestCase):
    def test_broken_json_backs_up_and_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text("{{{ not json", encoding="utf-8")
            store = SettingsStore(config, ROOT / "resources" / "default_config.json")
            settings = store.load()
            self.assertTrue(settings.auto_size)
            self.assertTrue(any("损坏" in w for w in settings.warnings))
            self.assertTrue(list(tmp_path.glob("config.bad-*.json")))

    def test_field_level_coercion(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"auto_size": "yes", "auto_size_concurrency": 999,
                                          "log_keep_count": "abc", "ai": {"enabled": "true"}}), encoding="utf-8")
            store = SettingsStore(config, ROOT / "resources" / "default_config.json")
            settings = store.load()
            self.assertTrue(settings.auto_size)
            self.assertEqual(settings.auto_size_concurrency, 8)
            self.assertEqual(settings.log_keep_count, 30)
            self.assertTrue(settings.ai.enabled)
            self.assertTrue(settings.warnings)

    def test_user_rules_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"user_rules": [
                {"id": "mine", "layer": 2, "pattern": "\\x\\", "match": "path_contains",
                 "effect": "我的", "risk": "safe", "impact": "测试用后果：删了没影响"},
                "不是对象", 123,
            ]}), encoding="utf-8")
            store = SettingsStore(config, ROOT / "resources" / "default_config.json")
            settings = store.load()
            self.assertEqual(len(settings.user_rules), 1)
            self.assertTrue(any("user_rules" in w for w in settings.warnings))

    def test_atomic_write_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            self.assertTrue(write_json(path, {"a": "中文"}))
            data, problem = read_json(path)
            self.assertIsNone(problem)
            self.assertEqual(data["a"], "中文")

if __name__ == "__main__":
    unittest.main(verbosity=2)
