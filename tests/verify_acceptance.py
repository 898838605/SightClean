"""验收自检脚本：只做检查，不修改任何东西。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import app_paths
from core.config import CacheStore, SettingsStore
from core.knowledge import load_from_files
from ui.table_model import COL_CHECK

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  " + detail) if detail else ""))


def pump(app, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_rows(app, window, timeout: float = 8.0) -> int:
    end = time.time() + timeout
    while time.time() < end:
        if window.model.rowCount() > 0:
            return window.model.rowCount()
        app.processEvents()
        time.sleep(0.02)
    return window.model.rowCount()


def main() -> int:
    app = QApplication(sys.argv)
    paths = app_paths.resolve()
    # 用临时配置：测试会改设置，别写进项目根的 config.json 里
    _tmp = Path(tempfile.mkdtemp(prefix="dch-acc-"))
    store = SettingsStore(_tmp / "config.json", paths.resources / "default_config.json")
    settings = store.load()
    settings.last_module = 0        # 验收只验文件页，别被上次的模块选择带偏
    settings.auto_size = True
    cache = CacheStore(paths.cache)
    cache.load()
    kb = load_from_files(paths.resources / "default_knowledge.json", paths.knowledge, paths.user_rules)

    from ui.main_window import MainWindow

    window = MainWindow(paths, settings, store, kb, cache)
    window.show()
    window.bootstrap()
    pump(app, 0.3)

    # ---------- 1. 红色目录可进入、可勾选（风险只做提示，不做拦截） ----------
    window.navigate(r"C:\Windows")
    wait_rows(app, window)
    pump(app, 0.4)
    model = window.model
    red_rows = [
        index for index in range(model.rowCount())
        if model.entry_at(index) is not None
        and model.entry_at(index).risk == "forbidden"
        and model.entry_at(index).is_dir
    ]
    check("C:\\Windows 下有红色文件夹", bool(red_rows), f"{len(red_rows)} 个")
    if red_rows:
        row = red_rows[0]
        entry = model.entry_at(row)
        flags = model.flags(model.index(row, COL_CHECK))
        check("红色项可以勾选", bool(flags & Qt.ItemIsUserCheckable), entry.name)
        check("红色项可被选中（能点、能右键）", bool(flags & Qt.ItemIsSelectable), entry.name)
        model.setData(model.index(row, COL_CHECK), Qt.Checked, Qt.CheckStateRole)
        check("勾上之后确实进了待删清单",
              any(e.human_path == entry.human_path for e in model.checked_entries()))
        model.clear_checks()
        # 双击进入
        before = window._current_dir
        window._on_double_click(model.index(row, 0))
        wait_rows(app, window, 6)
        pump(app, 0.3)
        check("红色文件夹双击能进去",
              os.path.normcase(window._current_dir) != os.path.normcase(before),
              f"{before} -> {window._current_dir}")
    window.navigate(r"C:\Windows")
    wait_rows(app, window)

    # ---------- 2. 切换目录清空勾选 ----------
    model.clear_checks()
    target = next((i for i in range(model.rowCount()) if model.is_checkable(model.entry_at(i))), -1)
    if target >= 0:
        model.set_checked(model.entry_at(target).human_path, True)
        pump(app, 0.05)
        check("勾上之后有计数", "已选 1 项" in window.lbl_selection.text(), window.lbl_selection.text())
        window.navigate(os.environ.get("TEMP", r"C:\Windows"))
        wait_rows(app, window)
        pump(app, 0.3)
        check("切换目录后勾选清空", model.checked_entries() == [])
        check("状态栏回到 0 项", "已选 0 项" in window.lbl_selection.text(), window.lbl_selection.text())
    else:
        check("找到可勾选项（跳过）", False)

    # ---------- 6. 切换目录会取消旧的大小计算 ----------
    calls = []
    real_cancel = window.size_engine.cancel
    window.size_engine.cancel = lambda: (calls.append(1), real_cancel())[1]
    window.navigate(r"C:\Windows")
    wait_rows(app, window)
    pump(app, 0.3)
    before = len(calls)
    window.navigate(r"C:\Windows\System32")
    wait_rows(app, window)
    pump(app, 0.3)
    after = len(calls)
    check("切目录调用了取消", after > before, f"cancel 次数 {before} -> {after}")
    window.size_engine.cancel = real_cancel

    window.close()
    pump(app, 0.2)

    print()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"通过 {len(RESULTS) - len(failed)} / {len(RESULTS)}")
    if failed:
        print("没通过：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
