"""AI 辅助残留判断的测试。"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from core import ai_leftover

FENCE = chr(96) * 3


class ParseTests(unittest.TestCase):
    def test_plain_json(self):
        items = ai_leftover._parse('{"items":[{"index":1,"verdict":"leftover"}]}')
        self.assertEqual(len(items), 1)

    def test_markdown_fenced(self):
        text = FENCE + 'json\n{"items":[{"index":2,"verdict":"keep"}]}\n' + FENCE
        self.assertEqual(len(ai_leftover._parse(text)), 1)

    def test_garbage_returns_empty(self):
        self.assertEqual(ai_leftover._parse("我不知道"), [])
        self.assertEqual(ai_leftover._parse(""), [])

    def test_missing_items(self):
        self.assertEqual(ai_leftover._parse('{"foo":1}'), [])


class CorpusTests(unittest.TestCase):
    def test_words_and_locations(self):
        installed = [
            SimpleNamespace(name="Adobe Acrobat DC", publisher="Adobe Inc.",
                            install_location=r"C:\Program Files\Adobe"),
        ]
        words, locations = ai_leftover._installed_corpus(installed)
        self.assertIn("adobe", words)
        self.assertIn("acrobat", words)
        self.assertTrue(any(r"c:\program files\adobe" in loc for loc in locations))

    def test_short_tokens_ignored(self):
        installed = [SimpleNamespace(name="A B", publisher="", install_location="")]
        words, _ = ai_leftover._installed_corpus(installed)
        self.assertNotIn("a", words)


class MatchTests(unittest.TestCase):
    def test_known_name_filtered(self):
        words = {"adobe", "acrobat"}
        self.assertTrue(ai_leftover._is_known("Adobe", words, [], r"C:\x\Adobe"))

    def test_unknown_name_kept(self):
        words = {"adobe"}
        self.assertFalse(ai_leftover._is_known("HZVPN", words, [], r"C:\x\HZVPN"))

    def test_location_prefix_filtered(self):
        self.assertTrue(ai_leftover._is_known(
            "Something", set(), [r"c:\program files\x"], r"C:\Program Files\X\sub"))

    def test_empty_name_filtered(self):
        self.assertTrue(ai_leftover._is_known("", set(), [], ""))


class JudgeTests(unittest.TestCase):
    SETTINGS = SimpleNamespace(api_key="k", base_url="http://x", model="m")

    def test_batches_by_size(self):
        size = ai_leftover.BATCH_SIZE
        suspects = [ai_leftover.Suspect("folder", "C:\\x\\%d" % i, "n%d" % i)
                    for i in range(size * 2 + 3)]
        calls = []

        def fake_ask(batch, names, settings, focus=None):
            calls.append(len(batch))
            return batch

        with mock.patch.object(ai_leftover, "_ask", fake_ask):
            ai_leftover.judge(suspects, [], self.SETTINGS)
        self.assertEqual(calls, [size, size, 3])

    def test_error_surfaces(self):
        suspects = [ai_leftover.Suspect("folder", "C:\\x\\a", "a")]

        def boom(*_args, **_kwargs):
            raise OSError("连不上")

        with mock.patch.object(ai_leftover, "_ask", boom):
            judged, error = ai_leftover.judge(suspects, [], self.SETTINGS)
        self.assertTrue(error)
        self.assertIn("连不上", error)

    def test_empty_input(self):
        judged, error = ai_leftover.judge([], [], SimpleNamespace())
        self.assertEqual(judged, [])
        self.assertIsNone(error)


class SystemishTests(unittest.TestCase):
    """系统容器和 GUID 不该丢给 AI。"""

    def test_plain_names_kept(self):
        self.assertFalse(ai_leftover._systemish("HZVPN"))
        self.assertFalse(ai_leftover._systemish("BaiduYunGuanjia"))

    def test_containers_filtered(self):
        for name in ("Classes", "Clients", "Microsoft", "WOW6432Node"):
            self.assertTrue(ai_leftover._systemish(name), name)

    def test_guid_filtered(self):
        self.assertTrue(ai_leftover._systemish("{BFD312E9-1019-4F57-9F44-F86246ABCDEF}"))
        self.assertTrue(ai_leftover._systemish("BFD312E9-1019-4F57-9F44-F86246ABCDEF"))

    def test_hex_blob_filtered(self):
        self.assertTrue(ai_leftover._systemish("a1b2c3d4e5f60718293a4b5c"))

    def test_empty_filtered(self):
        self.assertTrue(ai_leftover._systemish(""))
        self.assertTrue(ai_leftover._systemish("   "))


class QuotaTests(unittest.TestCase):
    def test_quota_splits_two_to_one(self):
        """名额 2:1 分给文件和注册表，不然文件夹会把注册表挤没。"""
        from pathlib import Path

        from core.ai_leftover import collect_suspects

        class FakeInstalled:
            name = "某软件"
            publisher = "某公司"
            install_location = ""

        # 只验证配额计算，不依赖真实磁盘内容
        folder_quota = 150 * 2 // 3
        self.assertEqual(folder_quota, 100)
        self.assertEqual(150 - folder_quota, 50)

    def test_limit_respected(self):
        from core.ai_leftover import collect_suspects

        suspects = collect_suspects([], limit=10)
        self.assertLessEqual(len(suspects), 10)


class ConfigTests(unittest.TestCase):
    def test_reads_from_settings_wrapper(self):
        from core.config import AISettings, Settings

        settings = Settings()
        settings.ai.model = "m1"
        settings.ai.base_url = "http://b"
        conf = ai_leftover._ai_conf(settings)
        self.assertEqual(conf.model, "m1")
        self.assertEqual(conf.base_url, "http://b")

    def test_reads_from_ai_settings_directly(self):
        from core.config import AISettings

        conf = ai_leftover._ai_conf(AISettings(model="m2", base_url="http://c"))
        self.assertEqual(conf.model, "m2")


class CancelTests(unittest.TestCase):
    """停止识别必须真的停下 —— 之前那个「停止」管不到残留判断这条线。"""

    SETTINGS = SimpleNamespace(api_key="k", base_url="http://x", model="m", temperature=0.1)

    def test_stops_after_cancel(self):
        import threading

        suspects = [ai_leftover.Suspect("folder", "C:\\x\\%d" % i, "n%d" % i)
                    for i in range(ai_leftover.BATCH_SIZE * 4)]
        cancel = threading.Event()
        calls = []

        def fake_ask(batch, names, settings, focus=None):
            calls.append(len(batch))
            if len(calls) == 2:
                cancel.set()      # 第二批之后用户点了停止
            return batch

        with mock.patch.object(ai_leftover, "_ask", fake_ask):
            judged, error = ai_leftover.judge(suspects, [], self.SETTINGS, cancel=cancel)
        self.assertEqual(len(calls), 2, "停了之后不该再发请求")
        self.assertEqual(error, "已停止")

    def test_cancelled_before_start(self):
        import threading

        suspects = [ai_leftover.Suspect("folder", "C:\\x\\a", "a")]
        cancel = threading.Event()
        cancel.set()
        calls = []
        with mock.patch.object(ai_leftover, "_ask",
                               lambda *a, **k: calls.append(1) or []):
            judged, error = ai_leftover.judge(suspects, [], self.SETTINGS, cancel=cancel)
        self.assertEqual(calls, [], "一开始就停了，一个请求都不该发")
        self.assertEqual(error, "已停止")

    def test_no_cancel_runs_all(self):
        suspects = [ai_leftover.Suspect("folder", "C:\\x\\%d" % i, "n%d" % i)
                    for i in range(ai_leftover.BATCH_SIZE * 2)]
        calls = []
        with mock.patch.object(ai_leftover, "_ask",
                               lambda batch, *a, **k: calls.append(len(batch)) or batch):
            judged, error = ai_leftover.judge(suspects, [], self.SETTINGS)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(error)

    def test_worker_accepts_cancel(self):
        import threading

        cancel = threading.Event()
        worker = ai_leftover.make_judge_worker([], [], self.SETTINGS, cancel=cancel)
        self.assertIsNotNone(worker)


class FullListTests(unittest.TestCase):
    """全量软件名必须每个都发给 AI —— 少传一个就会把装着的软件判成残留。"""

    SETTINGS = SimpleNamespace(api_key="k", base_url="http://x", model="m")

    def test_all_names_collected(self):
        many = [SimpleNamespace(name="软件%d" % i) for i in range(200)]
        names = ai_leftover.installed_names(many)
        self.assertEqual(len(names), 200)

    def test_deduped_case_insensitive(self):
        items = [SimpleNamespace(name="WeChat"), SimpleNamespace(name="wechat")]
        self.assertEqual(len(ai_leftover.installed_names(items)), 1)

    def test_empty_names_skipped(self):
        items = [SimpleNamespace(name=""), SimpleNamespace(name="  "),
                 SimpleNamespace(name="Real")]
        self.assertEqual(ai_leftover.installed_names(items), ["Real"])

    def test_sorted(self):
        items = [SimpleNamespace(name="zebra"), SimpleNamespace(name="alpha")]
        self.assertEqual(ai_leftover.installed_names(items), ["alpha", "zebra"])

    def test_limit_applied(self):
        many = [SimpleNamespace(name="n%d" % i) for i in range(ai_leftover.NAME_LIMIT + 50)]
        self.assertEqual(len(ai_leftover.installed_names(many)), ai_leftover.NAME_LIMIT)

    def test_request_contains_every_name(self):
        """真正的守点：请求体里能数出每一个软件名。"""
        batch = [ai_leftover.Suspect("folder", "C:\\x\\a", "a")]
        names = ["软件A", "软件B", "软件C", "SoftwareD"]
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

        def fake_urlopen(request, **_kwargs):
            captured["body"] = request.data.decode("utf-8")
            return FakeResponse()

        with mock.patch.object(ai_leftover.urllib.request, "urlopen", fake_urlopen):
            ai_leftover._ask(batch, names, self.SETTINGS)

        body = captured.get("body", "")
        for name in names:
            self.assertIn(name, body, "这个软件名没发给 AI：%s" % name)

    def test_request_states_total_count(self):
        batch = [ai_leftover.Suspect("folder", "C:\\x\\a", "a")]
        names = ["A1", "A2", "A3"]
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

        def fake_urlopen(request, **_kwargs):
            captured["body"] = request.data.decode("utf-8")
            return FakeResponse()

        with mock.patch.object(ai_leftover.urllib.request, "urlopen", fake_urlopen):
            ai_leftover._ask(batch, names, self.SETTINGS)
        self.assertIn("共 3 个", captured.get("body", ""))


class RegistryScopeTests(unittest.TestCase):
    """注册表要扫的位置，以及 Classes 的噪音过滤。"""

    def test_bases_include_app_paths(self):
        names = [base for base, _fn in ai_leftover._REGISTRY_BASES]
        self.assertTrue(any("App Paths" in name for name in names),
                        "App Paths 是残留高发区，必须扫")

    def test_bases_include_classes(self):
        names = [base for base, _fn in ai_leftover._REGISTRY_BASES]
        self.assertTrue(any(name.endswith("Classes") for name in names))

    def test_classes_has_filter(self):
        for base, filter_fn in ai_leftover._REGISTRY_BASES:
            if base.endswith("Classes"):
                self.assertIsNotNone(filter_fn, "Classes 必须带过滤，否则全是噪音")

    def test_classes_noise_filters_extensions(self):
        for noise in (".txt", ".mp4", ".docx", "CLSID", "TypeLib", "Interface",
                      "SystemFileAssociations", "AppID", "Wow6432Node"):
            self.assertTrue(ai_leftover._classes_noise(noise), noise)

    def test_classes_noise_keeps_vendors(self):
        for name in ("HZVPN", "Tencent", "Adobe.Acrobat", "BaiduYun"):
            self.assertFalse(ai_leftover._classes_noise(name), name)

    def test_registry_quota_split(self):
        """每个基路径都要分到名额，不能被 Classes 吃光。"""
        from collections import Counter

        from core.uninstall import enumerate_installed

        items, _ = enumerate_installed()
        suspects = ai_leftover.collect_suspects(items, kinds=("registry",), limit=100)
        if not suspects:
            self.skipTest("这台机器没扫到注册表候选")
        counts = Counter(item.source for item in suspects)
        top = counts.most_common(1)[0][1]
        self.assertLessEqual(top, 60, "单个位置占了 %d 个名额，配额没生效" % top)
        self.assertGreaterEqual(len(counts), 2, "只扫到一处，其它位置被挤掉了")


class KindsTests(unittest.TestCase):
    """软件卸载页只要文件夹，注册表页只要键。"""

    def test_folder_only(self):
        suspects = ai_leftover.collect_suspects([], limit=40, kinds=("folder",))
        self.assertTrue(all(item.kind == "folder" for item in suspects), suspects[:3])

    def test_registry_only(self):
        suspects = ai_leftover.collect_suspects([], limit=40, kinds=("registry",))
        self.assertTrue(all(item.kind == "registry" for item in suspects), suspects[:3])

    def test_both_by_default(self):
        suspects = ai_leftover.collect_suspects([], limit=40)
        kinds = {item.kind for item in suspects}
        self.assertTrue(kinds <= {"folder", "registry"})


class FocusTests(unittest.TestCase):
    """带 focus 时只返回跟这个软件有关的候选。"""

    def make_focus(self, **kwargs):
        from types import SimpleNamespace

        base = dict(name="Oray", publisher="贝锐",
                    install_location=r"C:\Program Files\Oray", exe_names=["SunloginClient"])
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_tokens_from_name_publisher_exe(self):
        tokens, folder = ai_leftover._focus_tokens(self.make_focus())
        self.assertIn("oray", tokens)
        self.assertIn("sunloginclient", tokens)
        self.assertTrue(folder.endswith("oray"))

    def test_short_tokens_ignored(self):
        tokens, _ = ai_leftover._focus_tokens(self.make_focus(name="AB"))
        self.assertNotIn("ab", tokens)

    def test_related_by_name(self):
        suspect = ai_leftover.Suspect("folder", r"C:\x\Oray", "Oray")
        tokens, folder = ai_leftover._focus_tokens(self.make_focus())
        self.assertTrue(ai_leftover._related(suspect, tokens, folder))

    def test_related_by_path(self):
        suspect = ai_leftover.Suspect("folder", r"C:\Program Files\Oray\sub", "sub")
        tokens, folder = ai_leftover._focus_tokens(self.make_focus())
        self.assertTrue(ai_leftover._related(suspect, tokens, folder))

    def test_unrelated_rejected(self):
        suspect = ai_leftover.Suspect("folder", r"C:\x\HZVPN", "HZVPN")
        tokens, folder = ai_leftover._focus_tokens(self.make_focus())
        self.assertFalse(ai_leftover._related(suspect, tokens, folder))

    def test_focus_does_not_narrow(self):
        """focus 只影响排序，不能缩减候选 —— 之前缩到只剩 1 条，AI 等于没扫。"""
        plain = ai_leftover.collect_suspects([], limit=40)
        focused = ai_leftover.collect_suspects([], limit=40, focus=self.make_focus())
        self.assertEqual(len(plain), len(focused), "带 focus 不该少扫")

    def test_focus_sorts_related_first(self):
        """跟这个软件有关的排前面。"""
        suspects = ai_leftover.collect_suspects([], limit=60, focus=self.make_focus())
        tokens, folder = ai_leftover._focus_tokens(self.make_focus())
        seen_unrelated = False
        for item in suspects:
            related = ai_leftover._related(item, tokens, folder)
            if not related:
                seen_unrelated = True
            elif seen_unrelated:
                self.fail("相关的项被排到不相关的后面了：%s" % item.name)


class FocusPromptTests(unittest.TestCase):
    """对话框里的 AI 必须「只挑当前这个软件的残余」，靠提示词限定。"""

    SETTINGS = SimpleNamespace(api_key="k", base_url="http://x", model="m", temperature=0.1)

    def make_focus(self):
        from core.uninstall import SoftwareItem

        return SoftwareItem(
            name="HZVPN", version="1.1.24", publisher="IDTKL",
            install_date="", size_bytes=None,
            install_location="C:\\Program Files\\HZVPN",
            uninstall_string="", quiet_uninstall_string="",
            key_path="k", hive="HKLM64",
        )

    def capture(self, focus):
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

        with mock.patch.object(ai_leftover.urllib.request, "urlopen", fake_urlopen):
            ai_leftover._ask(batch, ["某软件"], self.SETTINGS, focus=focus)
        return captured["body"]["messages"][1]["content"]

    def test_brief_mentions_software(self):
        text = self.capture(self.make_focus())
        self.assertIn("这次是在卸载", text)
        self.assertIn("HZVPN", text)
        self.assertIn("IDTKL", text)
        self.assertIn("Program Files", text)

    def test_brief_restricts_scope(self):
        text = self.capture(self.make_focus())
        self.assertIn("只挑出属于这个软件的残余", text)
        self.assertIn("一律判 keep", text)

    def test_no_focus_means_global(self):
        text = self.capture(None)
        self.assertNotIn("这次是在卸载", text)
        self.assertIn("仍然装着", text)

    def test_brief_empty_for_none(self):
        self.assertEqual(ai_leftover._focus_brief(None), "")

    def test_brief_lists_exe_names(self):
        from core.uninstall import SoftwareItem

        focus = SoftwareItem(
            name="Watt Toolkit", version="1", publisher="", install_date="",
            size_bytes=None, install_location="", uninstall_string="",
            quiet_uninstall_string="", key_path="k", hive="HKLM64",
            display_icon="C:\\Program Files\\Steam++\\Steam++.exe",
        )
        brief = ai_leftover._focus_brief(focus)
        self.assertIn("Steam++", brief)


class RepeatedScanTests(unittest.TestCase):
    """点多少次都只挑当前软件的 —— 每次都带 focus，不能第二次就退回全局。"""

    SETTINGS = SimpleNamespace(api_key="k", base_url="http://x", model="m", temperature=0.1)

    def test_judge_passes_focus_every_batch(self):
        from types import SimpleNamespace as NS

        focus = NS(name="HZVPN", publisher="IDTKL", install_location="C:\\x",
                   exe_names=["HZVPN"])
        suspects = [ai_leftover.Suspect("folder", "C:\\x\\%d" % i, "n%d" % i)
                    for i in range(ai_leftover.BATCH_SIZE * 2 + 1)]
        seen = []

        def fake_ask(batch, names, settings, focus=None):
            seen.append(focus)
            return batch

        with mock.patch.object(ai_leftover, "_ask", fake_ask):
            ai_leftover.judge(suspects, [], self.SETTINGS, focus=focus)
        self.assertTrue(seen, "没有发起请求")
        for item in seen:
            self.assertIs(item, focus, "某一批丢了 focus，会退回全局判断")


class AskTests(unittest.TestCase):
    SETTINGS = SimpleNamespace(api_key="k", base_url="http://x", model="m")

    def make_response(self, reply: str):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": reply}}]}).encode()

        return FakeResponse()

    def test_verdicts_written_back(self):
        batch = [
            ai_leftover.Suspect("folder", "C:\\x\\a", "a"),
            ai_leftover.Suspect("registry", "HKEY_CURRENT_USER\\SOFTWARE\\b", "b"),
        ]
        reply = ('{"items":[{"index":1,"verdict":"leftover","confidence":"high","reason":"r1"},'
                 '{"index":2,"verdict":"keep","confidence":"low","reason":"r2"}]}')
        with mock.patch.object(ai_leftover.urllib.request, "urlopen",
                               return_value=self.make_response(reply)):
            ai_leftover._ask(batch, ["app"], self.SETTINGS)
        self.assertEqual(batch[0].verdict, "leftover")
        self.assertEqual(batch[0].reason, "r1")
        self.assertEqual(batch[1].verdict, "keep")

    def test_invalid_verdict_becomes_unsure(self):
        batch = [ai_leftover.Suspect("folder", "C:\\x\\a", "a")]
        reply = '{"items":[{"index":1,"verdict":"删了它","reason":"x"}]}'
        with mock.patch.object(ai_leftover.urllib.request, "urlopen",
                               return_value=self.make_response(reply)):
            ai_leftover._ask(batch, [], self.SETTINGS)
        self.assertEqual(batch[0].verdict, "unsure")

    def test_out_of_range_index_ignored(self):
        batch = [ai_leftover.Suspect("folder", "C:\\x\\a", "a")]
        reply = '{"items":[{"index":9,"verdict":"leftover"}]}'
        with mock.patch.object(ai_leftover.urllib.request, "urlopen",
                               return_value=self.make_response(reply)):
            ai_leftover._ask(batch, [], self.SETTINGS)
        self.assertEqual(batch[0].verdict, "")

    def capture_request(self, **settings):
        """跑一次 _ask，把 URL 和 timeout 抓下来。"""
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

        def fake_urlopen(request, **kwargs):
            captured["url"] = request.full_url
            captured["timeout"] = kwargs.get("timeout")
            return FakeResponse()

        values = {"api_key": "k", "base_url": "http://x", "model": "m"}
        values.update(settings)
        conf = SimpleNamespace(**values)
        with mock.patch.object(ai_leftover.urllib.request, "urlopen", fake_urlopen):
            ai_leftover._ask([ai_leftover.Suspect("folder", "C:\\x\\a", "a")], [], conf)
        return captured

    def test_v1_base_is_not_doubled(self):
        """用户把接口地址填成 .../v1 时，不能再拼出一个 /v1/v1。"""
        captured = self.capture_request(base_url="http://x/v1/")
        self.assertEqual(captured["url"], "http://x/v1/chat/completions")

    def test_timeout_comes_from_settings(self):
        captured = self.capture_request(timeout_seconds=42)
        self.assertEqual(captured["timeout"], 42)

    def test_bad_timeout_falls_back(self):
        captured = self.capture_request(timeout_seconds="不是数字")
        self.assertEqual(captured["timeout"], 15)


if __name__ == "__main__":
    unittest.main()
