"""操作快照：每次批量执行前留一份「这次动了什么」。

**只存元数据** —— 名称、路径、风险、结果。不复制文件、不存注册表键值、
不存命令行、不存内存。快照回答的是「我什么时候动过什么、成没成」，
不是「能把它变回来」—— 能不能还原由 restore.py 按操作类型映射决定。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

SNAPSHOT_DIR_NAME = "snapshots"
MAX_COUNT = 200                      # 最多留 200 次
MAX_BYTES = 50 * 1024 * 1024         # 目录总共不超过 50 MB
SCHEMA = 1

OP_LABELS = {
    "delete": "删除文件",
    "uninstall": "卸载软件",
    "clean": "清理注册表",
    "leftover": "清理卸载残余",
    "kill": "结束进程",
}

RESULT_OK = "ok"
RESULT_FAIL = "fail"
RESULT_SKIPPED = "skipped"

RESULT_LABELS = {
    RESULT_OK: "成功",
    RESULT_FAIL: "失败",
    RESULT_SKIPPED: "已暂缓",
    "": "已发起",
}

# 哪些操作事后能还原。其余的只留记录。
RESTORABLE = {
    ("delete", "recycle"): "从回收站还原",
    ("clean", ""): "导入备份的 .reg",
}

@dataclass
class SnapshotItem:
    name: str
    path: str
    risk: str = ""
    result: str = ""
    error: str = ""

@dataclass
class Snapshot:
    id: str
    op_type: str
    time: str
    items: list[SnapshotItem] = field(default_factory=list)
    mode: str = ""
    backup: str = ""          # 仅注册表：backups/ 里的文件名
    note: str = ""

    @property
    def op_label(self) -> str:
        return OP_LABELS.get(self.op_type, self.op_type)

    @property
    def ok_count(self) -> int:
        return sum(1 for item in self.items if item.result == RESULT_OK)

    @property
    def fail_count(self) -> int:
        return sum(1 for item in self.items if item.result == RESULT_FAIL)

    @property
    def skipped_count(self) -> int:
        return sum(1 for item in self.items if item.result == RESULT_SKIPPED)

    @property
    def pending_count(self) -> int:
        return sum(1 for item in self.items if not item.result)

    @property
    def summary(self) -> str:
        bits = []
        if self.ok_count:
            bits.append(f"成功 {self.ok_count}")
        if self.fail_count:
            bits.append(f"失败 {self.fail_count}")
        if self.skipped_count:
            bits.append(f"暂缓 {self.skipped_count}")
        if self.pending_count:
            bits.append(f"未记录 {self.pending_count}")
        return " · ".join(bits) or "没有条目"

    @property
    def restore_hint(self) -> str:
        """能不能还原、怎么还原。空串表示只能追溯。"""
        return RESTORABLE.get((self.op_type, self.mode if self.op_type == "delete" else ""), "")

    @property
    def can_restore(self) -> bool:
        if self.op_type == "delete":
            return self.mode == "recycle" and self.ok_count > 0
        if self.op_type == "clean":
            return bool(self.backup) and self.ok_count > 0
        return False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["schema"] = SCHEMA
        return data

def snapshot_dir(log_dir: Path | str | None = None) -> Path:
    if log_dir is None:
        from app_paths import resolve

        log_dir = resolve().logs
    return Path(log_dir) / SNAPSHOT_DIR_NAME

def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")

def _unique_id(base: str, directory: Path) -> str:
    candidate = base
    index = 2
    while (directory / f"{candidate}.json").exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate

def begin(
    op_type: str,
    items: Iterable,
    mode: str = "",
    backup: str = "",
    note: str = "",
    log_dir: Path | str | None = None,
) -> str | None:
    """执行**之前**调一次。返回快照 id，失败返回 None（快照失败不该拦住执行）。"""
    try:
        directory = snapshot_dir(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        snap_id = _unique_id(f"{_stamp()}-{op_type}", directory)
        rows = []
        for item in items:
            rows.append(SnapshotItem(
                name=str(getattr(item, "name", "") or getattr(item, "display", "") or ""),
                path=str(getattr(item, "path", "") or getattr(item, "human_path", "") or
                         getattr(item, "identity", "") or ""),
                risk=str(getattr(item, "risk", "") or ""),
            ))
        snapshot = Snapshot(
            id=snap_id,
            op_type=op_type,
            time=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            items=rows,
            mode=mode,
            backup=backup,
            note=note,
        )
        path = directory / f"{snap_id}.json"
        path.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        prune(directory)
        return snap_id
    except (OSError, ValueError, TypeError):
        return None

def finish(
    snap_id: str | None,
    results: Sequence,
    log_dir: Path | str | None = None,
    backup: str = "",
) -> None:
    """执行**之后**回填结果。

    backup 只有注册表用得上：备份文件名是执行时才生成的，所以在这里补进去。
    """
    if not snap_id:
        return
    try:
        path = snapshot_dir(log_dir) / f"{snap_id}.json"
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        by_path = {}
        for raw in data.get("items", []):
            by_path.setdefault(raw.get("path", ""), raw)
        if backup:
            data["backup"] = backup
        for result in results:
            key = str(getattr(result, "path", "") or getattr(result, "human_path", "") or
                      getattr(result, "identity", "") or getattr(result, "pid", "") or "")
            if not key:
                leftover = getattr(result, "leftover", None)
                key = str(getattr(leftover, "path", "") or "")
            row = by_path.get(key)
            if row is None:
                continue
            ok = bool(getattr(result, "ok", False))
            row["result"] = RESULT_OK if ok else RESULT_FAIL
            row["error"] = str(getattr(result, "error", "") or "")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        pass

def _read(path: Path) -> Snapshot | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    items = [
        SnapshotItem(
            name=str(raw.get("name", "")),
            path=str(raw.get("path", "")),
            risk=str(raw.get("risk", "")),
            result=str(raw.get("result", "")),
            error=str(raw.get("error", "")),
        )
        for raw in data.get("items", []) if isinstance(raw, dict)
    ]
    return Snapshot(
        id=str(data.get("id", path.stem)),
        op_type=str(data.get("op_type", "")),
        time=str(data.get("time", "")),
        items=items,
        mode=str(data.get("mode", "")),
        backup=str(data.get("backup", "")),
        note=str(data.get("note", "")),
    )

def load_all(log_dir: Path | str | None = None) -> list[Snapshot]:
    """按时间倒序读全部快照。读坏的直接跳过，不打断列表。"""
    directory = snapshot_dir(log_dir)
    if not directory.exists():
        return []
    found: list[Snapshot] = []
    for path in directory.glob("*.json"):
        snapshot = _read(path)
        if snapshot is not None:
            found.append(snapshot)
    found.sort(key=lambda item: item.id, reverse=True)
    return found

def usage(log_dir: Path | str | None = None) -> tuple[int, int]:
    """(总字节数, 条数)。"""
    directory = snapshot_dir(log_dir)
    if not directory.exists():
        return 0, 0
    total = 0
    count = 0
    for path in directory.glob("*.json"):
        try:
            total += path.stat().st_size
            count += 1
        except OSError:
            continue
    return total, count

def prune(directory: Path | None = None) -> tuple[int, int]:
    """按「200 条」和「50 MB」双上限清理，先清最旧的。返回 (删了几条, 删了几字节)。"""
    if directory is None:
        directory = snapshot_dir()
    if not directory.exists():
        return 0, 0
    entries = []
    for path in directory.glob("*.json"):
        try:
            entries.append((path, path.stat().st_size, path.stat().st_mtime))
        except OSError:
            continue
    entries.sort(key=lambda item: item[2])      # 最旧的在前

    removed = 0
    freed = 0
    total = sum(size for _p, size, _m in entries)
    count = len(entries)

    while entries and (count > MAX_COUNT or total > MAX_BYTES):
        path, size, _mtime = entries.pop(0)
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
        freed += size
        count -= 1
        total -= size
    return removed, freed

def clear(log_dir: Path | str | None = None) -> tuple[int, int]:
    """清空快照目录。不碰回收站，也不碰 backups/。"""
    directory = snapshot_dir(log_dir)
    if not directory.exists():
        return 0, 0
    removed = 0
    freed = 0
    for path in directory.glob("*.json"):
        try:
            freed += path.stat().st_size
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed, freed
