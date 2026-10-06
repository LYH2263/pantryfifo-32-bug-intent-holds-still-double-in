from datetime import date
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.modules import replenish_intent as replenish
from app.engines import intent_hold
from app import flows

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]
    pending = [dict(it) for it in c.execute(
        """SELECT r.id, r.item_id, r.qty_short, items.name, items.unit, items.layer
           FROM replenish_intents r JOIN items ON items.id=r.item_id
           WHERE r.status='pending'""")]
    rows = intent_hold.fridge_paint(rows, pending)
    c.close(); return rows

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        if r["expiry"] <= today:
            r["level"] = "expired"
            out.append(r)
        else:
            # simple day diff via fromisoformat
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    try:
        out = flows.inbound_lot(c, body.item_id, body.qty, body.expiry)
    except flows.ItemNotFound:
        c.close(); raise HTTPException(404, "item")
    c.commit(); c.close(); return out

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    try:
        result = flows.consume_item(c, body.item_id, body.qty, body.note)
    except flows.QtyNonPositive:
        c.close(); raise HTTPException(400, "qty_non_positive")
    except flows.Shortage as e:
        # 短量失败:扣减未落地(真实批余量保持失败前);提交本事务里登记的补位意图。
        c.commit(); c.close()
        raise HTTPException(409, {**e.result, "item_id": e.item_id, "intent_id": e.intent_id})
    c.commit(); c.close(); return result

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    ids = flows.sweep_expired(c, date.today().isoformat())
    c.commit(); c.close(); return {"expired_ids": ids}

@app.get("/api/intents")
def intents(status: str | None = None):
    c = connect(); rows = replenish.list_intents(c, status); c.close(); return rows

class ConfirmIn(BaseModel):
    expiry: str | None = None

@app.post("/api/intents/{intent_id}/confirm")
def confirm_intent(intent_id: int, body: ConfirmIn | None = None):
    c = connect()
    try:
        out = replenish.confirm(c, intent_id, body.expiry if body else None)
    except replenish.IntentNotFound:
        c.close(); raise HTTPException(404, "intent_not_found")
    except replenish.ShortNonPositive:
        c.rollback(); c.close(); raise HTTPException(400, "qty_non_positive")
    except replenish.AlreadyFulfilled as e:
        c.rollback(); c.close()
        raise HTTPException(409, {"reason": e.reason, "intent_id": e.intent_id, "lot_id": e.lot_id})
    c.commit(); c.close(); return out

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
