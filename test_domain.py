"""领域逻辑：谱系追溯、鲜度窗口、规则版本、放行失效与退运回溯。"""
import unittest
from datetime import timedelta

from domain import (Batch, Booking, Box, Certificate, Delivery, Inspection, Lot, Shipment,
                    TempReading, node_deadline, parse_dt, shipment_form, shipment_impact,
                    trace_to_batches, validate_lot_inputs)
from feasibility import alternatives, assess, is_stale, latest_assessment, release_status
from rules import DEFAULT_RULES, select_rule
from store import Store

T0 = parse_dt("2026-09-26T06:00:00Z")
NOW = parse_dt("2026-09-27T08:00:00Z")


def hour(n):
    return T0 + timedelta(hours=n)


def base_store():
    store = Store()
    store.batches["B1"] = Batch("B1", "F1", "ENT1", "香格里拉·尼汝", 3200, "松茸", "鉴别员甲", T0, 10.0)
    store.batches["B2"] = Batch("B2", "F2", "ENT1", "香格里拉·格咱", 3500, "松茸", "鉴别员甲", T0, 8.0)
    store.deliveries["D1"] = Delivery("D1", "B1", "F1", "ENT1", 10.0, 800.0, hour(1))
    store.deliveries["D2"] = Delivery("D2", "B2", "F2", "ENT1", 8.0, 750.0, hour(1))
    return store


def ready_store():
    """单据齐全、可直接放行的场景。"""
    store = base_store()
    store.lots["L1"] = Lot("L1", "ENT1", [("B1", 10.0)], "鲜品", "A", hour(6))
    store.boxes["X1"] = Box("X1", "ENT1", [("L1", 10.0)], hour(7))
    store.shipments["S1"] = Shipment("S1", "ENT1", ["X1"], "日本", "昆明长水", "MU261",
                                     parse_dt("2026-09-28T10:30:00Z"), 8.0)
    store.certs["C1"] = Certificate("C1", "S1", "植物检疫证书", hour(25))
    store.certs["C2"] = Certificate("C2", "S1", "卫生证书", hour(25))
    store.inspections["I1"] = Inspection("I1", "S1", "属地", "通过", "海关甲", hour(25))
    store.inspections["I2"] = Inspection("I2", "S1", "口岸", "通过", "海关乙", hour(25))
    store.bookings["BK1"] = Booking("BK1", "S1", "昆明长水", "MU261",
                                    parse_dt("2026-09-28T10:30:00Z"))
    return store


class TraceTest(unittest.TestCase):
    def test_split_merge_and_transform_keep_lineage(self):
        store = base_store()
        store.lots["L1"] = Lot("L1", "ENT1", [("B1", 6.0)], "鲜品", "A", hour(6))
        store.lots["L2"] = Lot("L2", "ENT1", [("B2", 4.0)], "冷冻", "B", hour(6))
        store.boxes["X1"] = Box("X1", "ENT1", [("L1", 6.0)], hour(7))
        store.boxes["X2"] = Box("X2", "ENT1", [("X1", 4.0)], hour(8))   # 拆批
        store.boxes["X3"] = Box("X3", "ENT1", [("X1", 2.0)], hour(8))
        store.boxes["X4"] = Box("X4", "ENT1", [("X2", 3.0), ("L2", 2.0)], hour(9))  # 合箱
        traced = trace_to_batches(store, "X4")
        self.assertAlmostEqual(traced["B1"], 3.0)
        self.assertAlmostEqual(traced["B2"], 2.0)

    def test_deadline_by_form(self):
        store = base_store()
        store.lots["L1"] = Lot("L1", "ENT1", [("B1", 6.0)], "鲜品", "A", hour(6))
        store.lots["L2"] = Lot("L2", "ENT1", [("B2", 4.0)], "冷冻", "B", hour(6))
        self.assertEqual(node_deadline(store, "L1"), T0 + timedelta(hours=72))
        self.assertEqual(node_deadline(store, "L2"), hour(6) + timedelta(days=30))

    def test_late_conversion_rejected(self):
        store = base_store()
        with self.assertRaises(ValueError):
            validate_lot_inputs(store, "冷冻", [("B1", 1.0)], T0 + timedelta(hours=80))

    def test_non_matsutake_rejected(self):
        store = base_store()
        store.batches["B3"] = Batch("B3", "F3", "ENT1", "香格里拉", 3000, "牛肝菌", "鉴别员甲", T0, 5.0)
        store.deliveries["D3"] = Delivery("D3", "B3", "F3", "ENT1", 5.0, 100.0, hour(1))
        with self.assertRaises(ValueError):
            validate_lot_inputs(store, "鲜品", [("B3", 1.0)], hour(2))


class RuleTest(unittest.TestCase):
    def test_rule_selected_by_departure_date(self):
        may = select_rule(DEFAULT_RULES, "日本", "鲜品", parse_dt("2026-05-10T10:00:00Z"))
        autumn = select_rule(DEFAULT_RULES, "日本", "鲜品", parse_dt("2026-09-28T10:00:00Z"))
        self.assertEqual(may.rule_id, "JP-FRESH-2026H1")
        self.assertEqual(may.required_certs, ("植物检疫证书",))
        self.assertEqual(autumn.rule_id, "JP-FRESH-2026H2")
        self.assertEqual(autumn.required_certs, ("植物检疫证书", "卫生证书"))
        self.assertIsNone(select_rule(DEFAULT_RULES, "法国", "鲜品", parse_dt("2026-09-28T10:00:00Z")))


class FeasibilityTest(unittest.TestCase):
    def test_ready_shipment_is_releasable(self):
        store = ready_store()
        assessment = assess(store, store.shipments["S1"], NOW)
        self.assertEqual(assessment.conclusion, "允许出运")
        self.assertEqual(assessment.rule_id, "JP-FRESH-2026H2")

    def test_missing_cert_blocks(self):
        store = ready_store()
        del store.certs["C2"]
        assessment = assess(store, store.shipments["S1"], NOW)
        self.assertEqual(assessment.conclusion, "不可出运")
        self.assertTrue(any("卫生证书" in r for r in assessment.reasons))

    def test_conclusion_reused_only_while_no_new_events(self):
        store = ready_store()
        first, _ = release_status(store, store.shipments["S1"], NOW)
        self.assertEqual(first.conclusion, "允许出运")
        again, reassessed = release_status(store, store.shipments["S1"], NOW)
        self.assertFalse(reassessed)
        self.assertIs(again, first)

    def test_corrected_cert_invalidates_previous_conclusion(self):
        store = ready_store()
        first, _ = release_status(store, store.shipments["S1"], NOW)
        store.certs["C2"].state = "已作废"
        store.certs["C2B"] = Certificate("C2B", "S1", "卫生证书", hour(27))
        store.log("cert_corrected", "S1", "卫生证书补正", at=hour(27))
        self.assertTrue(is_stale(store, "S1", first))
        third, reassessed = release_status(store, store.shipments["S1"], hour(28))
        self.assertTrue(reassessed)
        self.assertIsNot(third, first)
        self.assertEqual(third.conclusion, "允许出运")  # 新证有效

    def test_flight_delay_requires_rebooking(self):
        store = ready_store()
        release_status(store, store.shipments["S1"], NOW)
        store.shipments["S1"].planned_departure = parse_dt("2026-09-28T23:59:00Z")
        store.log("flight_delayed", "S1", "MU261延误", at=hour(27))
        assessment, reassessed = release_status(store, store.shipments["S1"], hour(28))
        self.assertTrue(reassessed)
        self.assertEqual(assessment.conclusion, "不可出运")
        self.assertTrue(any("预约" in r for r in assessment.reasons))

    def test_temp_anomaly_blocks_until_resolved(self):
        store = ready_store()
        store.temps.append(TempReading("S1", hour(27), 9.0, True))
        store.log("temp_anomaly", "S1", "温度9℃超出0~4℃", at=hour(27))
        assessment, _ = release_status(store, store.shipments["S1"], hour(28))
        self.assertEqual(assessment.conclusion, "不可出运")
        store.temps[0].resolved = True
        store.log("temp_resolved", "S1", "已复温", at=hour(29))
        assessment, _ = release_status(store, store.shipments["S1"], hour(30))
        self.assertEqual(assessment.conclusion, "允许出运")

    def test_mixed_forms_blocked(self):
        store = base_store()
        store.lots["L1"] = Lot("L1", "ENT1", [("B1", 5.0)], "鲜品", "A", hour(6))
        store.lots["L2"] = Lot("L2", "ENT1", [("B2", 4.0)], "冷冻", "B", hour(6))
        store.boxes["X1"] = Box("X1", "ENT1", [("L1", 5.0)], hour(7))
        store.boxes["X2"] = Box("X2", "ENT1", [("L2", 4.0)], hour(7))
        store.shipments["S1"] = Shipment("S1", "ENT1", ["X1", "X2"], "日本", "昆明长水",
                                         "MU261", parse_dt("2026-09-28T10:30:00Z"), 8.0)
        self.assertIsNone(shipment_form(store, store.shipments["S1"]))
        assessment = assess(store, store.shipments["S1"], NOW)
        self.assertTrue(any("形态不一致" in r for r in assessment.reasons))

    def test_alternatives_offer_other_ports(self):
        store = ready_store()
        store.shipments["S1"].planned_departure = parse_dt("2026-09-28T23:59:00Z")
        options = alternatives(store, store.shipments["S1"], parse_dt("2026-09-28T12:00:00Z"))
        flights = {o["flight_no"] for o in options if o["kind"] == "改签航班"}
        self.assertIn("3U8085", flights)

    def test_alternatives_suggest_freezing_when_window_short(self):
        store = ready_store()
        options = alternatives(store, store.shipments["S1"], parse_dt("2026-09-28T21:00:00Z"))
        self.assertEqual(options[0]["kind"], "转冷冻加工")


class ImpactTest(unittest.TestCase):
    def test_rejection_impact_traces_farmers(self):
        store = ready_store()
        impact = shipment_impact(store, store.shipments["S1"])
        self.assertEqual(impact["batches"][0]["batch_id"], "B1")
        self.assertAlmostEqual(impact["batches"][0]["weight_kg"], 10.0)
        self.assertEqual(impact["farmers"], [{"farmer_id": "F1", "weight_kg": 10.0, "amount": 8000.0}])

    def test_impact_scales_partial_boxes(self):
        store = base_store()
        store.lots["L1"] = Lot("L1", "ENT1", [("B1", 10.0)], "鲜品", "A", hour(6))
        store.boxes["X1"] = Box("X1", "ENT1", [("L1", 10.0)], hour(7))
        store.boxes["X1"].remaining_kg = 6.0  # 已消耗4kg，剩余6kg随出运
        store.shipments["S1"] = Shipment("S1", "ENT1", ["X1"], "越南", "昆明长水",
                                         "CZ301", parse_dt("2026-09-28T09:20:00Z"), 3.0)
        impact = shipment_impact(store, store.shipments["S1"])
        self.assertAlmostEqual(impact["farmers"][0]["amount"], 4800.0)


if __name__ == "__main__":
    unittest.main()
