"""短量补位端到端:扣减失败 → 意图页 → 确认补入,三处对得上。

覆盖的验收:
  - 失败当次总表余量保持失败前;
  - 未确认意图不进 FEFO 候选、不在总表虚占、不冻结同品入库;
  - 确认才长新批,qty_in == 登记缺口;意图标已补入;总表条数 +1;
  - 同一条再点确认 → 409 already_fulfilled,不再长批;
  - 缺口为零/负 → 确认 400,不长批;
  - 确认瞬间同品过期批被收走,新批入库量不被改写。

需要 fastapi + httpx(TestClient);缺依赖时整文件跳过。
运行: python3 -m unittest app.tests.test_api_replenish_flow -v
"""

import os
import tempfile
import unittest

try:
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db import connect
    from app.seed import init_db
    HAS_API = True
except ImportError:  # pragma: no cover - 无 fastapi 的环境只跑模块级测试
    HAS_API = False


@unittest.skipUnless(HAS_API, "fastapi/httpx not installed")
class TestApiReplenishFlow(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = self._tmp.name
        init_db()
        self.t = TestClient(app)

    def tearDown(self):
        self._tmp.cleanup()

    # --- helpers ---------------------------------------------------------
    def fridge(self, item_id=None):
        rows = self.t.get("/api/fridge").json()
        return [r for r in rows if item_id is None or r["item_id"] == item_id]

    def stock(self, item_id=1):
        return sum(r["qty_remain"] for r in self.fridge(item_id))

    def pos_stock(self, item_id=1):
        # 与消费端口径一致:只有正余量批次是 FEFO 候选(脏数据负批不算)
        return sum(r["qty_remain"] for r in self.fridge(item_id) if r["qty_remain"] > 0)

    def intent_row(self, intent_id):
        return [i for i in self.t.get("/api/intents").json() if i["id"] == intent_id][0]

    def make_intent(self, item_id=1, extra=4.0):
        """制造一次短量失败,返回 (intent_id, 失败前余量)。"""
        before = self.pos_stock(item_id)
        r = self.t.post("/api/consume", json={"item_id": item_id, "qty": before + extra})
        assert r.status_code == 409, r.text
        return r.json()["detail"]["intent_id"], before

    # --- tests -----------------------------------------------------------
    def test_short_consume_registers_intent_and_keeps_balance(self):
        full_before = self.stock()
        iid, _ = self.make_intent()
        self.assertEqual(self.stock(), full_before)  # 失败当次余量保持失败前
        row = self.intent_row(iid)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(float(row["qty_short"]), 4.0)

    def test_fridge_has_no_ghost_or_inflated_rows(self):
        self.make_intent()
        for r in self.fridge():
            self.assertNotIn("缺口", str(r.get("name")))
            self.assertNotIn("qty_shown", r)
            self.assertNotIn("reserved", r)

    def test_pending_intent_not_a_candidate_and_inbound_not_blocked(self):
        iid, before = self.make_intent()
        # 未确认意图不进 FEFO 候选:同量再扣仍短量失败
        r = self.t.post("/api/consume", json={"item_id": 1, "qty": before + 4})
        self.assertEqual(r.status_code, 409)
        self.assertNotEqual(r.json()["detail"]["intent_id"], iid)
        # 另一条同品入库不被冻结,正常成功
        r = self.t.post("/api/lots", json={"item_id": 1, "qty": 10, "expiry": "2026-12-01"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.stock(), before + 10)

    def test_confirm_grows_batch_marks_intent(self):
        iid, _ = self.make_intent()
        cnt = len(self.fridge())
        r = self.t.post(f"/api/intents/{iid}/confirm", json={"expiry": "2026-11-01"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["qty"], 4)  # 新批入库量 == 登记缺口
        lot = [l for l in self.fridge() if l["id"] == body["lot_id"]][0]
        self.assertEqual((lot["qty_in"], lot["qty_remain"]), (4, 4))
        self.assertEqual(len(self.fridge()), cnt + 1)  # 总表条数 +1
        row = self.intent_row(iid)
        self.assertEqual((row["status"], row["lot_id"]), ("fulfilled", body["lot_id"]))

    def test_double_confirm_conflict_no_second_batch(self):
        iid, _ = self.make_intent()
        first = self.t.post(f"/api/intents/{iid}/confirm", json={}).json()
        cnt = len(self.fridge())
        r = self.t.post(f"/api/intents/{iid}/confirm", json={})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["reason"], "already_fulfilled")
        self.assertEqual(r.json()["detail"]["lot_id"], first["lot_id"])
        self.assertEqual(len(self.fridge()), cnt)  # 不再长批

    def test_non_positive_gap_confirm_rejected(self):
        c = connect()
        ids = []
        for bad in (0, -2):
            ids.append(c.execute(
                "INSERT INTO replenish_intents(item_id, qty_short, status, created_at)"
                " VALUES (1,?,'pending','x')", (bad,)).lastrowid)
        c.commit(); c.close()
        cnt = len(self.fridge())
        for iid in ids:
            r = self.t.post(f"/api/intents/{iid}/confirm", json={})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["detail"], "qty_non_positive")
        self.assertEqual(len(self.fridge()), cnt)

    def test_sweep_at_confirm_keeps_registered_gap(self):
        iid, _ = self.make_intent(item_id=2, extra=1.0)  # 鸡蛋 12 → 短 1
        c = connect()
        c.execute("UPDATE lots SET status='expired' WHERE item_id=2")  # 确认瞬间收走同品批
        c.commit(); c.close()
        r = self.t.post(f"/api/intents/{iid}/confirm", json={"expiry": "2026-11-15"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["qty"], 1)  # 入库量仍是登记缺口,不被收走量改写


if __name__ == "__main__":
    unittest.main()
