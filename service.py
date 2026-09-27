"""野生菌出口时效链的服务入口。

GET  /health    运行检查
GET  /contract  领域契约
POST /api/commands  命令写入（按调用方身份鉴权）
POST /api/views     视图读取（按角色遮蔽商业资料）
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from domain import ChainStore, COMMANDS, DomainError, dispatch

SERVICE_ID = "mushroom-export"
SERVICE_NAME = "野生菌出口时效链"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")

# 命令 → 允许的调用方角色；载荷中的行为主体必须与调用方身份一致。
COMMAND_ROLES = {
    "register_farmer": {"dispatcher"},
    "register_enterprise": {"dispatcher"},
    "register_officer": {"dispatcher"},
    "register_collection_point": {"dispatcher", "enterprise"},
    "register_port": {"dispatcher"},
    "define_rule": {"dispatcher"},
    "open_slot": {"dispatcher"},
    "record_collection": {"farmer", "dispatcher"},
    "identify_species": {"officer"},
    "deliver": {"enterprise", "dispatcher"},
    "start_processing": {"enterprise"},
    "pack_box": {"enterprise"},
    "split_box": {"enterprise"},
    "merge_boxes": {"enterprise"},
    "convert_form": {"enterprise"},
    "plan_shipment": {"enterprise"},
    "report_inspection": {"officer"},
    "issue_certificate": {"officer"},
    "request_correction": {"officer"},
    "resolve_correction": {"officer"},
    "book_port": {"enterprise"},
    "rebook_port": {"enterprise"},
    "delay_flight": {"enterprise", "carrier", "dispatcher"},
    "open_excursion": {"officer", "carrier"},
    "resolve_excursion": {"officer", "carrier"},
    "evaluate_release": {"dispatcher", "officer", "enterprise", "carrier"},
    "depart": {"enterprise"},
    "report_rejection": {"officer", "dispatcher"},
}

# 调用方身份必须与载荷主体一致的字段。
IDENTITY_FIELDS = {
    "enterprise": "enterprise_id",
    "officer": "officer_id",
    "farmer": "farmer_id",
}


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def authorize(command_name, viewer, payload):
    """鉴权并核对调用方身份与载荷行为主体一致。dispatcher 代表平台协同岗。"""
    kind = viewer.get("kind")
    allowed = COMMAND_ROLES.get(command_name)
    if not allowed:
        raise DomainError(f"未知命令：{command_name}")
    if kind not in allowed:
        raise PermissionError(f"角色 {kind} 无权执行 {command_name}")
    id_field = IDENTITY_FIELDS.get(kind)
    if id_field and payload.get(id_field) is not None \
            and payload[id_field] != viewer.get(id_field):
        raise PermissionError("调用方身份与命令主体不一致")


def run_view(store, name, viewer, payload):
    if name == "box":
        return store.box_view(payload["box_id"], viewer, payload["now"])
    if name == "settlement":
        return store.settlement_view(viewer)
    if name == "impact":
        return store.impact_view(payload["box_id"], viewer)
    if name == "rules":
        return {"rules": store.list_rules(payload.get("destination"))}
    raise DomainError(f"未知视图：{name}")


def make_handler(store: ChainStore):
    class Handler(BaseHTTPRequestHandler):
        """健康检查、契约读取与时效链命令/视图接口。"""

        def do_GET(self):
            if self.path == "/health":
                self._send_json(health_payload())
                return
            if self.path == "/contract":
                self._send_json(load_contract())
                return
            self.send_error(404)

        def do_POST(self):
            if self.path not in ("/api/commands", "/api/views"):
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                viewer = body.get("as") or {}
                if not viewer.get("kind"):
                    self._send_json({"error": "缺少调用方身份 as.kind"}, 401)
                    return
                if self.path == "/api/commands":
                    name = body.get("command")
                    payload = body.get("payload") or {}
                    if name not in COMMANDS:
                        self._send_json({"error": f"未知命令：{name}"}, 400)
                        return
                    try:
                        authorize(name, viewer, payload)
                        event = dispatch(store, name, payload)
                    except PermissionError as exc:
                        self._send_json({"error": str(exc)}, 403)
                        return
                    except DomainError as exc:
                        self._send_json({"error": str(exc)}, 422)
                        return
                    self._send_json({"event": event}, 202)
                    return
                name = body.get("view")
                try:
                    result = run_view(store, name, viewer, body.get("payload") or {})
                except PermissionError as exc:
                    self._send_json({"error": str(exc)}, 403)
                    return
                except DomainError as exc:
                    self._send_json({"error": str(exc)}, 422)
                    return
                self._send_json({"view": result})
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                self._send_json({"error": f"请求格式错误：{exc}"}, 400)

        def _send_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return Handler


store = ChainStore()
Handler = make_handler(store)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        missing = [c for c in COMMANDS if c not in contract["commands"]]
        assert not missing, f"契约缺少命令：{missing}"
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
