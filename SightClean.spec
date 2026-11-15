# -*- mode: python ; coding: utf-8 -*-
"""SightClean · PyInstaller 打包描述。

为什么要手写 spec 而不是纯命令行：
PySide6 的官方 hook 会把整个 Qt 目录的 DLL 一起收进来，--exclude-module 只能挡住
Python 层，挡不住 DLL。这里在 Analysis 之后手动把用不到的 Qt 组件剔掉：
Quick / Qml / Pdf / Network / OpenGL / Svg / 多媒体 / 定位……本程序一个都不用。
剔掉之后解包体积小一大截，冷启动能省下几百毫秒，被杀软扫描的文件也少。
"""
import sys

BLOCK_CIPHER = None

# 名字里带这些片段的二进制一律不要（小写比较）
DROPPED = (
    "qt6quick", "qt6qml", "qt6qmlmodels", "qt6qmlworkerscript", "qt6quickcontrols2",
    "qt6quicktemplates2", "qt6quickwidgets", "qt6pdf", "qt6network", "qt6opengl",
    "qt6svg", "qt6virtualkeyboard", "qt6multimedia", "qt6websockets", "qt6webchannel",
    "qt6webengine", "qt6nfc", "qt6bluetooth", "qt6positioning", "qt6serialport",
    "qt6designer", "qt6test", "qt6charts", "qt6datavisualization", "qt6remoteobjects",
    "qt6sensors", "qt6texttospeech", "qt6sql", "qt6dbus", "qt6help",
    # 注意：libcrypto-1_1.dll / libssl-1_1.dll 虽然也是 QtNetwork 的依赖，
    # 但 Python 自己的 _ssl 模块同样需要它们，删了 ai_client 一 import 就崩，绝不能剔。
    "qtnetwork.pyd", "qtqml.pyd", "qtquick.pyd", "qtpdf.pyd", "qtsql.pyd",
    "qtsvg.pyd", "qtmultimedia.pyd", "qtwebengine", "qt3d",
    "qml\\", "qml\\plugins", "translations\\qtwebengine",
    # 软件 OpenGL 回退库（19.7MB）。本程序是 QWidget 应用，走的是 raster 渲染，
    # 只有显式要求 software OpenGL 时才会加载它，剔掉能明显缩短解包时间。
    "opengl32sw",
    # 平台插件只留 qwindows；direct2d / minimal / offscreen 都是别的场景用的
    "platforms\\qdirect2d", "platforms\\qminimal", "platforms\\qoffscreen",
    # 图片解码插件：只留程序图标要用的。
    # 注意 iconengines\qico.dll 是 Qt 读 .ico 的必需品 ——
    # 之前整个目录被剔掉，QApplication.setWindowIcon() 静默失败，
    # 窗口标题栏就退回成默认图标了。
    "imageformats\\qjpeg", "imageformats\\qgif", "imageformats\\qtiff",
    # qpng 留着：resources/app.png 也要能读
    "imageformats\\qwebp", "imageformats\\qsvg", "imageformats\\qicns",
    "imageformats\\qtga", "imageformats\\qwbmp", "imageformats\\qpdf",
    "iconengines\\qsvgicon",
    "generic\\qtuiotouchplugin",
    "networkinformation\\", "tls\\q",
    # 99 个 Qt 自带翻译文件（qtbase_zh_CN.qm 之类）共 6.7MB。
    # 本程序的界面文字全是硬编码中文，用不到 Qt 的翻译表；
    # 顺带把解包文件数从 140 降到 40 出头，杀软逐个扫描的开销跟着掉。
    "translations\\",
    ".qm",
)

EXCLUDES = [
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQuick3D", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDBus",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSvg",
    "PySide6.QtSvgWidgets", "PySide6.QtXml", "PySide6.QtConcurrent",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtSerialPort", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "tkinter", "unittest", "pydoc", "pdb", "xmlrpc", "http.server", "sqlite3",
    "pytest", "numpy", "PIL",
]

HIDDEN = [
    "core.fs_browser", "core.size_calc", "core.delete", "core.ai_client",
    "core.knowledge", "core.risk", "core.config",
    "ui.main_window", "ui.table_model", "ui.dialogs",
]

a = Analysis(
    ["main.py"],
    pathex=[".", ".."],
    binaries=[],
    datas=[("resources", "resources")],
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)


def _keep(entry) -> bool:
    name = entry[0].lower().replace("/", "\\")
    return not any(fragment in name for fragment in DROPPED)


_before = len(a.binaries)
a.binaries = [entry for entry in a.binaries if _keep(entry)]
a.datas = [entry for entry in a.datas if _keep(entry)]
print(f"[spec] 剔掉 {_before - len(a.binaries)} 个用不到的 Qt 组件")

pyz = PYZ(a.pure, a.zipped_data, cipher=BLOCK_CIPHER)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SightClean",
    icon="resources/app.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    # 默认要管理员：写进 exe 的清单，双击就弹 UAC。
    # 之所以默认提权：Program Files、HKLM、系统进程这些东西普通权限动不了，
    # 与其让用户到处撞「需要管理员权限」，不如一上来就拿够。
    uac_admin=True,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
