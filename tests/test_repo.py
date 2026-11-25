"""基准仓核心规则测试：隔离组、失效传播、未决父图、暴露与污染。"""

import json
import tempfile
import unittest
from pathlib import Path

from benchbank import Ledger
from benchbank.load import ExchangeFormatError, parse_lines

DATA = Path(__file__).resolve().parent.parent / "data" / "exchanges.jsonl"


def ledger_from_rows(rows: list[dict]) -> Ledger:
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                     encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        path = fh.name
    return Ledger.from_path(path)


class SampleTimeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = Ledger.from_path(DATA)

    def test_bytes_fingerprint_is_not_identity(self):
        snap = self.ledger.snapshot()
        # a/11 与 b/11 同字节 z1，但身份各自独立、不并组
        self.assertIn("a/11", snap.images)
        self.assertIn("b/11", snap.images)
        self.assertEqual(snap.images["a/11"].bytes_id, "z1")
        self.assertEqual(snap.images["b/11"].bytes_id, "z1")
        self.assertNotIn("b/11", snap.group_members("a/11"))
        audit = self.ledger.bytes_audit()
        self.assertEqual(audit, {"z1": ["a/11", "b/11"]})

    def test_local_ids_from_different_stations_never_overwrite(self):
        snap = self.ledger.snapshot()
        self.assertEqual(len(snap.images), 9)
        self.assertTrue(all(i.startswith(("a/", "b/")) for i in snap.images))

    def test_pending_parent_blocks_independent_candidacy(self):
        early = self.ledger.snapshot(2)
        # b/20 尚未登记，d2 不能确认，b/19 不能作独立候选
        self.assertIn("b/19", early.pending)
        info = early.eligibility("b/19")
        self.assertFalse(info["eligible"])
        self.assertTrue(any("未决父图" in r for r in info["reasons"]))
        # step5 父图补登记后解禁
        later = self.ledger.snapshot(6)
        self.assertNotIn("b/19", later.pending)
        self.assertTrue(later.eligibility("b/19")["eligible"])

    def test_retire_propagates_only_through_derived_edges(self):
        snap = self.ledger.snapshot(6)
        self.assertIn("a/11", snap.invalid)          # 被撤回采集本身
        self.assertIn("a/12", snap.invalid)          # 派生依赖 a/11
        # 撤回不删除图像，隔离与追溯关系仍在
        self.assertIsNotNone(snap.images.get("a/11"))
        self.assertIn("a/11", snap.group_members("a/12"))
        info = snap.eligibility("a/12")
        self.assertFalse(info["eligible"])
        self.assertTrue(any("withdraw-a11" in r for r in info["reasons"]))

    def test_peer_does_not_propagate_retirement(self):
        rows = [
            {"step": 1, "kind": "brief", "body": {
                "round": "r1", "min": {"train": 1},
                "stations": {"train": ["x", "y"]}}},
            {"step": 1, "kind": "image", "body": {
                "id": "x/1", "bytes_id": "h1", "use": ["train"], "marked": True}},
            {"step": 1, "kind": "image", "body": {
                "id": "x/3", "bytes_id": "h3", "use": ["train"], "marked": True}},
            {"step": 1, "kind": "image", "body": {
                "id": "x/4", "bytes_id": "h4", "use": ["train"], "marked": True}},
            {"step": 1, "kind": "peer", "body": {
                "id": "p9", "left": "x/1", "right": "x/3"}},
            {"step": 1, "kind": "derived", "body": {
                "id": "d9", "item": "x/4", "parent": "x/1"}},
            {"step": 2, "kind": "retire", "body": {
                "item": "x/1", "id": "w1"}},
        ]
        snap = ledger_from_rows(rows).snapshot()
        self.assertIn("x/1", snap.invalid)
        self.assertIn("x/4", snap.invalid)      # 派生链失效
        self.assertNotIn("x/3", snap.invalid)   # 仅 peer 同源，不失效
        self.assertTrue(snap.eligibility("x/3")["eligible"])

    def test_evidence_withdrawal_splits_group_but_keeps_history(self):
        # step3：p1 把训练侧 a/12 与盲测侧 b/11 连通
        merged = self.ledger.snapshot(3)
        self.assertIn("b/11", merged.group_members("a/12"))
        # step6：wd1 订正撤回 p1，组拆开
        split = self.ledger.snapshot(6)
        self.assertNotIn("b/11", split.group_members("a/12"))
        self.assertEqual(split.group_members("b/11"), frozenset({"b/11"}))
        # 证据边保留且标记为已撤回，可追溯
        p1 = next(e for e in split.edges if e.evidence_id == "p1")
        self.assertTrue(p1.withdrawn)
        self.assertEqual(p1.alias_id, "wd1")

    def test_license_and_annotation_gate_usable_purposes(self):
        snap = self.ledger.snapshot(6)
        self.assertEqual(
            snap.usable_for("train"),
            ["a/22", "a/23", "b/11", "b/19", "b/20"],
        )
        self.assertEqual(snap.usable_for("tune"), ["b/15"])
        self.assertEqual(snap.usable_for("blind"), ["a/21", "a/22", "b/11"])
        # 未标注/无许可的图不计入用途
        rows = [
            {"step": 1, "kind": "brief", "body": {
                "round": "r1", "min": {"train": 1}, "stations": {"train": ["x"]}}},
            {"step": 1, "kind": "image", "body": {
                "id": "x/u", "bytes_id": "u", "use": ["blind"], "marked": True}},
            {"step": 1, "kind": "image", "body": {
                "id": "x/m", "bytes_id": "m", "use": ["train"], "marked": False}},
        ]
        s = ledger_from_rows(rows).snapshot()
        self.assertEqual(s.usable_for("train"), [])

    def test_contamination_is_detected_and_survives_correction(self):
        log = self.ledger.contamination_log()
        r7 = [c for c in log if c["round"] == "r7"]
        # 冲突在 step3 首次出现：训练 a/11、a/12 与盲测 b/11 同组
        self.assertEqual({c["detected_step"] for c in r7}, {3})
        pairs = {tuple(sorted(c["images"])) for c in r7}
        self.assertEqual(pairs, {("a/11", "b/11"), ("a/12", "b/11")})
        self.assertTrue(all(c["blind_comparison_invalid"] for c in r7))
        direct = next(c for c in r7 if c["images"] == ["a/12", "b/11"])
        self.assertEqual([e["evidence"] for e in direct["proof_path"]], ["p1"])
        # step6 证据已撤回，但污染编年史保留（历史暴露事实不消失）
        self.assertEqual(len([c for c in self.ledger.contamination_log()
                              if c["round"] == "r7"]), 2)

    def test_original_release_remains_interpretable(self):
        manifest = self.ledger.release_manifest("issued-7")
        self.assertEqual(
            sorted(manifest["purposes"]), ["blind", "train", "tune"],
        )
        train_ids = [x["id"] for x in manifest["purposes"]["train"]]
        self.assertEqual(train_ids, ["a/11", "a/12"])
        # 用原交付指纹回查身份
        self.assertEqual(
            {x["id"]: x["bytes_id"] for x in manifest["purposes"]["blind"]},
            {"b/11": "z1", "a/21": "z4"},
        )
        # 当时发的图现在已失效，清单仍如实标注
        self.assertTrue(all(x["currently_invalid"]
                            for x in manifest["purposes"]["train"]))

    def test_trace_image_explains_why(self):
        trace = self.ledger.trace_image("a/12")
        kinds = [e["event"] for e in trace["events"]]
        self.assertIn("registered", kinds)
        self.assertIn("retired", [e["event"] for e in trace["events"]])  # 父图撤回
        evidence = {e["evidence"] for e in trace["related_evidence"]}
        self.assertEqual(evidence, {"d1", "p1"})
        self.assertFalse(trace["current_eligibility"]["eligible"])

    def test_unregistered_receipt_id_does_not_gain_eligibility(self):
        rows = [
            {"step": 1, "kind": "brief", "body": {
                "round": "r1", "min": {"blind": 1}, "stations": {"blind": ["x"]}}},
            {"step": 1, "kind": "image", "body": {
                "id": "x/1", "bytes_id": "h1", "use": ["blind"], "marked": True}},
            {"step": 2, "kind": "receipt", "body": {
                "round": "r1", "release": "rel-1",
                "purpose": "blind", "ids": ["x/1", "x/ghost"]}},
        ]
        ledger = ledger_from_rows(rows)
        info = ledger.snapshot().eligibility("x/ghost")
        self.assertFalse(info["eligible"])
        self.assertEqual(info["reason"], "unregistered")
        ghost = next(x for x in ledger.release_manifest("rel-1")["purposes"]["blind"]
                     if x["id"] == "x/ghost")
        self.assertFalse(ghost["registered"])
        self.assertIsNone(ghost["bytes_id"])

    def test_exposure_lock_is_scoped_per_round(self):
        rows = [
            {"step": 1, "kind": "brief", "body": {
                "round": "r1", "min": {"blind": 1}, "stations": {"blind": ["x"]}}},
            {"step": 1, "kind": "brief", "body": {
                "round": "r2", "min": {"train": 1, "blind": 1},
                "stations": {"train": ["x"], "blind": ["x"]}}},
            {"step": 1, "kind": "image", "body": {
                "id": "x/1", "bytes_id": "h1",
                "use": ["train", "blind"], "marked": True}},
            {"step": 1, "kind": "image", "body": {
                "id": "x/2", "bytes_id": "h2",
                "use": ["train", "blind"], "marked": True}},
            {"step": 2, "kind": "receipt", "body": {
                "round": "r1", "release": "old-1",
                "purpose": "blind", "ids": ["x/1"]}},
        ]
        ledger = ledger_from_rows(rows)
        snap = ledger.snapshot()
        # r2 的规划不受 r1 盲测暴露影响（锁按轮次隔离）：r2 训练仍可行
        from benchbank.planner import revise_release
        plan = revise_release(ledger, "r2")
        self.assertTrue(plan["feasible"])
        # 同组不跨用途：训练组与盲测组互不相交
        train_group = set().union(*(snap.group_members(i)
                                    for i in plan["allocation"]["train"]))
        blind_group = set(plan["allocation"]["blind"])
        self.assertFalse(train_group & blind_group)
        # r1 轮次的暴露锁不作用于 r2：r1 盲测图 x/1 所在组在 r2 可被训练评估
        self.assertEqual(plan["blind_exposure"]["received_ids"], [])
        # 但同一轮 r1 内，暴露锁仍然生效：x/1 不能进 r1 训练
        plan_r1 = revise_release(ledger, "r1")
        self.assertNotIn("x/1", plan_r1["allocation"].get("train", []))

    def test_loader_rejects_out_of_order_steps(self):
        with self.assertRaises(ExchangeFormatError):
            parse_lines([
                '{"step": 2, "kind": "brief", "body": {"round": "r", "min": {"train": 1}, "stations": {}}}',
                '{"step": 1, "kind": "image", "body": {"id": "x/1", "bytes_id": "z", "use": ["train"], "marked": true}}',
            ])


if __name__ == "__main__":
    unittest.main()
