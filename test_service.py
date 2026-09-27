"""验证基础服务与领域契约保持一致，并覆盖时效协同接口主流程。"""
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, health_payload, load_contract
from store import build_store


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def read_json(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            return json.load(response)

    def test_health_identity(self):
        self.assertEqual(self.read_json("/health"), health_payload())

    def test_contract_identity_and_rules(self):
        contract = self.read_json("/contract")
        self.assertEqual(contract, load_contract())
        self.assertEqual(contract["service_id"], SERVICE_ID)
        self.assertGreaterEqual(len(contract["invariants"]), 3)

    def test_unknown_route_is_hidden(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


class ApiTest(unittest.TestCase):
    """时效协同接口：每类测试用独立农户编号，共享一个隔离仓储。"""

    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.store = build_store()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, actor=None, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(f"{self.base_url}{path}", data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if actor:
            request.add_header("X-Actor", actor)
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            payload = json.load(error)
            error.close()
            return error.code, payload

    def make_chain(self, ent="ENT1", farmer="F1", destination="日本", flight="MU261",
                   departure="2026-09-28T10:30:00Z", transit=8.0):
        """快速建立 采集→交售→加工→装箱→出运 链条，返回各环节数据。"""
        status, batch = self.call("POST", "/batches", f"enterprise:{ent}", {
            "farmer_id": farmer, "site": "香格里拉·尼汝", "altitude_m": 3200,
            "species": "松茸", "identified_by": "鉴别员甲",
            "collected_at": "2026-09-26T06:00:00Z", "weight_kg": 10.0})
        assert status == 201, batch
        status, delivery = self.call("POST", "/deliveries", f"enterprise:{ent}", {
            "batch_id": batch["batch_id"], "weight_kg": 10.0, "price_per_kg": 800.0,
            "delivered_at": "2026-09-26T07:00:00Z"})
        assert status == 201, delivery
        status, lot = self.call("POST", "/lots", f"enterprise:{ent}", {
            "inputs": [[batch["batch_id"], 10.0]], "form": "鲜品", "grade": "A",
            "processed_at": "2026-09-26T12:00:00Z"})
        assert status == 201, lot
        status, box = self.call("POST", "/boxes", f"enterprise:{ent}", {
            "contents": [[lot["lot_id"], 10.0]], "packed_at": "2026-09-26T13:00:00Z"})
        assert status == 201, box
        status, shipment = self.call("POST", "/shipments", f"enterprise:{ent}", {
            "box_ids": [box["box_id"]], "destination": destination, "port": "昆明长水",
            "flight_no": flight, "planned_departure": departure, "transit_hours": transit,
            "buyer_contract": {"contract_no": "JP-001", "unit_price": 3000.0},
            "at": "2026-09-27T05:00:00Z"})
        assert status == 201, shipment
        return batch, lot, box, shipment

    def test_auth_required(self):
        status, _ = self.call("GET", "/rules")
        self.assertEqual(status, 401)
        status, _ = self.call("GET", "/rules", "nobody")
        self.assertEqual(status, 401)
        status, payload = self.call("GET", "/rules", "dispatcher")
        self.assertEqual(status, 200)
        self.assertTrue(payload["rules"])
        status, _ = self.call("GET", "/health")
        self.assertEqual(status, 200)

    def test_enterprise_isolation(self):
        _, _, box, shipment = self.make_chain(farmer="F-ISO", destination="越南",
                                              flight="CZ301", departure="2026-09-28T09:20:00Z",
                                              transit=3.0)
        sid = shipment["shipment_id"]
        status, _ = self.call("GET", f"/shipments/{sid}", "enterprise:ENT2")
        self.assertEqual(status, 403)
        status, own = self.call("GET", f"/shipments/{sid}", "enterprise:ENT1")
        self.assertEqual(status, 200)
        self.assertIn("buyer_contract", own)
        status, view = self.call("GET", f"/shipments/{sid}", "inspector_port")
        self.assertEqual(status, 200)
        self.assertNotIn("buyer_contract", view)
        status, _ = self.call("GET", f"/boxes/{box['box_id']}/trace", "enterprise:ENT2")
        self.assertEqual(status, 403)
        status, trace = self.call("GET", f"/boxes/{box['box_id']}/trace", "dispatcher")
        self.assertEqual(status, 200)
        self.assertEqual(trace["batches"][0]["site"], "香格里拉·尼汝")

    def test_full_flow(self):
        batch, _, box, shipment = self.make_chain()
        sid, box_id = shipment["shipment_id"], box["box_id"]
        # 单据未齐：不可出运
        _, rel = self.call("GET", f"/shipments/{sid}/release?now=2026-09-27T06:00:00Z", "dispatcher")
        self.assertEqual(rel["conclusion"], "不可出运")
        self.assertTrue(rel["reassessed"])
        # 查验上报：属地与口岸重复上报同一查验只记一次
        status, _ = self.call("POST", "/inspections", "inspector_local", {
            "inspection_no": "INS-1", "shipment_id": sid, "result": "通过",
            "at": "2026-09-27T07:00:00Z"})
        self.assertEqual(status, 201)
        status, dup = self.call("POST", "/inspections", "inspector_port", {
            "inspection_no": "INS-1", "shipment_id": sid, "result": "通过",
            "at": "2026-09-27T07:05:00Z"})
        self.assertEqual(status, 200)
        self.assertTrue(dup["deduplicated"])
        self.assertEqual(dup["inspection"]["checkpoint"], "属地")
        # 口岸查验、证书签发、口岸预约
        self.call("POST", "/inspections", "inspector_port", {
            "inspection_no": "INS-2", "shipment_id": sid, "result": "通过",
            "at": "2026-09-27T07:10:00Z"})
        self.call("POST", "/certs", "inspector_local", {
            "cert_id": "C-1", "shipment_id": sid, "cert_type": "植物检疫证书",
            "issued_at": "2026-09-27T07:20:00Z"})
        self.call("POST", "/certs", "inspector_local", {
            "cert_id": "C-2", "shipment_id": sid, "cert_type": "卫生证书",
            "issued_at": "2026-09-27T07:25:00Z"})
        self.call("POST", "/bookings", "enterprise:ENT1", {
            "booking_id": "BK-1", "shipment_id": sid, "port": "昆明长水",
            "flight_no": "MU261", "departure": "2026-09-28T10:30:00Z",
            "at": "2026-09-27T07:30:00Z"})
        # 放行；无新事件时沿用有效结论
        _, rel = self.call("GET", f"/shipments/{sid}/release?now=2026-09-27T08:00:00Z", "dispatcher")
        self.assertEqual(rel["conclusion"], "允许出运")
        self.assertEqual(rel["rule_id"], "JP-FRESH-2026H2")
        _, rel = self.call("GET", f"/shipments/{sid}/release?now=2026-09-27T08:30:00Z", "dispatcher")
        self.assertFalse(rel["reassessed"])
        # 航班延误 → 旧结论失效，重判为不可出运
        _, delay = self.call("POST", "/flights/delay", "carrier", {
            "flight_no": "MU261", "new_departure": "2026-09-28T23:59:00Z",
            "at": "2026-09-27T09:00:00Z"})
        self.assertIn(sid, delay["affected"])
        _, rel = self.call("GET", f"/shipments/{sid}/release?now=2026-09-27T10:00:00Z", "dispatcher")
        self.assertTrue(rel["reassessed"])
        self.assertEqual(rel["conclusion"], "不可出运")
        self.assertTrue(any("预约" in r for r in rel["reasons"]))
        # 调度员箱视图：鲜度窗口、阻塞、所用规则、替代路线
        _, view = self.call("GET", f"/boxes/{box_id}/status?now=2026-09-27T10:00:00Z", "dispatcher")
        self.assertLess(view["freshness"]["remaining_hours"], 72)
        self.assertEqual(view["freshness"]["batches"][0]["batch_id"], batch["batch_id"])
        self.assertTrue(view["shipments"][0]["blockers"])
        self.assertEqual(view["shipments"][0]["rule_id"], "JP-FRESH-2026H2")
        flights = {a["flight_no"] for a in view["shipments"][0]["alternatives"]
                   if a["kind"] == "改签航班"}
        self.assertIn("3U8085", flights)
        # 改约到替代航班 → 重新放行
        self.call("POST", "/bookings/change", "enterprise:ENT1", {
            "shipment_id": sid, "port": "成都天府", "flight_no": "3U8085",
            "departure": "2026-09-27T13:20:00Z", "at": "2026-09-27T10:30:00Z"})
        _, rel = self.call("GET", f"/shipments/{sid}/release?now=2026-09-27T11:00:00Z", "dispatcher")
        self.assertEqual(rel["conclusion"], "允许出运")
        # 温控异常 → 阻塞；处置后放行
        _, temp = self.call("POST", "/temperatures", "carrier", {
            "shipment_id": sid, "celsius": 9.0, "at": "2026-09-27T11:30:00Z"})
        self.assertTrue(temp["anomaly"])
        _, rel = self.call("GET", f"/shipments/{sid}/release?now=2026-09-27T12:00:00Z", "dispatcher")
        self.assertEqual(rel["conclusion"], "不可出运")
        self.call("POST", "/temperatures/resolve", "enterprise:ENT1", {
            "shipment_id": sid, "note": "已复温并复核", "at": "2026-09-27T12:30:00Z"})
        _, rel = self.call("GET", f"/shipments/{sid}/release?now=2026-09-27T13:00:00Z", "dispatcher")
        self.assertEqual(rel["conclusion"], "允许出运")
        # 证书补正 → 触发重判，不沿用旧结论
        self.call("POST", "/certs/correct", "inspector_local", {
            "cert_id": "C-2", "new_cert_id": "C-2B", "issued_at": "2026-09-27T13:10:00Z"})
        _, rel = self.call("GET", f"/shipments/{sid}/release?now=2026-09-27T13:15:00Z", "dispatcher")
        self.assertTrue(rel["reassessed"])
        self.assertEqual(rel["conclusion"], "允许出运")
        # 出运与目的地拒收 → 退运回溯到采集批次与应付农户
        _, departed = self.call("POST", f"/shipments/{sid}/depart", "enterprise:ENT1",
                                {"at": "2026-09-27T13:18:00Z"})
        self.assertEqual(departed["state"], "运输中")
        _, rej = self.call("POST", f"/shipments/{sid}/reject", "inspector_port",
                           {"reason": "检出有害生物", "at": "2026-09-29T10:00:00Z"})
        self.assertEqual(rej["state"], "退运处置")
        self.assertEqual(rej["impact"]["batches"][0]["batch_id"], batch["batch_id"])
        self.assertEqual(rej["impact"]["farmers"],
                         [{"farmer_id": "F1", "weight_kg": 10.0, "amount": 8000.0}])

    def test_settlement_hides_buyer_contract(self):
        self.make_chain(farmer="F-SET", destination="越南", flight="CZ301",
                        departure="2026-09-28T09:20:00Z", transit=3.0)
        status, view = self.call("GET", "/settlements/F-SET", "settlement")
        self.assertEqual(status, 200)
        self.assertEqual(view["total_amount"], 8000.0)
        self.assertNotIn("buyer_contract", json.dumps(view, ensure_ascii=False))
        status, _ = self.call("GET", "/settlements/F-SET", "dispatcher")
        self.assertEqual(status, 403)

    def test_split_merge_trace(self):
        def batch(weight):
            status, b = self.call("POST", "/batches", "enterprise:ENT1", {
                "farmer_id": "F-SPLIT", "site": "香格里拉·格咱", "altitude_m": 3500,
                "species": "松茸", "identified_by": "鉴别员乙",
                "collected_at": "2026-09-26T06:00:00Z", "weight_kg": weight})
            assert status == 201, b
            self.call("POST", "/deliveries", "enterprise:ENT1", {
                "batch_id": b["batch_id"], "weight_kg": weight, "price_per_kg": 800.0,
                "delivered_at": "2026-09-26T07:00:00Z"})
            return b["batch_id"]

        def lot(inputs, form="鲜品"):
            status, l = self.call("POST", "/lots", "enterprise:ENT1", {
                "inputs": inputs, "form": form, "grade": "A",
                "processed_at": "2026-09-26T12:00:00Z"})
            assert status == 201, l
            return l["lot_id"]

        b1, b2 = batch(6.0), batch(4.0)
        l1, l2 = lot([[b1, 6.0]]), lot([[b2, 4.0]], form="冷冻")
        status, x1 = self.call("POST", "/boxes", "enterprise:ENT1", {
            "contents": [[l1, 6.0]], "packed_at": "2026-09-26T13:00:00Z"})
        status, split = self.call("POST", "/boxes/split", "enterprise:ENT1", {
            "box_id": x1["box_id"], "parts": [[None, 4.0], [None, 2.0]]})
        self.assertEqual(status, 201)
        xa = split["children"][0]["box_id"]
        status, x4 = self.call("POST", "/boxes", "enterprise:ENT1", {
            "contents": [[xa, 3.0], [l2, 2.0]], "packed_at": "2026-09-26T14:00:00Z"})
        self.assertEqual(status, 201)
        _, trace = self.call("GET", f"/boxes/{x4['box_id']}/trace", "dispatcher")
        weights = {row["batch_id"]: row["weight_kg"] for row in trace["batches"]}
        self.assertAlmostEqual(weights[b1], 3.0)
        self.assertAlmostEqual(weights[b2], 2.0)

    def test_validation(self):
        batch, lot, _, shipment = self.make_chain(farmer="F-VAL", destination="越南",
                                                  flight="CZ301", departure="2026-09-28T09:20:00Z",
                                                  transit=3.0)
        # 交售超重
        status, err = self.call("POST", "/deliveries", "enterprise:ENT1", {
            "batch_id": batch["batch_id"], "weight_kg": 1.0, "price_per_kg": 800.0,
            "delivered_at": "2026-09-26T08:00:00Z"})
        self.assertEqual(status, 400)
        # 他企业不得装箱
        status, _ = self.call("POST", "/boxes", "enterprise:ENT2", {
            "contents": [[lot["lot_id"], 1.0]], "packed_at": "2026-09-26T14:00:00Z"})
        self.assertEqual(status, 403)
        # 非企业角色不得登记批次
        status, _ = self.call("POST", "/batches", "dispatcher", {
            "farmer_id": "F-X", "site": "香格里拉", "species": "松茸",
            "identified_by": "鉴别员甲", "collected_at": "2026-09-26T06:00:00Z",
            "weight_kg": 1.0})
        self.assertEqual(status, 403)
        # 同一查验单号不得登记不同结论
        sid = shipment["shipment_id"]
        self.call("POST", "/inspections", "inspector_local", {
            "inspection_no": "INS-VAL", "shipment_id": sid, "result": "通过",
            "at": "2026-09-27T07:00:00Z"})
        status, _ = self.call("POST", "/inspections", "inspector_port", {
            "inspection_no": "INS-VAL", "shipment_id": sid, "result": "不通过",
            "at": "2026-09-27T07:05:00Z"})
        self.assertEqual(status, 409)
        # 超出鲜品窗口不得转冷冻
        status, b2 = self.call("POST", "/batches", "enterprise:ENT1", {
            "farmer_id": "F-VAL", "site": "香格里拉·尼汝", "altitude_m": 3200,
            "species": "松茸", "identified_by": "鉴别员甲",
            "collected_at": "2026-09-26T06:00:00Z", "weight_kg": 5.0})
        self.call("POST", "/deliveries", "enterprise:ENT1", {
            "batch_id": b2["batch_id"], "weight_kg": 5.0, "price_per_kg": 800.0,
            "delivered_at": "2026-09-26T07:00:00Z"})
        status, err = self.call("POST", "/lots", "enterprise:ENT1", {
            "inputs": [[b2["batch_id"], 5.0]], "form": "冷冻",
            "processed_at": "2026-09-30T00:00:00Z"})
        self.assertEqual(status, 400)
        self.assertIn("鲜品窗口", err["error"])


if __name__ == "__main__":
    unittest.main()
