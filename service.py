"""野生菌出口时效链的时效协同后端服务。"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from access import can_read_enterprise, parse_principal, shipment_view
from domain import (EPS, FORM_TEMP_RANGE, Batch, Booking, Box, Certificate, Delivery,
                    Inspection, Lot, Shipment, TempReading, delivered_weight,
                    freshness_detail, iso, node_deadline, node_weight, now_utc, parse_dt,
                    shipment_form, shipment_impact, trace_to_batches, validate_lot_inputs)
from feasibility import active_booking, alternatives, is_stale, latest_assessment, release_status
from rules import select_rule
from store import build_store

SERVICE_ID = "mushroom-export"
SERVICE_NAME = "野生菌出口时效链"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def req(body, key):
    value = body.get(key)
    if value is None or value == "":
        raise ApiError(400, f"缺少字段 {key}")
    return value


def get_or_404(table, key, label):
    obj = table.get(key)
    if obj is None:
        raise ApiError(404, f"{label}不存在 {key}")
    return obj


def require_enterprise(p):
    if p is None or p.role != "enterprise" or not p.enterprise_id:
        raise ApiError(403, "仅加工企业可执行该操作")
    return p.enterprise_id


def require_read(p, enterprise_id):
    if p is None or not can_read_enterprise(p, enterprise_id):
        raise ApiError(403, "无权查看该资料")


def own_shipment(store, p, shipment_id):
    shipment = get_or_404(store.shipments, shipment_id, "出运计划")
    if shipment.enterprise_id != p.enterprise_id:
        raise ApiError(403, "只能操作本企业的出运计划")
    return shipment


def event_time(body):
    return parse_dt(body["at"]) if body.get("at") else now_utc()


def query_now(query):
    return parse_dt(query["now"][0]) if query.get("now") else None


# ---- 序列化 ----

def batch_dict(b):
    return {"batch_id": b.batch_id, "farmer_id": b.farmer_id, "enterprise_id": b.enterprise_id,
            "site": b.site, "altitude_m": b.altitude_m, "species": b.species,
            "identified_by": b.identified_by, "collected_at": iso(b.collected_at),
            "weight_kg": b.weight_kg, "fresh_deadline": iso(b.fresh_deadline())}


def delivery_dict(d):
    return {"delivery_id": d.delivery_id, "batch_id": d.batch_id, "farmer_id": d.farmer_id,
            "enterprise_id": d.enterprise_id, "weight_kg": d.weight_kg,
            "price_per_kg": d.price_per_kg, "amount": round(d.weight_kg * d.price_per_kg, 2),
            "delivered_at": iso(d.delivered_at)}


def lot_dict(lot):
    return {"lot_id": lot.lot_id, "enterprise_id": lot.enterprise_id, "inputs": lot.inputs,
            "form": lot.form, "grade": lot.grade, "processed_at": iso(lot.processed_at),
            "remaining_kg": round(lot.remaining_kg, 3)}


def box_dict(box):
    return {"box_id": box.box_id, "enterprise_id": box.enterprise_id, "contents": box.contents,
            "packed_at": iso(box.packed_at), "state": box.state,
            "remaining_kg": round(box.remaining_kg, 3)}


def shipment_dict(s):
    return {"shipment_id": s.shipment_id, "enterprise_id": s.enterprise_id, "box_ids": s.box_ids,
            "destination": s.destination, "port": s.port, "flight_no": s.flight_no,
            "planned_departure": iso(s.planned_departure), "transit_hours": s.transit_hours,
            "state": s.state, "buyer_contract": s.buyer_contract}


def assessment_dict(a):
    return {"shipment_id": a.shipment_id, "conclusion": a.conclusion, "reasons": a.reasons,
            "rule_id": a.rule_id, "deadline": iso(a.deadline) if a.deadline else None,
            "assessed_at": iso(a.at)}


def inspection_dict(i):
    return {"inspection_no": i.inspection_no, "shipment_id": i.shipment_id,
            "checkpoint": i.checkpoint, "result": i.result, "inspector": i.inspector,
            "at": iso(i.at)}


def cert_dict(c):
    return {"cert_id": c.cert_id, "shipment_id": c.shipment_id, "cert_type": c.cert_type,
            "issued_at": iso(c.issued_at), "state": c.state, "superseded_by": c.superseded_by}


def booking_dict(b):
    return {"booking_id": b.booking_id, "shipment_id": b.shipment_id, "port": b.port,
            "flight_no": b.flight_no, "departure": iso(b.departure), "state": b.state}


def event_dict(e):
    return {"at": iso(e.at), "kind": e.kind, "shipment_id": e.shipment_id, "summary": e.summary}


def alt_dict(alt):
    out = dict(alt)
    for key in ("departure", "arrival"):
        if key in out:
            out[key] = iso(out[key])
    return out


# ---- 接口处理 ----

def h_health(store, p, body, query):
    return 200, health_payload()


def h_contract(store, p, body, query):
    return 200, load_contract()


def h_rules(store, p, body, query):
    rows = [{"rule_id": r.rule_id, "destination": r.destination, "form": r.form,
             "effective_from": r.effective_from.isoformat(),
             "effective_to": r.effective_to.isoformat() if r.effective_to else None,
             "max_transit_hours": r.max_transit_hours, "temp_range_c": [r.temp_min_c, r.temp_max_c],
             "required_certs": list(r.required_certs),
             "needs_port_inspection": r.needs_port_inspection} for r in store.rules]
    dest, form = query.get("destination", [None])[0], query.get("form", [None])[0]
    if dest:
        rows = [r for r in rows if r["destination"] == dest]
    if form:
        rows = [r for r in rows if r["form"] == form]
    return 200, {"rules": rows}


def h_post_batch(store, p, body, query):
    ent = require_enterprise(p)
    batch_id = body.get("batch_id") or store.next_id("B")
    if batch_id in store.batches:
        raise ApiError(409, f"批次号已存在 {batch_id}")
    batch = Batch(batch_id, req(body, "farmer_id"), ent, req(body, "site"),
                  int(body.get("altitude_m", 0)), req(body, "species"),
                  req(body, "identified_by"), parse_dt(req(body, "collected_at")),
                  float(req(body, "weight_kg")))
    if batch.weight_kg <= 0:
        raise ApiError(400, "采集重量必须为正")
    store.batches[batch_id] = batch
    return 201, batch_dict(batch)


def h_post_delivery(store, p, body, query):
    ent = require_enterprise(p)
    batch = get_or_404(store.batches, req(body, "batch_id"), "采集批次")
    if batch.enterprise_id != ent:
        raise ApiError(403, "只能收购本企业登记的批次")
    weight = float(req(body, "weight_kg"))
    if weight <= 0 or weight > batch.weight_kg - delivered_weight(store, batch.batch_id) + EPS:
        raise ApiError(400, "交售重量超出剩余采集量")
    delivery_id = body.get("delivery_id") or store.next_id("D")
    if delivery_id in store.deliveries:
        raise ApiError(409, f"交售单号已存在 {delivery_id}")
    delivery = Delivery(delivery_id, batch.batch_id, batch.farmer_id, ent, weight,
                        float(req(body, "price_per_kg")), parse_dt(req(body, "delivered_at")))
    store.deliveries[delivery_id] = delivery
    return 201, delivery_dict(delivery)


def h_post_lot(store, p, body, query):
    ent = require_enterprise(p)
    inputs = [(str(item[0]), float(item[1])) for item in req(body, "inputs")]
    if not inputs or any(kg <= 0 for _, kg in inputs):
        raise ApiError(400, "加工投入不能为空且重量为正")
    form, processed_at = req(body, "form"), parse_dt(req(body, "processed_at"))
    for batch_id, _ in inputs:
        batch = get_or_404(store.batches, batch_id, "采集批次")
        if batch.enterprise_id != ent:
            raise ApiError(403, "只能加工本企业收购的批次")
    try:
        validate_lot_inputs(store, form, inputs, processed_at)
    except ValueError as exc:
        raise ApiError(400, str(exc))
    lot_id = body.get("lot_id") or store.next_id("L")
    if lot_id in store.lots:
        raise ApiError(409, f"加工批号已存在 {lot_id}")
    lot = Lot(lot_id, ent, inputs, form, str(body.get("grade", "A")), processed_at)
    store.lots[lot_id] = lot
    return 201, lot_dict(lot)


def h_post_box(store, p, body, query):
    ent = require_enterprise(p)
    contents = [(str(item[0]), float(item[1])) for item in req(body, "contents")]
    if not contents or any(kg <= 0 for _, kg in contents):
        raise ApiError(400, "装箱内容不能为空且重量为正")
    nodes = []
    for node_id, kg in contents:
        node = store.lots.get(node_id) or store.boxes.get(node_id)
        if node is None:
            raise ApiError(404, f"来源节点不存在 {node_id}")
        if node.enterprise_id != ent:
            raise ApiError(403, "只能装本企业的货")
        if isinstance(node, Box) and node.state != "在库":
            raise ApiError(400, f"箱 {node_id} 状态为{node.state}，不可再合箱")
        if kg > node.remaining_kg + EPS:
            raise ApiError(400, f"节点 {node_id} 库存不足")
        nodes.append((node, kg))
    box_id = body.get("box_id") or store.next_id("X")
    if box_id in store.boxes:
        raise ApiError(409, f"箱码已存在 {box_id}")
    for node, kg in nodes:
        node.remaining_kg = round(node.remaining_kg - kg, 6)
        if isinstance(node, Box) and node.remaining_kg <= EPS:
            node.state = "已消耗"
    box = Box(box_id, ent, contents, parse_dt(req(body, "packed_at")))
    store.boxes[box_id] = box
    return 201, box_dict(box)


def h_split_box(store, p, body, query):
    ent = require_enterprise(p)
    box = get_or_404(store.boxes, req(body, "box_id"), "箱")
    if box.enterprise_id != ent:
        raise ApiError(403, "只能拆本企业的箱")
    if box.state != "在库":
        raise ApiError(400, f"箱状态为{box.state}，不可拆分")
    parts = [(item[0] or store.next_id("X"), float(item[1])) for item in req(body, "parts")]
    if not parts or any(kg <= 0 for _, kg in parts):
        raise ApiError(400, "拆分明细不能为空且重量为正")
    if abs(sum(kg for _, kg in parts) - box.remaining_kg) > EPS:
        raise ApiError(400, "拆分须恰好覆盖整箱剩余量")
    for bid, _ in parts:
        if bid in store.boxes:
            raise ApiError(409, f"箱码已存在 {bid}")
    packed_at = parse_dt(body["at"]) if body.get("at") else box.packed_at
    box.remaining_kg, box.state = 0.0, "已拆分"
    children = []
    for bid, kg in parts:
        child = Box(bid, ent, [(box.box_id, kg)], packed_at)
        store.boxes[bid] = child
        children.append(box_dict(child))
    return 201, {"children": children}


def h_trace(store, p, body, query, box_id):
    box = get_or_404(store.boxes, box_id, "箱")
    require_read(p, box.enterprise_id)
    rows = []
    for batch_id, kg in sorted(trace_to_batches(store, box_id).items()):
        batch = store.batches[batch_id]
        rows.append({"batch_id": batch_id, "weight_kg": round(kg, 3),
                     "farmer_id": batch.farmer_id, "site": batch.site,
                     "altitude_m": batch.altitude_m, "species": batch.species,
                     "collected_at": iso(batch.collected_at)})
    return 200, {"box_id": box_id, "batches": rows}


def h_box_status(store, p, body, query, box_id):
    """调度员箱视图：剩余鲜度窗口、当前阻塞、所用规则与替代路线。"""
    box = get_or_404(store.boxes, box_id, "箱")
    require_read(p, box.enterprise_id)
    now = query_now(query) or now_utc()
    deadline = node_deadline(store, box_id)
    total = node_weight(store, box_id)
    scale = box.remaining_kg / total if total else 0.0
    detail = freshness_detail(store, box_id, scale)
    shipment_rows = []
    for s in store.shipments.values():
        if box_id not in s.box_ids:
            continue
        assessment, _ = release_status(store, s, now)
        row = {"shipment_id": s.shipment_id, "destination": s.destination, "state": s.state,
               "planned_departure": iso(s.planned_departure), "conclusion": assessment.conclusion,
               "blockers": assessment.reasons, "rule_id": assessment.rule_id}
        if assessment.reasons:
            row["alternatives"] = [alt_dict(a) for a in alternatives(store, s, now)]
        shipment_rows.append(row)
    return 200, {"box_id": box_id, "state": box.state,
                 "forms": sorted({row["form"] for row in detail}),
                 "remaining_kg": round(box.remaining_kg, 3),
                 "freshness": {"deadline": iso(deadline),
                               "remaining_hours": round((deadline - now).total_seconds() / 3600, 2),
                               "batches": [{"batch_id": r["batch_id"],
                                            "weight_kg": round(r["weight_kg"], 3),
                                            "form": r["form"], "deadline": iso(r["deadline"])}
                                           for r in detail]},
                 "shipments": shipment_rows}


def h_post_inspection(store, p, body, query):
    if p is None or p.role not in ("inspector_local", "inspector_port"):
        raise ApiError(403, "仅查验人员可上报查验")
    checkpoint = "属地" if p.role == "inspector_local" else "口岸"
    shipment = get_or_404(store.shipments, req(body, "shipment_id"), "出运计划")
    no, result = req(body, "inspection_no"), req(body, "result")
    if result not in ("通过", "不通过"):
        raise ApiError(400, "查验结论只能为通过或不通过")
    existing = store.inspections.get(no)
    if existing is not None:
        if existing.shipment_id == shipment.shipment_id and existing.result == result:
            return 200, {"deduplicated": True, "inspection": inspection_dict(existing)}
        raise ApiError(409, "同一查验单号不得登记不同结论")
    insp = Inspection(no, shipment.shipment_id, checkpoint, result,
                      str(body.get("inspector") or p.role), parse_dt(req(body, "at")))
    store.inspections[no] = insp
    store.log("inspection_recorded", shipment.shipment_id, f"{checkpoint}查验{result}", insp.at)
    return 201, {"deduplicated": False, "inspection": inspection_dict(insp)}


def h_post_cert(store, p, body, query):
    if p is None or p.role != "inspector_local":
        raise ApiError(403, "仅属地海关可签发证书")
    shipment = get_or_404(store.shipments, req(body, "shipment_id"), "出运计划")
    cert_id = body.get("cert_id") or store.next_id("C")
    if cert_id in store.certs:
        raise ApiError(409, f"证书号已存在 {cert_id}")
    cert = Certificate(cert_id, shipment.shipment_id, req(body, "cert_type"),
                       parse_dt(req(body, "issued_at")))
    store.certs[cert_id] = cert
    store.log("cert_issued", shipment.shipment_id, f"签发{cert.cert_type}", cert.issued_at)
    return 201, cert_dict(cert)


def h_correct_cert(store, p, body, query):
    if p is None or p.role != "inspector_local":
        raise ApiError(403, "仅属地海关可补正证书")
    old = get_or_404(store.certs, req(body, "cert_id"), "证书")
    if old.state != "有效":
        raise ApiError(400, "证书已作废，不能再次补正")
    new_id = body.get("new_cert_id") or store.next_id("C")
    if new_id in store.certs:
        raise ApiError(409, f"证书号已存在 {new_id}")
    new = Certificate(new_id, old.shipment_id, old.cert_type, parse_dt(req(body, "issued_at")))
    old.state, old.superseded_by = "已作废", new_id
    store.certs[new_id] = new
    store.log("cert_corrected", old.shipment_id,
              f"{old.cert_type}补正：{old.cert_id}→{new_id}", new.issued_at)
    return 201, cert_dict(new)


def h_post_booking(store, p, body, query):
    require_enterprise(p)
    shipment = own_shipment(store, p, req(body, "shipment_id"))
    booking_id = body.get("booking_id") or store.next_id("BK")
    if booking_id in store.bookings:
        raise ApiError(409, f"预约号已存在 {booking_id}")
    booking = Booking(booking_id, shipment.shipment_id, req(body, "port"),
                      req(body, "flight_no"), parse_dt(req(body, "departure")))
    store.bookings[booking_id] = booking
    store.log("booking_made", shipment.shipment_id,
              f"口岸预约{booking.port} {booking.flight_no}", event_time(body))
    return 201, booking_dict(booking)


def h_change_booking(store, p, body, query):
    require_enterprise(p)
    shipment = own_shipment(store, p, req(body, "shipment_id"))
    old = active_booking(store, shipment.shipment_id)
    if old is not None:
        old.state = "已改约"
    booking = Booking(body.get("booking_id") or store.next_id("BK"), shipment.shipment_id,
                      req(body, "port"), req(body, "flight_no"), parse_dt(req(body, "departure")))
    store.bookings[booking.booking_id] = booking
    shipment.port, shipment.flight_no = booking.port, booking.flight_no
    shipment.planned_departure = booking.departure
    store.log("booking_changed", shipment.shipment_id,
              f"改约至{booking.port} {booking.flight_no} {iso(booking.departure)}", event_time(body))
    return 201, booking_dict(booking)


def h_flight_delay(store, p, body, query):
    if p is None or p.role not in ("carrier", "dispatcher"):
        raise ApiError(403, "仅承运方或调度员可登记航班延误")
    flight_no, new_departure = req(body, "flight_no"), parse_dt(req(body, "new_departure"))
    at = event_time(body)
    affected = []
    for s in store.shipments.values():
        if s.flight_no != flight_no or s.state != "待查验":
            continue
        if body.get("departure_date") and s.planned_departure.date().isoformat() != body["departure_date"]:
            continue
        s.planned_departure = new_departure
        store.log("flight_delayed", s.shipment_id, f"航班{flight_no}延误至{iso(new_departure)}", at)
        affected.append(s.shipment_id)
    return 200, {"affected": affected}


def h_post_temp(store, p, body, query):
    if p is None or p.role not in ("carrier", "enterprise"):
        raise ApiError(403, "仅承运方或货主企业可上报温度")
    shipment = get_or_404(store.shipments, req(body, "shipment_id"), "出运计划")
    if p.role == "enterprise" and shipment.enterprise_id != p.enterprise_id:
        raise ApiError(403, "只能上报本企业货物的温度")
    form = shipment_form(store, shipment)
    rule = select_rule(store.rules, shipment.destination, form, shipment.planned_departure) if form else None
    lo, hi = (rule.temp_min_c, rule.temp_max_c) if rule else FORM_TEMP_RANGE.get(form, (-30.0, 30.0))
    celsius, at = float(req(body, "celsius")), event_time(body)
    anomaly = not lo <= celsius <= hi
    store.temps.append(TempReading(shipment.shipment_id, at, celsius, anomaly))
    if anomaly:
        store.log("temp_anomaly", shipment.shipment_id, f"温度{celsius}℃超出{lo}~{hi}℃", at)
    return 201, {"anomaly": anomaly, "temp_range_c": [lo, hi]}


def h_resolve_temp(store, p, body, query):
    if p is None or p.role not in ("enterprise", "inspector_local", "inspector_port"):
        raise ApiError(403, "仅货主企业或查验人员可处置温控异常")
    shipment = get_or_404(store.shipments, req(body, "shipment_id"), "出运计划")
    if p.role == "enterprise" and shipment.enterprise_id != p.enterprise_id:
        raise ApiError(403, "只能处置本企业货物的异常")
    count = 0
    for reading in store.temps:
        if reading.shipment_id == shipment.shipment_id and reading.anomaly and not reading.resolved:
            reading.resolved, reading.note = True, str(body.get("note", ""))
            count += 1
    if count:
        store.log("temp_resolved", shipment.shipment_id, f"处置温控异常{count}条", event_time(body))
    return 200, {"resolved": count}


def h_post_shipment(store, p, body, query):
    ent = require_enterprise(p)
    box_ids = [str(x) for x in req(body, "box_ids")]
    boxes = [get_or_404(store.boxes, bid, "箱") for bid in box_ids]
    for box in boxes:
        if box.enterprise_id != ent:
            raise ApiError(403, "只能出运本企业的箱")
        if box.state != "在库":
            raise ApiError(400, f"箱 {box.box_id} 状态为{box.state}，不可配载")
    shipment_id = body.get("shipment_id") or store.next_id("S")
    if shipment_id in store.shipments:
        raise ApiError(409, f"出运计划号已存在 {shipment_id}")
    shipment = Shipment(shipment_id, ent, box_ids, req(body, "destination"), req(body, "port"),
                        req(body, "flight_no"), parse_dt(req(body, "planned_departure")),
                        float(req(body, "transit_hours")),
                        buyer_contract=dict(body.get("buyer_contract") or {}))
    store.shipments[shipment_id] = shipment
    for box in boxes:
        box.state = "已配载"
    store.log("shipment_changed", shipment_id, "出运计划创建", event_time(body))
    return 201, shipment_view(shipment_dict(shipment), p, ent)


def h_get_shipment(store, p, body, query, shipment_id):
    shipment = get_or_404(store.shipments, shipment_id, "出运计划")
    require_read(p, shipment.enterprise_id)
    data = shipment_view(shipment_dict(shipment), p, shipment.enterprise_id)
    last = latest_assessment(store, shipment_id)
    data["release"] = ({"conclusion": last.conclusion, "assessed_at": iso(last.at),
                        "stale": is_stale(store, shipment_id, last)} if last else None)
    data["events"] = [event_dict(e) for e in
                      sorted(store.shipment_events(shipment_id), key=lambda e: e.at)]
    return 200, data


def h_release(store, p, body, query, shipment_id):
    """当前放行结论：已失效的自动重新评估，不沿用旧结论。"""
    shipment = get_or_404(store.shipments, shipment_id, "出运计划")
    require_read(p, shipment.enterprise_id)
    assessment, reassessed = release_status(store, shipment, query_now(query))
    data = assessment_dict(assessment)
    data["reassessed"] = reassessed
    return 200, data


def h_depart(store, p, body, query, shipment_id):
    require_enterprise(p)
    shipment = own_shipment(store, p, shipment_id)
    if shipment.state != "待查验":
        raise ApiError(400, f"当前状态{shipment.state}不可出运")
    assessment, _ = release_status(store, shipment, event_time(body))
    if assessment.conclusion != "允许出运":
        raise ApiError(409, "放行结论无效：" + "；".join(assessment.reasons))
    shipment.state = "运输中"
    for bid in shipment.box_ids:
        store.boxes[bid].state = "已出运"
    store.log("departed", shipment_id, "冷链出境", event_time(body))
    return 200, shipment_dict(shipment)


def h_reject(store, p, body, query, shipment_id):
    if p is None or p.role not in ("enterprise", "inspector_local", "inspector_port"):
        raise ApiError(403, "仅货主企业或查验人员可登记拒收退运")
    shipment = get_or_404(store.shipments, shipment_id, "出运计划")
    if p.role == "enterprise" and shipment.enterprise_id != p.enterprise_id:
        raise ApiError(403, "只能处置本企业的出运计划")
    if shipment.state == "退运处置":
        raise ApiError(400, "已在退运处置中")
    shipment.state = "退运处置"
    for bid in shipment.box_ids:
        store.boxes[bid].state = "退运处置"
    store.log("rejected", shipment_id, str(body.get("reason", "拒收退运")), event_time(body))
    return 200, {"shipment_id": shipment_id, "state": shipment.state,
                 "impact": shipment_impact(store, shipment)}


def h_settlement(store, p, body, query, farmer_id):
    """农户结算视图：只有交售与应付金额，不暴露买方合同。"""
    if p is None or p.role not in ("settlement", "enterprise"):
        raise ApiError(403, "仅结算岗位或本企业可查看结算")
    rows = [delivery_dict(d) for d in store.deliveries.values()
            if d.farmer_id == farmer_id
            and (p.role == "settlement" or d.enterprise_id == p.enterprise_id)]
    return 200, {"farmer_id": farmer_id, "deliveries": rows,
                 "total_amount": round(sum(r["amount"] for r in rows), 2)}


def h_events(store, p, body, query):
    shipment_id = query.get("shipment_id", [None])[0]
    if shipment_id:
        shipment = get_or_404(store.shipments, shipment_id, "出运计划")
        require_read(p, shipment.enterprise_id)
        events = store.shipment_events(shipment_id)
    else:
        if p is None or not p.is_ops:
            raise ApiError(403, "仅查验或调度可查看全部事件")
        events = store.events
    return 200, {"events": [event_dict(e) for e in sorted(events, key=lambda e: e.at)]}


ROUTES = [
    ("GET", r"/health", False, h_health),
    ("GET", r"/contract", False, h_contract),
    ("GET", r"/rules", True, h_rules),
    ("POST", r"/batches", True, h_post_batch),
    ("POST", r"/deliveries", True, h_post_delivery),
    ("POST", r"/lots", True, h_post_lot),
    ("POST", r"/boxes", True, h_post_box),
    ("POST", r"/boxes/split", True, h_split_box),
    ("GET", r"/boxes/(?P<box_id>[^/]+)/trace", True, h_trace),
    ("GET", r"/boxes/(?P<box_id>[^/]+)/status", True, h_box_status),
    ("POST", r"/inspections", True, h_post_inspection),
    ("POST", r"/certs", True, h_post_cert),
    ("POST", r"/certs/correct", True, h_correct_cert),
    ("POST", r"/bookings", True, h_post_booking),
    ("POST", r"/bookings/change", True, h_change_booking),
    ("POST", r"/flights/delay", True, h_flight_delay),
    ("POST", r"/temperatures", True, h_post_temp),
    ("POST", r"/temperatures/resolve", True, h_resolve_temp),
    ("POST", r"/shipments", True, h_post_shipment),
    ("GET", r"/shipments/(?P<shipment_id>[^/]+)", True, h_get_shipment),
    ("GET", r"/shipments/(?P<shipment_id>[^/]+)/release", True, h_release),
    ("POST", r"/shipments/(?P<shipment_id>[^/]+)/depart", True, h_depart),
    ("POST", r"/shipments/(?P<shipment_id>[^/]+)/reject", True, h_reject),
    ("GET", r"/settlements/(?P<farmer_id>[^/]+)", True, h_settlement),
    ("GET", r"/events", True, h_events),
]

_FALLBACK_STORE = None


def get_store(server):
    """测试服务器未显式挂载仓储时使用进程级仓储。"""
    global _FALLBACK_STORE
    store = getattr(server, "store", None)
    if store is None:
        if _FALLBACK_STORE is None:
            _FALLBACK_STORE = build_store()
        store = _FALLBACK_STORE
    return store


class Handler(BaseHTTPRequestHandler):
    """时效协同后端接口。"""

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            status, payload = self._route(method)
        except ApiError as exc:
            status, payload = exc.status, {"error": exc.message}
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            status, payload = 400, {"error": f"请求无法处理：{exc}"}
        except Exception as exc:  # 保持服务可用
            status, payload = 500, {"error": f"服务内部错误：{exc}"}
        self._send_json(payload, status)

    def _route(self, method):
        path = unquote(urlparse(self.path).path)
        query = parse_qs(urlparse(self.path).query)
        for route_method, pattern, auth, func in ROUTES:
            if route_method != method:
                continue
            match = re.fullmatch(pattern, path)
            if not match:
                continue
            principal = parse_principal(self.headers.get("X-Actor", ""))
            if auth and principal is None:
                raise ApiError(401, "缺少有效身份，请设置 X-Actor 头")
            body = self._json_body() if method == "POST" else {}
            return func(get_store(self.server), principal, body, query, **match.groupdict())
        raise ApiError(404, "资源不存在")

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            raise ApiError(400, "请求体不是有效 JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        store = build_store()
        assert select_rule(store.rules, "日本", "鲜品", now_utc()), "目的地规则缺失"
        print("基础检查通过")
        return
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.store = build_store()
    print(f"{SERVICE_NAME} 启动于 :{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
