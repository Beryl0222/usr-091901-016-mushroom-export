"""领域模型：采集批次、农户交售、加工分级、箱码谱系与鲜度窗口。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

FORMS = ("鲜品", "冷冻", "干制")
SPECIES_EXPORT = "松茸"
FRESH_WINDOW = timedelta(hours=72)
FROZEN_SHELF = timedelta(days=30)
DRIED_SHELF = timedelta(days=180)
FORM_TEMP_RANGE = {"鲜品": (0.0, 4.0), "冷冻": (-25.0, -18.0), "干制": (-5.0, 25.0)}
EPS = 1e-6


def now_utc():
    return datetime.now(timezone.utc)


def parse_dt(text):
    """解析 ISO 8601 时间，缺省时区按 UTC。"""
    dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


@dataclass
class Batch:
    """采集批次：采集点、菌种鉴别结论与鲜度起点。"""
    batch_id: str
    farmer_id: str
    enterprise_id: str
    site: str
    altitude_m: int
    species: str
    identified_by: str
    collected_at: datetime
    weight_kg: float

    def fresh_deadline(self):
        return self.collected_at + FRESH_WINDOW


@dataclass
class Delivery:
    """农户交售：结算依据，累计交售不得超过采集量。"""
    delivery_id: str
    batch_id: str
    farmer_id: str
    enterprise_id: str
    weight_kg: float
    price_per_kg: float
    delivered_at: datetime


@dataclass
class Lot:
    """加工批次：分级与形态转化（鲜品/冷冻/干制），inputs 记录来源批次。"""
    lot_id: str
    enterprise_id: str
    inputs: list  # [(batch_id, kg)]
    form: str
    grade: str
    processed_at: datetime
    remaining_kg: float = 0.0

    def __post_init__(self):
        if not self.remaining_kg:
            self.remaining_kg = sum(kg for _, kg in self.inputs)


@dataclass
class Box:
    """箱码：可拆批、可合箱，contents 记录来源节点（加工批次或箱）与重量。"""
    box_id: str
    enterprise_id: str
    contents: list  # [(node_id, kg)]
    packed_at: datetime
    state: str = "在库"  # 在库 / 已拆分 / 已消耗 / 已配载 / 已出运 / 退运处置
    remaining_kg: float = 0.0

    def __post_init__(self):
        if not self.remaining_kg:
            self.remaining_kg = sum(kg for _, kg in self.contents)


@dataclass
class Inspection:
    """查验记录：inspection_no 全局唯一，属地与口岸重复上报只记一次。"""
    inspection_no: str
    shipment_id: str
    checkpoint: str  # 属地 / 口岸
    result: str      # 通过 / 不通过
    inspector: str
    at: datetime


@dataclass
class Certificate:
    """检疫证书：补正后旧证作废，新证生效。"""
    cert_id: str
    shipment_id: str
    cert_type: str
    issued_at: datetime
    state: str = "有效"  # 有效 / 已作废
    superseded_by: str = ""


@dataclass
class Booking:
    """口岸预约：改约后旧预约失效。"""
    booking_id: str
    shipment_id: str
    port: str
    flight_no: str
    departure: datetime
    state: str = "有效"  # 有效 / 已改约


@dataclass
class Shipment:
    """出运计划：目的地、口岸、航班与计划出运时间。"""
    shipment_id: str
    enterprise_id: str
    box_ids: list
    destination: str
    port: str
    flight_no: str
    planned_departure: datetime
    transit_hours: float
    state: str = "待查验"  # 待查验 / 运输中 / 退运处置
    buyer_contract: dict = field(default_factory=dict)


@dataclass
class TempReading:
    """冷链温度记录：超出适用区间即异常，需处置后方可放行。"""
    shipment_id: str
    at: datetime
    celsius: float
    anomaly: bool
    resolved: bool = False
    note: str = ""


@dataclass
class Assessment:
    """某一时点的放行可行性结论；其后出现新事件即失效，不得沿用。"""
    shipment_id: str
    at: datetime
    conclusion: str  # 允许出运 / 不可出运
    reasons: list
    rule_id: str
    deadline: datetime | None


# ---- 库存与谱系 ----

def node_weight(store, node_id):
    """节点（加工批次或箱）的原始总重。"""
    if node_id in store.lots:
        return sum(kg for _, kg in store.lots[node_id].inputs)
    if node_id in store.boxes:
        return sum(kg for _, kg in store.boxes[node_id].contents)
    raise KeyError(f"未知节点 {node_id}")


def trace_to_batches(store, node_id):
    """沿拆批、合箱与形态转化关系，把箱或加工批次折算回采集批次重量。"""
    if node_id in store.lots:
        out = {}
        for batch_id, kg in store.lots[node_id].inputs:
            out[batch_id] = out.get(batch_id, 0.0) + kg
        return out
    if node_id in store.boxes:
        out = {}
        for child_id, kg in store.boxes[node_id].contents:
            total = node_weight(store, child_id)
            ratio = kg / total if total else 0.0
            for batch_id, child_kg in trace_to_batches(store, child_id).items():
                out[batch_id] = out.get(batch_id, 0.0) + child_kg * ratio
        return out
    raise KeyError(f"未知节点 {node_id}")


def lot_deadline(store, lot):
    """加工批次的鲜度/保质期限：鲜品看采集时点，冷冻干制看加工时点。"""
    if lot.form == "鲜品":
        return min(store.batches[batch_id].fresh_deadline() for batch_id, _ in lot.inputs)
    return lot.processed_at + (FROZEN_SHELF if lot.form == "冷冻" else DRIED_SHELF)


def node_deadline(store, node_id):
    if node_id in store.lots:
        return lot_deadline(store, store.lots[node_id])
    if node_id in store.boxes:
        return min(node_deadline(store, child_id) for child_id, _ in store.boxes[node_id].contents)
    raise KeyError(f"未知节点 {node_id}")


def freshness_detail(store, node_id, scale=1.0):
    """逐采集批次列出形态与期限，供调度员查看鲜度构成。"""
    rows = []

    def walk(nid, ratio):
        if nid in store.lots:
            lot = store.lots[nid]
            for batch_id, kg in lot.inputs:
                rows.append({"batch_id": batch_id, "weight_kg": kg * ratio,
                             "form": lot.form, "deadline": lot_deadline(store, lot)})
        elif nid in store.boxes:
            total = node_weight(store, nid)
            for child_id, kg in store.boxes[nid].contents:
                walk(child_id, ratio * (kg / total if total else 0.0))

    walk(node_id, scale)
    return rows


def delivered_weight(store, batch_id):
    return sum(d.weight_kg for d in store.deliveries.values() if d.batch_id == batch_id)


def consumed_weight(store, batch_id):
    return sum(kg for lot in store.lots.values() for bid, kg in lot.inputs if bid == batch_id)


def available_stock(store, batch_id):
    """已交售但尚未加工的库存。"""
    return delivered_weight(store, batch_id) - consumed_weight(store, batch_id)


def validate_lot_inputs(store, form, inputs, processed_at):
    """加工投入校验：菌种须为出口松茸，冷冻干制转化须在鲜度窗口内完成。"""
    if form not in FORMS:
        raise ValueError(f"未知产品形态 {form}")
    for batch_id, kg in inputs:
        batch = store.batches.get(batch_id)
        if batch is None:
            raise ValueError(f"采集批次不存在 {batch_id}")
        if batch.species != SPECIES_EXPORT:
            raise ValueError(f"批次 {batch_id} 鉴别为{batch.species}，不得作为{SPECIES_EXPORT}出口")
        if kg > available_stock(store, batch_id) + EPS:
            raise ValueError(f"批次 {batch_id} 可加工库存不足")
        if processed_at < batch.collected_at:
            raise ValueError(f"加工时间早于批次 {batch_id} 的采集时间")
        if form in ("冷冻", "干制") and processed_at > batch.fresh_deadline():
            raise ValueError(f"批次 {batch_id} 已超出鲜品窗口，不得转{form}")


def shipment_form(store, shipment):
    """出运货物的产品形态；混装多种形态时返回 None。"""
    forms = {row["form"] for box_id in shipment.box_ids for row in freshness_detail(store, box_id)}
    return forms.pop() if len(forms) == 1 else None


def shipment_deadline(store, shipment):
    return min(node_deadline(store, box_id) for box_id in shipment.box_ids)


def shipment_impact(store, shipment):
    """拒收/退运影响：精确折算受影响的采集批次与应付农户结算。"""
    batch_kg = {}
    for box_id in shipment.box_ids:
        box = store.boxes[box_id]
        total = node_weight(store, box_id)
        ratio = box.remaining_kg / total if total else 0.0
        for batch_id, kg in trace_to_batches(store, box_id).items():
            batch_kg[batch_id] = batch_kg.get(batch_id, 0.0) + kg * ratio
    batches = []
    farmers = {}
    for batch_id, kg in sorted(batch_kg.items()):
        batch = store.batches[batch_id]
        deliveries = [d for d in store.deliveries.values() if d.batch_id == batch_id]
        delivered = sum(d.weight_kg for d in deliveries)
        price = (sum(d.price_per_kg * d.weight_kg for d in deliveries) / delivered) if delivered else 0.0
        amount = round(kg * price, 2)
        batches.append({"batch_id": batch_id, "farmer_id": batch.farmer_id, "site": batch.site,
                        "weight_kg": round(kg, 3), "price_per_kg": round(price, 2), "amount": amount})
        entry = farmers.setdefault(batch.farmer_id, {"farmer_id": batch.farmer_id, "weight_kg": 0.0, "amount": 0.0})
        entry["weight_kg"] = round(entry["weight_kg"] + kg, 3)
        entry["amount"] = round(entry["amount"] + amount, 2)
    return {"batches": batches, "farmers": sorted(farmers.values(), key=lambda f: f["farmer_id"])}
