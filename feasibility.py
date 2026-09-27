"""放行可行性：评估、失效重判、阻塞分析与替代路线。"""
from datetime import timedelta

from domain import Assessment, now_utc, shipment_deadline, shipment_form
from rules import find_flights, select_rule

# 这些事件出现后，此前的放行结论即失效，必须重新评估
STALE_KINDS = {"cert_issued", "cert_corrected", "booking_made", "booking_changed",
               "flight_delayed", "temp_anomaly", "temp_resolved", "inspection_recorded",
               "shipment_changed"}


def latest_assessment(store, shipment_id):
    for assessment in reversed(store.assessments):
        if assessment.shipment_id == shipment_id:
            return assessment
    return None


def is_stale(store, shipment_id, assessment):
    """评估之后出现影响结论的事件即失效，不得沿用。"""
    return any(event.shipment_id == shipment_id and event.kind in STALE_KINDS
               and event.at > assessment.at for event in store.events)


def valid_cert_types(store, shipment_id):
    return {cert.cert_type for cert in store.certs.values()
            if cert.shipment_id == shipment_id and cert.state == "有效"}


def inspection_passed(store, shipment_id, checkpoint):
    return any(i.shipment_id == shipment_id and i.checkpoint == checkpoint and i.result == "通过"
               for i in store.inspections.values())


def active_booking(store, shipment_id):
    for booking in store.bookings.values():
        if booking.shipment_id == shipment_id and booking.state == "有效":
            return booking
    return None


def unresolved_anomalies(store, shipment_id):
    return [r for r in store.temps
            if r.shipment_id == shipment_id and r.anomaly and not r.resolved]


def assess(store, shipment, now=None):
    """按当前单据与出运时点生效的目的地规则，重新评估放行可行性。"""
    now = now or now_utc()
    reasons = []
    form = shipment_form(store, shipment)
    if form is None:
        reasons.append("箱内产品形态不一致，不得混装出运")
    rule = (select_rule(store.rules, shipment.destination, form, shipment.planned_departure)
            if form else None)
    deadline = shipment_deadline(store, shipment)
    arrival = shipment.planned_departure + timedelta(hours=shipment.transit_hours)
    if rule is None:
        reasons.append(f"{shipment.destination}{form or ''}在计划出运日无适用检疫规则")
    else:
        for cert_type in rule.required_certs:
            if cert_type not in valid_cert_types(store, shipment.shipment_id):
                reasons.append(f"缺少有效{cert_type}")
        if not inspection_passed(store, shipment.shipment_id, "属地"):
            reasons.append("属地查检未通过")
        if rule.needs_port_inspection and not inspection_passed(store, shipment.shipment_id, "口岸"):
            reasons.append("口岸查验未通过")
        if shipment.transit_hours > rule.max_transit_hours:
            reasons.append(f"运输时长{shipment.transit_hours:g}小时超出规则上限{rule.max_transit_hours:g}小时")
    if unresolved_anomalies(store, shipment.shipment_id):
        reasons.append("存在未处置的温控异常")
    if arrival > deadline:
        reasons.append("预计到达晚于鲜度/保质期限")
    booking = active_booking(store, shipment.shipment_id)
    if booking is None:
        reasons.append("无有效口岸预约")
    elif booking.departure != shipment.planned_departure or booking.flight_no != shipment.flight_no:
        reasons.append("口岸预约与出运计划不一致，需改约或更正")
    if shipment.state == "待查验" and now > shipment.planned_departure:
        reasons.append("计划出运时间已过，货物仍未离港")
    conclusion = "允许出运" if not reasons else "不可出运"
    return Assessment(shipment.shipment_id, now, conclusion, reasons,
                      rule.rule_id if rule else "", deadline)


def release_status(store, shipment, now=None):
    """返回当前有效放行结论；结论缺失或已失效时重新评估，绝不沿用失效结论。"""
    now = now or now_utc()
    current = latest_assessment(store, shipment.shipment_id)
    if current is not None and not is_stale(store, shipment.shipment_id, current):
        return current, False
    fresh = assess(store, shipment, now)
    store.assessments.append(fresh)
    store.log("assessed", shipment.shipment_id, f"评估结论：{fresh.conclusion}", at=now)
    return fresh, True


def alternatives(store, shipment, now=None):
    """为受阻计划寻找替代路线：可改签的航班与口岸，或鲜度窗口内的形态转化。"""
    now = now or now_utc()
    form = shipment_form(store, shipment)
    deadline = shipment_deadline(store, shipment)
    options = []
    for cand in find_flights(shipment.destination, now, deadline):
        rule = select_rule(store.rules, shipment.destination, form, cand["departure"]) if form else None
        if rule is None or cand["transit_hours"] > rule.max_transit_hours:
            continue
        if cand["arrival"] > deadline:
            continue
        if cand["flight_no"] == shipment.flight_no and cand["departure"] == shipment.planned_departure:
            continue
        options.append({"kind": "改签航班", "rule_id": rule.rule_id, **cand})
        if len(options) >= 5:
            break
    if not options and form == "鲜品" and now < deadline:
        options.append({"kind": "转冷冻加工",
                        "note": "鲜度窗口内转为冷冻可将保质期限延长至30天，适用冷冻规则"})
    return options
