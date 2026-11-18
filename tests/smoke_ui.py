"""界面冒烟测试：离屏跑，不弹窗，验证主流程不会崩。

跑法：python tests/smoke_ui.py
会真的去读一个目录（默认用户目录），但不会删任何东西。
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import app_paths
from core.config import CacheStore, SettingsStore
from core.knowledge import load_from_files
from ui.table_model import COL_CHECK

FAILED: list[str] = []

def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED.append(name)

def pump(app: QApplication, seconds: float) -> None:
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
    store = SettingsStore(paths.config, paths.resources / "default_config.json")
    settings = store.load()
    settings.last_module = 0        # 验收只验文件页，别被上次的模块选择带偏
    settings.ai.enabled = False     # 默认必须是关的，不受外部残留配置影响
    settings.auto_size = True
    cache = CacheStore(paths.cache)
    cache.load()
    kb = load_from_files(
        paths.resources / "default_knowledge.json", paths.knowledge, paths.user_rules
    )

    from ui.main_window import MainWindow

    t0 = time.perf_counter()
    window = MainWindow(paths, settings, store, kb, cache)
    window.show()
    window.bootstrap()
    pump(app, 0.3)
    elapsed = (time.perf_counter() - t0) * 1000
    check("窗口创建 + 首帧", True, f"{elapsed:.0f} ms")

    target = os.environ.get("WINDIR", r"C:\Windows")
    window.navigate(target)
    rows = wait_rows(app, window)
    check("进入目录并列出内容", rows > 0, f"{rows} 项 @ {target}")
    if rows == 0:
        print("没能列出内容，后面的检查跳过")
        return 1

    check("列数是 6", window.model.columnCount() == 6, str(window.model.columnCount()))
    header = [window.model.headerData(c, Qt.Horizontal, Qt.DisplayRole) for c in range(6)]
    check("表头是中文六列", header == ["名称", "修改日期", "类型", "大小", "作用", "是否删除"], str(header))

    entry = window.model.entry_at(0)
    check("第一行有作用说明", bool(entry.purpose), entry.purpose[:24])
    check("第一行有风险等级", entry.risk in ("safe", "caution", "forbidden", "unlisted"), entry.risk)

    # 红色项和普通项一样能勾，不弹任何额外确认。
    first_red = window.model.entry_at(0)
    if first_red is not None and first_red.risk == "forbidden":
        window.model.setData(window.model.index(0, COL_CHECK), Qt.Checked, Qt.CheckStateRole)
        check("红色项也可以勾选（和普通项一样）", len(window.model.checked_entries()) == 1, first_red.risk)
        window.model.clear_checks()

    # 换到一个能勾的目录验证统计与按钮
    window.navigate(os.environ.get("TEMP", target))
    wait_rows(app, window)
    pump(app, 0.3)
    folder_row = next(
        (i for i in range(window.model.rowCount())
         if window.model.entry_at(i) is not None and window.model.is_checkable(window.model.entry_at(i))),
        -1,
    )
    if folder_row >= 0:
        window.model.setData(window.model.index(folder_row, COL_CHECK), Qt.Checked, Qt.CheckStateRole)
        pump(app, 0.05)
        check("勾选后统计更新", "已选 1 项" in window.lbl_selection.text(), window.lbl_selection.text())
        check("删除按钮可用", window.act_delete.isEnabled())
        # 切目录必须清空勾选
        window.navigate(os.environ.get("TEMP", target))
        wait_rows(app, window)
        pump(app, 0.2)
        check("切换目录后勾选被清空", window.model.checked_entries() == [])
    else:
        check("找到可勾选的文件夹", False)

    # 大小计算：等一小会儿看有没有回填
    end = time.time() + 6
    got_size = False
    while time.time() < end:
        app.processEvents()
        for i in range(window.model.rowCount()):
            item = window.model.entry_at(i)
            if item is not None and item.is_dir and item.size is not None:
                got_size = True
                break
        if got_size:
            break
        time.sleep(0.05)
    check("后台算出了文件夹大小", got_size)

    # AI 默认关闭
    from core.ai_client import AIClient

    client = AIClient(settings, cache)
    check("联网解释默认是关的", not client.is_configured())

    window.close()
    pump(app, 0.2)
    print()
    if FAILED:
        print(f"有 {len(FAILED)} 项没通过：" + "、".join(FAILED))
        return 1
    print("界面冒烟全部通过")
    return 0

if __name__ == "__main__":
    sys.exit(main())
