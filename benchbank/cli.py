"""图像基准仓命令行：领用方固定版本读清单，维护方查追溯、污染与缺口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .append import append_rows
from .load import ExchangeFormatError
from .planner import coverage_gap, revise_release
from .repo import Ledger


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False))


def _ledger(path: str) -> Ledger:
    try:
        return Ledger.from_path(path)
    except (ExchangeFormatError, FileNotFoundError) as exc:
        raise SystemExit(f"无法读取交换件: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchbank", description="图像基准仓")
    parser.add_argument("--data", default="data/exchanges.jsonl",
                        help="交换件 JSONL 路径（默认 data/exchanges.jsonl）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("groups", help="列出当前隔离组")
    sub.add_parser("releases", help="列出全部发布号及其是否仍可领取")

    p_img = sub.add_parser("image", help="单图追溯：资格、证据、事件")
    p_img.add_argument("id")

    p_elig = sub.add_parser("eligible", help="列出当前可参加某用途的图")
    p_elig.add_argument("purpose")

    p_man = sub.add_parser("manifest", help="固定发布号，读取该版各用途清单与身份")
    p_man.add_argument("release")
    p_man.add_argument("--step", type=int, default=None)

    p_plan = sub.add_parser("revise", help="同轮修订发布：延续旧分配并补齐任务单")
    p_plan.add_argument("round")
    p_plan.add_argument("--step", type=int, default=None)

    p_gap = sub.add_parser("gap", help="覆盖缺口：下一轮需要补哪类合格图")
    p_gap.add_argument("round")

    sub.add_parser("contamination", help="盲测比较失效编年史")
    sub.add_parser("bytes-audit", help="同字节不同身份审计")

    p_add = sub.add_parser("append", help="从 stdin 读取 JSONL 接续登记（同一步）")
    p_add.add_argument("--step", type=int, default=None)

    args = parser.parse_args(argv)
    ledger = _ledger(args.data) if args.cmd != "append" else None

    if args.cmd == "groups":
        snap = ledger.snapshot()
        _print({
            "as_of_step": snap.step,
            "groups": [
                {"group": key, "members": sorted(members),
                 "contains_invalid": sorted(members & snap.invalid)}
                for key, members in sorted(
                    ((k, v) for k, v in snap.group_key_to_members.items()),
                    key=lambda kv: kv[1],
                )
            ],
        })
    elif args.cmd == "releases":
        _print(ledger.releases(args.step if hasattr(args, "step") else None))
    elif args.cmd == "image":
        _print(ledger.trace_image(args.id))
    elif args.cmd == "eligible":
        snap = ledger.snapshot()
        _print({"purpose": args.purpose, "ids": snap.usable_for(args.purpose)})
    elif args.cmd == "manifest":
        _print(ledger.release_manifest(args.release, args.step))
    elif args.cmd == "revise":
        _print(revise_release(ledger, args.round, args.step))
    elif args.cmd == "gap":
        _print(coverage_gap(ledger, args.round))
    elif args.cmd == "contamination":
        _print(ledger.contamination_log())
    elif args.cmd == "bytes-audit":
        _print(ledger.bytes_audit())
    elif args.cmd == "append":
        raw = sys.stdin.read().strip().splitlines()
        items = [json.loads(line) for line in raw if line.strip()]
        step = append_rows(Path(args.data), items, step=args.step)
        _print({"appended": len(items), "step": step})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
