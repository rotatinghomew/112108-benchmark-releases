"""图像基准仓：从追加式交换记录重建任意交付时点的事实状态。

设计要点（对应 data/字典.md 与任务规则）：

* 图像身份是 ``站点/采集号``；``bytes_id`` 只是内容指纹，同字节不等于同一次
  采集，不同站的本地编号也天然不会互相覆盖。
* ``derived``（衍生品 -> 父品）与 ``peer``（参考材料同源）证据在有效时都形成
  隔离约束；``alias`` 撤回证据（边消失，两端图像保留），``retire`` 撤回采集
  （图像留痕但退出分发，失效只沿 *派生依赖* 传播，不沿 peer 传播）。
* 证据有效性要求两端图像均已登记；父图未登记的派生图不能成为独立候选。
* 所有判断都是时点函数 :class:`Snapshot`，历史发布可被原样回放，补发证明后
  旧发布号依然能解释当时发了哪些图。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .load import Row


# --------------------------------------------------------------------------- #
# 领域结构
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Image:
    id: str
    station: str
    bytes_id: str
    use: frozenset[str]
    marked: bool
    registered_step: int
    retired_step: Optional[int] = None
    retire_id: Optional[str] = None


@dataclass(frozen=True)
class Edge:
    """一条分组约束边。kind 为 derived 或 peer。"""

    evidence_id: str
    kind: str
    left: str
    right: str
    step: int
    alias_id: Optional[str] = None  # 被哪条 alias 撤回
    alias_step: Optional[int] = None

    @property
    def withdrawn(self) -> bool:
        return self.alias_id is not None


@dataclass(frozen=True)
class Receipt:
    round: str
    release: str
    purpose: str
    ids: tuple[str, ...]
    step: int


@dataclass(frozen=True)
class Brief:
    round: str
    minimums: dict[str, int]
    stations: dict[str, tuple[str, ...]]
    step: int


# --------------------------------------------------------------------------- #
# 并查集
# --------------------------------------------------------------------------- #


class _DSU:
    def __init__(self, members: list[str]):
        self.parent = {m: m for m in members}

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # 字典序小者作根，保证分组稳定可解释
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


# --------------------------------------------------------------------------- #
# 时点快照
# --------------------------------------------------------------------------- #


@dataclass
class Snapshot:
    step: int
    images: dict[str, Image]
    edges: list[Edge]
    briefs: dict[str, Brief]
    receipts: list[Receipt]
    # 派生边按 item 索引（含尚未确认的，用于 pending-parent 判断）
    derived_by_item: dict[str, list[Edge]]
    invalid: frozenset[str]
    pending: frozenset[str]
    groups: list[tuple[str, ...]]          # 按成员最小 id 排序
    group_of: dict[str, str]               # 图像 id -> 组键（成员 frozenset 的稳定标签）
    group_key_to_members: dict[str, frozenset[str]]

    # ---- 基础查询 ---------------------------------------------------------- #

    def active_edges(self) -> list[Edge]:
        return [e for e in self.edges if not e.withdrawn]

    def group_members(self, image_id: str) -> frozenset[str]:
        key = self.group_of.get(image_id)
        if key is None:
            return frozenset({image_id}) if image_id in self.images else frozenset()
        return self.group_key_to_members[key]

    def is_invalid(self, image_id: str) -> bool:
        return image_id in self.invalid

    def is_pending(self, image_id: str) -> bool:
        return image_id in self.pending

    # ---- 资格 -------------------------------------------------------------- #

    def eligibility(self, image_id: str) -> dict:
        """返回该图在当前时点能否参与分发及原因。"""
        img = self.images.get(image_id)
        if img is None:
            return {"id": image_id, "eligible": False, "reason": "unregistered",
                    "reasons": ["图像尚未登记"], "purposes": {}}
        reason_codes: list[str] = []
        reasons: list[str] = []
        if img.retired_step is not None:
            reason_codes.append("retired")
            reasons.append(f"采集已被 {img.retire_id} 撤回(step {img.retired_step})")
        dep = self._retired_dependency(image_id)
        if dep is not None:
            reason_codes.append("retired-dependency")
            chain, retire = dep
            reasons.append(
                "派生依赖的父图已撤回: " + " -> ".join(chain)
                + f"（{retire}）；失效只沿派生依赖传播"
            )
        if image_id in self.pending:
            reason_codes.append("pending-parent")
            missing = sorted({
                e.right for e in self.derived_by_item.get(image_id, ())
                if not e.withdrawn and e.right not in self.images
            })
            reasons.append("尚有未决父图，不能列为独立候选: " + ", ".join(missing))
        purposes = {}
        if not reasons:
            for purpose in sorted(img.use):
                ok = bool(img.marked)
                purposes[purpose] = {
                    "allowed_by_license": True,
                    "marked": img.marked,
                    "usable": ok,
                }
        return {
            "id": image_id,
            "station": img.station,
            "bytes_id": img.bytes_id,
            "eligible": not reasons,
            "reason": None if not reasons else reason_codes[0],
            "reason_codes": reason_codes,
            "reasons": reasons,
            "purposes": purposes,
        }

    def eligible_images(self) -> list[str]:
        return [i for i in self.images if self.eligibility(i)["eligible"]]

    def usable_for(self, purpose: str) -> list[str]:
        """可参加某用途的图：资格有效 + 站点许可 + 已标注。"""
        out = []
        for image_id, img in self.images.items():
            info = self.eligibility(image_id)
            if not info["eligible"]:
                continue
            p = info["purposes"].get(purpose)
            if p and p["usable"]:
                out.append(image_id)
        return sorted(out)

    def _retired_dependency(self, image_id: str) -> Optional[tuple[list[str], str]]:
        """沿有效派生边向上找被撤回的祖先，返回链路与撤回证据号。"""
        stack: list[tuple[str, list[str]]] = [(image_id, [image_id])]
        seen = set()
        while stack:
            cur, chain = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for e in self.derived_by_item.get(cur, ()):
                if e.withdrawn:
                    continue
                parent = e.right
                img = self.images.get(parent)
                new_chain = chain + [parent]
                if img is not None and img.retired_step is not None:
                    return new_chain, img.retire_id
                stack.append((parent, new_chain))
        return None

    # ---- 领用与暴露 -------------------------------------------------------- #

    def receipts_for(self, round_id: str) -> list[Receipt]:
        return [r for r in self.receipts if r.round == round_id]

    def exposed_ids(self, round_id: str) -> frozenset[str]:
        """某轮盲测方实际领到的图（历史暴露事实）。"""
        out: set[str] = set()
        for r in self.receipts_for(round_id):
            if r.purpose == "blind":
                out.update(r.ids)
        return frozenset(out)

    def exposed_groups(self, round_id: str) -> dict[str, frozenset[str]]:
        """暴露图 -> 按 *当前* 能确认的同组关系扩展到的整组成员。"""
        result = {}
        for image_id in sorted(self.exposed_ids(round_id)):
            result[image_id] = self.group_members(image_id)
        return result

    def purpose_groups(self, round_id: str) -> dict[str, frozenset[str]]:
        """某轮各用途已领用图当前所在的隔离组键集合。"""
        by_purpose: dict[str, set[str]] = defaultdict(set)
        for r in self.receipts_for(round_id):
            for image_id in r.ids:
                key = self.group_of.get(image_id)
                if key is not None:
                    by_purpose[r.purpose].add(key)
        return {p: frozenset(ks) for p, ks in by_purpose.items()}

    def proof_path(self, left: str, right: str) -> Optional[list[Edge]]:
        """在当前有效边上 BFS 两图之间的连通证据链。"""
        if left == right:
            return []
        adj: dict[str, list[Edge]] = defaultdict(list)
        for e in self.active_edges():
            if e.left in self.images and e.right in self.images:
                adj[e.left].append(e)
                adj[e.right].append(e)
        queue = [left]
        prev: dict[str, tuple[str, Edge]] = {}
        seen = {left}
        while queue:
            cur = queue.pop(0)
            for e in adj.get(cur, ()):
                nxt = e.right if e.left == cur else e.left
                if nxt in seen:
                    continue
                seen.add(nxt)
                prev[nxt] = (cur, e)
                if nxt == right:
                    path = []
                    node = nxt
                    while node != left:
                        node, edge = prev[node]
                        path.append(edge)
                    return list(reversed(path))
                queue.append(nxt)
        return None


# --------------------------------------------------------------------------- #
# 仓
# --------------------------------------------------------------------------- #


class Ledger:
    """追加式交换日志的内存重建器；不写死任何站点、轮次或分组名。"""

    def __init__(self, rows: list[Row]):
        self.rows = list(rows)
        self.max_step = max(r.step for r in self.rows)

    @classmethod
    def from_path(cls, path: str) -> "Ledger":
        from .load import load

        return cls(load(path))

    # ---- 内部：截至某 step 的素材 ------------------------------------------ #

    def _materials(self, step: int):
        images: dict[str, Image] = {}
        raw_edges: list[Edge] = []
        aliases: dict[str, tuple[str, int]] = {}
        retires: dict[str, tuple[str, int]] = {}
        briefs: dict[str, Brief] = {}
        receipts: list[Receipt] = []

        for row in self.rows:
            if row.step > step:
                break
            b = row.body
            kind = row.kind
            if kind == "image":
                image_id = b["id"]
                station = image_id.split("/", 1)[0] if "/" in image_id else image_id
                images[image_id] = Image(
                    id=image_id,
                    station=station,
                    bytes_id=b["bytes_id"],
                    use=frozenset(b["use"]),
                    marked=bool(b["marked"]),
                    registered_step=row.step,
                )
            elif kind in ("derived", "peer"):
                left, right = (
                    (b["item"], b["parent"]) if kind == "derived" else (b["left"], b["right"])
                )
                raw_edges.append(
                    Edge(b["id"], kind, left, right, row.step)
                )
            elif kind == "alias":
                aliases[b["evidence"]] = (b["id"], row.step)
            elif kind == "retire":
                retires[b["item"]] = (b["id"], row.step)
            elif kind == "brief":
                briefs[b["round"]] = Brief(
                    round=b["round"],
                    minimums=dict(b["min"]),
                    stations={k: tuple(v) for k, v in b["stations"].items()},
                    step=row.step,
                )
            elif kind == "receipt":
                receipts.append(
                    Receipt(
                        round=b["round"],
                        release=b["release"],
                        purpose=b["purpose"],
                        ids=tuple(b["ids"]),
                        step=row.step,
                    )
                )

        # alias 作用到证据边
        edges: list[Edge] = []
        for e in raw_edges:
            if e.evidence_id in aliases:
                alias_id, alias_step = aliases[e.evidence_id]
                edges.append(Edge(
                    e.evidence_id, e.kind, e.left, e.right, e.step,
                    alias_id=alias_id, alias_step=alias_step,
                ))
            else:
                edges.append(e)

        # retire 作用到图像（保留图像用于追溯与隔离）
        for image_id, (retire_id, retire_step) in retires.items():
            if image_id in images:
                img = images[image_id]
                images[image_id] = Image(
                    img.id, img.station, img.bytes_id, img.use, img.marked,
                    img.registered_step, retire_step, retire_id,
                )
        return images, edges, briefs, receipts, aliases, retires

    def snapshot(self, step: Optional[int] = None) -> Snapshot:
        step = self.max_step if step is None else step
        images, edges, briefs, receipts, _aliases, _retires = self._materials(step)

        derived_by_item: dict[str, list[Edge]] = defaultdict(list)
        for e in edges:
            if e.kind == "derived":
                derived_by_item[e.left].append(e)

        # 失效：被撤回采集 + 沿有效派生边依赖它们的衍生品（peer 不传播）
        invalid: set[str] = set()
        for img in images.values():
            if img.retired_step is not None:
                invalid.add(img.id)

        # 父 -> 子 的有效派生索引，撤回沿派生依赖向下传播
        children: dict[str, list[str]] = defaultdict(list)
        for e in edges:
            if e.kind == "derived" and not e.withdrawn:
                children[e.right].append(e.left)

        stack = list(invalid)
        while stack:
            parent = stack.pop()
            for child in children.get(parent, ()):
                if child not in invalid:
                    invalid.add(child)
                    stack.append(child)

        # 未决父图：有效派生边的父图尚未登记
        pending = {
            e.left
            for e in edges
            if e.kind == "derived"
            and not e.withdrawn
            and e.right not in images
        }

        # 隔离组：已登记图像上的全部有效边（含失效图像，隔离关系仍需留痕）
        dsu = _DSU(sorted(images))
        active = [
            e for e in edges
            if not e.withdrawn and e.left in images and e.right in images
        ]
        for e in active:
            dsu.union(e.left, e.right)

        buckets: dict[str, set[str]] = defaultdict(set)
        for image_id in images:
            buckets[dsu.find(image_id)].add(image_id)
        groups = sorted((tuple(sorted(members)) for members in buckets.values()),
                        key=lambda members: members)
        group_key_to_members = {
            self._group_key(members): frozenset(members) for members in groups
        }
        group_of = {
            image_id: key
            for key, members in group_key_to_members.items()
            for image_id in members
        }

        return Snapshot(
            step=step,
            images=images,
            edges=edges,
            briefs=briefs,
            receipts=receipts,
            derived_by_item=dict(derived_by_item),
            invalid=frozenset(invalid),
            pending=frozenset(pending),
            groups=groups,
            group_of=group_of,
            group_key_to_members=group_key_to_members,
        )

    @staticmethod
    def _group_key(members: tuple[str, ...]) -> str:
        # 组没有永久名字；键由当前成员决定，组拆开后键自然不同
        return "grp:" + "|".join(members)

    # ---- 字节指纹审计：同字节 != 同身份 ------------------------------------ #

    def bytes_audit(self, step: Optional[int] = None) -> dict[str, list[str]]:
        snap = self.snapshot(step)
        by_bytes: dict[str, list[str]] = defaultdict(list)
        for img in snap.images.values():
            by_bytes[img.bytes_id].append(img.id)
        return {
            bytes_id: sorted(ids)
            for bytes_id, ids in sorted(by_bytes.items())
            if len(ids) > 1
        }

    # ---- 发布清单（领用方固定版本后读取）---------------------------------- #

    def release_manifest(self, release: str, step: Optional[int] = None) -> dict:
        """按原交付指纹回查某发布号当时/当前可解释的清单。"""
        snap = self.snapshot(step)
        items = [r for r in snap.receipts if r.release == release]
        if not items:
            raise KeyError(f"发布号 {release!r} 无任何领用记录")
        rounds = sorted({r.round for r in items})
        purposes: dict[str, list[dict]] = defaultdict(list)
        for r in sorted(items, key=lambda x: (x.purpose, x.ids)):
            for image_id in r.ids:
                img = snap.images.get(image_id)
                purposes[r.purpose].append({
                    "id": image_id,
                    "station": img.station if img else image_id.split("/", 1)[0],
                    "bytes_id": img.bytes_id if img else None,
                    "registered": img is not None,
                    "currently_invalid": image_id in snap.invalid,
                })
        later_tags = {
            x.release for x in snap.receipts
            if x.round in rounds and x.release != release and x.step >= max(i.step for i in items)
        }
        return {
            "release": release,
            "rounds": rounds,
            "claimable": not later_tags,  # 已有同轮后续发布 => 原包不得当当前包继续领取
            "superseded_by": sorted(later_tags),
            "purposes": {k: v for k, v in sorted(purposes.items())},
            "as_of_step": snap.step,
        }

    def releases(self, step: Optional[int] = None) -> list[dict]:
        snap = self.snapshot(step)
        out = []
        by_tag: dict[str, list[Receipt]] = defaultdict(list)
        for r in snap.receipts:
            by_tag[r.release].append(r)
        for tag, rs in sorted(by_tag.items(), key=lambda kv: min(r.step for r in kv[1])):
            rounds = sorted({r.round for r in rs})
            later = {
                x.release for x in snap.receipts
                if x.round in rounds and x.release != tag
                and x.step >= max(r.step for r in rs)
            }
            out.append({
                "release": tag,
                "rounds": rounds,
                "steps": sorted({r.step for r in rs}),
                "claimable": not later,
                "superseded_by": sorted(later),
            })
        return out

    # ---- 污染编年史 -------------------------------------------------------- #

    def contamination_log(self) -> list[dict]:
        """逐时点扫描：同一轮不同用途的领用在当前有效关系下同组即冲突。

        只在冲突 *首次出现* 的时点登记一次；证据随后被订正撤回，登记依然
        保留（历史暴露与成绩不可对比的事实不因分组改名/拆开而消失）。
        """
        seen: set[tuple] = set()
        log: list[dict] = []
        for step in range(1, self.max_step + 1):
            snap = self.snapshot(step)
            by_round: dict[str, list[Receipt]] = defaultdict(list)
            for r in snap.receipts:
                by_round[r.round].append(r)
            for round_id, rs in by_round.items():
                for i, r1 in enumerate(rs):
                    for r2 in rs[i + 1:]:
                        if r1.purpose == r2.purpose:
                            continue
                        pair = tuple(sorted((r1.purpose, r2.purpose)))
                        for id1 in r1.ids:
                            for id2 in r2.ids:
                                if (id1 in snap.group_of and id2 in snap.group_of
                                        and snap.group_of[id1] == snap.group_of[id2]):
                                    key = (round_id, r1.release, r2.release,
                                           pair, tuple(sorted((id1, id2))))
                                    if key in seen:
                                        continue
                                    seen.add(key)
                                    path = snap.proof_path(id1, id2) or []
                                    log.append({
                                        "detected_step": step,
                                        "round": round_id,
                                        "purposes": list(pair),
                                        "receipts": [
                                            {"release": r1.release, "purpose": r1.purpose},
                                            {"release": r2.release, "purpose": r2.purpose},
                                        ],
                                        "images": [id1, id2],
                                        "proof_path": [
                                            {"evidence": e.evidence_id, "kind": e.kind,
                                             "left": e.left, "right": e.right}
                                            for e in path
                                        ],
                                        "blind_comparison_invalid": "blind" in pair,
                                    })
        return log

    # ---- 单图追溯 ---------------------------------------------------------- #

    def trace_image(self, image_id: str) -> dict:
        """维护视图：一张图为何分在某边、被谁撤回、参加过哪些领用。"""
        snap = self.snapshot()
        img = snap.images.get(image_id)
        # 收集派生祖先，使父图撤回也能在子品追溯中解释失效原因
        ancestor_ids = set()
        stack = [image_id]
        while stack:
            cur = stack.pop()
            for e in snap.derived_by_item.get(cur, ()):
                if not e.withdrawn and e.right not in ancestor_ids:
                    ancestor_ids.add(e.right)
                    stack.append(e.right)
        related_edge_ids = {
            e.evidence_id for e in snap.edges if image_id in (e.left, e.right)
        }
        events = []
        for row in self.rows:
            b = row.body
            if row.kind == "image" and b.get("id") == image_id:
                events.append({"step": row.step, "event": "registered",
                               "detail": {"bytes_id": b["bytes_id"], "use": b["use"],
                                          "marked": b["marked"]}})
            if row.kind in ("derived", "peer"):
                l, rr = (b.get("item"), b.get("parent")) if row.kind == "derived" \
                    else (b.get("left"), b.get("right"))
                if image_id in (l, rr):
                    events.append({"step": row.step, "event": f"{row.kind}-evidence",
                                   "detail": b})
            if row.kind == "retire" and b.get("item") in (ancestor_ids | {image_id}):
                events.append({"step": row.step, "event": "retired", "detail": b})
            if row.kind == "receipt" and image_id in b.get("ids", []):
                events.append({"step": row.step, "event": "receipt", "detail": b})
            if row.kind == "alias" and b.get("evidence") in related_edge_ids:
                events.append({"step": row.step, "event": "alias", "detail": b})
        related_edges = [
            {
                "evidence": e.evidence_id, "kind": e.kind,
                "left": e.left, "right": e.right,
                "active": not e.withdrawn,
                "withdrawn_by": e.alias_id, "withdrawn_step": e.alias_step,
            }
            for e in snap.edges if image_id in (e.left, e.right)
        ]
        info = snap.eligibility(image_id) if img else {"eligible": False,
                                                       "reason": "unregistered"}
        return {
            "id": image_id,
            "exists": img is not None,
            "current_eligibility": info,
            "current_group": sorted(snap.group_members(image_id)),
            "related_evidence": related_edges,
            "events": events,
        }
