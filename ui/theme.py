"""主题与字号。

两件事：
1. **深色 / 浅色**：一套 QSS 铺给 Qt 控件，一套调色板给自绘的表格和风险点
2. **字号缩放**：小 / 中 / 大，全局字体大小

默认跟随系统（读 Windows 的 AppsUseLightTheme），读不到就用浅色。
"""
from __future__ import annotations

LIGHT = {
    "bg": "#ffffff",
    "panel": "#f6f8fa",
    "text": "#1f2328",
    "muted": "#57606a",
    "border": "#d0d7de",
    "hover": "#eef1f4",
    "select": "#dbeafe",
    "ok": "#1a7f37",
    "warn": "#9a6700",
    "danger": "#cf222e",
    "gray": "#8b949e",
    "danger_bg": "#fff1f0",
    "input": "#ffffff",
}

DARK = {
    "bg": "#1b1f23",
    "panel": "#22272e",
    "text": "#c9d1d9",
    "muted": "#8b949e",
    "border": "#373e47",
    "hover": "#2a3038",
    "select": "#2d3f57",
    "ok": "#3fb950",
    "warn": "#d29922",
    "danger": "#f85149",
    "gray": "#6e7681",
    "danger_bg": "#3a2224",
    "input": "#22272e",
}

PALETTES = {"light": LIGHT, "dark": DARK}

SCALES = {"small": 8, "medium": 9, "large": 11}
SCALE_LABELS = (("小", "small"), ("中", "medium"), ("大", "large"))
THEME_LABELS = (("跟随系统", "system"), ("浅色", "light"), ("深色", "dark"))

_current = "light"

def detect_system() -> str:
    """读系统主题。读不到就当浅色 —— 浅色在任何情况下都能看清。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(value) else "dark"
    except (OSError, ValueError, ImportError):
        return "light"

def resolve(theme: str) -> str:
    """把「系统」这种相对值落成 light/dark。"""
    text = str(theme or "system").strip().lower()
    if text in ("light", "dark"):
        return text
    return detect_system()

def current() -> str:
    return _current

def colors() -> dict:
    return PALETTES.get(_current, LIGHT)

def color(name: str) -> str:
    return colors().get(name, "#000000")

def build_qss(theme: str, scale: str) -> str:
    palette = PALETTES.get(theme, LIGHT)
    size = SCALES.get(scale, 9)
    return f"""
QWidget {{
    background: {palette["bg"]};
    color: {palette["text"]};
    font-size: {size}pt;
}}
QMainWindow, QDialog {{ background: {palette["bg"]}; }}

QTableView, QTableWidget {{
    background: {palette["bg"]};
    alternate-background-color: {palette["panel"]};
    gridline-color: {palette["border"]};
    selection-background-color: {palette["select"]};
    selection-color: {palette["text"]};
    border: 1px solid {palette["border"]};
}}
QHeaderView::section {{
    background: {palette["panel"]};
    color: {palette["muted"]};
    border: none;
    border-bottom: 1px solid {palette["border"]};
    padding: 4px 6px;
}}
QHeaderView::section:hover {{ background: {palette["hover"]}; }}

QPushButton, QToolButton {{
    background: {palette["panel"]};
    color: {palette["text"]};
    border: 1px solid {palette["border"]};
    border-radius: 4px;
    padding: 4px 12px;
}}
QPushButton:hover, QToolButton:hover {{ background: {palette["hover"]}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {palette["gray"]}; }}
QToolButton {{ border: none; background: transparent; }}

QLineEdit, QComboBox, QSpinBox, QTextEdit, QTextBrowser, QPlainTextEdit {{
    background: {palette["input"]};
    color: {palette["text"]};
    border: 1px solid {palette["border"]};
    border-radius: 4px;
    padding: 3px 6px;
}}
QComboBox QAbstractItemView {{
    background: {palette["input"]};
    color: {palette["text"]};
    selection-background-color: {palette["select"]};
}}

QMenu {{ background: {palette["panel"]}; color: {palette["text"]}; border: 1px solid {palette["border"]}; }}
QMenu::item:selected {{ background: {palette["select"]}; }}
QMenuBar {{ background: {palette["bg"]}; color: {palette["text"]}; }}
QStatusBar {{ background: {palette["panel"]}; color: {palette["muted"]}; }}
QSplitter::handle {{ background: {palette["border"]}; }}
QScrollBar:vertical, QScrollBar:horizontal {{ background: {palette["panel"]}; }}
QScrollBar::handle {{ background: {palette["border"]}; border-radius: 4px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QCheckBox, QRadioButton, QLabel {{ background: transparent; }}
QGroupBox {{ border: 1px solid {palette["border"]}; border-radius: 4px; margin-top: 8px; }}
QGroupBox::title {{ color: {palette["muted"]}; left: 8px; padding: 0 4px; }}
"""

def apply(app, theme: str = "system", scale: str = "medium") -> str:
    """铺样式并写 回全局调色板。返回实际生效的 light/dark。"""
    global _current
    resolved = resolve(theme)
    _current = resolved
    try:
        app.setStyleSheet(build_qss(resolved, scale))
        font = app.font()
        font.setPointSize(SCALES.get(scale, 9))
        font.setFamily(font.family() or "Microsoft YaHei UI")
        app.setFont(font)
    except Exception:  # noqa: BLE001 - 样式出问题不能拦住启动
        pass

    # 自绘那部分（表格圆点、风险文字）跟着换色
    palette = PALETTES[resolved]
    try:
        from core import risk

        risk.set_colors({
            "safe": palette["ok"],
            "caution": palette["warn"],
            "forbidden": palette["danger"],
            "unlisted": palette["gray"],
        })
    except Exception:  # noqa: BLE001
        pass
    return resolved
