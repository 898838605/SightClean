"""软件来源的插件化：一种来源一个 provider。

借鉴 WinClean 的做法 —— 注册表、winget、孤儿目录各管各的，
合起来交给界面。好处是：

1. 加一种新来源只需要写一个 provider，主流程不动
2. 某个来源读失败，只影响它自己，不会让整张表空掉
3. 每条记录都带着 source，界面上能看出「这是从哪来的」
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawItem:
    """各 provider 统一产出的东西。不含卸载命令以外的判断逻辑。"""

    name: str
    version: str = ""
    publisher: str = ""
    install_date: str = ""
    size_bytes: int | None = None
    install_location: str = ""
    uninstall_string: str = ""
    quiet_uninstall_string: str = ""
    key_path: str = ""          # 身份标识：注册表键路径 / 目录路径 / 包 id
    hive: str = ""              # HKLM64 / HKCU32 / winget / (未登记)
    source: str = "registry"    # registry / winget / orphan
    hidden: bool = False        # 系统默认隐藏的（SystemComponent=1 之类）
    display_icon: str = ""      # DisplayIcon：通常是主 exe 路径
    product_code: str = ""      # MSI 产品码（键名是 GUID 时能拿到）


@dataclass
class ProviderResult:
    """一个 provider 跑完的结果：拿到了什么 + 出了什么问题。"""

    items: list[RawItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def count(self) -> int:
        return len(self.items)


SOURCE_LABELS = {
    "registry": "注册表",
    "winget": "winget",
    "orphan": "未登记",
}
