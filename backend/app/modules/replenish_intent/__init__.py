"""短量补位意图 (replenish intent)。

按临期(FEFO)消费因余量不足失败时登记「补位意图」:品项 + 缺口量。

设计决定:未确认意图【不冻结】任何数量。
  - 意图只是本表一行记录,不是 lot:不进 FEFO 候选、不进全层余量、不占真实库存;
  - 正在进行的第二次消费、另一条同品入库只看见真实在架批次,与未确认意图互不争额度;
  - 唯一的「额度」是意图自身的单次状态翻转 pending → fulfilled:
    确认时以条件 UPDATE ... WHERE status='pending' 原子生效一次,
    命中 0 行即已被确认(或并发抢先)→ 抛 AlreadyFulfilled,不会双开新批;
  - 确认成功才插入【唯一一个】新批,qty_in 钉死为登记时的缺口量——
    确认瞬间即便过期下架扫走同品批次、他条入库发生,缺口量也不被改写;
  - confirm 只做翻转 + 插批,【绝不】触碰任何其它真实批次(不扣减、不下架)。

本模块只依赖标准库 sqlite3 连接,便于脱离 FastAPI 直接测试。
"""

from datetime import datetime, timezone

STATUS_PENDING = "pending"
STATUS_FULFILLED = "fulfilled"


class IntentError(Exception):
    reason = "intent_error"


class IntentNotFound(IntentError):
    reason = "intent_not_found"


class AlreadyFulfilled(IntentError):
    reason = "already_fulfilled"

    def __init__(self, intent_id, lot_id=None):
        super().__init__(self.reason)
        self.intent_id = intent_id
        self.lot_id = lot_id


class ShortNonPositive(IntentError):
    reason = "qty_non_positive"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register(c, item_id: int, qty_short: float) -> int:
    """登记补位意图,返回 intent id。缺口量非正数(0/负)拒绝登记。"""
    qty_short = float(qty_short)
    if not qty_short > 0:  # 同时挡住 0、负数、NaN
        raise ShortNonPositive()
    cur = c.execute(
        "INSERT INTO replenish_intents(item_id, qty_short, status, created_at) VALUES (?,?,?,?)",
        (item_id, qty_short, STATUS_PENDING, _now()),
    )
    return cur.lastrowid


def list_intents(c, status: str | None = None) -> list[dict]:
    q = """SELECT r.*, items.name, items.unit, items.layer FROM replenish_intents r
           JOIN items ON items.id = r.item_id"""
    args: list = []
    if status:
        q += " WHERE r.status=?"
        args.append(status)
    q += " ORDER BY r.id DESC"
    return [dict(r) for r in c.execute(q, args)]


def confirm(c, intent_id: int, expiry: str | None = None) -> dict:
    """确认补入:原子翻转状态并插入【唯一】新批。调用方负责 commit/rollback。

    - 意图不存在 → IntentNotFound(状态不变、不插批)
    - 缺口量非正数 → ShortNonPositive(不翻转、不插批)
    - 已确认过 / 被并发抢先 → AlreadyFulfilled(不再增批,绝不双开)

    新批 qty_in / qty_remain 一律取登记时的缺口量,
    与确认瞬间的下架/消费/他条入库完全无关;本函数也不动任何其它批次。
    """
    row = c.execute("SELECT * FROM replenish_intents WHERE id=?", (intent_id,)).fetchone()
    if not row:
        raise IntentNotFound()
    qty = float(row["qty_short"])
    if not qty > 0:
        raise ShortNonPositive()

    # 额度唯一落点:仅当仍为 pending 才能翻转;命中 0 行说明已确认过(或并发抢先)。
    cur = c.execute(
        "UPDATE replenish_intents SET status=?, confirmed_at=? "
        "WHERE id=? AND status=?",
        (STATUS_FULFILLED, _now(), intent_id, STATUS_PENDING),
    )
    if cur.rowcount == 0:
        prior = c.execute("SELECT lot_id FROM replenish_intents WHERE id=?", (intent_id,)).fetchone()
        raise AlreadyFulfilled(intent_id, prior["lot_id"] if prior else None)

    # 新批 qty_in 用登记时的缺口量,与确认瞬间的下架/消费/他条入库无关。
    lot = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (row["item_id"], qty, qty, expiry, "on_shelf", "clean"),
    )
    lot_id = lot.lastrowid
    c.execute(
        "UPDATE replenish_intents SET lot_id=? WHERE id=?",
        (lot_id, intent_id),
    )
    return {
        "ok": True,
        "intent_id": intent_id,
        "lot_id": lot_id,
        "item_id": row["item_id"],
        "qty": qty,
        "status": STATUS_FULFILLED,
    }
