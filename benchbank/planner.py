"""同轮发布的修订规划与覆盖缺口报告。

规则（字典 + 任务说明）：

1. 同一隔离组的图像不跨用途分配；
2. 盲测方已经看到的组（按当前能确认的同组关系）不能转给同轮训练/调参团队，
   即使后来换了分组名——暴露是事实；证据被订正拆开后，从未暴露的拆出部分
   不再被盲测锁扣住；
3. 修订优先延续不受影响的旧用途分配；失效/失格/与暴露冲突的旧分配才摘除；
4. 满足任务单各用途最低可标注图数与可接受站点范围；无法满足时明确报告堵住
   的是哪条承诺，不偷偷降级。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .repo import Ledger, Snapshot


@dataclass(frozen=True)
class BlockedCommitment:
    purpose: str
    requirement: str   # minimum | stations
    required: object
    available: object
    reason: str


def _station(image_id: str) -> str:
    return image_id.split("/", 1)[0] if "/" in image_id else image_id


def revise_release(
    ledger: Ledger,
    round_id: str,
    step: int | None = None,
) -> dict:
    snap = ledger.snapshot(step)
    brief = snap.briefs.get(round_id)
    if brief is None:
        raise KeyError(f"轮次 {round_id!r} 没有任务单(brief)")

    prior = snap.receipts_for(round_id)
    old_by_purpose: dict[str, list[str]] = defaultdict(list)
    old_release_by_purpose: dict[str, str] = {}
    for r in prior:
        old_by_purpose[r.purpose].extend(r.ids)
        old_release_by_purpose.setdefault(r.purpose, r.release)

    # 盲测暴露锁：暴露图 + 当前与其确认同组的全部成员
    exposed = set(snap.exposed_ids(round_id))
    locked_groups: set[str] = set()
    for image_id in exposed:
        key = snap.group_of.get(image_id)
        if key is not None:
            locked_groups.add(key)

    def group_key(image_id: str) -> str | None:
        return snap.group_of.get(image_id)

    def blind_locked(image_id: str) -> bool:
        key = group_key(image_id)
        return key is not None and key in locked_groups

    # 已被某用途占用的隔离组
    occupied: dict[str, str] = {}
    plan: dict[str, list[str]] = {}
    dropped: list[dict] = []

    def take(image_id: str, purpose: str) -> bool:
        key = group_key(image_id)
        if key is not None and key in occupied and occupied[key] != purpose:
            return False
        if key is not None:
            occupied[key] = purpose
        plan.setdefault(purpose, []).append(image_id)
        return True

    # ---- 第一步：延续旧分配（盲测优先，它持有暴露事实）------------------- #
    order = sorted(old_by_purpose, key=lambda p: (0 if p == "blind" else 1, p))
    for purpose in order:
        for image_id in dict.fromkeys(old_by_purpose[purpose]):  # 去重保序
            info = snap.eligibility(image_id)
            if not info["eligible"]:
                dropped.append({"id": image_id, "purpose": purpose,
                                "stage": "continuity",
                                "reason": "; ".join(info["reasons"])})
                continue
            pinfo = info["purposes"].get(purpose)
            if not pinfo or not pinfo["usable"]:
                dropped.append({"id": image_id, "purpose": purpose,
                                "stage": "continuity",
                                "reason": f"无 {purpose} 许可或未标注"})
                continue
            if purpose != "blind" and blind_locked(image_id):
                dropped.append({"id": image_id, "purpose": purpose,
                                "stage": "continuity",
                                "reason": "该组已在同轮盲测暴露，不能转给训练/调参"})
                continue
            key = group_key(image_id)
            if key is not None and key in occupied and occupied[key] != purpose:
                dropped.append({"id": image_id, "purpose": purpose,
                                "stage": "continuity",
                                "reason": f"隔离组已分配给用途 {occupied[key]}"})
                continue
            take(image_id, purpose)

    # ---- 第二步：补齐任务单 ---------------------------------------------- #
    blocked: list[BlockedCommitment] = []
    fill_log: list[dict] = []
    for purpose in sorted(brief.minimums):
        need = brief.minimums[purpose]
        allowed_stations = set(brief.stations.get(purpose, ()))
        chosen = plan.setdefault(purpose, [])
        chosen_set = set(chosen)

        candidates = [
            i for i in snap.usable_for(purpose)
            if i not in chosen_set
            and (purpose == "blind" or not blind_locked(i))
            and (not allowed_stations or _station(i) in allowed_stations)
        ]
        # 同组不能跨用途：过滤已被 *其他* 用途占用的组
        candidates = [
            i for i in candidates
            if group_key(i) not in occupied or occupied.get(group_key(i)) == purpose
        ]
        # 确定性：站点、id 双关键字排序
        candidates.sort(key=lambda i: (_station(i), i))
        candidates.sort(key=lambda i: (_station(i), i))

        for image_id in candidates:
            if len(chosen) >= need:
                break
            if take(image_id, purpose):
                chosen_set.add(image_id)
                fill_log.append({"id": image_id, "purpose": purpose,
                                 "station": _station(image_id)})

        # ---- 可行性诊断 -------------------------------------------------- #
        if len(chosen) < need:
            blocked.append(BlockedCommitment(
                purpose=purpose, requirement="minimum",
                required=need, available=len(chosen),
                reason=(
                    f"用途 {purpose} 至少需要 {need} 张可标注图，"
                    f"在站点 {sorted(allowed_stations)} 与盲测暴露隔离规则下"
                    f"仅能安排 {len(chosen)} 张"
                ),
            ))
        used_stations = {_station(i) for i in chosen}
        if allowed_stations and not used_stations <= allowed_stations:
            blocked.append(BlockedCommitment(
                purpose=purpose, requirement="stations",
                required=sorted(allowed_stations),
                available=sorted(used_stations),
                reason=f"用途 {purpose} 出现了任务单不可接受的站点",
            ))

    # ---- 汇总 ------------------------------------------------------------- #
    exposed_now = {
        image_id: sorted(members)
        for image_id, members in sorted(snap.exposed_groups(round_id).items())
    }
    contaminated = [
        c for c in ledger.contamination_log() if c["round"] == round_id
    ]
    return {
        "round": round_id,
        "as_of_step": snap.step,
        "prior_releases": sorted({r.release for r in prior}),
        "blind_exposure": {
            "received_ids": sorted(exposed),
            "locked_groups": sorted(locked_groups),
            "group_members_at_this_step": exposed_now,
        },
        "allocation": {
            purpose: sorted(ids) for purpose, ids in sorted(plan.items())
        },
        "stations_by_purpose": {
            purpose: sorted({_station(i) for i in ids})
            for purpose, ids in sorted(plan.items())
        },
        "retained_from_prior": {
            purpose: sorted(set(plan.get(purpose, [])) & set(old_by_purpose.get(purpose, [])))
            for purpose in sorted(set(plan) | set(old_by_purpose))
        },
        "dropped_from_prior": dropped,
        "newly_filled": fill_log,
        "minimums": dict(brief.minimums),
        "station_limits": {k: list(v) for k, v in brief.stations.items()},
        "feasible": not blocked,
        "blocked_commitments": [b.__dict__ for b in blocked],
        "invalid_blind_comparisons": contaminated,
    }


def coverage_gap(ledger: Ledger, round_id: str, step: int | None = None) -> dict:
    """下一轮/当前轮还需补哪类合格图：按用途统计合格供给与缺口。"""
    snap = ledger.snapshot(step)
    brief = snap.briefs.get(round_id)
    if brief is None:
        raise KeyError(f"轮次 {round_id!r} 没有任务单(brief)")

    block_reasons: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for image_id in sorted(snap.images):
        info = snap.eligibility(image_id)
        if not info["eligible"]:
            for code in info.get("reason_codes", [info["reason"] or "blocked"]):
                block_reasons[code]["ids"].append(image_id)

    per_purpose = {}
    for purpose, need in sorted(brief.minimums.items()):
        allowed = set(brief.stations.get(purpose, ()))
        usable = snap.usable_for(purpose)
        in_scope = [i for i in usable if not allowed or _station(i) in allowed]
        stations = sorted({_station(i) for i in in_scope})
        per_purpose[purpose] = {
            "minimum": need,
            "eligible_available": len(in_scope),
            "shortfall": max(0, need - len(in_scope)),
            "stations_present": stations,
            "acceptable_stations": sorted(allowed),
            "candidate_ids": in_scope,
        }
    return {
        "round": round_id,
        "as_of_step": snap.step,
        "purposes": per_purpose,
        "blocked_images": {k: dict(v) for k, v in block_reasons.items()},
        "invalid_ids": sorted(snap.invalid),
        "pending_parent_ids": sorted(snap.pending),
    }
