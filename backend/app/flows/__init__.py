"""库存事务流程:把 HTTP 层与 sqlite 事务隔开,只依赖标准库,便于直接单测。

所有函数接收一个已打开的 sqlite3 连接(Row 工厂),自行执行 SQL,
但【不 commit / 不 close】——由调用方(HTTP 端点或测试)决定提交与回滚。
短量等业务失败用异常带出结果,提交策略由调用方掌握。
"""

import json
from datetime import datetime, timezone

from app.engines.fefo import consume_fefo, expire_lots
from app.modules import replenish_intent as replenish


class FlowError(Exception):
    reason = "flow_error"


class ItemNotFound(FlowError):
    reason = "item_not_found"


class QtyNonPositive(FlowError):
    reason = "qty_non_positive"


class Shortage(FlowError):
    """FEFO 短量失败:扣减未落地,已登记补位意图(同一事务,待调用方提交)。"""

    reason = "short"

    def __init__(self, result: dict, item_id: int, intent_id: int):
        super().__init__(self.reason)
        self.result = result
        self.item_id = item_id
        self.intent_id = intent_id


def inbound_lot(c, item_id: int, qty: float, expiry: str | None) -> dict:
    """普通分批入库。未确认意图不冻结额度,所以这里不看任何意图。"""
    if not c.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone():
        raise ItemNotFound()
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (item_id, qty, qty, expiry, "on_shelf", "clean"),
    )
    return {"id": cur.lastrowid}


def consume_item(c, item_id: int, qty: float, note: str = "") -> dict:
    """FEFO 扣减。只认真实在架批,pending 意图不进候选、不会被扣。

    成功:扣减落地 + 写消费流水,返回 result(调用方 commit)。
    短量:真实批余量保持失败前,登记补位意图后抛 Shortage(调用方 commit 意图)。
    """
    lots = [dict(r) for r in c.execute(
        "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0",
        (item_id,),
    )]
    result = consume_fefo(lots, qty)
    if not result["ok"] and result["reason"] == "qty_non_positive":
        raise QtyNonPositive()
    if not result["ok"]:
        # 尚未对任何批次执行 UPDATE:真实批余量维持失败前。仅登记补位意图。
        intent_id = replenish.register(c, item_id, result["short"])
        raise Shortage(result, item_id, intent_id)

    for d in result["deductions"]:
        c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
        rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
        if rem <= 0:
            c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
    c.execute(
        "INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
        (note, json.dumps(result), datetime.now(timezone.utc).isoformat()),
    )
    return result


def sweep_expired(c, today: str) -> list[int]:
    lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
    ids = expire_lots(lots, today)
    for i in ids:
        c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
    return ids
