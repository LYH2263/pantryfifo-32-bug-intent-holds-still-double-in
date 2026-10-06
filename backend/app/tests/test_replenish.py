"""短量补位 / 意图冻结 / FEFO 对账测试(只依赖标准库,可脱离 FastAPI 运行)。

运行:cd backend && python3 -m unittest app.tests.test_replenish -v
"""

import os
import sqlite3
import tempfile
import unittest

from app import flows, seed
from app.db import connect
from app.engines import intent_hold
from app.modules import replenish_intent as ri


class Base(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        os.environ["DATA_DIR"] = self._tmpdir.name
        self.c = connect()
        seed.create_schema(self.c)
        self.c.executemany(
            "INSERT INTO items(name,layer,unit) VALUES (?,?,?)",
            [("牛奶", "upper", "盒"), ("鸡蛋", "mid", "个")],
        )
        self.c.commit()

    def tearDown(self):
        self.c.close()

    # --- helpers ---
    def add_lot(self, item_id, qty, expiry, status="on_shelf", dq="clean"):
        cur = self.c.execute(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
            (item_id, qty, qty, expiry, status, dq),
        )
        self.c.commit()
        return cur.lastrowid

    def lots(self, item_id=None, status=None):
        q = "SELECT * FROM lots WHERE 1=1"
        args = []
        if item_id is not None:
            q += " AND item_id=?"; args.append(item_id)
        if status is not None:
            q += " AND status=?"; args.append(status)
        return [dict(r) for r in self.c.execute(q, args)]

    def lot(self, lid):
        return dict(self.c.execute("SELECT * FROM lots WHERE id=?", (lid,)).fetchone())

    def intent(self, iid):
        return dict(self.c.execute("SELECT * FROM replenish_intents WHERE id=?", (iid,)).fetchone())

    def pending(self, item_id=None):
        q = "SELECT * FROM replenish_intents WHERE status='pending'"
        args = []
        if item_id is not None:
            q += " AND item_id=?"; args.append(item_id)
        return [dict(r) for r in self.c.execute(q, args)]


class TestShortageKeepsStockAndRegistersIntent(Base):
    def test_failed_consume_does_not_touch_real_lots(self):
        a = self.add_lot(1, 2, "2026-10-01")
        b = self.add_lot(1, 1, "2026-09-28")
        lots_before = len(self.lots())

        with self.assertRaises(flows.Shortage) as cm:
            flows.consume_item(self.c, 1, 5)
        self.c.commit()

        # 缺口 = 5 - (2+1) = 2
        self.assertEqual(cm.exception.result["short"], 2.0)
        # 真实批余量保持失败前,状态仍是在架,批数不变
        self.assertEqual(self.lot(a)["qty_remain"], 2)
        self.assertEqual(self.lot(a)["status"], "on_shelf")
        self.assertEqual(self.lot(b)["qty_remain"], 1)
        self.assertEqual(len(self.lots()), lots_before)
        # 意图页:一条 pending,缺口量 2
        pend = self.pending(1)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["qty_short"], 2.0)
        self.assertIsNone(pend[0]["lot_id"])


class TestPendingIntentNotInFefo(Base):
    def test_pending_intent_is_not_a_consumable_candidate(self):
        self.add_lot(1, 2, "2026-10-01")
        self.add_lot(1, 1, "2026-09-28")
        with self.assertRaises(flows.Shortage):
            flows.consume_item(self.c, 1, 5)
        self.c.commit()

        # 有缺口 2 的 pending 意图存在,但真实在架量恰为 3:消费 3 必须【成功】,
        # 证明意图既没凑数(否则会算成可满足)也不会被扣掉。
        out = flows.consume_item(self.c, 1, 3)
        self.c.commit()
        self.assertTrue(out["ok"])
        # 扣减只落到真实批(id 很小),绝不落到 800000+ 的意图占位 id
        for d in out["deductions"]:
            self.assertLess(d["lot_id"], 800000)
        self.assertEqual(sum(l["qty_remain"] for l in self.lots(1, "on_shelf")), 0)
        # 意图仍在,pending 数量不变,且没有产生任何 id>=800000 的 lot 行
        self.assertEqual(len(self.pending(1)), 1)
        self.assertFalse(any(l["id"] >= 800000 for l in self.lots()))


class TestConfirmOnce(Base):
    def test_confirm_grows_one_batch_and_reconfirm_is_conflict(self):
        self.add_lot(1, 3, "2026-09-28")
        with self.assertRaises(flows.Shortage) as cm:
            flows.consume_item(self.c, 1, 5)
        self.c.commit()
        iid = cm.exception.intent_id
        lots_before = len(self.lots())

        out = ri.confirm(self.c, iid, "2026-12-01")
        self.c.commit()

        self.assertTrue(out["ok"])
        self.assertEqual(out["qty"], 2.0)
        # 恰好长一批,新批入/余量都 = 登记缺口 2
        self.assertEqual(len(self.lots()), lots_before + 1)
        new = self.lot(out["lot_id"])
        self.assertEqual(new["qty_in"], 2.0)
        self.assertEqual(new["qty_remain"], 2.0)
        self.assertEqual(new["status"], "on_shelf")
        # 意图页标成已补入并回填 lot_id
        got = self.intent(iid)
        self.assertEqual(got["status"], ri.STATUS_FULFILLED)
        self.assertEqual(got["lot_id"], out["lot_id"])
        self.assertEqual(got["qty_short"], 2.0)

        # 同一条再点一次:冲突,不再长批
        with self.assertRaises(ri.AlreadyFulfilled) as acm:
            ri.confirm(self.c, iid, "2026-12-01")
        self.c.rollback()
        self.assertEqual(acm.exception.lot_id, out["lot_id"])
        self.assertEqual(len(self.lots()), lots_before + 1)
        self.assertEqual(len(self.pending(1)), 0)


class TestInboundNotFrozen(Base):
    def test_another_inbound_succeeds_and_confirm_still_one_batch(self):
        self.add_lot(1, 3, "2026-09-28")
        with self.assertRaises(flows.Shortage) as cm:
            flows.consume_item(self.c, 1, 5)
        self.c.commit()
        iid = cm.exception.intent_id
        lots_before = len(self.lots())

        # 意图页已占用缺口 2,但另一条同品入库照常成功(未确认意图不冻额度)
        lid = flows.inbound_lot(self.c, 1, 7, "2027-01-01")["id"]
        self.c.commit()
        self.assertEqual(self.lot(lid)["qty_in"], 7.0)
        self.assertEqual(len(self.lots()), lots_before + 1)
        # 意图仍是 pending,没有被这条入库消费掉
        self.assertEqual(len(self.pending(1)), 1)

        # 确认后只再长一条 2 的新批,不双开;总表条数 = 原始 + 入库1 + 补入1
        out = ri.confirm(self.c, iid, "2026-12-01")
        self.c.commit()
        self.assertEqual(out["qty"], 2.0)
        self.assertEqual(len(self.lots()), lots_before + 2)
        on_shelf_qty = sum(l["qty_remain"] for l in self.lots(1, "on_shelf"))
        # 原始 3(消费失败未扣) + 入库 7 + 补入 2
        self.assertEqual(on_shelf_qty, 3 + 7 + 2)


class TestConfirmPinsQtyAndDoesNotSweep(Base):
    def test_expired_lot_swept_at_confirm_time_does_not_rewrite_new_batch(self):
        # 登记缺口 2
        iid = ri.register(self.c, 1, 2)
        self.c.commit()
        # 同品有一条过期在架批 4,以及一条正常批
        expired = self.add_lot(1, 4, "2026-01-01")
        healthy = self.add_lot(1, 5, "2026-12-01")

        # 确认窗口内过期下架扫走同品批
        ids = flows.sweep_expired(self.c, "2026-10-06")
        self.c.commit()
        self.assertIn(expired, ids)
        self.assertEqual(self.lot(expired)["status"], "expired")

        out = ri.confirm(self.c, iid, "2026-12-15")
        self.c.commit()

        # 新批入库量仍是登记缺口 2,不是被带走的 4
        new = self.lot(out["lot_id"])
        self.assertEqual(new["qty_in"], 2.0)
        self.assertEqual(new["qty_remain"], 2.0)
        # 意图页显示的仍是原缺口 2
        self.assertEqual(self.intent(iid)["qty_short"], 2.0)
        # 被扫走的批维持过期态、量 4;正常批毫发无损
        self.assertEqual(self.lot(expired)["qty_remain"], 4)
        self.assertEqual(self.lot(healthy)["qty_remain"], 5)

    def test_confirm_alone_never_touches_other_lots(self):
        iid = ri.register(self.c, 1, 2)
        self.c.commit()
        expired = self.add_lot(1, 4, "2026-01-01")
        before = self.lot(expired)

        out = ri.confirm(self.c, iid, "2026-12-15")
        self.c.commit()

        after = self.lot(expired)
        self.assertEqual(before, after)  # 确认本身不扣/不下架任何同品批
        self.assertEqual(self.lot(out["lot_id"])["qty_in"], 2.0)


class TestNonPositiveShort(Base):
    def test_register_rejects_zero_and_negative(self):
        with self.assertRaises(ri.ShortNonPositive):
            ri.register(self.c, 1, 0)
        with self.assertRaises(ri.ShortNonPositive):
            ri.register(self.c, 1, -3)
        self.c.rollback()
        self.assertEqual(self.pending(1), [])

    def _force_intent(self, qty):
        cur = self.c.execute(
            "INSERT INTO replenish_intents(item_id,qty_short,status) VALUES (?,?,?)",
            (1, qty, ri.STATUS_PENDING),
        )
        self.c.commit()
        return cur.lastrowid

    def test_confirm_zero_or_negative_grows_no_batch(self):
        lots_before = len(self.lots())
        for bad in (0, -2):
            iid = self._force_intent(bad)
            with self.assertRaises(ri.ShortNonPositive):
                ri.confirm(self.c, iid, "2026-12-15")
            self.c.rollback()
            # 不翻转、不长批
            self.assertEqual(self.intent(iid)["status"], ri.STATUS_PENDING)
            self.assertIsNone(self.intent(iid)["lot_id"])
        self.assertEqual(len(self.lots()), lots_before)

    def test_zero_consume_is_rejected_without_intent(self):
        self.add_lot(1, 3, "2026-12-01")
        with self.assertRaises(flows.QtyNonPositive):
            flows.consume_item(self.c, 1, 0)
        self.c.rollback()
        self.assertEqual(self.pending(1), [])


class TestEndToEndReconciliation(Base):
    def test_failure_to_confirm_reconciles_fridge_intents_and_lots(self):
        self.add_lot(1, 2, "2026-10-01")
        self.add_lot(1, 1, "2026-09-28")

        # 1) 扣减失败进入意图页
        with self.assertRaises(flows.Shortage) as cm:
            flows.consume_item(self.c, 1, 5)
        self.c.commit()
        iid = cm.exception.intent_id
        self.assertEqual(cm.exception.result["short"], 2.0)

        # 2) 失败后总表(在架量/条数)维持失败前
        self.assertEqual(sum(l["qty_remain"] for l in self.lots(1, "on_shelf")), 3)
        real_count = len(self.lots(1, "on_shelf"))

        # 3) 意图页 = pending,且冰箱展示里出现一个明确占位行(不改真实余量)
        pend = ri.list_intents(self.c, ri.STATUS_PENDING)
        self.assertEqual([p["id"] for p in pend], [iid])
        fridge_rows = [dict(r) for r in self.c.execute(
            """SELECT lots.*, items.name, items.layer, items.unit FROM lots
               JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf' AND lots.item_id=1""")]
        painted = intent_hold.fridge_paint(fridge_rows, pend)
        real = [r for r in painted if not r["is_intent"]]
        holds = [r for r in painted if r["is_intent"]]
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["status"], "intent_hold")
        self.assertEqual(holds[0]["qty_remain"], 2.0)
        self.assertEqual(sum(r["qty_shown"] for r in real), 3)  # 真实余量不被加占位
        self.assertTrue(all(r["qty_shown"] == r["qty_remain"] for r in real))

        # 4) 确认补入
        out = ri.confirm(self.c, iid, "2026-12-01")
        self.c.commit()

        # 5) 总表对上新批:条数 +1,在架量 +2
        self.assertEqual(len(self.lots(1, "on_shelf")), real_count + 1)
        self.assertEqual(sum(l["qty_remain"] for l in self.lots(1, "on_shelf")), 5)
        self.assertEqual(self.lot(out["lot_id"])["qty_in"], 2.0)

        # 6) 意图页标成已补入;pending 列表清空,冰箱不再有占位行
        self.assertEqual(self.intent(iid)["status"], ri.STATUS_FULFILLED)
        self.assertEqual(ri.list_intents(self.c, ri.STATUS_PENDING), [])
        pend_after = ri.list_intents(self.c, ri.STATUS_PENDING)
        fridge_rows2 = [dict(r) for r in self.c.execute(
            """SELECT lots.*, items.name, items.layer, items.unit FROM lots
               JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf' AND lots.item_id=1""")]
        painted2 = intent_hold.fridge_paint(fridge_rows2, pend_after)
        self.assertFalse(any(r["is_intent"] for r in painted2))


if __name__ == "__main__":
    unittest.main()
