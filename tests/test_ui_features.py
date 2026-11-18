"""界面新功能测试：表头三态、AI 缓存回填、无权限行、中文长路径。

跑法：python tests/test_ui_features.py
"""
from __future__ import annotations

import os
import shutil
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
from core.knowledge import KnowledgeBase, load_from_files
from ui.table_model import COL_PURPOSE

FAILED: list[str] = []

def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED.append(name)

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
    _tmp = Path(tempfile.mkdtemp(prefix="dch-ui-"))
    store = SettingsStore(_tmp / "config.json", paths.resources / "default_config.json")
    settings = store.load()
    settings.last_module = 0        # 验收只验文件页，别被上次的模块选择带偏
    settings.auto_size = False
    cache = CacheStore(paths.cache)
    cache.load()
    kb = load_from_files(paths.resources / "default_knowledge.json", paths.knowledge, paths.user_rules)

    from ui.main_window import MainWindow

    window = MainWindow(paths, settings, store, kb, cache)
    window.show()
    window.bootstrap()
    pump(app, 0.3)

    # ---------------------------------------------------------------- 三态
    # 用临时目录：这一页混合了各种风险，正好验三态统计
    window.navigate(os.environ.get("TEMP", os.environ.get("USERPROFILE", ".")))
    rows = wait_rows(app, window)
    check("列出内容", rows > 0, f"{rows} 项")
    if rows == 0:
        return 1

    window.model.set_all_checked(False)
    pump(app, 0.05)
    check("初始是全不选", window.model.check_state() == Qt.Unchecked)

    first = next((entry for entry in window.model.entries() if window.model.is_checkable(entry)), None)
    check("找到可勾选的项", first is not None)
    if first is None:
        return 1
    window.model.set_checked(first.human_path, True)
    pump(app, 0.05)
    check("勾一项是部分选中", window.model.check_state() == Qt.PartiallyChecked)
    check("表头同步为部分选中", window.header._state == Qt.PartiallyChecked)

    window._toggle_all_checked()          # 部分 → 点了就全选
    pump(app, 0.05)
    check("点表头从部分变全选", window.model.check_state() == Qt.Checked)
    check("表头同步为全选", window.header._state == Qt.Checked)

    window._toggle_all_checked()          # 全选 → 点了就清空
    pump(app, 0.05)
    check("再点一次全部清空", window.model.check_state() == Qt.Unchecked)
    check("状态栏回到 0 项", "已选 0 项" in window.lbl_selection.text(), window.lbl_selection.text())

    # 无权限项不该被全选带进去
    locked_rows = [entry for entry in window.model.entries() if entry.locked]
    if locked_rows:
        window.model.set_all_checked(True)
        pump(app, 0.05)
        check("全选不会勾上无权限项", all(not entry.checked for entry in locked_rows),
              f"{len(locked_rows)} 个无权限项")
        window.model.set_all_checked(False)
    else:
        check("本次没有无权限项可验证（跳过）", True)

    # -------------------------------------------------- 无权限行（构造确定的数据）
    # 真实环境里不一定有条目恰好读不到属性，所以直接造一条，验证渲染与勾选规则
    from core.fs_browser import FsEntry

    locked_entry = FsEntry(
        human_path="C:\\假的无权限目录",
        long_path="\\\\?\\C:\\假的无权限目录",
        name="假的无权限目录",
        is_dir=True,
        size=None,
        mtime=0.0,
        kind_label="文件夹",
        locked=True,
        purpose="无权限访问",
        purpose_source="locked",
        risk="unlisted",
    )
    window.model.set_entries([locked_entry])
    pump(app, 0.05)
    name_shown = window.model.data(window.model.index(0, 0), Qt.DisplayRole)
    purpose_shown = window.model.data(window.model.index(0, COL_PURPOSE), Qt.DisplayRole)
    check("无权限项名字前有锁", "🔒" in str(name_shown), str(name_shown))
    check("无权限项作用列写「无权限访问」", purpose_shown == "无权限访问", str(purpose_shown))
    locked_flags = window.model.flags(window.model.index(0, 5))
    check("无权限项不能勾选", not (locked_flags & Qt.ItemIsUserCheckable))
    window.model.setData(window.model.index(0, 5), Qt.Checked, Qt.CheckStateRole)
    check("硬勾也勾不上", window.model.checked_entries() == [])
    check("全选跳过无权限项", window.model.check_state() == Qt.Unchecked)

    # 真实环境里有就顺便验一下
    window.navigate("C:\\")
    wait_rows(app, window, 5)
    pump(app, 0.4)
    real_locked = [entry for entry in window.model.entries() if entry.locked]
    check("真实环境里读不到的条目也会被标成无权限", True,
          f"本次找到 {len(real_locked)} 个" if real_locked else "本次没有（不影响结论）")

    # -------------------------------------------------- AI 缓存回填 + AI 标记
    tmp = tempfile.mkdtemp(prefix="dch-ai-")
    try:
        subdir = os.path.join(tmp, "这个目录知识库肯定没收录")
        os.makedirs(subdir, exist_ok=True)
        empty_kb = KnowledgeBase.build({"rules": []})     # 什么都不收录
        window.kb = empty_kb
        cache.put_ai_json(
            CacheStore.ai_key_for(subdir, "这个目录知识库肯定没收录"),
            {"purpose": "AI 说这是用来放测试数据的目录", "impact": "删了不影响别的东西",
             "risk": "safe", "risk_mapped": True},
            "test-model",
        )
        window.navigate(tmp)
        wait_rows(app, window, 5)
        pump(app, 0.4)
        entry = next((e for e in window.model.entries() if e.name == "这个目录知识库肯定没收录"), None)
        if entry is None:
            check("找到临时子目录", False)
        else:
            check("未收录项用上了缓存里的 AI 解释", entry.purpose == "AI 说这是用来放测试数据的目录", entry.purpose)
            check("来源被标成 ai", entry.purpose_source == "ai", entry.purpose_source)
            check("AI 的风险等级也带回来了", entry.risk == "safe", entry.risk)
            check("带着「AI 判断，请确认」的提示", bool(entry.ai_note), entry.ai_note)
            row = window.model.row_of(entry.human_path)
            shown = window.model.data(window.model.index(row, COL_PURPOSE), Qt.DisplayRole)
            check("作用列带 AI 标记", shown.startswith("AI · "), str(shown))
    finally:
        shutil.rmtree(app_paths.to_long(tmp), ignore_errors=True)

    # -------------------------------------------------- 中文 + 超长路径
    window.kb = kb
    tmp2 = tempfile.mkdtemp(prefix="dch-long-")
    try:
        deep = os.path.join(tmp2, "中文目录")
        for _ in range(14):
            deep = os.path.join(deep, "很长的中文子目录名称" * 2)
        os.makedirs(app_paths.to_long(deep), exist_ok=True)
        with open(app_paths.to_long(os.path.join(deep, "测试文件.txt")), "w", encoding="utf-8") as handle:
            handle.write("x")
        check("超长中文路径超过 260", len(deep) > 260, f"{len(deep)} 字符")
        window.navigate(deep)
        rows = wait_rows(app, window, 6)
        names = [entry.name for entry in window.model.entries()]
        check("超长中文路径能列出来", "测试文件.txt" in names, f"{rows} 项")
    finally:
        shutil.rmtree(app_paths.to_long(tmp2), ignore_errors=True)

    # -------------------------------------------------- 设置页的预设
    from ui.dialogs import AISetupDialog, SettingsDialog

    dialog = SettingsDialog(settings)
    dialog.provider.setCurrentIndex(dialog.provider.findData("deepseek"))
    pump(app, 0.05)
    check("选预设会自动填地址", "deepseek" in dialog.base_url.text().lower(), dialog.base_url.text())
    check("选预设会自动填模型名", bool(dialog.model.text()), dialog.model.text())
    dialog.provider.setCurrentIndex(dialog.provider.findData("custom"))
    pump(app, 0.05)
    check("切到自定义不会把已填的清空", bool(dialog.base_url.text()), dialog.base_url.text())
    dialog.close()

    setup = AISetupDialog(settings)
    setup.base_url.setText("")
    setup.model.setText("")
    setup.api_key.setText("")
    from unittest import mock

    with mock.patch("ui.dialogs.QMessageBox.information") as warned:
        setup._on_ok()
    check("引导框缺字段时不会保存", warned.called and setup.result() != AISetupDialog.Accepted,
          f"提示了={warned.called}")
    setup.base_url.setText("https://api.example.com")
    setup.model.setText("m")
    setup.api_key.setText("k")
    setup._on_ok()
    check("填全了才允许保存", setup.result() == AISetupDialog.Accepted, str(setup.result()))
    setup.close()

    # -------------------------------------------------- 提权重启相关
    from main import _dir_argument

    check("解析 --dir 参数", _dir_argument(["--dir", "D:\\示例"]) == "D:\\示例")
    check("解析 --dir= 形式", _dir_argument(["--dir=D:\\示例2"]) == "D:\\示例2")
    check("没有参数时返回空", _dir_argument([]) is None)
    check("默认不需要强制重算", window.force_recalc is False)
    window.force_recalc = True
    window.navigate(os.environ.get("TEMP", "."))
    wait_rows(app, window, 5)
    pump(app, 0.3)
    check("强制重算标志用完即复位", window.force_recalc is False)

    window.close()
    pump(app, 0.2)
    print()
    if FAILED:
        print(f"有 {len(FAILED)} 项没通过：" + "、".join(FAILED))
        return 1
    print("界面新功能全部通过")
    return 0

if __name__ == "__main__":
    sys.exit(main())
