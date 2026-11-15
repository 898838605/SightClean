@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

echo.
echo  ============================================
echo    SightClean - 一键打包成单个 exe
echo  ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo  [x] 没找到 python，请先装 Python 3.10 或更新版本，
    echo      安装时记得勾上 "Add python.exe to PATH"。
    goto :fail
)

python -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo  [!] 还没装依赖，正在安装...
    python -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)

python build.py
if errorlevel 1 goto :fail

echo.
echo  [ok] 打包完成，dist 目录里有：
dir /b dist
echo.
echo  把 dist 里的 exe 和使用说明.txt 一起发给别人就能用。
pause
exit /b 0

:fail
echo.
echo  [x] 打包失败，看上面的输出。
pause
exit /b 1
