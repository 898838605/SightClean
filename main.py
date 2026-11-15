"""SightClean —— 入口。

启动顺序刻意这样安排：
1. 先把窗口画出来（用户立刻看见界面）；
2. 再做盘符枚举、知识库匹配这些活（QTimer.singleShot(0) 推到事件循环里）。
任何配置/知识库解析失败都只影响那一块，窗口照常起来。
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

import app_paths
from core.config import CacheStore, SettingsStore, ensure_first_run_files
from core.knowledge import load_from_files

APP_TITLE = "SightClean"
_STARTED_AT = time.perf_counter()

def _log_startup(stage: str) -> None:
    """启动耗时写进 logs/启动耗时.log，方便验收「2 秒内起来」这条硬要求。"""
    try:
        path = app_paths.resolve().logs / "启动耗时.log"
        elapsed = (time.perf_counter() - _STARTED_AT) * 1000
        line = time.strftime("%Y-%m-%d %H:%M:%S") + f"  {stage}: {elapsed:.0f} ms\n"
        with open(app_paths.to_long(path), "a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:  # noqa: BLE001
        pass

def _dir_argument(argv: list[str] | None = None) -> str | None:
    """读取 --dir 参数：以管理员身份重启后，新进程靠它回到原来的目录。"""
    args = list(sys.argv[1:] if argv is None else argv)
    for index, arg in enumerate(args):
        if arg == "--dir" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--dir="):
            return arg[len("--dir="):]
    return None

def _module_argument(argv: list[str] | None = None) -> int | None:
    """读取 --module 参数：提权重启后回到刚才那个模块。"""
    args = list(sys.argv[1:] if argv is None else argv)
    for index, arg in enumerate(args):
        value = None
        if arg == "--module" and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("--module="):
            value = arg[len("--module="):]
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return None
    return None

def _install_excepthook() -> None:
    """没接住的意外只弹一次，记到 logs 里，程序不退出。"""
    state = {"shown": False}

    def hook(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            path = app_paths.resolve().logs / f"错误-{time.strftime('%Y%m%d-%H%M%S')}.log"
            with open(app_paths.to_long(path), "w", encoding="utf-8") as handle:
                handle.write(text)
        except Exception:  # noqa: BLE001
            pass
        if state["shown"]:
            return
        state["shown"] = True
        try:
            QMessageBox.critical(
                None,
                "出了点小状况",
                "刚才那一下没成功，但程序没有坏，你可以继续用。\n"
                "具体的记录已经放在程序旁边的 logs 文件夹里了。",
            )
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook

def build_settings_and_knowledge():
    paths = app_paths.resolve()
    notes = ensure_first_run_files(
        paths.resources / "default_config.json",
        paths.resources / "default_knowledge.json",
        paths.config,
        paths.knowledge,
        paths.resources / "default_junk.json",
        paths.junk,
    )
    store = SettingsStore(paths.config, paths.resources / "default_config.json")
    settings = store.load()
    cache = CacheStore(paths.cache)
    cache.load()
    knowledge = load_from_files(
        paths.resources / "default_knowledge.json",
        paths.knowledge,
        paths.user_rules,
        settings.user_rules,
    )
    warnings: list[str] = []
    if not paths.portable:
        warnings.append("程序所在的文件夹不能写东西，设置改放到用户目录了")
    warnings.extend(settings.warnings)
    warnings.extend(knowledge.warnings)
    warnings.extend(cache.warnings)
    return paths, store, settings, cache, knowledge, warnings, notes

def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName(APP_TITLE)
    app.setOrganizationName("SightClean")
    # 全局图标：主窗口和所有子弹窗都会自动继承，不用一个个设
    icon_path = Path(app_paths.resource_dir()) / "app.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    font = QFont("Microsoft YaHei UI")
    if not font.exactMatch():
        font = QFont("Microsoft YaHei")
    font.setPointSize(9)
    app.setFont(font)
    _install_excepthook()

    try:
        from ui.main_window import MainWindow

        paths, store, settings, cache, knowledge, warnings, notes = build_settings_and_knowledge()
        window = MainWindow(paths, settings, store, knowledge, cache)
        start_dir = _dir_argument()
        if start_dir:
            # 提权重启带过来的目录：先回到这儿，并跳过大小缓存重算一遍
            settings.last_dir = start_dir
            window.force_recalc = True
        start_module = _module_argument()
        if start_module is not None:
            # 提权重启带过来的模块：回到那一页，并且重新读一遍（🔒 可能已经能打开了）
            settings.last_module = start_module
            window.force_recalc = True
    except Exception:  # noqa: BLE001 - 启动阶段的错必须让人看见并说清楚
        detail = traceback.format_exc()
        try:
            path = app_paths.resolve().logs / f"启动失败-{time.strftime('%Y%m%d-%H%M%S')}.log"
            with open(app_paths.to_long(path), "w", encoding="utf-8") as handle:
                handle.write(detail)
        except Exception:  # noqa: BLE001
            pass
        QMessageBox.critical(
            None,
            APP_TITLE,
            "程序没能启动起来。\n\n"
            "如果它旁边有 logs 文件夹，里面记了具体原因；\n"
            "把那个文件发给做这个工具的人就能查。",
        )
        return 1

    window.show()
    _log_startup("窗口已显示")

    def _after_show() -> None:
        try:
            window.bootstrap()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        _log_startup("首屏就绪（盘符与目录已异步加载）")
        window.show_startup_notes(warnings, notes)

    QTimer.singleShot(0, _after_show)
    return app.exec()

if __name__ == "__main__":
    sys.exit(main())
