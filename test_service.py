"""验证基础服务、领域契约与 HTTP 命令/视图链路。"""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, health_payload, load_contract


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread, cls.base_url = start_server()

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


class ApiFlowTest(unittest.TestCase):
    """HTTP 端到端：建一条唯一前缀主线，验证鉴权、查验去重与角色遮蔽。"""

    PREFIX = "HTTP-"

    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread, cls.base_url = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _request(self, path, body, expected):
        req = Request(f"{self.base_url}{path}",
                      data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=2) as response:
                self.assertEqual(response.status, expected)
                return json.load(response)
        except HTTPError as exc:
            self.assertEqual(exc.code, expected)
            return json.load(exc)

    def cmd(self, name, payload, as_role, expected=202):
        return self._request("/api/commands",
                             {"command": name, "payload": payload, "as": as_role},
                             expected)

    def view(self, name, payload, as_role=None, expected=200):
        body = {"view": name, "payload": payload}
        if as_role:
            body["as"] = as_role
        return self._request("/api/views", body, expected)

    def test_end_to_end_flow(self):
        p = self.PREFIX
        T = "2026-10-01T08:00Z"

        # 平台建档
        self.cmd("register_farmer", {"farmer_id": p + "F", "name": "阿木"},
                 {"kind": "dispatcher"})
        self.cmd("register_enterprise", {"enterprise_id": p + "E", "name": "云珍公司"},
                 {"kind": "dispatcher"})
        self.cmd("register_officer",
                 {"officer_id": p + "O1", "name": "属地关", "agency": "属地海关"},
                 {"kind": "dispatcher"})
        self.cmd("register_officer",
                 {"officer_id": p + "O2", "name": "口岸关", "agency": "口岸海关"},
                 {"kind": "dispatcher"})
        self.cmd("register_collection_point",
                 {"point_id": p + "P", "name": "香格里拉", "altitude_m": 3500},
                 {"kind": "dispatcher"})
        self.cmd("register_port", {"port_id": p + "KMG", "name": "昆明"},
                 {"kind": "dispatcher"})

        # 越权：菌农不能定义规则
        denied = self.cmd("define_rule",
                          {"destination": "德国", "form": "鲜品", "version": "DE-V1",
                           "effective_from": "2026-01-01T00:00Z", "requirements": []},
                          {"kind": "farmer", "farmer_id": p + "F"}, 403)
        self.assertIn("无权", denied["error"])
        # 身份冒充：载荷主体与调用方不一致
        denied = self.cmd("pack_box", {"enterprise_id": p + "OTHER"},
                          {"kind": "enterprise", "enterprise_id": p + "E"}, 403)
        self.assertIn("身份", denied["error"])

        self.cmd("define_rule",
                 {"destination": "德国", "form": "鲜品", "version": "DE-V1",
                  "effective_from": "2026-01-01T00:00Z",
                  "requirements": ["植检证"], "max_temp_c": 8,
                  "min_grade": "二级", "shelf_life_hours_on_departure": 12},
                 {"kind": "dispatcher"})
        col = self.cmd("record_collection",
                       {"farmer_id": p + "F", "point_id": p + "P", "species": "松茸",
                        "quantity": 50, "collected_at": T, "shelf_life_hours": 72},
                       {"kind": "farmer", "farmer_id": p + "F"})["event"]["payload"]["collection_id"]
        self.cmd("identify_species",
                 {"collection_id": col, "identified_species": "松茸",
                  "officer_id": p + "O1", "identified_at": T},
                 {"kind": "officer", "officer_id": p + "O1"})
        bat = self.cmd("deliver",
                       {"collection_id": col, "enterprise_id": p + "E", "quantity": 50,
                        "delivered_at": T, "unit_price": 150,
                        "buyer_contract": "CT-DE-9"},
                       {"kind": "enterprise", "enterprise_id": p + "E"})["event"]["payload"]["batch_id"]
        self.cmd("start_processing",
                 {"batch_ids": [bat], "enterprise_id": p + "E", "at": T},
                 {"kind": "enterprise", "enterprise_id": p + "E"})
        self.cmd("pack_box",
                 {"enterprise_id": p + "E",
                  "items": [{"batch_id": bat, "quantity": 50}],
                  "form": "鲜品", "grade": "一级", "packed_at": T,
                  "box_id": p + "B"},
                 {"kind": "enterprise", "enterprise_id": p + "E"})

        # 属地与口岸对同一次查验重复上报
        self.cmd("report_inspection",
                 {"box_id": p + "B", "officer_id": p + "O1",
                  "inspection_type": "检疫查验", "result": "合格", "at": T},
                 {"kind": "officer", "officer_id": p + "O1"})
        dedup = self.cmd("report_inspection",
                         {"box_id": p + "B", "officer_id": p + "O2",
                          "inspection_type": "检疫查验", "result": "合格", "at": T},
                         {"kind": "officer", "officer_id": p + "O2"})
        self.assertEqual(dedup["event"]["type"], "inspection_deduplicated")

        slot = self.cmd("open_slot",
                        {"port_id": p + "KMG", "slot_from": "2026-10-01T18:00Z"},
                        {"kind": "dispatcher"})["event"]["payload"]["slot_id"]
        self.cmd("plan_shipment",
                 {"enterprise_id": p + "E", "box_id": p + "B", "destination": "德国",
                  "planned_departure": "2026-10-02T08:00Z",
                  "route": [p + "KMG"], "flight_no": "LH728"},
                 {"kind": "enterprise", "enterprise_id": p + "E"})
        self.cmd("book_port",
                 {"enterprise_id": p + "E", "box_id": p + "B",
                  "port_id": p + "KMG", "slot_id": slot, "at": T},
                 {"kind": "enterprise", "enterprise_id": p + "E"})
        self.cmd("issue_certificate",
                 {"box_id": p + "B", "officer_id": p + "O1", "at": T},
                 {"kind": "officer", "officer_id": p + "O1"})

        # 角色遮蔽：查验人员看不到买方合同
        officer = self.view("box", {"box_id": p + "B", "now": T},
                            {"kind": "officer", "officer_id": p + "O1"})["view"]
        self.assertIsNone(officer["commercial"])
        self.assertNotIn("CT-DE-9", json.dumps(officer, ensure_ascii=False))
        # 所属企业能看到自己的合同
        ent = self.view("box", {"box_id": p + "B", "now": T},
                        {"kind": "enterprise", "enterprise_id": p + "E"})["view"]
        self.assertEqual(ent["commercial"]["buyer_contracts"], ["CT-DE-9"])
        # 菌农结算不含合同字段
        farmer = self.view("settlement", {},
                           {"kind": "farmer", "farmer_id": p + "F"})["view"]
        self.assertEqual(farmer["total_due"], 7500)
        self.assertNotIn("CT-DE-9", json.dumps(farmer, ensure_ascii=False))
        # 缺少调用方身份 → 401
        body = self._request("/api/views",
                             {"view": "box", "payload": {"box_id": p + "B", "now": T}},
                             401)
        self.assertIn("身份", body["error"])


if __name__ == "__main__":
    unittest.main()
