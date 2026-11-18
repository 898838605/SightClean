"""截图工具：把主界面和确认对话框渲染成 png，方便验收外观。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import app_paths
from core.config import CacheStore, SettingsStore
from core.delete import DeleteItem
from core.knowledge import load_from_files


def pump(app, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    app = QApplication(sys.argv)
    paths = app_paths.resolve()
    # 用临时配置：测试会改设置，别写进项目根的 config.json 里
    _tmp = Path(tempfile.mkdtemp(prefix="dch-shot-"))
    store = SettingsStore(_tmp / "config.json", paths.resources / "default_config.json")
    settings = store.load()
    settings.last_module = 0        # 验收只验文件页，别被上次的模块选择带偏
    settings.auto_size = True
    cache = CacheStore(paths.cache)
    cache.load()
    kb = load_from_files(paths.resources / "default_knowledge.json", paths.knowledge, paths.user_rules)

    from ui.confirm_flow import ConfirmDialog
    from ui.dialogs import AISetupDialog, SettingsDialog
    from ui.main_window import MainWindow

    window = MainWindow(paths, settings, store, kb, cache)
    window.resize(1200, 760)
    window.show()
    window.bootstrap()
    pump(app, 0.5)
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
    os.makedirs(out, exist_ok=True)

    window.navigate(os.environ.get("WINDIR", r"C:\Windows"))
    pump(app, 1.5)
    window.grab().save(os.path.join(out, "screenshot-main.png"))

    # 用户目录：一片「未收录」，正好看清灰色和黄色的区别
    window.navigate(os.environ.get("USERPROFILE", r"C:\Users"))
    pump(app, 2.0)
    window.grab().save(os.path.join(out, "screenshot-unlisted.png"))

    # 勾几行，看统计与按钮状态
    from ui.table_model import COL_CHECK

    for row in (0, 3, 5):
        if row < window.model.rowCount():
            window.model.setData(window.model.index(row, COL_CHECK), Qt.Checked, Qt.CheckStateRole)
    pump(app, 0.3)
    window.grab().save(os.path.join(out, "screenshot-checked.png"))

    items = [
        DeleteItem(human_path=r"C:\Users\X\.ssh\id_rsa", display="id_rsa", is_dir=False,
                   risk="forbidden", size=1679,
                   effect="远程登录服务器用的密钥，删了没有备份就再也连不上那台服务器。"),
        DeleteItem(human_path=r"D:\项目\源码备份", display="源码备份", is_dir=True,
                   risk="forbidden", size=13314398618,
                   effect="这个项目的版本历史，删了整个项目只剩当前状态。"),
        DeleteItem(human_path=r"C:\Users\X\AppData\Local\Temp\a.tmp", display="a.tmp", is_dir=False,
                   risk="safe", size=2048,
                   effect="程序运行时临时写下的东西，用完就没用了。"),
        DeleteItem(human_path=r"C:\Users\X\Downloads\安装包.exe", display="安装包.exe", is_dir=False,
                   risk="caution", size=104857600,
                   effect="浏览器默认的下载目录，你自己下载的东西都在这里。"),
        DeleteItem(human_path=r"C:\platform-tools", display="platform-tools", is_dir=True,
                   risk="unlisted", size=816664576,
                   effect="未收录，建议确认后再删"),
    ]
    dialog = ConfirmDialog(items, "彻底删除", "下面每一项都可以单独取消。动手之前会先备份。",
                          irreversible=True,
                          modes=(("移入回收站（之后还能还原）", "recycle"),
                                 ("彻底删除（回收站里也不会有）", "permanent")))
    dialog._mode_group.buttons()[1].setChecked(True)
    dialog.confirm_input.setText("确认")
    dialog.show()
    pump(app, 0.5)
    dialog.grab().save(os.path.join(out, "screenshot-confirm.png"))
    dialog.close()

    from ui.nav import PAGE_SOFTWARE

    window._switch_module(PAGE_SOFTWARE)
    pump(app, 2.0)
    window.grab().save(os.path.join(out, "screenshot-software.png"))

    from ui.nav import PAGE_REGISTRY

    window._switch_module(PAGE_REGISTRY)
    pump(app, 0.4)
    window.registry_page.navigate(r"HKEY_LOCAL_MACHINE\SOFTWARE")
    pump(app, 0.8)
    window.grab().save(os.path.join(out, "screenshot-registry.png"))

    from ui.nav import PAGE_PROCESS

    window._switch_module(PAGE_PROCESS)
    pump(app, 2.8)          # CPU 采样本身要 1.5 秒左右
    window.grab().save(os.path.join(out, "screenshot-process.png"))

    window._switch_module(0)
    pump(app, 0.3)

    settings_dialog = SettingsDialog(settings, window)
    settings_dialog.provider.setCurrentIndex(settings_dialog.provider.findData("deepseek"))
    settings_dialog.show()
    pump(app, 0.4)
    settings_dialog.grab().save(os.path.join(out, "screenshot-settings.png"))
    settings_dialog.close()

    setup_dialog = AISetupDialog(settings, window)
    setup_dialog.show()
    pump(app, 0.4)
    setup_dialog.grab().save(os.path.join(out, "screenshot-ai-setup.png"))
    setup_dialog.close()

    window.close()
    pump(app, 0.2)
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
