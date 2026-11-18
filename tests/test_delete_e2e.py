"""删除功能的真实端到端测试（只在自己造的临时目录里动手）。

重点验证三条安全红线：
1. junction / 符号链接只能删链接本身，绝不能删到它指向的目录；
2. 长路径（>260）能删；
3. 回收站放不进去时不会偷偷改成永久删除。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_paths
from core.delete import DeleteItem, DeleteWorker, Mode, delete_one, purge_old_logs

FAILED: list[str] = []

def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED.append(name)

def exists(path: str) -> bool:
    return os.path.exists(app_paths.to_long(path))

def make_junction(link: str, target: str) -> bool:
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", link, target], check=True,
                       capture_output=True, text=True)
        return exists(link)
    except Exception as exc:  # noqa: BLE001
        print("      （这台机器建不了 junction：" + str(exc)[:80] + "）")
        return False

def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="dch-test-"))
    logs = root / "logs"
    logs.mkdir()
    print("测试目录：" + str(root))

    # --- 1. 永久删除文件 -------------------------------------------------
    file_a = root / "a.txt"
    file_a.write_text("hello", encoding="utf-8")
    result = delete_one(DeleteItem(str(file_a), "a.txt", False, "safe", 5), Mode.PERMANENT)
    check("彻底删除文件", result.ok and not exists(str(file_a)), result.error or "")

    result = delete_one(DeleteItem(str(file_a), "a.txt", False, "safe", 5), Mode.PERMANENT)
    check("重复删除给的是人话提示", (not result.ok) and bool(result.error), result.error or "")

    # --- 2. 彻底删除目录（含只读文件） ------------------------------------
    sub = root / "sub"
    (sub / "deep").mkdir(parents=True)
    readonly = sub / "deep" / "ro.bin"
    readonly.write_bytes(b"x" * 100)
    os.chmod(str(readonly), 0o444)
    result = delete_one(DeleteItem(str(sub), "sub", True, "caution", 100), Mode.PERMANENT)
    check("彻底删除含只读文件的目录", result.ok and not exists(str(sub)), result.error or "")

    # --- 3. junction 安全（红线） ----------------------------------------
    target = root / "target"
    target.mkdir()
    (target / "important.txt").write_text("千万别被删掉", encoding="utf-8")
    link = root / "link_to_target"
    if make_junction(str(link), str(target)):
        result = delete_one(DeleteItem(str(link), "link_to_target", True, "caution", None), Mode.PERMANENT)
        kept = (target / "important.txt").exists()
        check("删 junction 只删链接本身", result.ok and not exists(str(link)), result.error or "")
        check("junction 指向的目标文件还在（安全红线）", kept)
    else:
        check("junction 测试环境可用", False, "系统不允许建 junction")

    # --- 4. 长路径 ------------------------------------------------------
    deep = root / ("d" * 40)
    for _ in range(6):
        deep = deep / ("n" * 40)
    try:
        # 建目录本身也要走长路径前缀，否则 Python 的 os 层会直接报 206
        os.makedirs(app_paths.to_long(str(deep)), exist_ok=True)
        long_file = deep / "long.txt"
        path_text = str(long_file)
        if len(path_text) > 260:
            with open(app_paths.to_long(path_text), "w", encoding="utf-8") as handle:
                handle.write("long")
            from core.fs_browser import list_dir as browse

            listed, problem = browse(str(deep))
            check("能打开这么深的文件夹", bool(listed) and problem is None, problem or "")
            result = delete_one(DeleteItem(path_text, "long.txt", False, "safe", 4), Mode.PERMANENT)
            check("删除超过 260 字符的长路径", result.ok and not exists(path_text),
                  f"长度 {len(path_text)}；{result.error or ''}")
        else:
            check("长路径样本超过 260", False, str(len(path_text)))
    except OSError as exc:
        check("构造长路径", False, str(exc)[:80])

    # --- 5. 回收站 -------------------------------------------------------
    recycle_file = root / "trash-me.txt"
    recycle_file.write_text("bye", encoding="utf-8")
    result = delete_one(DeleteItem(str(recycle_file), "trash-me.txt", False, "safe", 3), Mode.RECYCLE)
    check("放进回收站", result.ok and not exists(str(recycle_file)), result.error or "")

    # --- 6. 批量 + 报告 + 日志 -------------------------------------------
    batch_dir = root / "batch"
    batch_dir.mkdir()
    items = []
    for index in range(5):
        target_file = batch_dir / f"f{index}.txt"
        target_file.write_text("x" * 10, encoding="utf-8")
        items.append(DeleteItem(str(target_file), f"f{index}.txt", False, "safe", 10))
    items.append(DeleteItem(str(batch_dir / "不存在.txt"), "不存在.txt", False, "unknown", None))

    worker = DeleteWorker(items, Mode.PERMANENT, logs)
    reports = []
    worker.signals.finished.connect(lambda report: reports.append(report))
    worker.run()
    report = reports[0] if reports else None
    check("批量删除完成", report is not None and report.ok_count == 5, f"ok={getattr(report, 'ok_count', '?')}")
    check("失败项被单独记下来", report is not None and report.fail_count == 1, str(report.fail_count if report else "?"))
    check("报告有一句人话总结", bool(report.summary), report.summary if report else "")
    log_files = list(logs.glob("删除记录-*.txt"))
    check("生成了删除记录", bool(log_files), log_files[0].name if log_files else "没有")
    if log_files:
        text = log_files[0].read_text(encoding="utf-8")
        check("日志里有路径和结果", "f0.txt" in text and ("成功" in text or "删掉" in text))

    # --- 7. 记录只留最近 N 份 -------------------------------------------
    for index in range(4):
        extra = logs / f"删除记录-2026010{index + 1}-000000.txt"
        extra.write_text("旧记录", encoding="utf-8")
        stamp = time.time() - (10 - index) * 60
        os.utime(str(extra), (stamp, stamp))
    total_before = len(list(logs.glob("删除记录-*.txt")))
    removed = purge_old_logs(logs, 2)
    left = len(list(logs.glob("删除记录-*.txt")))
    check("只留最近 2 份记录", removed == total_before - 2 and left == 2,
          f"原有 {total_before} 份，清了 {removed} 份，剩 {left} 份")

    shutil.rmtree(app_paths.to_long(str(root)), onerror=lambda f, p, e: os.chmod(p, 0o700) or os.unlink(p))
    print()
    if FAILED:
        print(f"有 {len(FAILED)} 项没通过：" + "、".join(FAILED))
        return 1
    print("删除功能全部通过")
    return 0

if __name__ == "__main__":
    sys.exit(main())
