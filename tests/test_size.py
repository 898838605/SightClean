"""文件夹大小计算的单测：正确性、链接防绕圈、深度上限、取消、线程池端到端。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_paths
from core.size_calc import CancelFlag, MAX_DEPTH, SizeEngine, measure


def make_tree(root: Path, layout: dict[str, int]) -> int:
    """造一棵树并返回总字节数。键是相对路径，值是文件大小。"""
    total = 0
    for relative, size in layout.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(app_paths.to_long(str(target)), "wb") as handle:
            handle.write(b"x" * size)
        total += size
    return total


class MeasureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dch-size-"))

    def tearDown(self):
        shutil.rmtree(app_paths.to_long(str(self.tmp)), ignore_errors=True)

    def test_flat(self):
        expected = make_tree(self.tmp, {"a.bin": 100, "b.bin": 200})
        result = measure(str(self.tmp), CancelFlag(1))
        self.assertEqual(result.size, expected)
        self.assertEqual(result.state, "ok")

    def test_nested(self):
        expected = make_tree(self.tmp, {
            "a.bin": 10, "sub/b.bin": 20, "sub/deep/c.bin": 30, "sub/deep/again/d.bin": 40,
        })
        result = measure(str(self.tmp), CancelFlag(1))
        self.assertEqual(result.size, expected)
        self.assertEqual(result.state, "ok")

    def test_junction_is_not_followed(self):
        target = self.tmp / "target"
        target.mkdir()
        make_tree(target, {"big.bin": 5000})
        holder = self.tmp / "holder"
        holder.mkdir()
        make_tree(holder, {"small.bin": 10})
        link = holder / "link"
        try:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           check=True, capture_output=True)
        except Exception:
            self.skipTest("这台机器建不了 junction")
        result = measure(str(holder), CancelFlag(1))
        # 只算 small.bin，绝不把 5000 字节的目标目录算进来
        self.assertEqual(result.size, 10, "junction 被跟进去了")
        self.assertGreaterEqual(result.skipped, 1)
        self.assertEqual(result.state, "partial")

    def test_depth_limit(self):
        deep = self.tmp / "deep"
        os.makedirs(app_paths.to_long(str(deep)), exist_ok=True)
        current = deep
        for index in range(MAX_DEPTH + 6):
            current = current / f"l{index}"
            os.makedirs(app_paths.to_long(str(current)), exist_ok=True)
            with open(app_paths.to_long(str(current / "f.bin")), "wb") as handle:
                handle.write(b"x" * 1)
        result = measure(str(deep), CancelFlag(1))
        self.assertEqual(result.state, "partial", "超深度应该被标成 partial")
        self.assertIsNotNone(result.size)
        self.assertGreaterEqual(result.skipped, 1)

    def test_cancel_is_immediate(self):
        make_tree(self.tmp, {"a.bin": 10})
        flag = CancelFlag(1)
        flag.cancel()
        result = measure(str(self.tmp), flag)
        self.assertEqual(result.state, "cancelled")

    def test_denied_root(self):
        missing = self.tmp / "根本不存在"
        result = measure(str(missing), CancelFlag(1))
        self.assertEqual(result.state, "denied")
        self.assertIsNone(result.size)
        self.assertTrue(result.reason)


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication

        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dch-engine-"))
        self.results = []
        self.done = []
        self.engine = SizeEngine(max_concurrency=2)
        self.engine.sizeReady.connect(lambda *args: self.results.append(args))
        self.engine.batchDone.connect(lambda gen: self.done.append(gen))
        self.engine.start()

    def tearDown(self):
        self.engine.stop()
        shutil.rmtree(app_paths.to_long(str(self.tmp)), ignore_errors=True)

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def test_end_to_end(self):
        first = self.tmp / "one"
        second = self.tmp / "two"
        first.mkdir()
        second.mkdir()
        one = make_tree(first, {"a.bin": 111})
        two = make_tree(second, {"b.bin": 222, "sub/c.bin": 333})
        self.engine.enqueue([str(first), str(second)], 7)
        end = time.time() + 10
        while time.time() < end and not self.done:
            self.app.processEvents()
            time.sleep(0.02)
        sizes = {path: size for _gen, path, size, _state in self.results}
        self.assertEqual(sizes.get(str(first)), one)
        self.assertEqual(sizes.get(str(second)), two)
        self.assertEqual(self.done, [7])
        self.assertTrue(all(state == "ok" for _g, _p, _s, state in self.results))

    def test_cancel_drops_results(self):
        # 挑一整棵 Windows 目录：正常要扫几秒，取消一定发生在中途
        big = os.environ.get("WINDIR", r"C:\Windows")
        self.engine.enqueue([big], 8)
        self.pump(0.3)
        self.engine.cancel()
        self.pump(1.5)
        leaked = [item for item in self.results if item[0] == 8]
        self.assertEqual(leaked, [], "取消之后不该再有结果回来")

    def test_new_batch_supersedes_old(self):
        first = self.tmp / "a"
        first.mkdir()
        make_tree(first, {"x.bin": 5})
        self.engine.enqueue([str(first)], 11)
        self.engine.enqueue([str(first)], 12)      # 新一批直接顶掉旧的
        end = time.time() + 8
        while time.time() < end and 12 not in self.done:
            self.app.processEvents()
            time.sleep(0.02)
        self.assertIn(12, self.done)


if __name__ == "__main__":
    unittest.main(verbosity=2)
