"""Filesystem path safety shared by cleanup features.

Windows junctions are reparse points but are not consistently reported by
``os.path.islink`` on older Python versions. Recursive cleanup must never
walk through one and delete the link target.
"""
from __future__ import annotations

import os
import stat

FILE_ATTRIBUTE_DIRECTORY = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x0010)
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def is_reparse_stat(info: os.stat_result) -> bool:
    """Return whether an lstat/stat result describes a link or reparse point."""
    attrs = int(getattr(info, "st_file_attributes", 0) or 0)
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(
        int(getattr(info, "st_mode", 0) or 0)
    )


def is_reparse_point(path: str | os.PathLike[str]) -> bool:
    """Return whether ``path`` itself is a symlink, junction, or similar."""
    try:
        if os.path.islink(path):
            return True
    except OSError:
        pass
    try:
        return is_reparse_stat(os.lstat(path))
    except OSError:
        return False


def remove_reparse_point(path: str | os.PathLike[str]) -> None:
    """Remove only the link/reparse point, never its target."""
    info = os.lstat(path)
    dir_like = stat.S_ISDIR(info.st_mode) or bool(
        int(getattr(info, "st_file_attributes", 0) or 0) & FILE_ATTRIBUTE_DIRECTORY
    )
    if dir_like:
        os.rmdir(path)
    else:
        os.unlink(path)


def remove_tree(path: str | os.PathLike[str]) -> None:
    """Recursively remove a tree while treating nested reparse points as leaves."""
    with os.scandir(path) as scanner:
        entries = list(scanner)
    for entry in entries:
        info = entry.stat(follow_symlinks=False)
        if is_reparse_stat(info):
            remove_reparse_point(entry.path)
        elif stat.S_ISDIR(info.st_mode):
            remove_tree(entry.path)
        else:
            os.unlink(entry.path)
    os.rmdir(path)
