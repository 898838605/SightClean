"""三个页面的列表过滤逻辑（文件模块用的是另一套，自己实现）。

约定：
- _all_rows 是数据源，_rows 是当前可见
- 勾选状态挂在 row 对象上，过滤不动它
- set_all_checked 只作用于可见行（各 Model 自己保证）
"""
from __future__ import annotations


class FilterMixin:
    _filter_key = ""
    _all_rows: list
    _rows: list

    def set_filter(self, text: str) -> None:
        key = str(text or "").strip().lower()
        if key == self._filter_key:
            return
        self._filter_key = key
        self.rebuild_visible()
        self.checkedChanged.emit()

    def filter_key(self) -> str:
        return self._filter_key

    def is_filtered(self) -> bool:
        return bool(self._filter_key)

    def _matches(self, row) -> bool:
        return self._filter_key in self.haystack(row)

    def haystack(self, row) -> str:      # 子类必须实现
        raise NotImplementedError

    def rebuild_visible(self) -> None:   # 子类必须实现（要 beginResetModel）
        raise NotImplementedError

    def hidden_checked_count(self) -> int:
        """被过滤藏起来、但还勾着的项数。"""
        if not self._filter_key:
            return 0
        visible = {id(row) for row in self._rows}
        return sum(1 for row in self._all_rows if row.checked and id(row) not in visible)

    def total_count(self) -> int:
        return len(self._all_rows)
