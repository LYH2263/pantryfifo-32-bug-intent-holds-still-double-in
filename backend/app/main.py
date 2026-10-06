import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots
from app.modules import replenish_intent as replenish
from app.engines import intent_hold

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
        "SELECT id, item_id, qty_short FROM replenish_intents WHERE status='pending'")]
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
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    lots = [dict(r) for r in c.execute(
        "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (body.item_id,))]
    pending = [dict(it) for it in c.execute(
        "SELECT id, item_id, qty_short FROM replenish_intents WHERE item_id=? AND status='pending'",
        (body.item_id,))]
    lots = intent_hold.mix_pending(lots, pending)
    result = consume_fefo(lots, body.qty)
    if not result["ok"] and result["reason"] == "qty_non_positive":
        c.close(); raise HTTPException(400, result["reason"])
    if not result["ok"]:
        # 短量失败:扣减不落地,全层余量保持失败前;只登记补位意图(品项+缺口量)。
        intent_id = replenish.register(c, body.item_id, result["short"])
        c.commit(); c.close()
        raise HTTPException(409, {**result, "item_id": body.item_id, "intent_id": intent_id})
    for d in result["deductions"]:
        c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
        rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
        if rem <= 0:
            c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
    c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
              (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close(); return result

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
    ids = expire_lots(lots, date.today().isoformat())
    for i in ids:
        c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
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
