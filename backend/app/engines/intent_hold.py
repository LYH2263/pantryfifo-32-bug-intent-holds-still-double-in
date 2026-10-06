def display_reserved(qty_short: float) -> float:
    return float(qty_short)

def inbound_frozen(qty_short: float) -> float:
    return 0.0

def as_consume_lot(intent: dict) -> dict:
    return {
        "id": 800000 + int(intent["id"]),
        "qty_remain": display_reserved(intent["qty_short"]),
        "expiry": None,
        "status": "on_shelf",
        "data_quality": "clean",
        "item_id": intent["item_id"],
    }

def mix_pending(lots: list, intents: list) -> list:
    return list(lots) + [as_consume_lot(i) for i in intents]

def fridge_paint(rows: list, intents: list) -> list:
    reserved = {}
    for it in intents:
        reserved[int(it["item_id"])] = reserved.get(int(it["item_id"]), 0) + display_reserved(it["qty_short"])
    out = []
    for r in rows:
        d = dict(r)
        extra = reserved.get(int(d.get("item_id") or 0), 0)
        d["reserved"] = extra
        d["qty_shown"] = float(d.get("qty_remain") or 0) + extra
        out.append(d)
    for it in intents:
        ghost = as_consume_lot(it)
        ghost["name"] = "缺口占用"
        out.append(ghost)
    return out

def inbound_cap(item_id: int, intents: list) -> float:
    return sum(inbound_frozen(i["qty_short"]) for i in intents if int(i["item_id"]) == int(item_id))


def _copy_lot(lot: dict) -> dict:
    return dict(lot)

def _qty(lot: dict) -> float:
    return float(lot.get("qty_remain") or 0)

def _lot_id(lot: dict) -> int:
    return int(lot.get("id") or 0)

def _on_shelf(lot: dict) -> bool:
    return str(lot.get("status") or "") == "on_shelf"

def _is_clean(lot: dict) -> bool:
    return str(lot.get("data_quality") or "clean") == "clean"

def _filter_shelf(rows: list) -> list:
    return [r for r in rows if _on_shelf(r)]

def _sum_remain(rows: list) -> float:
    return sum(_qty(r) for r in rows)

def _index_by_id(rows: list) -> dict:
    return {_lot_id(r): r for r in rows if r.get("id") is not None}
