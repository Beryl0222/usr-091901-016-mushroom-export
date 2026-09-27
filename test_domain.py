"""领域不变量测试：以香格里拉鲜松茸出口日本为主线，覆盖契约全部 invariants。"""
import unittest

from domain import ChainStore, DomainError, conservation_check, dispatch

T0 = "2026-09-10T08:00Z"          # 采集时刻
NOW = "2026-09-10T20:00Z"         # 调度观察时刻（采集后 12h）
DEP = "2026-09-11T12:00Z"         # 计划起飞（采集后 28h，剩余鲜度 44h）
SLOT = "2026-09-11T06:00Z"        # 昆明口岸时段


class MatsutakeFixture:
    """搭建标准主线：两户菌农、两家企业、属地/口岸关员、两条目的地规则。"""

    def __init__(self):
        self.s = ChainStore()
        s = self.s
        s.register_farmer("f1", "扎西")
        s.register_farmer("f2", "卓玛")
        s.register_enterprise("e1", "雪域松茸公司")
        s.register_enterprise("e2", "别家公司")
        s.register_officer("o1", "属地李关", "属地海关")
        s.register_officer("o2", "口岸王关", "口岸海关")
        s.register_collection_point("p1", "香格里拉采集点", altitude_m=3600)
        s.register_port("KMG", "昆明口岸")
        s.register_port("CAN", "广州口岸")

        # f1 采 100kg，f2 采 60kg；鲜度窗口 72h
        s.record_collection("f1", "p1", "待鉴松茸", 100, T0, shelf_life_hours=72)
        s.record_collection("f2", "p1", "待鉴松茸", 60, T0, shelf_life_hours=72)
        self.col1 = s.event_log[-2]["payload"]["collection_id"]
        self.col2 = s.event_log[-1]["payload"]["collection_id"]
        s.identify_species(self.col1, "松茸", "o1", T0)
        s.identify_species(self.col2, "松茸", "o1", T0)

        # 交售：买方合同是企业商业资料
        s.deliver(self.col1, "e1", 100, T0, unit_price=200,
                  buyer_contract="CT-JP-001")
        s.deliver(self.col2, "e1", 60, T0, unit_price=180,
                  buyer_contract="CT-JP-002")
        self.b1 = s.event_log[-2]["payload"]["batch_id"]
        self.b3 = s.event_log[-1]["payload"]["batch_id"]
        s.start_processing([self.b1, self.b3], "e1", T0)

        # 日本鲜品规则两个版本，生效时段前后相接、不重叠
        s.define_rule("日本", "鲜品", "JP-FRESH-V1", "2026-01-01T00:00Z",
                      requirements=["植物检疫证书", "放射性检测"],
                      max_temp_c=10, min_grade="二级",
                      shelf_life_hours_on_departure=24,
                      effective_to="2026-09-20T00:00Z")
        s.define_rule("日本", "鲜品", "JP-FRESH-V2", "2026-09-20T00:00Z",
                      requirements=["植物检疫证书", "放射性检测", "产地溯源"],
                      max_temp_c=7, min_grade="一级",
                      shelf_life_hours_on_departure=36)
        s.define_rule("日本", "冷冻", "JP-FRZ-V1", "2026-01-01T00:00Z",
                      requirements=["植物检疫证书", "速冻工艺声明"],
                      max_temp_c=-15)

        # B1: 70kg（f1 40 + f2 30）；B2: 90kg（f1 60 + f2 30）
        s.pack_box("e1", [{"batch_id": self.b1, "quantity": 40},
                          {"batch_id": self.b3, "quantity": 30}],
                   "鲜品", "一级", T0, box_id="B1")
        s.pack_box("e1", [{"batch_id": self.b1, "quantity": 60},
                          {"batch_id": self.b3, "quantity": 30}],
                   "鲜品", "一级", T0, box_id="B2")

        s.open_slot("KMG", SLOT, capacity_boxes=10)
        self.slot_kmg = self._slot_id("KMG")
        s.open_slot("CAN", "2026-09-11T04:00Z", capacity_boxes=10)
        self.slot_can = self._slot_id("CAN")

    def _slot_id(self, port):
        return next(iter(self.s.ports[port]["slots"]))

    def release_b1(self, departure=DEP):
        """把 B1 走到允许出运。"""
        s = self.s
        s.report_inspection("B1", "o1", "检疫查验", "合格", NOW)
        s.plan_shipment("e1", "B1", "日本", departure, ["KMG"], "CA999")
        s.book_port("e1", "B1", "KMG", self.slot_kmg, NOW)
        cert = s.issue_certificate("B1", "o1", NOW)
        return cert["payload"]["certificate_id"]


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.fx = MatsutakeFixture()
        self.s = self.fx.s

    def test_basic_lineage_to_collections(self):
        rows = {r["collection_id"]: r["quantity"]
                for r in self.s.box_view("B1", {"kind": "dispatcher"}, NOW)["lineage"]}
        self.assertEqual(rows, {self.fx.col1: 40, self.fx.col2: 30})

    def test_split_preserves_lineage_and_quantity(self):
        self.s.split_box("B2", "e1", [45, 45], NOW)
        c1, c2 = self.s.boxes["B2"]["split_into"]
        # 每个子箱按比例携带来源
        for child in (c1, c2):
            rows = {r["collection_id"]: r["quantity"]
                    for r in self.s.box_view(child, {"kind": "dispatcher"}, NOW)["lineage"]}
            self.assertEqual(rows, {self.fx.col1: 30, self.fx.col2: 15})
        # 子箱是新实体：不继承放行结论
        self.assertEqual(self.s.boxes[c1]["state"], "待查验")
        self.assertIsNone(self.s.boxes[c1]["certificate_id"])
        self.assertEqual(self.s.boxes["B2"]["state"], "已拆分")
        self.assertEqual(conservation_check(self.s), [])

    def test_split_must_conserve(self):
        with self.assertRaises(DomainError):
            self.s.split_box("B2", "e1", [45, 40], NOW)

    def test_merge_preserves_lineage(self):
        self.s.split_box("B2", "e1", [45, 45], NOW)
        c1, c2 = self.s.boxes["B2"]["split_into"]
        ev = self.s.merge_boxes([c1, c2], "e1", NOW)
        mid = ev["payload"]["new_box_id"]
        rows = {r["collection_id"]: r["quantity"]
                for r in self.s.box_view(mid, {"kind": "dispatcher"}, NOW)["lineage"]}
        self.assertEqual(rows, {self.fx.col1: 60, self.fx.col2: 30})
        self.assertEqual(self.s.boxes[mid]["quantity"], 90)
        self.assertEqual(self.s.boxes[mid]["state"], "待查验")
        self.assertIsNone(self.s.boxes[mid]["certificate_id"])
        self.assertEqual(conservation_check(self.s), [])

    def test_convert_to_frozen_stops_freshness_clock(self):
        # 鲜度逼近越界后转冷冻：时钟停止，按冷冻规则判定
        self.s.convert_form("B2", "e1", "冷冻", "2026-09-13T06:00Z")
        self.s.report_inspection("B2", "o1", "检疫查验", "合格", NOW)
        self.s.plan_shipment("e1", "B2", "日本", "2026-09-20T12:00Z", ["KMG"], "CA111")
        self.s.book_port("e1", "B2", "KMG", self.fx.slot_kmg, NOW)
        view = self.s.box_view("B2", {"kind": "dispatcher"}, NOW)
        self.assertEqual(view["freshness"]["clock"], "stopped")
        verdict = self.s.evaluate_release("B2", NOW)
        self.assertTrue(verdict["feasible"], verdict["blockers"])
        self.assertEqual(verdict["rule"]["version"], "JP-FRZ-V1")
        self.assertEqual(conservation_check(self.s), [])

    def test_convert_invalidates_old_certificate(self):
        self.fx.release_b1()
        self.assertEqual(self.s.boxes["B1"]["state"], "允许出运")
        self.s.convert_form("B1", "e1", "冷冻", NOW)
        self.assertEqual(self.s.boxes["B1"]["state"], "待查验")
        self.assertIsNone(self.s.boxes["B1"]["certificate_id"])


class RuleTimingTest(unittest.TestCase):
    def setUp(self):
        self.fx = MatsutakeFixture()
        self.s = self.fx.s

    def test_rule_selected_by_planned_departure(self):
        self.s.report_inspection("B1", "o1", "检疫查验", "合格", NOW)
        self.s.book_port("e1", "B1", "KMG", self.fx.slot_kmg, NOW)
        # 起飞 9/12 → V1
        self.s.plan_shipment("e1", "B1", "日本", DEP, ["KMG"], "CA999")
        self.assertEqual(self.s.evaluate_release("B1", NOW)["rule"]["version"],
                         "JP-FRESH-V1")
        # 改到 9/21 → 必须按新时点取 V2，旧结论不能沿用
        self.s.plan_shipment("e1", "B1", "日本", "2026-09-21T12:00Z", ["KMG"], "CA999")
        verdict = self.s.evaluate_release("B1", NOW)
        self.assertEqual(verdict["rule"]["version"], "JP-FRESH-V2")
        # V2 要求起飞时剩余 36h；9/21 已不可能满足
        self.assertTrue(any("要求 36h" in b for b in verdict["blockers"]))

    def test_departure_without_matching_rule_blocks(self):
        self.s.report_inspection("B1", "o1", "检疫查验", "合格", NOW)
        self.s.book_port("e1", "B1", "KMG", self.fx.slot_kmg, NOW)
        self.s.plan_shipment("e1", "B1", "越南", DEP, ["KMG"], "VN1")
        verdict = self.s.evaluate_release("B1", NOW)
        self.assertFalse(verdict["feasible"])
        self.assertTrue(any("无生效规则" in b for b in verdict["blockers"]))

    def test_stale_release_not_reused_after_delay(self):
        cert_id = self.fx.release_b1()
        # 延误到 9/11 20:00（剩余 36h ≥ 24h）：旧证失效，可按新时点重新签发
        self.s.delay_flight(self.s.boxes["B1"]["shipment_id"],
                            "2026-09-11T20:00Z", "天气", NOW)
        self.assertFalse(self.s.certificates[cert_id]["valid"])
        self.assertEqual(self.s.boxes["B1"]["state"], "待查验")
        new_cert = self.s.issue_certificate("B1", "o1", NOW)
        self.assertTrue(self.s.certificates[new_cert["payload"]["certificate_id"]]["valid"])
        # 再延误到 9/12 12:00（剩余 20h < 24h）：再次失效，不能再放行
        self.s.delay_flight(self.s.boxes["B1"]["shipment_id"],
                            "2026-09-12T12:00Z", "机械故障", NOW)
        with self.assertRaises(DomainError):
            self.s.issue_certificate("B1", "o1", NOW)

    def test_certificate_correction_requires_re_evaluation(self):
        cert_id = self.fx.release_b1()
        self.s.request_correction(cert_id, "放射性检测报告页码缺失", NOW)
        self.assertFalse(self.s.certificates[cert_id]["valid"])
        # 补正材料齐全不恢复旧证书
        self.s.resolve_correction(cert_id, "o1", "材料已补", NOW)
        self.assertFalse(self.s.certificates[cert_id]["valid"])
        verdict = self.s.evaluate_release("B1", NOW)
        self.assertTrue(any("已失效" in n for n in verdict["notices"]))
        # notices 不阻断重新签发；重新评估签发后才放行
        self.s.issue_certificate("B1", "o1", NOW)
        self.assertEqual(self.s.boxes["B1"]["state"], "允许出运")

    def test_rebook_invalidates_and_offers_alternatives(self):
        self.fx.release_b1()
        booking = next(b for b in self.s.bookings.values() if b["active"])
        self.s.rebook_port(booking["id"], "CAN", self.fx.slot_can, NOW)
        self.assertIsNone(self.s.boxes["B1"]["certificate_id"])
        self.assertEqual(self.s.boxes["B1"]["state"], "待查验")
        view = self.s.box_view("B1", {"kind": "dispatcher"}, NOW)
        ports = {a["port_id"] for a in view["alternative_routes"]}
        self.assertIn("KMG", ports)  # 改约后昆明时段成为替代路线

    def test_excursion_blocks_until_re_certified(self):
        cert_id = self.fx.release_b1()
        # 峰值 9℃ 未超日本规则 10℃ 限值，但异常未处置期间不得放行
        self.s.open_excursion("B1", "o2", 9.0, NOW, note="冷机停机")
        self.assertFalse(self.s.certificates[cert_id]["valid"])
        verdict = self.s.evaluate_release("B1", NOW)
        self.assertTrue(any("温控异常" in b for b in verdict["blockers"]))
        with self.assertRaises(DomainError):
            self.s.issue_certificate("B1", "o1", NOW)
        # 处置完成只代表温度恢复，旧结论不自动复活；按当前状态重新评估签发
        exc = list(self.s.excursions)[0]
        self.s.resolve_excursion(exc, NOW)
        self.assertFalse(self.s.certificates[cert_id]["valid"])
        self.s.issue_certificate("B1", "o1", NOW)
        self.assertEqual(self.s.boxes["B1"]["state"], "允许出运")

    def test_excursion_over_rule_limit_still_blocks_after_resolve(self):
        # 峰值 12.5℃ 超过规则 10℃ 限值：即便处置完成，超限事实仍阻断放行
        self.fx.release_b1()
        self.s.open_excursion("B1", "o2", 12.5, NOW, note="冷机长时间停机")
        exc = list(self.s.excursions)[0]
        self.s.resolve_excursion(exc, NOW)
        verdict = self.s.evaluate_release("B1", NOW)
        self.assertTrue(any("超过" in b and "10" in b for b in verdict["blockers"]))


class InspectionDedupTest(unittest.TestCase):
    def setUp(self):
        self.fx = MatsutakeFixture()
        self.s = self.fx.s

    def test_local_and_port_report_same_inspection_once(self):
        self.s.report_inspection("B1", "o1", "检疫查验", "合格", NOW)
        self.s.report_inspection("B1", "o2", "检疫查验", "合格", NOW)
        records = [i for i in self.s.inspections.values()
                   if i["box_id"] == "B1" and i["inspection_type"] == "检疫查验"]
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]["agencies"]), {"属地海关", "口岸海关"})
        self.assertEqual(set(records[0]["reported_by"]), {"o1", "o2"})
        kinds = [e["type"] for e in self.s.event_log]
        self.assertEqual(kinds.count("inspection_reported"), 1)
        self.assertEqual(kinds.count("inspection_deduplicated"), 1)

    def test_next_day_is_a_new_inspection(self):
        self.s.report_inspection("B1", "o1", "检疫查验", "合格", NOW)
        self.s.report_inspection("B1", "o2", "检疫查验", "合格",
                                 "2026-09-11T01:00Z")
        self.assertEqual(len([i for i in self.s.inspections.values()
                              if i["box_id"] == "B1"]), 2)


class FreshnessTest(unittest.TestCase):
    def setUp(self):
        self.fx = MatsutakeFixture()
        self.s = self.fx.s

    def test_remaining_window_and_expiry(self):
        # 采集 9/10 08:00 + 72h = 9/13 08:00；观察时刻 9/11 00:00 → 56h
        view = self.s.box_view("B1", {"kind": "dispatcher"}, "2026-09-11T00:00Z")
        self.assertAlmostEqual(view["freshness"]["remaining_hours"], 56.0, places=1)
        self.assertFalse(view["freshness"]["expired"])
        expired = self.s.evaluate_release("B1", "2026-09-13T09:00Z")
        self.assertTrue(any("鲜度窗口已越界" in b for b in expired["blockers"]))

    def test_expired_fresh_cannot_depart(self):
        self.fx.release_b1()
        with self.assertRaises(DomainError):
            self.s.depart("B1", "e1", "2026-09-13T09:00Z")


class AccessControlTest(unittest.TestCase):
    def setUp(self):
        self.fx = MatsutakeFixture()
        self.s = self.fx.s

    def test_enterprises_isolated(self):
        with self.assertRaises(DomainError):
            self.s.box_view("B1", {"kind": "enterprise", "enterprise_id": "e2"}, NOW)
        with self.assertRaises(DomainError):
            self.s.split_box("B1", "e2", [35, 35], NOW)

    def test_officer_sees_traceability_but_no_commercial_terms(self):
        import json
        view = self.s.box_view("B1", {"kind": "officer", "officer_id": "o1"}, NOW)
        self.assertTrue(view["lineage"])          # 查验职责需要溯源
        self.assertIsNone(view["commercial"])
        self.assertNotIn("CT-JP-001", json.dumps(view, ensure_ascii=False, default=str))
        self.assertNotIn("unit_price", json.dumps(view, ensure_ascii=False, default=str))

    def test_farmer_sees_only_own_and_no_buyer_contract(self):
        import json
        view = self.s.box_view("B1", {"kind": "farmer", "farmer_id": "f1"}, NOW)
        self.assertEqual({r["farmer_id"] for r in view["lineage"]}, {"f1"})
        self.assertNotIn("CT-JP", json.dumps(view, ensure_ascii=False, default=str))
        # 未参与该箱交售的菌农不可见
        self.s.register_farmer("f3", "外人")
        with self.assertRaises(DomainError):
            self.s.box_view("B1", {"kind": "farmer", "farmer_id": "f3"}, NOW)

    def test_settlement_omits_contract_and_is_scoped(self):
        self.fx.release_b1()
        self.s.depart("B1", "e1", DEP)
        self.s.report_rejection("B1", "o2", "日方检出农残", "2026-09-13T02:00Z",
                                rejected_quantity=35)
        s1 = self.s.settlement_view({"kind": "farmer", "farmer_id": "f1"})
        self.assertEqual(len(s1["lines"]), 1)
        # 35/70 拒收：f1 来源 40 → 20kg 被扣；应付 80*200
        self.assertEqual(s1["lines"][0]["rejected_quantity"], 20)
        self.assertEqual(s1["total_due"], 16000)
        s2 = self.s.settlement_view({"kind": "farmer", "farmer_id": "f2"})
        self.assertEqual(s2["lines"][0]["rejected_quantity"], 15)
        self.assertEqual(s2["total_due"], 45 * 180)
        import json
        self.assertNotIn("CT-JP", json.dumps(s1, ensure_ascii=False))

    def test_officer_cannot_see_payment_in_impact(self):
        self.fx.release_b1()
        self.s.depart("B1", "e1", DEP)
        self.s.report_rejection("B1", "o2", "日方拒收", "2026-09-13T02:00Z", 35)
        officer_view = self.s.impact_view("B1", {"kind": "officer", "officer_id": "o2"})
        self.assertTrue(all(r["payment_deduction"] is None for r in officer_view["farmers"]))
        with self.assertRaises(DomainError):
            self.s.impact_view("B1", {"kind": "farmer", "farmer_id": "f1"})


class RejectionImpactTest(unittest.TestCase):
    def setUp(self):
        self.fx = MatsutakeFixture()
        self.s = self.fx.s
        self.fx.release_b1()
        self.s.depart("B1", "e1", DEP)
        self.s.report_rejection("B1", "o2", "日方放射性超标",
                                "2026-09-13T02:00Z", rejected_quantity=35)

    def test_impact_locates_collections_and_payments(self):
        impact = self.s.impact_view("B1", {"kind": "dispatcher"})
        rows = {r["collection_id"]: r["rejected_quantity"]
                for r in impact["collections"]}
        self.assertEqual(rows, {self.fx.col1: 20, self.fx.col2: 15})
        pay = {r["farmer_id"]: r["payment_deduction"] for r in impact["farmers"]}
        self.assertEqual(pay, {"f1": 4000.0, "f2": 2700.0})

    def test_impact_through_split_chain(self):
        # B2 拆成两个 45 子箱，其中一个部分退运 22.5
        self.s.split_box("B2", "e1", [45, 45], NOW)
        child = self.s.boxes["B2"]["split_into"][0]
        self.s.report_inspection(child, "o1", "检疫查验", "合格", NOW)
        self.s.plan_shipment("e1", child, "日本", DEP, ["KMG"], "CA222")
        self.s.book_port("e1", child, "KMG", self.fx.slot_kmg, NOW)
        self.s.issue_certificate(child, "o1", NOW)
        self.s.depart(child, "e1", DEP)
        self.s.report_rejection(child, "o2", "包装破损", "2026-09-13T02:00Z", 22.5)
        impact = self.s.impact_view(child, {"kind": "dispatcher"})
        rows = {r["collection_id"]: r["rejected_quantity"]
                for r in impact["collections"]}
        # 子箱 f1 30 / f2 15；拒收一半 → 15 / 7.5
        self.assertEqual(rows, {self.fx.col1: 15, self.fx.col2: 7.5})


class WorkflowGuardTest(unittest.TestCase):
    def setUp(self):
        self.fx = MatsutakeFixture()
        self.s = self.fx.s

    def test_full_happy_path_releases_and_departs(self):
        self.fx.release_b1()
        view = self.s.box_view("B1", {"kind": "dispatcher"}, NOW)
        self.assertTrue(view["feasibility"]["feasible"], view["feasibility"]["blockers"])
        self.s.depart("B1", "e1", DEP)
        self.assertEqual(self.s.boxes["B1"]["state"], "运输中")

    def test_cannot_issue_without_local_quarantine(self):
        self.s.plan_shipment("e1", "B1", "日本", DEP, ["KMG"], "CA999")
        self.s.book_port("e1", "B1", "KMG", self.fx.slot_kmg, NOW)
        with self.assertRaises(DomainError):
            self.s.issue_certificate("B1", "o1", NOW)

    def test_no_double_booking(self):
        self.s.book_port("e1", "B1", "KMG", self.fx.slot_kmg, NOW)
        with self.assertRaises(DomainError):
            self.s.book_port("e1", "B1", "CAN", self.fx.slot_can, NOW)

    def test_unknown_command(self):
        with self.assertRaises(DomainError):
            dispatch(self.s, "nope", {})

    def test_conservation_after_mixed_operations(self):
        self.s.split_box("B2", "e1", [45, 45], NOW)
        c1, c2 = self.s.boxes["B2"]["split_into"]
        self.s.merge_boxes([c1, c2], "e1", NOW)
        self.assertEqual(conservation_check(self.s), [])


if __name__ == "__main__":
    unittest.main()
