"""主题与字号：即时生效、持久化、默认跟随系统。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from core.risk import COLORS, Risk
from ui import theme

ROOT = Path(__file__).resolve().parent.parent

class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

class DetectTests(unittest.TestCase):
    def test_system_returns_light_or_dark(self):
        self.assertIn(theme.detect_system(), ("light", "dark"))

    def test_unreadable_falls_back_to_light(self):
        with mock.patch("winreg.OpenKey", side_effect=OSError("no")):
            self.assertEqual(theme.detect_system(), "light")

    def test_resolve_system(self):
        with mock.patch.object(theme, "detect_system", return_value="dark"):
            self.assertEqual(theme.resolve("system"), "dark")
        self.assertEqual(theme.resolve("light"), "light")
        self.assertEqual(theme.resolve("dark"), "dark")
        self.assertEqual(theme.resolve(""), theme.resolve("system"))
        self.assertEqual(theme.resolve("乱写"), theme.resolve("system"))

class ApplyTests(Base):
    def tearDown(self):
        theme.apply(self.app, "light", "medium")

    def test_qss_differs_by_theme(self):
        light = theme.build_qss("light", "medium")
        dark = theme.build_qss("dark", "medium")
        self.assertNotEqual(light, dark)
        self.assertIn(theme.LIGHT["bg"], light)
        self.assertIn(theme.DARK["bg"], dark)

    def test_apply_sets_stylesheet(self):
        theme.apply(self.app, "dark", "medium")
        self.assertIn(theme.DARK["bg"], self.app.styleSheet())
        self.assertEqual(theme.current(), "dark")

    def test_scale_changes_font(self):
        theme.apply(self.app, "light", "small")
        self.assertEqual(self.app.font().pointSize(), theme.SCALES["small"])
        theme.apply(self.app, "light", "large")
        self.assertEqual(self.app.font().pointSize(), theme.SCALES["large"])

    def test_scale_changes_qss(self):
        self.assertNotEqual(theme.build_qss("light", "small"), theme.build_qss("light", "large"))

    def test_risk_colors_follow_theme(self):
        theme.apply(self.app, "light", "medium")
        light_danger = COLORS[Risk.FORBIDDEN]
        theme.apply(self.app, "dark", "medium")
        dark_danger = COLORS[Risk.FORBIDDEN]
        self.assertNotEqual(light_danger, dark_danger)
        self.assertEqual(light_danger, theme.LIGHT["danger"])
        self.assertEqual(dark_danger, theme.DARK["danger"])

    def test_all_four_levels_mapped(self):
        theme.apply(self.app, "dark", "medium")
        for level in (Risk.SAFE, Risk.CAUTION, Risk.FORBIDDEN, Risk.UNLISTED):
            self.assertTrue(COLORS[level].startswith("#"))

class PersistenceTests(unittest.TestCase):
    def test_round_trip(self):
        from core.config import SettingsStore

        tmp = Path(tempfile.mkdtemp(prefix="th-"))
        path = tmp / "config.json"
        store = SettingsStore(path, ROOT / "resources" / "default_config.json")
        settings = store.load()
        self.assertEqual(settings.theme, "system")
        self.assertEqual(settings.font_scale, "medium")

        settings.theme = "dark"
        settings.font_scale = "large"
        store.save(settings)

        again = SettingsStore(path, ROOT / "resources" / "default_config.json").load()
        self.assertEqual(again.theme, "dark")
        self.assertEqual(again.font_scale, "large")
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["theme"], "dark")

    def test_old_config_without_fields(self):
        from core.config import SettingsStore

        tmp = Path(tempfile.mkdtemp(prefix="th2-"))
        old = json.loads((ROOT / "resources" / "default_config.json").read_text(encoding="utf-8"))
        old.pop("theme", None)
        old.pop("font_scale", None)
        path = tmp / "config.json"
        path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        settings = SettingsStore(path, ROOT / "resources" / "default_config.json").load()
        self.assertEqual(settings.theme, "system", "缺字段时按「跟随系统」")
        self.assertEqual(settings.font_scale, "medium")

    def test_default_config_has_fields(self):
        doc = json.loads((ROOT / "resources" / "default_config.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["theme"], "system")
        self.assertEqual(doc["font_scale"], "medium")

if __name__ == "__main__":
    unittest.main(verbosity=2)
