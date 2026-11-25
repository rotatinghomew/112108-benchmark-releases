"""读取来源交换 JSONL。

记录是按交付次序追加的事实。本模块只做形状校验与解析，不解释效力
（效力判断在 :mod:`benchbank.repo` 中按当前快照计算）。
站点、轮次、用途、分组名都来自数据，解析器不写死任何具体取值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

KINDS = {"brief", "image", "derived", "peer", "alias", "retire", "receipt"}


class ExchangeFormatError(ValueError):
    """交换件形状不符合字典约定。"""


@dataclass(frozen=True)
class Row:
    step: int
    kind: str
    body: dict[str, Any]
    seq: int  # 在文件中的物理行序，用于同一步内稳定排序


# 各记录类型的必填字段；字段含义见 data/字典.md
_REQUIRED: dict[str, tuple[str, ...]] = {
    "brief": ("round", "min", "stations"),
    "image": ("id", "bytes_id", "use", "marked"),
    "derived": ("id", "item", "parent"),
    "peer": ("id", "left", "right"),
    "alias": ("id", "evidence"),
    "retire": ("id", "item"),
    "receipt": ("round", "release", "purpose", "ids"),
}


def parse_lines(lines: Iterable[str]) -> list[Row]:
    rows: list[Row] = []
    last_step = 0
    for seq, raw in enumerate(lines):
        text = raw.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ExchangeFormatError(f"第 {seq + 1} 行不是合法 JSON: {exc}") from exc
        if not isinstance(obj, dict) or set(obj) != {"step", "kind", "body"}:
            raise ExchangeFormatError(
                f"第 {seq + 1} 行顶层必须且只能有 step/kind/body"
            )
        step, kind, body = obj["step"], obj["kind"], obj["body"]
        if not isinstance(step, int) or isinstance(step, bool) or step < 1:
            raise ExchangeFormatError(f"第 {seq + 1} 行 step 必须为正整数")
        if step < last_step:
            raise ExchangeFormatError(f"第 {seq + 1} 行 step 退回，记录必须按交付次序排列")
        last_step = step
        if kind not in KINDS:
            raise ExchangeFormatError(f"第 {seq + 1} 行未知记录类型 {kind!r}")
        if not isinstance(body, dict) or not body:
            raise ExchangeFormatError(f"第 {seq + 1} 行 body 必须是非空对象")
        missing = [f for f in _REQUIRED[kind] if f not in body]
        if missing:
            raise ExchangeFormatError(f"第 {seq + 1} 行 {kind} 缺少字段 {missing}")
        if kind == "image":
            if not isinstance(body["use"], list) or not body["use"]:
                raise ExchangeFormatError(f"第 {seq + 1} 行 use 必须是非空数组")
            if not isinstance(body["marked"], bool):
                raise ExchangeFormatError(f"第 {seq + 1} 行 marked 必须是布尔")
        if kind == "brief":
            if not isinstance(body["min"], dict) or not body["min"]:
                raise ExchangeFormatError(f"第 {seq + 1} 行 brief.min 必须是非空对象")
            if not isinstance(body["stations"], dict):
                raise ExchangeFormatError(f"第 {seq + 1} 行 brief.stations 必须是对象")
        if kind == "receipt":
            if not isinstance(body["ids"], list) or not body["ids"]:
                raise ExchangeFormatError(f"第 {seq + 1} 行 receipt.ids 必须是非空数组")
        rows.append(Row(step=step, kind=kind, body=body, seq=seq))
    if not rows:
        raise ExchangeFormatError("交换件为空")
    return rows


def load(path: str | Path) -> list[Row]:
    return parse_lines(Path(path).read_text(encoding="utf-8").splitlines())
