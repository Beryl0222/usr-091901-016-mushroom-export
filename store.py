"""内存仓储与事件日志：时效相关事件按时间留痕，驱动放行结论失效。"""
from dataclasses import dataclass
from datetime import datetime

from domain import now_utc
from rules import DEFAULT_RULES


@dataclass
class Event:
    at: datetime
    kind: str
    shipment_id: str
    summary: str


class Store:
    def __init__(self, rules=None):
        self.batches = {}
        self.deliveries = {}
        self.lots = {}
        self.boxes = {}
        self.shipments = {}
        self.inspections = {}
        self.certs = {}
        self.bookings = {}
        self.temps = []
        self.assessments = []
        self.events = []
        self.rules = list(rules if rules is not None else DEFAULT_RULES)
        self.counters = {}

    def next_id(self, prefix):
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}-{self.counters[prefix]:04d}"

    def log(self, kind, shipment_id, summary, at=None):
        self.events.append(Event(at or now_utc(), kind, shipment_id, summary))

    def shipment_events(self, shipment_id):
        return [e for e in self.events if e.shipment_id == shipment_id]


def build_store():
    return Store()
