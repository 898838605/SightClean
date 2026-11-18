"""四模块容器测试：切页、工具栏收放、状态栏、清理、清空 AI 缓存。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

import app_paths
from core.config import CacheStore, SettingsStore
from core.knowledge import load_from_files
from ui.nav import PAGE_FILES, PAGE_PROCESS, PAGE_REGISTRY, PAGE_SOFTWARE

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

def main() -> int:
    app = QApplication(sys.argv)
    paths = app_paths.resolve()
    # 用临时配置，别把测试里改过的设置写进项目根的 config.json（会串味到冒烟测试）
    import tempfile

    temp_dir = Path(tempfile.mkdtemp(prefix="dch-mod-"))
    store = SettingsStore(temp_dir / "config.json", paths.resources / "default_config.json")
    settings = store.load()
    settings.last_module = 0
    settings.auto_size = False
    cache = CacheStore(paths.cache)
    cache.load()
    kb = load_from_files(paths.resources / "default_knowledge.json", paths.knowledge, paths.user_rules)

    from ui.main_window import MainWindow

    window = MainWindow(paths, settings, store, kb, cache)
    window.show()
    window.bootstrap()
    pump(app, 0.4)

    # ---------- 容器结构 ----------
    check("右侧是四页堆栈", window.stack.count() == 4, str(window.stack.count()))
    check("默认停在文件页", window._current_module == PAGE_FILES)
    check("文件页就是原来的分栏", window.stack.widget(PAGE_FILES) is window.files_view)
    check("左侧盘符栏已移除", not hasattr(window, "drive_list"),
          "整页都留给表格了")
    check("盘符数据仍在（给面包屑下拉用）", len(window._drives) > 0, f"{len(window._drives)} 个盘")
    check("面包屑接了盘符下拉", window.breadcrumb._roots_provider is not None)

    # ---------- 工具栏收放 ----------
    check("文件页有返回按钮", window.act_back.isVisible())
    check("文件页有算大小", window.act_size.isVisible())

    window._switch_module(PAGE_SOFTWARE)
    pump(app, 0.1)
    check("切到软件页", window.stack.currentIndex() == PAGE_SOFTWARE)
    check("返回按钮收起", not window.act_back.isVisible())
    check("上一级收起", not window.act_up.isVisible())
    check("算大小收起", not window.act_size.isVisible())
    check("刷新仍在", window.act_refresh.isVisible())
    check("AI 识别仍在", window.act_ai_page.isVisible())
    check("设置在", window.act_settings.isVisible())
    check("工具栏不再有「以管理员身份重新启动」", not hasattr(window, "act_admin"),
          "默认就是管理员，这个按钮去掉了")
    toolbar_texts = [a.text() for a in window.toolbar.actions() if a.text()]
    check("工具栏不再有「怎么用」", not any("怎么用" in t for t in toolbar_texts),
          "帮助挪到菜单里了")
    check("有「AI 扫描卸载残余文件夹」按钮", hasattr(window, "act_global_clean"),
          "软件卸载页的 AI 文件夹扫描入口")
    check("按钮文案说清是 AI 在扫",
          "AI" in window.act_global_clean.text() and "残余" in window.act_global_clean.text(),
          window.act_global_clean.text())
    check("提示只有一句话",
          "\n" not in window.act_global_clean.toolTip(),
          repr(window.act_global_clean.toolTip()[:40]))
    check("有「让 AI 找注册表残留」按钮", hasattr(window, "act_ai_registry"),
          "注册表清理页的 AI 入口")
    check("有「清理注册表」按钮", hasattr(window, "act_dead_registry"),
          "按规则清理失效项")
    window._apply_module_ui(PAGE_REGISTRY)
    _reg_bar = [a.text() for a in window.toolbar.actions() if a.text() and a.isVisible()]
    check("清理注册表在注册表页可见",
          any("清理注册表" in t and "AI" not in t for t in _reg_bar),
          " | ".join(_reg_bar))

    window._apply_module_ui(PAGE_FILES)
    check("文件页两个都隐藏",
          not window.act_global_clean.isVisible() and not window.act_ai_registry.isVisible())
    window._apply_module_ui(PAGE_SOFTWARE)
    check("软件页只显示文件夹扫描",
          window.act_global_clean.isVisible() and not window.act_ai_registry.isVisible())
    window._apply_module_ui(PAGE_REGISTRY)
    check("注册表页只显示注册表 AI",
          not window.act_global_clean.isVisible() and window.act_ai_registry.isVisible())
    check("有「一键清理 C 盘垃圾」按钮", hasattr(window, "act_junk"),
          "文件页的批量清理入口")
    check("有「一键清理进程」按钮", hasattr(window, "act_clean_process"),
          "进程页的可疑进程清理入口")
    for _mod, _name in ((PAGE_FILES, "文件"), (PAGE_PROCESS, "进程"),
                        (PAGE_REGISTRY, "注册表")):
        window._apply_module_ui(_mod)
        check("清理进程按钮在%s页%s" % (_name, "可见" if _mod == PAGE_PROCESS else "隐藏"),
              window.act_clean_process.isVisible() == (_mod == PAGE_PROCESS))
    window._apply_module_ui(PAGE_SOFTWARE)
    for _mod, _name in ((PAGE_FILES, "文件"), (PAGE_SOFTWARE, "软件"),
                        (PAGE_REGISTRY, "注册表")):
        window._apply_module_ui(_mod)
        check("垃圾清理按钮在%s页%s" % (_name, "可见" if _mod == PAGE_FILES else "隐藏"),
              window.act_junk.isVisible() == (_mod == PAGE_FILES))
    window._apply_module_ui(PAGE_SOFTWARE)

    # 帮助菜单里的两个外链
    _help = None
    for _act in window.menuBar().actions():
        if _act.text() == "帮助":
            _help = _act.menu()
            break
    check("有「帮助」菜单", _help is not None)

    if _help is not None:
        _texts = [a.text() for a in _help.actions() if not a.isSeparator()]
        check("帮助里有「项目地址」", any("项目地址" in t for t in _texts), " | ".join(_texts))
        check("帮助里有「教程」", any("教程" in t for t in _texts), " | ".join(_texts))

        from ui.main_window import GITHUB_URL, TUTORIAL_URL
        check("项目地址指向 GitHub",
              GITHUB_URL == "https://github.com/898838605/SightClean", GITHUB_URL)
        check("教程指向微信文章",
              TUTORIAL_URL.startswith("https://mp.weixin.qq.com/s/"), TUTORIAL_URL)

        from unittest import mock as _m

        with _m.patch("ui.main_window.QDesktopServices.openUrl", return_value=True) as _open:
            window._open_github()
            _called = _open.call_args[0][0].toString() if _open.called else ""
            check("点「项目地址」打开的是 GitHub", _called == GITHUB_URL, _called)

        with _m.patch("ui.main_window.QDesktopServices.openUrl", return_value=True) as _open:
            window._open_tutorial()
            _called = _open.call_args[0][0].toString() if _open.called else ""
            check("点「教程」打开的是文章", _called == TUTORIAL_URL, _called)

    # 设置要放在菜单栏（工具和帮助之间），工具栏里不该再有
    _menu_titles = [a.text() for a in window.menuBar().actions()]
    check("菜单栏有「设置」", any("设置" in t for t in _menu_titles), " | ".join(_menu_titles))
    if any("设置" in t for t in _menu_titles):
        _idx = next(i for i, t in enumerate(_menu_titles) if "设置" in t)
        check("设置夹在工具和帮助之间",
              _menu_titles[_idx - 1] == "工具" and _menu_titles[_idx + 1] == "帮助",
              "%s / %s" % (_menu_titles[_idx - 1], _menu_titles[_idx + 1]))
    for _mod, _name in ((PAGE_FILES, "文件"), (PAGE_SOFTWARE, "软件"),
                        (PAGE_REGISTRY, "注册表"), (PAGE_PROCESS, "进程")):
        window._apply_module_ui(_mod)
        _bar = [a.text() for a in window.toolbar.actions() if a.text()]
        check("%s模块工具栏没有设置按钮" % _name,
              not any("设置" in t for t in _bar))
    window._apply_module_ui(PAGE_SOFTWARE)

    # AI 找注册表残留：不该要求用户先去软件页刷新
    window._installed_cache = (0.0, [])
    installed = window._installed_for_ai()
    check("AI 能自己拿到已装软件", len(installed) > 0, "%d 个" % len(installed))
    import time as _t
    start = _t.time()
    again = window._installed_for_ai()
    check("第二次走缓存", _t.time() - start < 0.05, "%.3f 秒" % (_t.time() - start))
    check("缓存结果一致", len(again) == len(installed))

    from unittest import mock as _mock

    with _mock.patch("core.ai_leftover.collect_suspects", return_value=[]), \
         _mock.patch("PySide6.QtWidgets.QMessageBox.information") as _info:
        window.scan_registry_leftovers()
        texts = [str(c) for c in _info.call_args_list]
        check("不再提示「先把软件列表读出来」",
              not any("先把软件列表" in t for t in texts))

    # 名称列要能拖动调整 —— Stretch 模式拖不动，用户抱怨过
    from PySide6.QtWidgets import QHeaderView as _QHeader
    for label, table in (("文件", window.table),
                         ("软件", window.software_page.table),
                         ("注册表", window.registry_page.table),
                         ("进程", window.process_page.table)):
        mode = table.horizontalHeader().sectionResizeMode(0)
        check("%s页名称列可拖动" % label, mode == _QHeader.ResizeMode.Interactive,
              "当前模式 %s" % mode.name)
        table.setColumnWidth(0, 420)
        check("%s页名称列宽度能改" % label,
              table.horizontalHeader().sectionSize(0) == 420)

    # 停止按钮要跟着 AI 忙闲走，四个模块都一样
    window._on_ai_busy(True)
    check("AI 忙时停止按钮可用", window.act_ai_stop.isEnabled())
    check("AI 忙时识别按钮禁用", not window.act_ai_page.isEnabled())
    window._on_ai_busy(False)
    check("AI 闲时停止按钮禁用", not window.act_ai_stop.isEnabled())
    check("AI 闲时识别按钮可用", window.act_ai_page.isEnabled())

    for mod, name in ((PAGE_FILES, "文件"), (PAGE_SOFTWARE, "软件"),
                      (PAGE_REGISTRY, "注册表"), (PAGE_PROCESS, "进程")):
        window._apply_module_ui(mod)
        check("停止按钮在%s模块可见" % name, window.act_ai_stop.isVisible())
    window._apply_module_ui(PAGE_SOFTWARE)

    check("有「扫描位置」按钮", hasattr(window, "act_scan_scopes"),
          "让用户勾要扫哪些地方")

    # 真的走一遍「选位置」的三种结局，防止属性名写错这种运行时才炸的问题
    from unittest import mock as _mock

    from PySide6.QtWidgets import QDialog as _QDialog

    before_scopes = list(window.settings.scan_scopes)
    with _mock.patch("ui.scan_scope_dialog.ScanScopeDialog.exec",
                     return_value=_QDialog.DialogCode.Rejected):
        window.choose_scan_scopes()
    check("取消不改设置", window.settings.scan_scopes == before_scopes)

    with _mock.patch("ui.scan_scope_dialog.ScanScopeDialog.exec",
                     return_value=_QDialog.DialogCode.Accepted), \
         _mock.patch("ui.scan_scope_dialog.ScanScopeDialog.chosen",
                     return_value=["appdata", "packages"]):
        window.choose_scan_scopes()
    check("确认后保存", window.settings.scan_scopes == ["appdata", "packages"],
          str(window.settings.scan_scopes))

    with _mock.patch("ui.scan_scope_dialog.ScanScopeDialog.exec",
                     return_value=_QDialog.DialogCode.Accepted), \
         _mock.patch("ui.scan_scope_dialog.ScanScopeDialog.chosen", return_value=[]), \
         _mock.patch("PySide6.QtWidgets.QMessageBox.information") as _info:
        window.choose_scan_scopes()
    check("一个都不选时给提示", _info.called)

    window._apply_module_ui(PAGE_SOFTWARE)      # 后面的检查按软件模块来
    check("执行按钮改成卸载", "卸载" in window.act_delete.text(), window.act_delete.text())
    check("状态栏说已选 0 项", window.lbl_selection.text() == "已选 0 项", window.lbl_selection.text())

    window._switch_module(PAGE_REGISTRY)
    pump(app, 0.1)
    check("注册表页执行按钮", "清理" in window.act_delete.text(), window.act_delete.text())

    window._switch_module(PAGE_PROCESS)
    pump(app, 0.1)
    check("进程页执行按钮", "结束" in window.act_delete.text(), window.act_delete.text())

    window._switch_module(PAGE_FILES)
    pump(app, 0.1)
    check("切回文件页按钮回来", window.act_back.isVisible())
    check("执行按钮改回删除", "删除" in window.act_delete.text(), window.act_delete.text())

    # ---------- 切页清理：勾选 / 后台任务 ----------
    window.navigate(os.environ.get("WINDIR", r"C:\Windows"))
    end = time.time() + 6
    while time.time() < end and window.model.rowCount() == 0:
        app.processEvents()
        time.sleep(0.02)
    if window.model.rowCount() > 0:
        row = next((i for i in range(window.model.rowCount())
                    if window.model.is_checkable(window.model.entry_at(i))), 0)
        from unittest import mock

        window.model.set_checked(window.model.entry_at(row).human_path, True)
        pump(app, 0.05)
        check("文件页有勾选", len(window.model.checked_entries()) == 1)

        cancelled = []
        real_cancel = window.size_engine.cancel
        window.size_engine.cancel = lambda: (cancelled.append(1), real_cancel())[1]
        window._switch_module(PAGE_SOFTWARE)
        pump(app, 0.1)
        check("切走就清空勾选", window.model.checked_entries() == [])
        check("切走就停掉大小计算", bool(cancelled), f"cancel 调了 {len(cancelled)} 次")
        window.size_engine.cancel = real_cancel

    # ---------- 导航联动 ----------
    window.nav._list.setCurrentRow(PAGE_PROCESS)
    pump(app, 0.15)
    check("点导航能切页", window._current_module == PAGE_PROCESS, str(window._current_module))
    window._switch_module(PAGE_FILES)
    pump(app, 0.1)

    # ---------- 设置页：清空 AI 缓存 ----------
    from ui.dialogs import SettingsDialog

    cache.put_ai_json("v1|ain|test", {"purpose": "假解释", "impact": "", "risk": "safe"}, "m")
    check("缓存里有东西", cache.get_ai("v1|ain|test") is not None)
    dialog = SettingsDialog(settings, window, cache=cache)
    with mock.patch("ui.dialogs.QMessageBox.question", return_value=__import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.Yes), \
         mock.patch("ui.dialogs.QMessageBox.information"):
        dialog._clear_ai_cache()
    check("清空按钮真的清掉了", cache.get_ai("v1|ain|test") is None)
    dialog.close()

    # ---------- 盘根点「上一级」= 磁盘列表 ----------
    window._switch_module(PAGE_FILES)
    pump(app, 0.2)
    root = window._drives[0].root if window._drives else "C:\\"
    window.navigate(root)
    pump(app, 0.8)
    check("先进到盘根", os.path.normcase(window._current_dir) == os.path.normcase(root),
          window._current_dir)

    window._go_up()
    pump(app, 0.4)
    check("盘根再往上到磁盘列表", window._current_dir == "", repr(window._current_dir))
    check("磁盘列表有内容", window.model.rowCount() > 0, f"{window.model.rowCount()} 个盘")
    check("状态栏说共几个盘", "个盘" in window.lbl_status.text(), window.lbl_status.text())

    drive_rows = [window.model.entry_at(i) for i in range(window.model.rowCount())]
    check("盘符条目不可删", all(not row.deletable for row in drive_rows),
          str([row.deletable for row in drive_rows]))
    from PySide6.QtCore import Qt as _Qt

    flags = window.model.flags(window.model.index(0, 5))
    check("盘符勾选框点不动", not (flags & _Qt.ItemIsUserCheckable))
    window.model.set_all_checked(True)
    check("全选不会勾上盘符", window.model.checked_entries() == [])
    window.model.clear_checks()

    if drive_rows:
        window._on_double_click(window.model.index(0, 0))
        pump(app, 0.8)
        check("双击盘符能进去", os.path.normcase(window._current_dir) == os.path.normcase(root),
              window._current_dir)

    # ---------- 切过去就该有数据，不用再点刷新 ----------
    window._switch_module(PAGE_SOFTWARE)
    pump(app, 0.6)
    check("切到软件页直接有列表", window.software_page.model.rowCount() > 0,
          f"{window.software_page.model.rowCount()} 个软件")

    window._switch_module(PAGE_REGISTRY)
    pump(app, 0.6)
    check("切到注册表页直接有列表", window.registry_page.model.rowCount() > 0,
          f"{window.registry_page.model.rowCount()} 个根")

    # ---------- 四个模块点「识别本页未知项」都要先弹填密钥引导 ----------
    from unittest import mock

    for module, name in ((PAGE_SOFTWARE, "软件卸载"), (PAGE_REGISTRY, "注册表清理"),
                         (PAGE_PROCESS, "进程管理"), (PAGE_FILES, "文件清理")):
        window._switch_module(module)
        pump(app, 0.25)
        with mock.patch.object(window, "_prompt_ai_setup", return_value=False) as prompt:
            window._explain_current_page()
        check(f"{name}点识别会先弹填密钥", prompt.called)

    # 填好之后就该真的往下走，而不是又弹一次
    # 注意：引导框在真实流程里会把 settings 改好，所以这里也要把配置真的写上，
    # 否则主窗口认为「配好了」、页面却仍觉得「没配」，两边会对不上。
    window._switch_module(PAGE_SOFTWARE)
    pump(app, 0.4)
    window.settings.ai.enabled = True
    window.settings.ai.base_url = "https://example.com"
    window.settings.ai.model = "test-model"
    window.settings.ai.api_key = "test-key"
    page = window.software_page
    page.ai = window.ai
    check("页面自己也认得「已配置」", page.ai.is_configured())
    with mock.patch.object(window, "_prompt_ai_setup", return_value=True), \
         mock.patch.object(window.ai, "explain_many", return_value=0) as batch, \
         mock.patch("PySide6.QtWidgets.QMessageBox.question",
                    return_value=__import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.Yes):
        window._explain_current_page()
    check("配置好了就直接开始识别、不再弹引导", batch.called)

    # ---------- 记住模块 ----------
    window._switch_module(PAGE_REGISTRY)
    pump(app, 0.1)
    check("记住当前模块", settings.last_module == PAGE_REGISTRY, str(settings.last_module))

    window.close()
    pump(app, 0.2)
    print()
    if FAILED:
        print(f"有 {len(FAILED)} 项没通过：" + "、".join(FAILED))
        return 1
    print("模块容器全部通过")
    return 0

if __name__ == "__main__":
    sys.exit(main())
