"""短量补位验收测试:登记 → 确认长新批 → 三处对得上(意图状态 / 总表批次 / 新批入库量)。

只用标准库 sqlite3,脱离 FastAPI 直接测模块与引擎:
  python3 -m unittest app.tests.test_replenish_intent -v
"""

import importlib.util
import os
import tempfile
import unittest

from app.engines.fefo import consume_fefo
from app.modules import replenish_intent as ri


class FreshDB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = self._tmp.name
        from app.seed import init_db
        init_db()
        from app.db import connect
        self.c = connect()

    def tearDown(self):
        self.c.close()
        self._tmp.cleanup()

    # --- helpers ---------------------------------------------------------
    def lots(self, item_id=1, status="on_shelf"):
        return [dict(r) for r in self.c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status=?", (item_id, status))]

    def stock_total(self, item_id=1):
        return sum(float(l["qty_remain"]) for l in self.lots(item_id))

    def intent(self, intent_id):
        return dict(self.c.execute(
            "SELECT * FROM replenish_intents WHERE id=?", (intent_id,)).fetchone())


class TestRegister(FreshDB):
    def test_register_positive_gap(self):
        iid = ri.register(self.c, 1, 2.5)
        row = self.intent(iid)
        self.assertEqual(row["status"], ri.STATUS_PENDING)
        self.assertEqual(float(row["qty_short"]), 2.5)

    def test_register_rejects_zero_and_negative(self):
        for bad in (0, 0.0, -1, -0.5):
            with self.assertRaises(ri.ShortNonPositive):
                ri.register(self.c, 1, bad)
        self.assertEqual(
            self.c.execute("SELECT COUNT(*) n FROM replenish_intents").fetchone()["n"], 0)


class TestConfirm(FreshDB):
    def test_confirm_grows_exactly_one_lot_with_registered_gap(self):
        before = len(self.lots(1))
        iid = ri.register(self.c, 1, 2.5)
        out = ri.confirm(self.c, iid, "2026-12-01")
        self.c.commit()
        # 总表条数 +1,新批入库量 == 登记缺口量
        after = self.lots(1)
        self.assertEqual(len(after), before + 1)
        new = [l for l in after if l["id"] == out["lot_id"]]
        self.assertEqual(len(new), 1)
        self.assertEqual(float(new[0]["qty_in"]), 2.5)
        self.assertEqual(float(new[0]["qty_remain"]), 2.5)
        self.assertEqual(new[0]["expiry"], "2026-12-01")
        # 意图页标成已补入
        row = self.intent(iid)
        self.assertEqual(row["status"], ri.STATUS_FULFILLED)
        self.assertEqual(row["lot_id"], out["lot_id"])
        self.assertIsNotNone(row["confirmed_at"])

    def test_double_confirm_cannot_grow_second_batch(self):
        iid = ri.register(self.c, 1, 2.0)
        first = ri.confirm(self.c, iid)
        self.c.commit()
        count = len(self.lots(1))
        total = self.stock_total(1)
        with self.assertRaises(ri.AlreadyFulfilled) as ctx:
            ri.confirm(self.c, iid)
        self.c.rollback()
        # 冲突里带回原新批 id,总表条数与余量保持第一次确认后的样子
        self.assertEqual(ctx.exception.lot_id, first["lot_id"])
        self.assertEqual(len(self.lots(1)), count)
        self.assertEqual(self.stock_total(1), total)

    def test_non_positive_gap_confirm_never_grows(self):
        # 库里被写入零/负缺口(绕过 register):确认不得翻转、不得长批
        for bad in (0, -3):
            cur = self.c.execute(
                "INSERT INTO replenish_intents(item_id, qty_short, status, created_at) VALUES (?,?,?,?)",
                (1, bad, ri.STATUS_PENDING, "2026-10-06T00:00:00+00:00"))
            iid = cur.lastrowid
            self.c.commit()  # 坏缺口先落库,再验证确认路径
            count = len(self.lots(1))
            with self.assertRaises(ri.ShortNonPositive):
                ri.confirm(self.c, iid)
            self.c.rollback()
            self.assertEqual(self.intent(iid)["status"], ri.STATUS_PENDING)
            self.assertEqual(len(self.lots(1)), count)

    def test_confirm_unknown_intent(self):
        with self.assertRaises(ri.IntentNotFound):
            ri.confirm(self.c, 99999)

    def test_sweep_at_confirm_does_not_rewrite_gap(self):
        # 登记缺口后、确认前,同品过期批被收走:新批入库量仍是登记缺口量
        iid = ri.register(self.c, 1, 2.5)
        self.c.execute("UPDATE lots SET status='expired' WHERE item_id=1 AND status='on_shelf'")
        self.assertEqual(self.stock_total(1), 0.0)  # 收走生效
        out = ri.confirm(self.c, iid)
        self.c.commit()
        lot = dict(self.c.execute("SELECT * FROM lots WHERE id=?", (out["lot_id"],)).fetchone())
        self.assertEqual(float(lot["qty_in"]), 2.5)
        self.assertEqual(float(lot["qty_remain"]), 2.5)
        self.assertEqual(self.intent(iid)["status"], ri.STATUS_FULFILLED)


class TestPendingIntentIsolation(FreshDB):
    def test_pending_intent_is_not_a_fefo_candidate(self):
        # 未确认意图不进 FEFO 候选:真实余量不足就必须短量失败,幽灵批不得救场
        ri.register(self.c, 1, 5.0)
        self.c.commit()
        real = self.lots(1)  # 候选只来自真实在架批次
        result = consume_fefo(real, self.stock_total(1) + 5.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "short")
        self.assertAlmostEqual(result["short"], 5.0)
        # 扣减明细只引用真实批次 id
        real_ids = {l["id"] for l in real}
        for d in result["deductions"]:
            self.assertIn(d["lot_id"], real_ids)

    def test_failed_consume_leaves_balance_untouched(self):
        # 失败当次:引擎只给方案不落地,总表余量保持失败前
        before = self.stock_total(1)
        result = consume_fefo(self.lots(1), before + 1.0)
        self.assertFalse(result["ok"])
        self.assertEqual(self.stock_total(1), before)

    def test_hold_engine_removed(self):
        # 虚占引擎已删除:未确认意图不冻结、不展示占用、不进候选
        self.assertIsNone(importlib.util.find_spec("app.engines.intent_hold"))


if __name__ == "__main__":
    unittest.main()
