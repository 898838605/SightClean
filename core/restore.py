"""还原能力：只有两种操作事后能真的还原。

- 文件删除（回收站）→ 从回收站还原
- 注册表清理 → 导入 backups/ 里那份 .reg

软件卸载和结束进程**没有**还原路径：程序不会假装能撤销它们，
只把记录留着供排查。这一点在撤销中心会明说，不给假按钮。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from core.snapshot import Snapshot

REG_TIMEOUT = 180
RECYCLE_TIMEOUT = 60

def _run_powershell(script: str, timeout: int) -> tuple[int, str]:
    """跑一段 PowerShell，返回 (退出码, 输出)。拿不到 pwsh 就退回 powershell。"""
    for exe in ("powershell", "pwsh"):
        try:
            completed = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-Command", script],
                capture_output=True, text=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")
    return 127, "系统里没有可用的 PowerShell"

def restore_registry(snapshot: Snapshot, backup_dir: Path | None = None) -> tuple[bool, str]:
    if not snapshot.backup:
        return False, "这条记录没有对应的备份文件"
    if backup_dir is None:
        from core.registry import backup_dir as reg_backup_dir

        backup_dir = reg_backup_dir()
    path = Path(backup_dir) / snapshot.backup
    if not path.exists():
        return False, f"备份文件不在了：{path}（可能被手动删掉或挪走了）"
    try:
        completed = subprocess.run(
            ["reg", "import", str(path)],
            capture_output=True, text=True, timeout=REG_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False, "导入没跑起来，可能是权限不够"
    if completed.returncode == 0:
        return True, f"已经把 {snapshot.ok_count} 个键导回去了"
    return False, "还原没成功，这条备份里可能有需要管理员的键（试试以管理员身份重新启动）"

def restore_recycle(paths: list[str]) -> tuple[bool, str, list[str]]:
    """从回收站还原。返回 (是否全部成功, 说明, 没还原成功的路径)。

    回收站条目没法用 send2trash 那样的句柄直接定位，只能靠「原目录 + 名称」
    反查。同名同大小的文件删过两次会分不清，所以这里宁可少还原也不错还原。
    """
    targets = [str(path).replace("'", "''") for path in paths if path]
    if not targets:
        return False, "没有要还原的路径", []

    listing = ",".join(f"'{item}'" for item in targets)
    script = f"""
$ErrorActionPreference = 'Stop'
$targets = @({listing})
$shell = New-Object -ComObject Shell.Application
$bin = $shell.Namespace(10)
if ($null -eq $bin) {{ Write-Output 'NOBIN'; exit 4 }}
$restored = 0
$failed = @()
foreach ($target in $targets) {{
    $leaf = Split-Path -Leaf $target
    $parent = Split-Path -Parent $target
    $hit = $null
    foreach ($item in $bin.Items()) {{
        try {{
            $from = $item.ExtendedProperty('System.Recycle.DeletedFrom')
            if ($null -eq $from) {{ continue }}
            if ($item.Name -eq $leaf -and $from.TrimEnd('\\') -eq $parent.TrimEnd('\\')) {{ $hit = $item; break }}
        }} catch {{ }}
    }}
    if ($null -eq $hit) {{ $failed += $target; continue }}
    $done = $false
    foreach ($verb in $hit.Verbs()) {{
        $name = ($verb.Name -replace '&','')
        if ($name -match '还原|复原|Restore') {{ $verb.DoIt(); $done = $true; break }}
    }}
    if ($done) {{ $restored++ }} else {{ $failed += $target }}
}}
Write-Output ("RESTORED=" + $restored)
foreach ($item in $failed) {{ Write-Output ("MISSED=" + $item) }}
"""
    code, output = _run_powershell(script, RECYCLE_TIMEOUT)
    if code == 127:
        return False, output, paths
    if "NOBIN" in output:
        return False, "打不开回收站", paths

    missed = [line.split("MISSED=", 1)[1].strip()
              for line in output.splitlines() if line.startswith("MISSED=")]
    restored = 0
    for line in output.splitlines():
        if line.startswith("RESTORED="):
            try:
                restored = int(line.split("=", 1)[1].strip())
            except ValueError:
                restored = 0
    if restored and not missed:
        return True, f"从回收站还原了 {restored} 项", []
    if restored and missed:
        return False, f"还原了 {restored} 项，还有 {len(missed)} 项没找到（可能回收站已清空）", missed
    return False, "回收站里找不到这些项，可能已经被清空了", missed or list(paths)

def restore(snapshot: Snapshot) -> tuple[bool, str]:
    """按快照类型分派。调用方负责先问用户一次。"""
    if snapshot.op_type == "clean":
        return restore_registry(snapshot)
    if snapshot.op_type == "delete" and snapshot.mode == "recycle":
        paths = [item.path for item in snapshot.items if item.result == "ok"]
        if not paths:
            return False, "这条记录里没有成功删除的项"
        ok, message, _missed = restore_recycle(paths)
        return ok, message
    if snapshot.op_type == "delete":
        return False, "这条是「彻底删除」，文件已经不在回收站里了，没法还原"
    return False, "这类操作没有还原路径，只能查看记录"

