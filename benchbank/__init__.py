"""图像基准仓：隔离组、撤回、重新发版与领用追溯。

模块划分：

* :mod:`benchbank.load`   —— 交换件 JSONL 的形状校验与解析
* :mod:`benchbank.repo`   —— 时点快照、隔离组、资格、暴露、污染与发布清单
* :mod:`benchbank.planner` —— 同轮修订规划与覆盖缺口
* :mod:`benchbank.append` —— 接续登记新的交付与领用事实
"""

from .append import append_rows
from .load import ExchangeFormatError, Row, load, parse_lines
from .planner import coverage_gap, revise_release
from .repo import Brief, Edge, Image, Ledger, Receipt, Snapshot

__all__ = [
    "append_rows",
    "coverage_gap",
    "Brief",
    "Edge",
    "ExchangeFormatError",
    "Image",
    "Ledger",
    "Receipt",
    "Row",
    "Snapshot",
    "load",
    "parse_lines",
    "revise_release",
]
