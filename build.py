"""一键打包成单个 exe。

用法：  python build.py
产物：  dist\\SightClean.exe

打包策略：
- --onefile：单文件交付，免安装；
- --noupx：UPX 会拖慢解包启动、还容易被杀软误报，不用；
- --exclude-module：把没用到的 Qt 模块全部剪掉，这是启动能压在 2 秒内的关键；
- 只带 resources 一个目录，其余 Python 文件都在 exe 里。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "SightClean"

# 这些 Qt 模块本程序一个都用不到，剪掉能省 100MB+ 体积和可观的启动时间
EXCLUDES = [
    "PySide6.QtNetwork",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuick3D",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDBus",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtXml",
    "PySide6.QtConcurrent",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtUiTools",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtPrintSupport",
    "tkinter",
    "unittest",
    "pydoc",
    "pdb",
    "xmlrpc",
    "http.server",
    "sqlite3",
]


def main() -> int:
    if not (ROOT / "main.py").exists():
        print("没找到 main.py，请在项目根目录运行本脚本")
        return 1

    for folder in ("build", "dist"):
        target = ROOT / folder
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

    spec = ROOT / f"{APP_NAME}.spec"
    if spec.exists():
        # 优先用 spec：里面做了 Qt 组件级别的裁剪，比命令行更省体积、启动更快
        args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(spec)]
    else:
        args = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--windowed",
            "--noupx",
            "--name",
            APP_NAME,
            "--add-data",
            f"resources{os.pathsep}resources",
            "--paths",
            str(ROOT),
        ]
        for module in EXCLUDES:
            args += ["--exclude-module", module]
        args.append(str(ROOT / "main.py"))

    started = time.perf_counter()
    print("开始打包，可能要一两分钟…")
    import subprocess

    result = subprocess.run(args, cwd=str(ROOT), text=True)
    if result.returncode != 0:
        print("打包失败，看上面的输出")
        return result.returncode

    manual = ROOT / "使用说明.txt"
    if manual.exists():
        try:
            shutil.copyfile(manual, ROOT / "dist" / manual.name)
            print("已经把 使用说明.txt 一起放进 dist")
        except OSError:
            print("使用说明.txt 复制失败，可以手动拷过去")

    exe = ROOT / "dist" / f"{APP_NAME}.exe"
    elapsed = time.perf_counter() - started
    if exe.exists():
        size_mb = exe.stat().st_size / 1024 / 1024
        print(f"打包完成：{exe}")
        print(f"体积 {size_mb:.1f} MB，用时 {elapsed:.0f} 秒")
    else:
        print("打包命令跑完了，但没找到 exe，检查 dist 目录")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
