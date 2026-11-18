"""联网识别的单测：JSON 解析、脱敏、批量进度、中断、缓存。全部离线跑。"""
from __future__ import annotations

import os as _os

_os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication as _QApp

    _QT_APP = _QApp.instance() or _QApp([])
except Exception:  # noqa: BLE001
    _QT_APP = None

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QCoreApplication

from core.ai_client import (
    AI_NOTE,
    PROVIDERS,
    AIClient,
    AIResult,
    map_risk,
    mask_path,
    normalize_base_url,
    parse_reply,
)
from core.config import CacheStore, Settings

FENCE = chr(96) * 3

class ParseTests(unittest.TestCase):
    def test_plain_json(self):
        parsed = parse_reply('{"作用说明":"临时缓存","删除影响":"下次要重新生成","风险等级":"safe"}')
        self.assertEqual(parsed["purpose"], "临时缓存")
        self.assertEqual(parsed["impact"], "下次要重新生成")
        self.assertEqual(parsed["risk"], "safe")
        self.assertTrue(parsed["risk_mapped"])

    def test_fenced_json(self):
        body = '{"作用说明":"系统文件","删除影响":"开不了机","风险等级":"forbidden"}'
        raw = "好的：" + chr(10) + FENCE + "json" + chr(10) + body + chr(10) + FENCE + chr(10) + "希望有帮助"
        parsed = parse_reply(raw)
        self.assertEqual(parsed["risk"], "forbidden")
        self.assertEqual(parsed["purpose"], "系统文件")

    def test_json_inside_chatter(self):
        raw = '我说：{"作用说明":"下载目录","删除影响":"自己下载的东西没了","风险等级":"caution"} 完毕'
        parsed = parse_reply(raw)
        self.assertEqual(parsed["purpose"], "下载目录")

    def test_english_keys(self):
        parsed = parse_reply('{"purpose":"缓存","impact":"重建","risk":"caution"}')
        self.assertEqual(parsed["purpose"], "缓存")
        self.assertEqual(parsed["risk"], "caution")

    def test_missing_purpose_raises(self):
        with self.assertRaises(ValueError):
            parse_reply('{"风险等级":"safe"}')

    def test_not_json_raises(self):
        with self.assertRaises(ValueError):
            parse_reply("这个文件夹是临时缓存，可以删。")

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse_reply("")

    def test_unknown_risk_falls_back_to_caution(self):
        parsed = parse_reply('{"作用说明":"某个东西","删除影响":"不清楚","风险等级":"紫"}')
        self.assertEqual(parsed["risk"], "caution")
        self.assertFalse(parsed["risk_mapped"])

    def test_chinese_risk_words(self):
        for word, expected in (("放心删", "safe"), ("看情况", "caution"), ("禁止", "forbidden"), ("危险", "forbidden")):
            self.assertEqual(map_risk(word)[0], expected, word)
            payload = '{"作用说明":"x","风险等级":"%s"}' % word
            self.assertEqual(parse_reply(payload)["risk"], expected)

class MaskTests(unittest.TestCase):
    def test_user_profile_is_masked(self):
        home = os.environ.get("USERPROFILE", "")
        if not home:
            self.skipTest("没有 USERPROFILE")
        masked = mask_path(os.path.join(home, "Documents", "a.txt"))
        self.assertTrue(masked.startswith("%USER%"))
        self.assertNotIn(os.path.basename(home), masked)

    def test_other_path_untouched(self):
        self.assertEqual(mask_path("D:\\别的\\地方"), "D:\\别的\\地方")

class BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dch-ai-"))
        self.cache = CacheStore(self.tmp / "cache.json")
        self.settings = Settings()
        self.settings.ai.enabled = True
        self.settings.ai.base_url = "https://example.com"
        self.settings.ai.model = "test-model"
        self.settings.ai.api_key = "test-key"
        self.settings.ai.mask_username = True
        self.results = []
        self.progress = []
        self.failed = []
        self.finished = []
        self.client = AIClient(self.settings, self.cache)
        self.client.resultReady.connect(lambda result: self.results.append(result))
        self.client.progress.connect(lambda *args: self.progress.append(args))
        self.client.failed.connect(lambda *args: self.failed.append(args))
        self.client.batchFinished.connect(lambda *args: self.finished.append(args))

    def tearDown(self):
        self.client.shutdown()

    def pump(self, seconds: float, until=None) -> None:
        end = time.time() + seconds
        while time.time() < end:
            _QT_APP.processEvents()
            if until is not None and until():
                return
            time.sleep(0.01)

    def test_batch_reports_progress_and_finish(self):
        def fake(task):
            return AIResult(human_path=task.human_path, name=task.name,
                            purpose="测试说明", impact="影响不大", risk="safe")

        pairs = [(f"C:\\测试\\{index}.txt", f"{index}.txt") for index in range(3)]
        with mock.patch("core.ai_client._ask_service", side_effect=fake):
            self.assertEqual(self.client.explain_many(pairs), 3)
            self.pump(5, until=lambda: bool(self.finished))

        self.assertEqual(len(self.results), 3)
        self.assertEqual(self.finished, [(3, 0)])
        self.assertEqual(self.progress[-1][:2], (3, 3))

    def test_one_failure_does_not_stop_others(self):
        def flaky(task):
            if "bad" in task.human_path:
                raise ValueError("对面回的不是 JSON")
            return AIResult(human_path=task.human_path, name=task.name,
                            purpose="好的", impact="", risk="safe")

        pairs = [(r"C:\测试\good1.txt", "good1.txt"), (r"C:\测试\bad.txt", "bad.txt"),
                 (r"C:\测试\good2.txt", "good2.txt")]
        with mock.patch("core.ai_client._ask_service", side_effect=flaky):
            self.client.explain_many(pairs)
            self.pump(5, until=lambda: bool(self.finished))

        self.assertEqual(len(self.results), 2)
        self.assertEqual(len(self.failed), 1)
        self.assertIn("重试", self.failed[0][1])
        self.assertEqual(self.finished, [(2, 1)])

    def test_results_are_cached_and_reused(self):
        calls = []

        def fake(task):
            calls.append(task.human_path)
            return AIResult(human_path=task.human_path, name=task.name,
                            purpose="存过的说明", impact="", risk="caution")

        pair = [(r"C:\测试\cached.txt", "cached.txt")]
        with mock.patch("core.ai_client._ask_service", side_effect=fake):
            self.client.explain_many(pair)
            self.pump(5, until=lambda: bool(self.finished))

        self.assertEqual(len(calls), 1)
        self.results.clear()
        self.finished.clear()
        with mock.patch("core.ai_client._ask_service", side_effect=fake) as patched:
            self.client.explain_many(pair)
            self.pump(1.0, until=lambda: bool(self.finished))
            self.assertFalse(patched.called, "命中缓存时不该再发请求")
        self.assertEqual(len(self.results), 1)
        self.assertTrue(self.results[0].cached)

    def test_requests_really_run_one_at_a_time(self):
        """串行：任何时刻最多只有一条在跑。"""
        state = {"now": 0, "peak": 0}
        guard = __import__("threading").Lock()

        def fake(task):
            with guard:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with guard:
                state["now"] -= 1
            return AIResult(human_path=task.human_path, name=task.name,
                            purpose="x", impact="", risk="safe")

        pairs = [(f"C:\\测试\\s{index}.txt", f"s{index}.txt") for index in range(4)]
        with mock.patch("core.ai_client._ask_service", side_effect=fake):
            self.client.explain_many(pairs)
            self.pump(8, until=lambda: bool(self.finished))
        self.assertEqual(state["peak"], 1, f"同时跑了 {state['peak']} 条，不串行")

    def test_cancel_stops_the_batch(self):
        def slow(task):
            time.sleep(0.15)
            return AIResult(human_path=task.human_path, name=task.name,
                            purpose="慢", impact="", risk="safe")

        pairs = [(f"C:\\测试\\{index}.txt", f"{index}.txt") for index in range(5)]
        with mock.patch("core.ai_client._ask_service", side_effect=slow):
            self.client.explain_many(pairs)
            self.pump(0.05)
            self.client.cancel_all()
            self.pump(0.8)
        self.assertLess(len(self.results), 5, "取消之后不该把 5 条全跑完")

    def test_not_configured_reports_plainly(self):
        self.settings.ai.api_key = ""
        self.client.explain_many([(r"C:\x", "x")])
        self.assertTrue(self.failed)
        self.assertIn("还没配置", self.failed[0][1])

    def test_mask_username_reaches_the_prompt(self):
        home = os.environ.get("USERPROFILE", "")
        if not home:
            self.skipTest("没有 USERPROFILE")
        sent = {}

        def capture(task):
            sent["path"] = mask_path(task.human_path) if task.masked_send else task.human_path
            return AIResult(human_path=task.human_path, name=task.name, purpose="x", impact="", risk="safe")

        pair = [(os.path.join(home, "Documents", "简历.docx"), "简历.docx")]
        with mock.patch("core.ai_client._ask_service", side_effect=capture):
            self.client.explain_many(pair)
            self.pump(5, until=lambda: bool(self.finished))
        self.assertTrue(sent["path"].startswith("%USER%"), sent.get("path"))
        self.assertNotIn(os.path.basename(home), sent["path"])

class BaseUrlTests(unittest.TestCase):
    def test_plain_host_kept(self):
        self.assertEqual(normalize_base_url("https://api.deepseek.com"),
                         "https://api.deepseek.com")

    def test_trailing_slash_removed(self):
        self.assertEqual(normalize_base_url("https://api.deepseek.com/"),
                         "https://api.deepseek.com")

    def test_v1_suffix_removed(self):
        self.assertEqual(normalize_base_url("https://api.deepseek.com/v1"),
                         "https://api.deepseek.com")
        self.assertEqual(normalize_base_url("https://api.deepseek.com/v1/"),
                         "https://api.deepseek.com")

    def test_empty(self):
        self.assertEqual(normalize_base_url(""), "")
        self.assertEqual(normalize_base_url(None), "")


class ProviderTests(unittest.TestCase):
    def test_presets_are_complete(self):
        for key in ("deepseek", "kimi", "openai", "custom"):
            self.assertIn(key, PROVIDERS)
        for key, (label, url, model) in PROVIDERS.items():
            self.assertTrue(label, key)
            if key != "custom":
                self.assertTrue(url.startswith("http"), key)
                self.assertTrue(model, key)

class TemperatureTests(unittest.TestCase):
    """temperature 交给用户填，默认 0.1，异常值退回默认。"""

    @classmethod
    def make_dialog(cls):
        from core.config import Settings
        from ui.dialogs import SettingsDialog

        return SettingsDialog(Settings())

    def test_default_is_0_1(self):
        from core.config import Settings

        self.assertAlmostEqual(Settings().ai.temperature, 0.1)

    def test_round_trip(self):
        from core.config import Settings

        settings = Settings()
        settings.ai.temperature = 0.7
        restored = Settings.from_mapping(settings.to_mapping())
        self.assertAlmostEqual(restored.ai.temperature, 0.7)

    def test_zero_allowed(self):
        from core.config import Settings

        data = {"ai": {"temperature": 0}}
        self.assertAlmostEqual(Settings.from_mapping(data).ai.temperature, 0.0)

    def test_out_of_range_falls_back(self):
        from core.config import Settings

        for bad in (-0.5, 2.5, 99):
            data = {"ai": {"temperature": bad}}
            self.assertAlmostEqual(Settings.from_mapping(data).ai.temperature, 0.1,
                                   msg="%r 该退回默认" % bad)

    def test_garbage_falls_back(self):
        from core.config import Settings

        for bad in ("abc", None, True, [], {}):
            data = {"ai": {"temperature": bad}}
            self.assertAlmostEqual(Settings.from_mapping(data).ai.temperature, 0.1)

    def test_string_number_accepted(self):
        from core.config import Settings

        data = {"ai": {"temperature": "0.5"}}
        self.assertAlmostEqual(Settings.from_mapping(data).ai.temperature, 0.5)

    def test_missing_key_uses_default(self):
        from core.config import Settings

        self.assertAlmostEqual(Settings.from_mapping({"ai": {}}).ai.temperature, 0.1)

    def test_request_uses_configured_value(self):
        """请求体里的 temperature 必须是用户填的那个。"""
        import json
        from types import SimpleNamespace
        from unittest import mock

        from core import ai_leftover

        batch = [ai_leftover.Suspect("folder", "C:\\x\\a", "a")]
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

        def fake_urlopen(request, **_kwargs):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        settings = SimpleNamespace(api_key="k", base_url="http://x", model="m",
                                   temperature=0.42)
        with mock.patch.object(ai_leftover.urllib.request, "urlopen", fake_urlopen):
            ai_leftover._ask(batch, ["app"], settings)
        self.assertAlmostEqual(captured["body"]["temperature"], 0.42)

    def test_settings_dialog_has_control(self):
        dialog = self.make_dialog()
        self.assertTrue(hasattr(dialog, "temperature"))
        self.assertAlmostEqual(dialog.temperature.value(), 0.1)
        self.assertLessEqual(dialog.temperature.minimum(), 0.0)
        self.assertGreaterEqual(dialog.temperature.maximum(), 2.0)
        self.assertTrue(dialog.temperature.toolTip(), "要有说明")
        _QT_APP.processEvents()


if __name__ == "__main__":
    unittest.main(verbosity=2)
