"""修订发布与覆盖缺口测试：暴露锁、延续分配、拆分解锁、承诺堵塞。"""

import json
import tempfile
import unittest
from pathlib import Path

from benchbank import Ledger
from benchbank.append import append_rows
from benchbank.planner import coverage_gap, revise_release

DATA = Path(__file__).resolve().parent.parent / "data" / "exchanges.jsonl"


def write_temp(rows: list[dict]) -> Path:
    fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                     encoding="utf-8")
    for row in rows:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    fh.close()
    return Path(fh.name)


def brief(round_id="r1", minimums=None, stations=None):
    return {"step": 1, "kind": "brief", "body": {
        "round": round_id,
        "min": minimums or {"train": 1, "blind": 1},
        "stations": stations or {"train": ["x", "y"], "blind": ["x", "y"]},
    }}


def image(step, image_id, bytes_id, use=("train", "blind"), marked=True):
    return {"step": step, "kind": "image", "body": {
        "id": image_id, "bytes_id": bytes_id, "use": list(use), "marked": marked}}


def receipt(step, round_id, release, purpose, ids):
    return {"step": step, "kind": "receipt", "body": {
        "round": round_id, "release": release, "purpose": purpose, "ids": ids}}


class SampleRevision(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = Ledger.from_path(DATA)

    def test_revision_keeps_unaffected_allocations(self):
        plan = revise_release(self.ledger, "r7")
        self.assertTrue(plan["feasible"])
        # 盲测、调参旧分配不受影响，原样延续
        self.assertEqual(plan["allocation"]["blind"], ["a/21", "b/11"])
        self.assertEqual(plan["allocation"]["tune"], ["b/15"])
        # 训练旧分配 a/11、a/12 因撤回连带失效被摘除
        self.assertNotIn("a/11", plan["allocation"]["train"])
        self.assertNotIn("a/12", plan["allocation"]["train"])
        dropped = {(d["id"], d["purpose"]) for d in plan["dropped_from_prior"]}
        self.assertIn(("a/11", "train"), dropped)
        self.assertIn(("a/12", "train"), dropped)
        # 补齐到最低 2 张，且站点落在 a/b
        self.assertGreaterEqual(len(plan["allocation"]["train"]), 2)
        self.assertTrue(set(plan["stations_by_purpose"]["train"]) <= {"a", "b"})
        # 盲测暴露锁：暴露组的图不得进入训练
        locked = set()
        for members in plan["blind_exposure"]["group_members_at_this_step"].values():
            locked.update(members)
        self.assertFalse(set(plan["allocation"]["train"]) & locked)

    def test_contaminated_comparison_flagged_in_plan(self):
        plan = revise_release(self.ledger, "r7")
        self.assertTrue(plan["invalid_blind_comparisons"])
        self.assertTrue(all(c["blind_comparison_invalid"]
                            for c in plan["invalid_blind_comparisons"]))

    def test_old_release_marked_not_claimable_after_revision(self):
        before = self.ledger.releases()
        issued = next(r for r in before if r["release"] == "issued-7")
        self.assertTrue(issued["claimable"])  # 样例日志里尚无同轮后续发布
        # 模拟修订发布作为新的领用事实登记
        path = write_temp([json.loads(l) for l in
                           DATA.read_text(encoding="utf-8").splitlines()])
        plan = revise_release(Ledger.from_path(path), "r7")
        step = append_rows(path, [{
            "kind": "receipt",
            "body": {"round": "r7", "release": "revised-7",
                     "purpose": "train",
                     "ids": plan["allocation"]["train"]},
        }])
        self.assertEqual(step, 7)
        ledger2 = Ledger.from_path(path)
        old = next(r for r in ledger2.releases() if r["release"] == "issued-7")
        self.assertFalse(old["claimable"])
        self.assertEqual(old["superseded_by"], ["revised-7"])
        new = next(r for r in ledger2.releases() if r["release"] == "revised-7")
        self.assertTrue(new["claimable"])
        # 原包仍能解释当时发了什么
        manifest = ledger2.release_manifest("issued-7")
        self.assertIn("blind", manifest["purposes"])

    def test_coverage_gap_reports_shortfall_and_blocked(self):
        gap = coverage_gap(self.ledger, "r7")
        self.assertEqual(gap["purposes"]["train"]["minimum"], 2)
        self.assertGreaterEqual(gap["purposes"]["train"]["eligible_available"], 2)
        self.assertEqual(gap["purposes"]["tune"]["shortfall"], 0)
        self.assertIn("a/11", gap["invalid_ids"])
        self.assertIn("a/12", gap["invalid_ids"])


class BlindLockSemantics(unittest.TestCase):
    def test_exposed_group_cannot_move_to_same_round_training(self):
        rows = [
            brief(),
            image(1, "x/1", "h1"),
            image(1, "x/2", "h2"),
            image(1, "x/3", "h3"),
            receipt(2, "r1", "rel-1", "blind", ["x/1"]),
            receipt(2, "r1", "rel-1", "train", ["x/2"]),
            # step3 补交证明把训练图与已暴露盲测图连通
            {"step": 3, "kind": "peer", "body": {
                "id": "p1", "left": "x/2", "right": "x/1"}},
        ]
        ledger = Ledger.from_path(write_temp(rows))
        plan = revise_release(ledger, "r1")
        self.assertNotIn("x/2", plan["allocation"].get("train", []))
        drop = next(d for d in plan["dropped_from_prior"] if d["id"] == "x/2")
        self.assertIn("盲测暴露", drop["reason"])
        # x/1 仍在盲测
        self.assertEqual(plan["allocation"]["blind"], ["x/1"])

    def test_corrected_split_releases_never_exposed_part(self):
        rows = [
            brief(minimums={"train": 2, "blind": 1},
                  stations={"train": ["x"], "blind": ["x"]}),
            image(1, "x/1", "h1"),
            image(1, "x/2", "h2"),
            image(1, "x/3", "h3"),
            receipt(2, "r1", "rel-1", "blind", ["x/1"]),
            # 曾把 x/2 与 x/1 连组（x/2 因此暴露过）
            {"step": 3, "kind": "peer", "body": {
                "id": "p1", "left": "x/2", "right": "x/1"}},
            # 授权订正：p1 撤回，x/2 拆出；x/3 从未与暴露侧相连
            {"step": 4, "kind": "alias", "body": {
                "id": "a1", "evidence": "p1"}},
        ]
        ledger = Ledger.from_path(write_temp(rows))
        snap = ledger.snapshot()
        plan = revise_release(ledger, "r1")
        # 拆出且从未在盲测发行中暴露的 x/3 不被扣住
        self.assertIn("x/3", snap.usable_for("train"))
        # x/2 虽已拆开，但它确实在盲测领用同组暴露的历史事实下出现过：
        # 暴露以实际领到的图为准，x/2 本身未被领到，拆开后锁随当前同组关系解除
        locked = set()
        for members in plan["blind_exposure"]["group_members_at_this_step"].values():
            locked.update(members)
        self.assertEqual(locked, {"x/1"})
        self.assertNotIn("x/2", locked)
        self.assertNotIn("x/3", locked)

    def test_renaming_group_does_not_erase_exposure(self):
        # 组没有永久名字；换分组名（换证据表示）不改变暴露事实
        rows = [
            brief(),
            image(1, "x/1", "h1"),
            image(1, "x/2", "h2"),
            receipt(2, "r1", "rel-1", "blind", ["x/1"]),
            {"step": 3, "kind": "peer", "body": {
                "id": "p1", "left": "x/2", "right": "x/1"}},
            {"step": 4, "kind": "alias", "body": {
                "id": "a1", "evidence": "p1"}},
            # 重新提交一条等价证明把两边再次连通
            {"step": 5, "kind": "peer", "body": {
                "id": "p2", "left": "x/2", "right": "x/1"}},
        ]
        plan = revise_release(Ledger.from_path(write_temp(rows)), "r1")
        locked = set()
        for members in plan["blind_exposure"]["group_members_at_this_step"].values():
            locked.update(members)
        self.assertEqual(locked, {"x/1", "x/2"})

    def test_blocked_commitment_is_named_not_silently_degraded(self):
        rows = [
            brief(minimums={"train": 3, "blind": 1},
                  stations={"train": ["z"], "blind": ["x", "y"]}),
            image(1, "x/1", "h1"),
            image(1, "x/2", "h2", use=("train",)),
            image(1, "x/3", "h3", use=("train",)),
            receipt(2, "r1", "rel-1", "blind", ["x/1"]),
        ]
        plan = revise_release(Ledger.from_path(write_temp(rows)), "r1")
        self.assertFalse(plan["feasible"])
        blocked = plan["blocked_commitments"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["purpose"], "train")
        self.assertEqual(blocked[0]["requirement"], "minimum")
        self.assertEqual(blocked[0]["required"], 3)
        self.assertIn("train", blocked[0]["reason"])

    def test_one_group_never_split_across_purposes(self):
        rows = [
            brief(minimums={"train": 2, "blind": 1, "tune": 1},
                  stations={"train": ["x"], "blind": ["x"], "tune": ["x"]}),
            image(1, "x/1", "h1", use=("train", "blind", "tune")),
            image(1, "x/2", "h2", use=("train", "blind", "tune")),
            image(1, "x/3", "h3", use=("train", "blind", "tune")),
            {"step": 1, "kind": "peer", "body": {
                "id": "p1", "left": "x/1", "right": "x/2"}},
        ]
        ledger = Ledger.from_path(write_temp(rows))
        plan = revise_release(ledger, "r1")
        snap = ledger.snapshot()
        used_purpose = {}
        for purpose, ids in plan["allocation"].items():
            for image_id in ids:
                for member in snap.group_members(image_id):
                    used_purpose.setdefault(member, purpose)
        # 同组成员只能落在一个用途
        for purpose, ids in plan["allocation"].items():
            for image_id in ids:
                for member in snap.group_members(image_id):
                    self.assertEqual(used_purpose[member], purpose)


class AppendRegistration(unittest.TestCase):
    def test_append_continues_steps_and_rebuilds(self):
        path = write_temp([
            brief(),
            image(1, "x/1", "h1"),
        ])
        used = append_rows(path, [
            {"kind": "image", "body": {
                "id": "y/9", "bytes_id": "h9",
                "use": ["blind"], "marked": True}},
            {"kind": "receipt", "body": {
                "round": "r1", "release": "rel-2",
                "purpose": "blind", "ids": ["y/9"]}},
        ])
        self.assertEqual(used, 2)
        ledger = Ledger.from_path(path)
        self.assertIn("y/9", ledger.snapshot().images)
        manifest = ledger.release_manifest("rel-2")
        self.assertEqual(manifest["purposes"]["blind"][0]["id"], "y/9")


if __name__ == "__main__":
    unittest.main()
