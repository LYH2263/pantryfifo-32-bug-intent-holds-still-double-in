"""补位意图的【展示】投影:只给界面看,绝不参与任何真实库存计算。

铁律:
  - pending 意图不是 lot。这里产出的占位行带 is_intent=True、status='intent_hold',
    绝不能伪装成 on_shelf 批次,更不能进 FEFO 候选 / 全层真实余量 / 批数统计;
  - 未确认意图【不冻结】入库额度:另一条同品入库照常成功,不存在「一处占着另一处双开」;
  - 真实批上挂的 reserved 只是界面提示数字(有多少同品缺口待补),不改 qty_remain。

消费与入库一律以 lots 表真实在架批次为准,与本投影无关。
"""


def display_reserved(qty_short: float) -> float:
    return float(qty_short)


def as_placeholder(intent: dict) -> dict:
    """把意图投影成一个明确的【占位行】(不是 lot)。"""
    return {
        "id": 800000 + int(intent["id"]),  # 仅作前端 key,与真实 lot id 错开
        "intent_id": int(intent["id"]),
        "is_intent": True,
        "label": "补位占用",
        "name": intent.get("name"),  # 品项名(JOIN 得到,可能为空)
        "item_name": intent.get("name"),
        "unit": intent.get("unit"),
        "layer": intent.get("layer"),
        "item_id": intent["item_id"],
        "qty_remain": display_reserved(intent["qty_short"]),  # 仅展示
        "qty_shown": display_reserved(intent["qty_short"]),
        "reserved": display_reserved(intent["qty_short"]),
        "expiry": None,
        "status": "intent_hold",
        "data_quality": "clean",
    }


def fridge_paint(rows: list, intents: list) -> list:
    """真实在架批 + pending 意图占位行的展示列表(不改真实批余量)。"""
    reserved: dict[int, float] = {}
    for it in intents:
        key = int(it["item_id"])
        reserved[key] = reserved.get(key, 0.0) + display_reserved(it["qty_short"])

    out = []
    for r in rows:
        d = dict(r)
        d["is_intent"] = False
        d["reserved"] = reserved.get(int(d.get("item_id") or 0), 0.0)
        d["qty_shown"] = float(d.get("qty_remain") or 0)
        out.append(d)
    for it in intents:
        out.append(as_placeholder(it))
    return out
