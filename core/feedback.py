"""「这条说明不对」反馈：只记在本地一个小文件里。

三条硬规矩：
1. **不联网、不上传** —— 没有任何网络调用
2. **不自动改知识库** —— 只是把话记下来，改不改由你自己决定
3. 只存路径、名称、原说明、你的话、时间，不存文件内容
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app_paths import to_long

FILE_NAME = "feedback.json"
SCHEMA = 1
MAX_ITEMS = 200

MODULE_LABELS = {
    "files": "文件清理",
    "software": "软件卸载",
    "registry": "注册表清理",
    "process": "进程管理",
}

@dataclass
class FeedbackItem:
    path: str = ""
    name: str = ""
    purpose: str = ""        # 当时显示的那句说明
    note: str = ""           # 用户填的话（可以为空）
    time: str = ""
    module: str = ""

def feedback_path() -> Path:
    from app_paths import resolve

    return resolve().root / FILE_NAME

def _read_doc() -> dict:
    path = feedback_path()
    if not path.exists():
        return {"schema": SCHEMA, "items": []}
    try:
        data = json.loads(Path(to_long(path)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": SCHEMA, "items": []}
    if not isinstance(data, dict):
        return {"schema": SCHEMA, "items": []}
    if not isinstance(data.get("items"), list):
        data["items"] = []
    return data

def add(path: str, name: str, purpose: str, note: str = "", module: str = "") -> tuple[bool, str]:
    """记一条。返回 (成功, 说明)。写不进去也如实说，不假装成功。"""
    document = _read_doc()
    items = document["items"]
    items.append(asdict(FeedbackItem(
        path=str(path or ""),
        name=str(name or ""),
        purpose=str(purpose or ""),
        note=str(note or ""),
        time=time.strftime("%Y-%m-%dT%H:%M:%S"),
        module=str(module or ""),
    )))
    # 只留最近 200 条，防止无限长
    if len(items) > MAX_ITEMS:
        document["items"] = items[-MAX_ITEMS:]
    document["schema"] = SCHEMA
    target = feedback_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        Path(to_long(target)).write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        return False, f"没记下来：{exc}"
    return True, str(target)

def load_all() -> list[FeedbackItem]:
    items = []
    for raw in _read_doc().get("items", []):
        if not isinstance(raw, dict):
            continue
        items.append(FeedbackItem(
            path=str(raw.get("path", "")),
            name=str(raw.get("name", "")),
            purpose=str(raw.get("purpose", "")),
            note=str(raw.get("note", "")),
            time=str(raw.get("time", "")),
            module=str(raw.get("module", "")),
        ))
    items.sort(key=lambda item: item.time, reverse=True)
    return items

def count() -> int:
    return len(_read_doc().get("items", []))

def clear() -> int:
    target = feedback_path()
    if not target.exists():
        return 0
    try:
        target.unlink()
        return 1
    except OSError:
        return 0

def export(target: str | Path) -> tuple[bool, str]:
    """导成 CSV（UTF-8 BOM，Excel 直接能开）。"""
    import csv

    path = Path(target)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(to_long(path), "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["时间", "模块", "名称", "路径", "当时显示的说明", "我的反馈"])
            for item in load_all():
                writer.writerow([item.time, MODULE_LABELS.get(item.module, item.module),
                                 item.name, item.path, item.purpose, item.note])
    except OSError as exc:
        return False, f"导出没成：{exc}"
    return True, str(path)
