"""接续登记新的交付与领用事实。

追加而非改写：每行沿用 step/kind/body 形状，step 自动接续当前最大交付次序。
同一步内的多条记录视为同批，先后不代表效力。不写死任何站点、批次或分组。
"""

from __future__ import annotations

import json
from pathlib import Path

from .load import KINDS, parse_lines


def next_step(path: str | Path) -> int:
    rows = parse_lines(Path(path).read_text(encoding="utf-8").splitlines())
    return max(r.step for r in rows) + 1


def append_rows(
    path: str | Path,
    bodies: list[dict],
    kinds: list[str] | None = None,
    step: int | None = None,
) -> int:
    """把若干 body 作为同一步追加。

    * ``bodies`` 中每项可以是完整的 ``{"kind": ..., "body": ...}``，也可以是
      纯 body，此时用并行参数 ``kinds`` 给出类型；
    * 返回所用 step。
    """
    path = Path(path)
    target_step = step if step is not None else next_step(path)
    lines = []
    for idx, item in enumerate(bodies):
        if "kind" in item and "body" in item and set(item) == {"kind", "body"}:
            kind, body = item["kind"], item["body"]
        else:
            if kinds is None:
                raise ValueError("纯 body 记录必须通过 kinds 参数给出记录类型")
            kind, body = kinds[idx], item
        if kind not in KINDS:
            raise ValueError(f"未知记录类型 {kind!r}")
        if not isinstance(body, dict) or not body:
            raise ValueError("body 必须是非空对象")
        lines.append(json.dumps(
            {"step": target_step, "kind": kind, "body": body},
            ensure_ascii=False,
        ))
    with path.open("a", encoding="utf-8") as fh:
        if path.stat().st_size and not path.read_text(encoding="utf-8").endswith("\n"):
            fh.write("\n")
        fh.write("\n".join(lines) + "\n")
    # 追加后立即回读校验，拒绝写出无法重建的日志
    parse_lines(path.read_text(encoding="utf-8").splitlines())
    return target_step
