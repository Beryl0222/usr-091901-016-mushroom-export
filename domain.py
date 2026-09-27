"""野生菌出口时效链领域层。

只依赖标准库。核心语义见 domain_contract.json 的 invariants：
- 来源关系（拆批/合箱/形态转换）可追溯且数量守恒；
- 目的地规则按计划出运时点选版本，事件驱动使放行结论失效；
- 查验以自然键全局去重，属地与口岸重复上报只记一次、双方留痕；
- 按角色遮蔽商业资料（买方合同、结算价）；
- 拒收按来源比例定位采集批次与农户应付款；
- 鲜品鲜度窗口，冷冻/干制停止鲜度时钟。
"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Any, Callable

EPS = 1e-6

# 查验类别（自然键的业务组成部分）；机构是查验的属性而非类别。
INSPECTION_TYPES = ("检疫查验", "开箱复核", "温度监测")
FORMS = ("鲜品", "冷冻", "干制")
GRADES = ("特等", "一级", "二级", "混级")


def _ts(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _quant(x: float) -> float:
    return round(float(x), 6)


class DomainError(ValueError):
    """业务规则拒绝。"""


class ChainStore:
    """内存聚合根。每个公开方法对应契约中的一个 command 或 view。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = count(1)
        self.event_log: list[dict[str, Any]] = []

        self.farmers: dict[str, dict] = {}
        self.enterprises: dict[str, dict] = {}
        self.officers: dict[str, dict] = {}
        self.points: dict[str, dict] = {}
        self.ports: dict[str, dict] = {}

        self.collections: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}
        self.boxes: dict[str, dict] = {}
        # (目的地, 形态) -> 按生效时间排序的规则版本
        self.rules: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.shipments: dict[str, dict] = {}
        self.certificates: dict[str, dict] = {}
        self.bookings: dict[str, dict] = {}
        self.inspections: dict[tuple, dict] = {}
        self.excursions: dict[str, dict] = {}

    # ---- 基础工具 ----------------------------------------------------------

    def _id(self, prefix: str) -> str:
        return f"{prefix}{next(self._seq):04d}"

    def _record(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {"id": self._id("ev"), "type": kind, "payload": payload}
        self.event_log.append(event)
        return event

    # ---- 参与者 ------------------------------------------------------------

    def register_farmer(self, farmer_id: str, name: str, **_) -> dict:
        with self._lock:
            if farmer_id in self.farmers:
                raise DomainError("菌农已存在")
            self.farmers[farmer_id] = {"id": farmer_id, "name": name}
            return self._record("farmer_registered",
                                {"farmer_id": farmer_id, "name": name})

    def register_enterprise(self, enterprise_id: str, name: str, **_) -> dict:
        with self._lock:
            if enterprise_id in self.enterprises:
                raise DomainError("企业已存在")
            self.enterprises[enterprise_id] = {"id": enterprise_id, "name": name}
            return self._record("enterprise_registered",
                                {"enterprise_id": enterprise_id, "name": name})

    def register_officer(self, officer_id: str, name: str, agency: str, **_) -> dict:
        if agency not in ("属地海关", "口岸海关"):
            raise DomainError("查验人员机构必须是属地海关或口岸海关")
        with self._lock:
            if officer_id in self.officers:
                raise DomainError("查验人员已存在")
            self.officers[officer_id] = {"id": officer_id, "name": name, "agency": agency}
            return self._record("officer_registered",
                                {"officer_id": officer_id, "name": name, "agency": agency})

    def register_collection_point(self, point_id: str, name: str, altitude_m: int, **_) -> dict:
        with self._lock:
            if point_id in self.points:
                raise DomainError("采集点已存在")
            self.points[point_id] = {"id": point_id, "name": name,
                                     "altitude_m": int(altitude_m)}
            return self._record("point_registered",
                                {"point_id": point_id, "name": name,
                                 "altitude_m": int(altitude_m)})

    def register_port(self, port_id: str, name: str, **_) -> dict:
        with self._lock:
            if port_id in self.ports:
                raise DomainError("口岸已存在")
            self.ports[port_id] = {"id": port_id, "name": name, "slots": {}}
            return self._record("port_registered", {"port_id": port_id, "name": name})

    # ---- 采集 → 鉴别 → 交售 → 装箱 ----------------------------------------

    def record_collection(self, farmer_id: str, point_id: str, species: str,
                          quantity: float, collected_at: str,
                          shelf_life_hours: float = 72.0, **_) -> dict:
        """记录一次新鲜松茸采集；鲜度窗口自采集时刻起算。"""
        quantity = float(quantity)
        with self._lock:
            if farmer_id not in self.farmers:
                raise DomainError("菌农不存在")
            if point_id not in self.points:
                raise DomainError("采集点不存在")
            if quantity <= 0:
                raise DomainError("数量必须为正")
            if shelf_life_hours <= 0:
                raise DomainError("鲜度时长必须为正")
            cid = self._id("col")
            self.collections[cid] = {
                "id": cid, "farmer_id": farmer_id, "point_id": point_id,
                "species": species, "quantity": _quant(quantity),
                "collected_at": _ts(collected_at),
                "shelf_life_hours": float(shelf_life_hours),
                "identified_species": None,
                "delivered_quantity": 0.0,
            }
            return self._record("collection_recorded", {
                "collection_id": cid, "farmer_id": farmer_id, "point_id": point_id,
                "species": species, "quantity": _quant(quantity),
                "collected_at": _iso(_ts(collected_at))})

    def identify_species(self, collection_id: str, identified_species: str,
                         officer_id: str, identified_at: str, **_) -> dict:
        with self._lock:
            col = self._require_collection(collection_id)
            officer = self.officers.get(officer_id)
            if not officer or officer["agency"] != "属地海关":
                raise DomainError("菌种鉴别由属地海关人员执行")
            if col["identified_species"] is not None:
                raise DomainError("该采集批次已鉴别")
            col["identified_species"] = identified_species
            return self._record("species_identified", {
                "collection_id": collection_id,
                "identified_species": identified_species,
                "officer_id": officer_id,
                "identified_at": _iso(_ts(identified_at))})

    def deliver(self, collection_id: str, enterprise_id: str, quantity: float,
                delivered_at: str, unit_price: float, buyer_contract: str, **_) -> dict:
        """农户向企业交售。买方合同只挂在批次上，结算视图不返回它。"""
        quantity = float(quantity)
        with self._lock:
            col = self._require_collection(collection_id)
            if enterprise_id not in self.enterprises:
                raise DomainError("加工企业不存在")
            if col["identified_species"] is None:
                raise DomainError("未鉴别的采集批次不得交售")
            if quantity <= 0:
                raise DomainError("交售数量必须为正")
            if unit_price < 0:
                raise DomainError("结算单价不能为负")
            if col["delivered_quantity"] + quantity > col["quantity"] + EPS:
                raise DomainError("交售数量超过采集数量")
            batch_id = self._id("bat")
            self.batches[batch_id] = {
                "id": batch_id, "farmer_id": col["farmer_id"],
                "enterprise_id": enterprise_id, "collection_id": collection_id,
                "species": col["identified_species"], "quantity": _quant(quantity),
                "delivered_at": _ts(delivered_at), "unit_price": float(unit_price),
                "buyer_contract": buyer_contract, "state": "已交售",
                "used_quantity": 0.0,
            }
            col["delivered_quantity"] = _quant(col["delivered_quantity"] + quantity)
            return self._record("delivered", {
                "batch_id": batch_id, "collection_id": collection_id,
                "farmer_id": col["farmer_id"], "enterprise_id": enterprise_id,
                "quantity": _quant(quantity),
                "delivered_at": _iso(_ts(delivered_at))})

    def start_processing(self, batch_ids: list[str], enterprise_id: str, at: str, **_) -> dict:
        with self._lock:
            for bid in batch_ids:
                batch = self.batches.get(bid)
                if not batch or batch["enterprise_id"] != enterprise_id:
                    raise DomainError(f"批次 {bid} 不存在或不属于该企业")
                if batch["state"] != "已交售":
                    raise DomainError(f"批次 {bid} 当前状态 {batch['state']}，不能投入加工")
            for bid in batch_ids:
                self.batches[bid]["state"] = "加工中"
            return self._record("processing_started",
                                {"batch_ids": list(batch_ids), "at": _iso(_ts(at))})

    def pack_box(self, enterprise_id: str, items: list[dict], form: str,
                 grade: str, packed_at: str, box_id: str | None = None,
                 destination: str | None = None, **_) -> dict:
        """把已加工批次的部分数量装入箱。箱持有的 sources 是全部追溯与分摊的根。"""
        with self._lock:
            if form not in FORMS:
                raise DomainError("形态必须是鲜品/冷冻/干制")
            if grade not in GRADES:
                raise DomainError(f"等级必须是 {'/'.join(GRADES)}")
            if not items:
                raise DomainError("装箱至少包含一个批次")
            sources = []
            total = 0.0
            species = None
            for item in items:
                bid = item["batch_id"]
                qty = float(item["quantity"])
                batch = self.batches.get(bid)
                if not batch or batch["enterprise_id"] != enterprise_id:
                    raise DomainError(f"批次 {bid} 不存在或不属于该企业")
                if batch["state"] != "加工中":
                    raise DomainError(f"批次 {bid} 未处于加工中")
                if qty <= 0:
                    raise DomainError("装箱数量必须为正")
                if batch["used_quantity"] + qty > batch["quantity"] + EPS:
                    raise DomainError(f"批次 {bid} 装箱量超过可用量")
                if species is None:
                    species = batch["species"]
                elif batch["species"] != species:
                    raise DomainError("同一箱不得混装不同菌种")
                batch["used_quantity"] = _quant(batch["used_quantity"] + qty)
                sources.append({"batch_id": bid, "quantity": _quant(qty)})
                total += qty
            box_id = box_id or self._id("box")
            if box_id in self.boxes:
                raise DomainError("箱码已存在")
            self.boxes[box_id] = {
                "id": box_id, "enterprise_id": enterprise_id, "species": species,
                "form": form, "grade": grade, "quantity": _quant(total),
                "sources": sources, "packed_at": _ts(packed_at),
                "frozen_or_dried_at": None, "state": "待查验",
                "destination": destination, "certificate_id": None,
                "shipment_id": None, "rejection": None,
            }
            return self._record("box_packed", {
                "box_id": box_id, "enterprise_id": enterprise_id, "form": form,
                "grade": grade, "quantity": _quant(total), "sources": sources,
                "packed_at": _iso(_ts(packed_at))})

    def split_box(self, box_id: str, enterprise_id: str, parts: list[float], at: str, **_) -> dict:
        """拆批：按数量比例拆分每个来源贡献，前后守恒。新箱是新实体，重走查验放行。"""
        with self._lock:
            box = self._require_box(box_id, enterprise_id)
            parts = [float(p) for p in parts]
            if len(parts) < 2 or any(p <= 0 for p in parts):
                raise DomainError("拆批至少两个正数分量")
            if abs(sum(parts) - box["quantity"]) > 1e-4:
                raise DomainError("拆批分量之和必须等于原箱数量")
            if box["state"] in ("运输中", "退运处置"):
                raise DomainError("运输中或已退运箱不能拆批")
            new_ids = []
            for qty in parts:
                ratio = qty / box["quantity"]
                nid = self._id("box")
                self.boxes[nid] = {
                    "id": nid, "enterprise_id": box["enterprise_id"],
                    "species": box["species"], "form": box["form"],
                    "grade": box["grade"], "quantity": _quant(qty),
                    "sources": [{"batch_id": s["batch_id"],
                                 "quantity": _quant(s["quantity"] * ratio)}
                                for s in box["sources"]],
                    "packed_at": box["packed_at"],
                    "frozen_or_dried_at": box["frozen_or_dried_at"],
                    "state": "待查验",
                    "destination": box["destination"],
                    "certificate_id": None,
                    "shipment_id": None,
                    "rejection": None,
                    "parent_id": box_id,
                }
                new_ids.append(nid)
            box["state"] = "已拆分"
            box["split_into"] = new_ids
            self._discard_release(box, "拆批")
            return self._record("box_split",
                                {"box_id": box_id, "new_box_ids": new_ids,
                                 "at": _iso(_ts(at))})

    def merge_boxes(self, box_ids: list[str], enterprise_id: str, at: str, **_) -> dict:
        """合箱：来源贡献相加，前后守恒；放行结论不随箱转移。"""
        with self._lock:
            if len(box_ids) < 2:
                raise DomainError("合箱至少两箱")
            if len(set(box_ids)) != len(box_ids):
                raise DomainError("合箱清单有重复箱")
            bases = [self._require_box(bid, enterprise_id) for bid in box_ids]
            for b in bases:
                if b["state"] in ("运输中", "退运处置", "已拆分", "已合箱"):
                    raise DomainError(f"箱 {b['id']} 状态 {b['state']}，不能合箱")
            species, form = bases[0]["species"], bases[0]["form"]
            if any(b["species"] != species for b in bases):
                raise DomainError("只能合箱相同菌种")
            if any(b["form"] != form for b in bases):
                raise DomainError("只能合箱相同形态（鲜/冷冻/干制）")
            merged: dict[str, float] = defaultdict(float)
            total = 0.0
            for b in bases:
                for s in b["sources"]:
                    merged[s["batch_id"]] += s["quantity"]
                total += b["quantity"]
                b["state"] = "已合箱"
                self._discard_release(b, "合箱")
            nid = self._id("box")
            self.boxes[nid] = {
                "id": nid, "enterprise_id": enterprise_id, "species": species,
                "form": form, "grade": "混级", "quantity": _quant(total),
                "sources": [{"batch_id": k, "quantity": _quant(v)}
                            for k, v in sorted(merged.items())],
                "packed_at": min(b["packed_at"] for b in bases),
                "frozen_or_dried_at": next((b["frozen_or_dried_at"] for b in bases
                                            if b["frozen_or_dried_at"]), None),
                "state": "待查验", "destination": None,
                "certificate_id": None, "shipment_id": None,
                "rejection": None, "merged_from": list(box_ids),
            }
            return self._record("boxes_merged", {
                "new_box_id": nid, "box_ids": list(box_ids),
                "quantity": _quant(total), "at": _iso(_ts(at))})

    def convert_form(self, box_id: str, enterprise_id: str, new_form: str, at: str, **_) -> dict:
        """鲜品转冷冻/干制：鲜度时钟在转换时刻停止，旧结论失效，重走目的地规则。"""
        with self._lock:
            box = self._require_box(box_id, enterprise_id)
            if new_form not in ("冷冻", "干制"):
                raise DomainError("只能转为冷冻或干制")
            if box["form"] == new_form:
                raise DomainError("形态未变化")
            if box["state"] in ("运输中", "退运处置", "已拆分", "已合箱"):
                raise DomainError("该箱当前状态不能转换形态")
            box["form"] = new_form
            box["frozen_or_dried_at"] = _ts(at)
            box["state"] = "待查验"
            self._discard_release(box, f"转为{new_form}")
            return self._record("form_converted", {
                "box_id": box_id, "new_form": new_form, "at": _iso(_ts(at))})

    # ---- 目的地规则 --------------------------------------------------------

    def define_rule(self, destination: str, form: str, version: str,
                    effective_from: str, requirements: list[str],
                    max_temp_c: float | None = None,
                    min_grade: str | None = None,
                    shelf_life_hours_on_departure: float | None = None,
                    effective_to: str | None = None, **_) -> dict:
        """定义某目的地×形态的规则版本及生效时段（左闭右开）。"""
        with self._lock:
            if form not in FORMS:
                raise DomainError("规则形态必须是鲜品/冷冻/干制")
            key = (destination, form)
            start = _ts(effective_from)
            end = _ts(effective_to) if effective_to else None
            if end and end <= start:
                raise DomainError("规则生效结束必须晚于开始")
            for r in self.rules[key]:
                if r["version"] == version:
                    raise DomainError("规则版本已存在")
                # 左闭右开区间重叠：[start,end) ∩ [r.start,r.end) 非空；None 端为正无穷
                far = datetime.max.replace(tzinfo=timezone.utc)
                r_end = r["effective_to"] or far
                new_end = end or far
                if start < r_end and r["effective_from"] < new_end:
                    raise DomainError("同一目的地与形态的规则生效时段不得重叠")
            self.rules[key].append({
                "destination": destination, "form": form, "version": version,
                "effective_from": start, "effective_to": end,
                "requirements": list(requirements),
                "max_temp_c": max_temp_c, "min_grade": min_grade,
                "shelf_life_hours_on_departure": shelf_life_hours_on_departure,
            })
            self.rules[key].sort(key=lambda r: r["effective_from"])
            return self._record("rule_defined", {
                "destination": destination, "form": form, "version": version,
                "effective_from": _iso(start),
                "effective_to": _iso(end) if end else None})

    def _rule_at(self, destination: str, form: str, when: datetime) -> dict | None:
        for r in self.rules.get((destination, form), []):
            if r["effective_from"] <= when and (
                    r["effective_to"] is None or when < r["effective_to"]):
                return r
        return None

    def list_rules(self, destination: str | None = None, **_) -> list[dict]:
        with self._lock:
            out = []
            for (dest, form), versions in self.rules.items():
                if destination and dest != destination:
                    continue
                for r in versions:
                    out.append({
                        "destination": dest, "form": form, "version": r["version"],
                        "effective_from": _iso(r["effective_from"]),
                        "effective_to": _iso(r["effective_to"])
                        if r["effective_to"] else None,
                        "requirements": list(r["requirements"]),
                        "max_temp_c": r["max_temp_c"],
                        "min_grade": r["min_grade"],
                        "shelf_life_hours_on_departure":
                            r["shelf_life_hours_on_departure"],
                    })
            return out

    # ---- 出运计划 / 查验 / 证书 / 口岸 ------------------------------------

    def plan_shipment(self, enterprise_id: str, box_id: str, destination: str,
                      planned_departure: str, route: list[str], flight_no: str, **_) -> dict:
        with self._lock:
            box = self._require_box(box_id, enterprise_id)
            if box["state"] in ("已拆分", "已合箱", "退运处置"):
                raise DomainError("该箱不是可出运实体")
            if not route:
                raise DomainError("路线至少包含一个口岸")
            dep = _ts(planned_departure)
            sid = box.get("shipment_id") or self._id("shp")
            shp = self.shipments.get(sid)
            changed = False
            if shp is None:
                shp = {"id": sid, "enterprise_id": enterprise_id, "box_id": box_id,
                       "destination": destination, "route": list(route),
                       "flight_no": flight_no, "planned_departure": dep,
                       "actual_departure": None, "state": "计划中", "version": 0}
                self.shipments[sid] = shp
                box["shipment_id"] = sid
                box["destination"] = destination
                changed = True
            else:
                if dep != shp["planned_departure"]:
                    shp["planned_departure"] = dep
                    shp["version"] += 1
                    changed = True
                if destination != shp["destination"]:
                    shp["destination"] = destination
                    box["destination"] = destination
                    shp["version"] += 1
                    changed = True
                if route:
                    shp["route"] = list(route)
                if flight_no:
                    shp["flight_no"] = flight_no
            if changed:
                # 出运时点/目的地是规则选择参数，变化即旧结论失效。
                self._discard_release(box, "计划出运时间/目的地变更")
            return self._record("shipment_planned", {
                "shipment_id": sid, "box_id": box_id, "destination": destination,
                "planned_departure": _iso(dep), "route": shp["route"],
                "flight_no": flight_no, "version": shp["version"]})

    def report_inspection(self, box_id: str, officer_id: str, inspection_type: str,
                          result: str, at: str, temperature_c: float | None = None,
                          agency: str | None = None, **_) -> dict:
        """查验上报。

        自然键 =（箱, 查验类别, 当地日期）。查验类别是业务作业（检疫查验等），
        机构是作业属性：属地海关与口岸海关对同一次查验的重复上报只产生一条记录，
        两个机构与上报人都进入留痕。
        """
        with self._lock:
            if box_id not in self.boxes:
                raise DomainError("箱不存在")
            officer = self.officers.get(officer_id)
            if not officer:
                raise DomainError("查验人员不存在")
            if agency is not None and agency != officer["agency"]:
                raise DomainError("上报机构与人员所属机构不一致")
            agency = officer["agency"]
            if inspection_type not in INSPECTION_TYPES:
                raise DomainError(f"查验类别必须是 {'/'.join(INSPECTION_TYPES)}")
            if result not in ("合格", "不合格"):
                raise DomainError("查验结论必须是合格/不合格")
            at_dt = _ts(at)
            key = (box_id, inspection_type, at_dt.date().isoformat())
            existing = self.inspections.get(key)
            if existing is not None:
                if officer_id not in existing["reported_by"]:
                    existing["reported_by"].append(officer_id)
                if agency not in existing["agencies"]:
                    existing["agencies"].append(agency)
                return self._record("inspection_deduplicated", {
                    "inspection_id": existing["id"], "box_id": box_id,
                    "officer_id": officer_id, "agency": agency,
                    "inspection_type": inspection_type, "at": _iso(at_dt)})
            iid = self._id("ins")
            self.inspections[key] = {
                "id": iid, "box_id": box_id, "inspection_type": inspection_type,
                "result": result, "at": at_dt, "temperature_c": temperature_c,
                "agencies": [agency], "reported_by": [officer_id],
            }
            if result == "不合格":
                self._discard_release(self.boxes[box_id],
                                      f"{inspection_type}结论不合格")
            return self._record("inspection_reported", {
                "inspection_id": iid, "box_id": box_id,
                "inspection_type": inspection_type, "result": result,
                "agencies": [agency], "reported_by": [officer_id],
                "at": _iso(at_dt), "temperature_c": temperature_c})

    def issue_certificate(self, box_id: str, officer_id: str, at: str,
                          requirements_checked: list[str] | None = None, **_) -> dict:
        """属地海关签发证书前，必须按计划出运时点通过可行性判定。"""
        with self._lock:
            officer = self.officers.get(officer_id)
            if not officer or officer["agency"] != "属地海关":
                raise DomainError("证书由属地海关签发")
            if box_id not in self.boxes:
                raise DomainError("箱不存在")
            verdict = self._evaluate(box_id, _ts(at))
            if not verdict["feasible"]:
                raise DomainError("可行性判定未通过：" + "；".join(verdict["blockers"]))
            cid = self._id("cert")
            self.certificates[cid] = {
                "id": cid, "box_id": box_id,
                "rule_version": verdict["rule"]["version"],
                "destination": verdict["destination"],
                "form": self.boxes[box_id]["form"],
                "issued_at": _ts(at), "officer_id": officer_id,
                "requirements_checked": (
                    list(requirements_checked) if requirements_checked is not None
                    else list(verdict["rule"]["requirements"])),
                "valid": True, "invalidation_reason": None,
                "shipment_version": verdict["shipment_version"],
            }
            box = self.boxes[box_id]
            box["certificate_id"] = cid
            box["state"] = "允许出运"
            return self._record("certificate_issued", {
                "certificate_id": cid, "box_id": box_id,
                "rule_version": verdict["rule"]["version"],
                "issued_at": _iso(_ts(at)), "officer_id": officer_id})

    def request_correction(self, certificate_id: str, reason: str, at: str, **_) -> dict:
        with self._lock:
            cert = self.certificates.get(certificate_id)
            if not cert:
                raise DomainError("证书不存在")
            if not cert["valid"]:
                raise DomainError("证书已失效，须重新评估签发")
            box = self.boxes[cert["box_id"]]
            self._discard_release(box, f"证书补正：{reason}", _ts(at))
            return self._record("correction_requested", {
                "certificate_id": certificate_id, "reason": reason,
                "at": _iso(_ts(at))})

    def resolve_correction(self, certificate_id: str, officer_id: str, note: str,
                           at: str, **_) -> dict:
        """补正资料齐全不恢复旧证书；仍须按当前时点重新判定签发。"""
        with self._lock:
            cert = self.certificates.get(certificate_id)
            if not cert:
                raise DomainError("证书不存在")
            cert["correction_resolved"] = {
                "officer_id": officer_id, "note": note, "at": _ts(at)}
            return self._record("correction_resolved", {
                "certificate_id": certificate_id, "note": note,
                "at": _iso(_ts(at))})

    def open_slot(self, port_id: str, slot_from: str, capacity_boxes: int = 100, **_) -> dict:
        with self._lock:
            port = self.ports.get(port_id)
            if not port:
                raise DomainError("口岸不存在")
            slot_id = self._id("slot")
            port["slots"][slot_id] = {"id": slot_id, "port_id": port_id,
                                      "from": _ts(slot_from),
                                      "capacity": int(capacity_boxes), "bookings": []}
            return self._record("slot_opened", {
                "slot_id": slot_id, "port_id": port_id,
                "from": _iso(_ts(slot_from)), "capacity": int(capacity_boxes)})

    def book_port(self, enterprise_id: str, box_id: str, port_id: str,
                  slot_id: str, at: str, **_) -> dict:
        with self._lock:
            box = self._require_box(box_id, enterprise_id)
            slot = self._require_slot(port_id, slot_id)
            if len(slot["bookings"]) >= slot["capacity"]:
                raise DomainError("该时段约满")
            if self._active_booking(box_id):
                raise DomainError("该箱已有有效预约，请改约而非重复预约")
            bid = self._id("bk")
            self.bookings[bid] = {"id": bid, "box_id": box_id, "port_id": port_id,
                                  "slot_id": slot_id, "at": _ts(at),
                                  "active": True, "rebooked_from": None}
            slot["bookings"].append(bid)
            # 预约本身不推翻证书；但把预约锚定到当前评估链路上（事件留痕）。
            self._record("port_booked", {
                "booking_id": bid, "box_id": box_id, "port_id": port_id,
                "slot_id": slot_id, "at": _iso(_ts(at))})
            return self.event_log[-1]

    def rebook_port(self, booking_id: str, new_port_id: str, new_slot_id: str,
                    at: str, **_) -> dict:
        with self._lock:
            booking = self.bookings.get(booking_id)
            if not booking or not booking["active"]:
                raise DomainError("预约不存在或已失效")
            new_slot = self._require_slot(new_port_id, new_slot_id)
            if len(new_slot["bookings"]) >= new_slot["capacity"]:
                raise DomainError("新时段约满")
            old_slot = self.ports[booking["port_id"]]["slots"][booking["slot_id"]]
            old_slot["bookings"] = [b for b in old_slot["bookings"] if b != booking_id]
            booking["active"] = False
            box_id = booking["box_id"]
            bid = self._id("bk")
            self.bookings[bid] = {"id": bid, "box_id": box_id,
                                  "port_id": new_port_id, "slot_id": new_slot_id,
                                  "at": _ts(at), "active": True,
                                  "rebooked_from": booking_id}
            new_slot["bookings"].append(bid)
            # 改约可能改变口岸温度/作业条件，旧放行结论不得沿用。
            self._discard_release(self.boxes[box_id],
                                  f"口岸改约：{booking['port_id']}→{new_port_id}",
                                  _ts(at))
            return self._record("port_rebooked", {
                "booking_id": bid, "rebooked_from": booking_id,
                "box_id": box_id, "new_port_id": new_port_id,
                "new_slot_id": new_slot_id, "at": _iso(_ts(at))})

    def delay_flight(self, shipment_id: str, new_departure: str, reason: str,
                     at: str, **_) -> dict:
        with self._lock:
            shp = self.shipments.get(shipment_id)
            if not shp:
                raise DomainError("出运计划不存在")
            new_dt = _ts(new_departure)
            if new_dt <= shp["planned_departure"]:
                raise DomainError("延误后的起飞时间必须晚于原计划")
            shp["planned_departure"] = new_dt
            shp["version"] += 1
            self._discard_release(self.boxes[shp["box_id"]],
                                  f"航班延误（{reason}），起飞改至 {_iso(new_dt)}",
                                  _ts(at))
            return self._record("flight_delayed", {
                "shipment_id": shipment_id, "new_departure": _iso(new_dt),
                "reason": reason, "at": _iso(_ts(at)), "version": shp["version"]})

    def open_excursion(self, box_id: str, officer_id: str, max_temp_c: float,
                       started_at: str, note: str = "", **_) -> dict:
        with self._lock:
            if box_id not in self.boxes:
                raise DomainError("箱不存在")
            if officer_id not in self.officers:
                raise DomainError("查验人员不存在")
            eid = self._id("exc")
            self.excursions[eid] = {
                "id": eid, "box_id": box_id, "max_temp_c": float(max_temp_c),
                "started_at": _ts(started_at), "resolved_at": None,
                "officer_id": officer_id, "note": note}
            self._discard_release(self.boxes[box_id],
                                  f"温控异常：峰值 {max_temp_c}℃",
                                  _ts(started_at))
            return self._record("excursion_opened", {
                "excursion_id": eid, "box_id": box_id,
                "max_temp_c": float(max_temp_c),
                "started_at": _iso(_ts(started_at)), "note": note})

    def resolve_excursion(self, excursion_id: str, at: str, **_) -> dict:
        """异常处置完成只表示温控恢复，不自动恢复旧放行结论。"""
        with self._lock:
            exc = self.excursions.get(excursion_id)
            if not exc:
                raise DomainError("温控异常不存在")
            if exc["resolved_at"] is not None:
                raise DomainError("该异常已处置")
            exc["resolved_at"] = _ts(at)
            return self._record("excursion_resolved", {
                "excursion_id": excursion_id, "at": _iso(_ts(at))})

    # ---- 放行评估 ----------------------------------------------------------

    def _discard_release(self, box: dict, reason: str, at: datetime | None = None) -> None:
        """使箱当前有效放行结论失效；只在确有结论可失效时留痕。"""
        cid = box.get("certificate_id")
        cert = self.certificates.get(cid) if cid else None
        had_valid = bool(cert and cert["valid"]) or box.get("state") == "允许出运"
        if cert and cert["valid"]:
            cert["valid"] = False
            cert["invalidation_reason"] = reason
            box["certificate_id"] = None
            box["last_certificate_id"] = cid
        if box.get("state") == "允许出运":
            box["state"] = "待查验"
        if had_valid:
            self._record("release_invalidated",
                         {"box_id": box["id"], "reason": reason,
                          "at": _iso(at) if at else None})

    def _freshness(self, box: dict, now: datetime) -> dict:
        if box["form"] != "鲜品":
            return {"clock": "stopped",
                    "stopped_at": _iso(box["frozen_or_dried_at"])
                    if box["frozen_or_dried_at"] else None,
                    "remaining_hours": None, "expired": False}
        cols = [self.collections[self.batches[s["batch_id"]]["collection_id"]]
                for s in box["sources"]]
        oldest = min(c["collected_at"] for c in cols)
        life = min(c["shelf_life_hours"] for c in cols)
        deadline = oldest + timedelta(hours=life)
        remaining = (deadline - now).total_seconds() / 3600.0
        return {"clock": "running", "collected_at": _iso(oldest),
                "deadline": _iso(deadline), "shelf_life_hours": life,
                "remaining_hours": _quant(remaining), "expired": now > deadline}

    def _unresolved_excursion(self, box_id: str) -> dict | None:
        return next((e for e in self.excursions.values()
                     if e["box_id"] == box_id and e["resolved_at"] is None), None)

    def _active_booking(self, box_id: str) -> dict | None:
        return next((b for b in self.bookings.values()
                     if b["box_id"] == box_id and b["active"]), None)

    def _require_slot(self, port_id: str, slot_id: str) -> dict:
        port = self.ports.get(port_id)
        slot = port and port["slots"].get(slot_id)
        if not slot:
            raise DomainError("口岸预约时段不存在")
        return slot

    def _passed_local_quarantine(self, box_id: str, now: datetime) -> bool:
        """合格的检疫查验，且属地海关参与留痕（属地查检职责）。"""
        for ins in self.inspections.values():
            if (ins["box_id"] == box_id and ins["inspection_type"] == "检疫查验"
                    and ins["result"] == "合格" and ins["at"] <= now
                    and "属地海关" in ins["agencies"]):
                return True
        return False

    def _evaluate(self, box_id: str, now: datetime) -> dict[str, Any]:
        box = self.boxes[box_id]
        blockers: list[str] = []
        notices: list[str] = []
        shp = self.shipments.get(box["shipment_id"]) if box.get("shipment_id") else None
        destination = shp["destination"] if shp else box.get("destination")
        dep = shp["planned_departure"] if shp else None

        if box["state"] in ("已拆分", "已合箱"):
            blockers.append("该箱已被拆分/合箱，不是可出运实体")
        if box.get("rejection"):
            blockers.append("已被目的地拒收/退运")
        if not destination:
            blockers.append("尚未确定目的地")
        if shp is None:
            blockers.append("尚未建立出运计划（含计划出运时间）")

        rule = None
        if destination and shp is not None:
            rule = self._rule_at(destination, box["form"], dep)
            if rule is None:
                blockers.append(
                    f"目的地 {destination} 对 {box['form']} 在计划出运时点 "
                    f"{_iso(dep)} 无生效规则")

        fresh = self._freshness(box, now)
        if fresh["clock"] == "running":
            if fresh["expired"]:
                blockers.append("鲜度窗口已越界，鲜品不得放行（可转冷冻/干制后按相应规则重评）")
            elif rule and rule["shelf_life_hours_on_departure"] is not None:
                elapsed_at_dep = (dep - _ts(fresh["collected_at"])).total_seconds() / 3600.0
                remain_at_dep = fresh["shelf_life_hours"] - elapsed_at_dep
                if remain_at_dep < rule["shelf_life_hours_on_departure"] - EPS:
                    blockers.append(
                        f"按计划起飞时点剩余鲜度 {remain_at_dep:.1f}h，低于目的地要求 "
                        f"{rule['shelf_life_hours_on_departure']}h")

        box_excursions = [e for e in self.excursions.values() if e["box_id"] == box_id]
        unresolved = [e for e in box_excursions if e["resolved_at"] is None]
        if unresolved:
            peak = max(e["max_temp_c"] for e in unresolved)
            blockers.append(f"温控异常未处置（峰值 {peak}℃）")
            if rule and rule["max_temp_c"] is not None and peak > rule["max_temp_c"]:
                blockers.append(
                    f"温度峰值超过 {destination} {rule['version']} 限值 "
                    f"{rule['max_temp_c']}℃")
        elif rule and rule["max_temp_c"] is not None:
            # 已处置的异常不阻断流程，但若峰值曾超目的地限值，货物已越限，仍不得放行。
            peak = max((e["max_temp_c"] for e in box_excursions), default=None)
            if peak is not None and peak > rule["max_temp_c"]:
                blockers.append(
                    f"温控记录峰值 {peak}℃ 超过限值 {rule['max_temp_c']}℃")
        if rule and rule["max_temp_c"] is not None:
            for ins in self.inspections.values():
                if (ins["box_id"] == box_id and ins["temperature_c"] is not None
                        and ins["temperature_c"] > rule["max_temp_c"]):
                    blockers.append(
                        f"查验记录温度 {ins['temperature_c']}℃ 超过限值 "
                        f"{rule['max_temp_c']}℃")
                    break

        if rule and rule.get("min_grade"):
            order = {"特等": 4, "一级": 3, "二级": 2, "混级": 1}
            if order.get(box["grade"], 0) < order.get(rule["min_grade"], 99):
                blockers.append(f"等级 {box['grade']} 低于目的地要求 {rule['min_grade']}")

        if not self._passed_local_quarantine(box_id, now):
            blockers.append("缺少属地海关参与的合格检疫查验")

        booking = self._active_booking(box_id)
        if booking is None:
            blockers.append("尚无有效口岸预约")
        elif dep is not None:
            slot = self.ports[booking["port_id"]]["slots"][booking["slot_id"]]
            if slot["from"] > dep:
                blockers.append("口岸预约时段晚于计划起飞时间")

        if box["certificate_id"]:
            cert = self.certificates.get(box["certificate_id"])
        elif box.get("last_certificate_id"):
            cert = self.certificates.get(box["last_certificate_id"])
        else:
            cert = None
        if cert and not cert["valid"]:
            # 提示而非硬阻塞：重新签发正是解除该状态的动作。
            notices.append("原放行结论已失效，须重新评估签发："
                           + (cert["invalidation_reason"] or ""))

        return {
            "box_id": box_id,
            "feasible": not blockers,
            "blockers": blockers,
            "notices": notices,
            "destination": destination,
            "form": box["form"],
            "rule": ({"version": rule["version"],
                      "requirements": list(rule["requirements"]),
                      "max_temp_c": rule["max_temp_c"],
                      "min_grade": rule["min_grade"],
                      "shelf_life_hours_on_departure":
                          rule["shelf_life_hours_on_departure"]} if rule else None),
            "rule_selection_time": _iso(dep) if dep else None,
            "shipment_version": shp["version"] if shp else None,
            "freshness": fresh,
            "active_booking": ({"booking_id": booking["id"],
                                "port_id": booking["port_id"],
                                "slot_id": booking["slot_id"]} if booking else None),
        }

    def evaluate_release(self, box_id: str, at: str, **_) -> dict:
        with self._lock:
            if box_id not in self.boxes:
                raise DomainError("箱不存在")
            verdict = self._evaluate(box_id, _ts(at))
            self._record("release_evaluated", {
                "box_id": box_id, "at": at,
                "feasible": verdict["feasible"], "blockers": verdict["blockers"]})
            return verdict

    def depart(self, box_id: str, enterprise_id: str, at: str, **_) -> dict:
        with self._lock:
            box = self._require_box(box_id, enterprise_id)
            verdict = self._evaluate(box_id, _ts(at))
            if not verdict["feasible"]:
                raise DomainError("当前不允许出运：" + "；".join(verdict["blockers"]))
            box["state"] = "运输中"
            shp = self.shipments[box["shipment_id"]]
            shp["state"] = "在途"
            shp["actual_departure"] = _ts(at)
            return self._record("departed", {
                "box_id": box_id, "at": at,
                "rule_version": verdict["rule"]["version"]})

    def report_rejection(self, box_id: str, officer_id: str, reason: str,
                         at: str, rejected_quantity: float | None = None, **_) -> dict:
        """目的地拒收/退运。可部分拒收；数量按来源结构精确分摊。"""
        with self._lock:
            if officer_id not in self.officers:
                raise DomainError("查验人员不存在")
            box = self.boxes.get(box_id)
            if not box:
                raise DomainError("箱不存在")
            qty = float(rejected_quantity) if rejected_quantity is not None else box["quantity"]
            if qty <= 0 or qty > box["quantity"] + EPS:
                raise DomainError("拒收数量超出箱数量")
            box["rejection"] = {"reason": reason, "at": _ts(at),
                                "officer_id": officer_id, "quantity": _quant(qty)}
            box["state"] = "退运处置"
            if box.get("shipment_id"):
                self.shipments[box["shipment_id"]]["state"] = "退运处置"
            self._discard_release(box, f"目的地拒收/退运：{reason}", _ts(at))
            return self._record("rejection_reported", {
                "box_id": box_id, "reason": reason, "quantity": _quant(qty),
                "at": at})

    # ---- 视图 --------------------------------------------------------------

    def _box_batch_quantities(self, box_id: str, qty: float) -> dict[str, float]:
        """把箱内指定数量（可部分）沿来源结构归并到交售批次，拆批按比例守恒。"""
        totals: dict[str, float] = defaultdict(float)

        def walk(bid: str, q: float) -> None:
            b = self.boxes[bid]
            scale = q / b["quantity"] if b["quantity"] else 0.0
            for s in b["sources"]:
                totals[s["batch_id"]] += s["quantity"] * scale
            for child in b.get("split_into", []):
                walk(child, self.boxes[child]["quantity"])

        walk(box_id, qty)
        return totals

    def _lineage(self, box_id: str) -> list[dict]:
        """展开到原始采集批次（整个箱的来源量）。"""
        totals = self._box_batch_quantities(box_id, self.boxes[box_id]["quantity"])
        per_collection: dict[str, float] = defaultdict(float)
        farmer_of: dict[str, str] = {}
        for bid, q in totals.items():
            batch = self.batches[bid]
            per_collection[batch["collection_id"]] += q
            farmer_of[batch["collection_id"]] = batch["farmer_id"]
        rows = []
        for cid, q in per_collection.items():
            col = self.collections[cid]
            rows.append({"collection_id": cid, "farmer_id": farmer_of[cid],
                         "point_id": col["point_id"],
                         "species": col["identified_species"] or col["species"],
                         "collected_at": _iso(col["collected_at"]),
                         "quantity": _quant(q)})
        return sorted(rows, key=lambda r: r["collection_id"])

    def box_view(self, box_id: str, viewer: dict, now: str) -> dict:
        """箱视图。调度员可见鲜度窗口、阻塞、所用规则与替代路线。"""
        with self._lock:
            box = self.boxes.get(box_id)
            if not box:
                raise DomainError("箱不存在")
            kind = viewer["kind"]
            if kind == "enterprise" and viewer["enterprise_id"] != box["enterprise_id"]:
                raise DomainError("企业之间商业资料互不可见")
            if kind == "farmer":
                owners = {self.batches[s["batch_id"]]["farmer_id"]
                          for s in box["sources"]}
                if viewer["farmer_id"] not in owners:
                    raise DomainError("菌农只能查看本人交售货物")
            if kind == "officer" and viewer["officer_id"] not in self.officers:
                raise DomainError("查验人员不存在")

            verdict = self._evaluate(box_id, _ts(now))
            shp = self.shipments.get(box.get("shipment_id"))
            view = {
                "box_id": box_id,
                "enterprise_id": box["enterprise_id"],
                "species": box["species"], "form": box["form"],
                "grade": box["grade"], "quantity": box["quantity"],
                "state": box["state"],
                "destination": verdict["destination"],
                "freshness": verdict["freshness"],
                "feasibility": {"feasible": verdict["feasible"],
                                "blockers": verdict["blockers"]},
                "rule": verdict["rule"],
                "rule_selection_time": verdict["rule_selection_time"],
                "shipment": ({"shipment_id": shp["id"],
                              "planned_departure": _iso(shp["planned_departure"]),
                              "actual_departure": _iso(shp["actual_departure"])
                              if shp["actual_departure"] else None,
                              "route": shp["route"], "flight_no": shp["flight_no"],
                              "version": shp["version"], "state": shp["state"]}
                             if shp else None),
                "active_booking": verdict["active_booking"],
                "alternative_routes": (
                    self._alternatives(box, shp, _ts(now)) if shp else []),
                "lineage": self._lineage(box_id),
            }
            # ---- 角色遮蔽 -------------------------------------------------
            if kind == "enterprise":
                if viewer["enterprise_id"] != box["enterprise_id"]:
                    raise DomainError("企业之间商业资料互不可见")
                view["commercial"] = {
                    "buyer_contracts": sorted({
                        self.batches[s["batch_id"]]["buyer_contract"]
                        for s in box["sources"]})}
            else:
                # 买方合同只向所属企业出现；查验、调度、菌农均不可见。
                view["commercial"] = None
            if kind == "farmer":
                fid = viewer["farmer_id"]
                view["lineage"] = [r for r in view["lineage"]
                                   if r["farmer_id"] == fid]
                # 他人交售量与企业商务安排不属于菌农视野。
                view["enterprise_id"] = None
            if kind == "officer":
                # 查验人员按职责读取：溯源/查验/规则可见，商务字段不下发。
                view["commercial"] = None
            view["viewer"] = {"kind": kind}
            return view

    def _alternatives(self, box: dict, shp: dict, now: datetime) -> list[dict]:
        """替代路线：其他口岸仍有余位、且能在计划起飞前完成查验的时段。"""
        out = []
        current = self._active_booking(box["id"])
        for port_id, port in self.ports.items():
            for slot in port["slots"].values():
                if len(slot["bookings"]) >= slot["capacity"]:
                    continue
                if slot["from"] > shp["planned_departure"]:
                    continue  # 赶不上起飞
                if current and port_id == current["port_id"] \
                        and slot["id"] == current["slot_id"]:
                    continue
                blockers = []
                rule = self._rule_at(shp["destination"], box["form"],
                                     shp["planned_departure"])
                if rule is None:
                    blockers.append("该出运时点无生效规则")
                fresh = self._freshness(box, now)
                if fresh["clock"] == "running":
                    if fresh["expired"]:
                        blockers.append("鲜度已越界")
                    elif rule and rule["shelf_life_hours_on_departure"] is not None:
                        remain = (fresh["shelf_life_hours"]
                                  - (shp["planned_departure"]
                                     - _ts(fresh["collected_at"])).total_seconds() / 3600.0)
                        if remain < rule["shelf_life_hours_on_departure"] - EPS:
                            blockers.append("起飞时点鲜度不足")
                if self._unresolved_excursion(box["id"]):
                    blockers.append("存在未处置温控异常")
                out.append({"port_id": port_id, "slot_id": slot["id"],
                            "slot_from": _iso(slot["from"]),
                            "rule_version": rule["version"] if rule else None,
                            "feasible": not blockers, "blockers": blockers})
        return out[:10]

    def settlement_view(self, viewer: dict) -> dict:
        """菌农结算：只看本人交售与应付款；拒收/退运量按来源比例扣减；不含买方合同。"""
        with self._lock:
            if viewer["kind"] != "farmer":
                raise DomainError("结算视图仅对菌农开放")
            fid = viewer["farmer_id"]
            # 全市场退运量按批次归并一次。
            rejected_per_batch: dict[str, float] = defaultdict(float)
            for b in self.boxes.values():
                if b["state"] == "退运处置" and b.get("rejection"):
                    for bid, q in self._box_batch_quantities(
                            b["id"], b["rejection"]["quantity"]).items():
                        rejected_per_batch[bid] += q
            rows = []
            total_due = 0.0
            for batch in self.batches.values():
                if batch["farmer_id"] != fid:
                    continue
                rejected_qty = min(batch["quantity"], rejected_per_batch.get(batch["id"], 0.0))
                payable_qty = batch["quantity"] - rejected_qty
                due = payable_qty * batch["unit_price"]
                total_due += due
                rows.append({
                    "batch_id": batch["id"],
                    "collection_id": batch["collection_id"],
                    "species": batch["species"],
                    "quantity": batch["quantity"],
                    "unit_price": batch["unit_price"],
                    "rejected_quantity": _quant(rejected_qty),
                    "payable_quantity": _quant(payable_qty),
                    "due": _quant(due),
                })
            return {"farmer_id": fid, "lines": rows,
                    "total_due": _quant(total_due)}

    def impact_view(self, box_id: str, viewer: dict) -> dict:
        """拒收/退运影响：受影响采集批次、数量与各菌农应付扣减。"""
        with self._lock:
            if viewer["kind"] not in ("dispatcher", "officer", "enterprise"):
                raise DomainError("影响分析不对该角色开放")
            box = self.boxes.get(box_id)
            if not box:
                raise DomainError("箱不存在")
            if viewer["kind"] == "enterprise" \
                    and viewer["enterprise_id"] != box["enterprise_id"]:
                raise DomainError("企业之间商业资料互不可见")
            if not box.get("rejection"):
                return {"box_id": box_id, "rejected": False,
                        "collections": [], "farmers": []}
            rejected_qty = box["rejection"]["quantity"]
            batch_qty = self._box_batch_quantities(box_id, rejected_qty)
            collections: dict[str, float] = defaultdict(float)
            farmers: dict[str, dict[str, float]] = {}
            for bid, q in batch_qty.items():
                batch = self.batches[bid]
                collections[batch["collection_id"]] += q
                f = farmers.setdefault(batch["farmer_id"],
                                       {"rejected_quantity": 0.0,
                                        "payment_deduction": 0.0})
                f["rejected_quantity"] += q
                f["payment_deduction"] += q * batch["unit_price"]
            col_rows = []
            for cid, q in sorted(collections.items()):
                col = self.collections[cid]
                col_rows.append({
                    "collection_id": cid, "farmer_id": col["farmer_id"],
                    "point_id": col["point_id"],
                    "collected_at": _iso(col["collected_at"]),
                    "rejected_quantity": _quant(q)})
            # 应付农户属于结算敏感信息：调度员与所属企业可见，查验人员不可见。
            show_payment = viewer["kind"] in ("dispatcher", "enterprise")
            farmer_rows = [{
                "farmer_id": fid,
                "rejected_quantity": _quant(v["rejected_quantity"]),
                "payment_deduction": _quant(v["payment_deduction"])
                    if show_payment else None,
            } for fid, v in sorted(farmers.items())]
            return {"box_id": box_id, "rejected": True,
                    "reason": box["rejection"]["reason"],
                    "rejected_at": _iso(box["rejection"]["at"]),
                    "rejected_quantity": _quant(rejected_qty),
                    "collections": col_rows, "farmers": farmer_rows}

    # ---- 守卫 --------------------------------------------------------------

    def _require_collection(self, cid: str) -> dict:
        col = self.collections.get(cid)
        if not col:
            raise DomainError("采集批次不存在")
        return col

    def _require_box(self, box_id: str, enterprise_id: str) -> dict:
        box = self.boxes.get(box_id)
        if not box:
            raise DomainError("箱不存在")
        if box["enterprise_id"] != enterprise_id:
            raise DomainError("该箱不属于本企业，禁止操作")
        return box


# ---- 命令分发（HTTP 与测试共用） ------------------------------------------

COMMANDS: dict[str, Callable[..., dict]] = {}
for _name in [
    "register_farmer", "register_enterprise", "register_officer",
    "register_collection_point", "register_port",
    "record_collection", "identify_species", "deliver", "start_processing",
    "pack_box", "split_box", "merge_boxes", "convert_form",
    "define_rule", "plan_shipment", "report_inspection",
    "issue_certificate", "request_correction", "resolve_correction",
    "open_slot", "book_port", "rebook_port",
    "delay_flight", "open_excursion", "resolve_excursion",
    "evaluate_release", "depart", "report_rejection",
]:
    COMMANDS[_name] = getattr(ChainStore, _name)


def dispatch(store: ChainStore, name: str, payload: dict) -> dict:
    fn = COMMANDS.get(name)
    if fn is None:
        raise DomainError(f"未知命令：{name}")
    return fn(store, **(payload or {}))


def conservation_check(store: ChainStore) -> list[str]:
    """守恒自检：活箱对每个批次的来源引用量必须等于该批次累计装箱量且不超量。"""
    problems = []
    referenced: dict[str, float] = defaultdict(float)
    for b in store.boxes.values():
        if b["state"] in ("已拆分", "已合箱"):
            continue  # 退役节点，数量已转移到后继箱
        for s in b["sources"]:
            referenced[s["batch_id"]] += s["quantity"]
    for bid, batch in store.batches.items():
        ref = referenced.get(bid, 0.0)
        used = batch.get("used_quantity", 0.0)
        if abs(ref - used) > 1e-3:
            problems.append(f"批次 {bid} 来源引用 {ref} 与装箱用量 {used} 不一致")
        if ref > batch["quantity"] + 1e-3:
            problems.append(f"批次 {bid} 超量追溯 {ref}>{batch['quantity']}")
    return problems
