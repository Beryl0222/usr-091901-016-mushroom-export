"""目的地检疫规则与航班时刻：规则按计划出运时间选择生效版本。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone


@dataclass
class Rule:
    """某目的地对某形态货物的检疫准入要求，带生效区间。"""
    rule_id: str
    destination: str
    form: str
    effective_from: date
    effective_to: date | None
    max_transit_hours: float
    temp_min_c: float
    temp_max_c: float
    required_certs: tuple
    needs_port_inspection: bool

    def covers(self, day):
        return self.effective_from <= day and (self.effective_to is None or day <= self.effective_to)


def _d(text):
    return date.fromisoformat(text)


DEFAULT_RULES = [
    # 日本：2026 下半年起鲜品加严，需卫生证书且运输时限收紧
    Rule("JP-FRESH-2026H1", "日本", "鲜品", _d("2026-01-01"), _d("2026-06-30"),
         48, 0.0, 4.0, ("植物检疫证书",), True),
    Rule("JP-FRESH-2026H2", "日本", "鲜品", _d("2026-07-01"), None,
         36, 0.0, 4.0, ("植物检疫证书", "卫生证书"), True),
    Rule("JP-FROZEN-1", "日本", "冷冻", _d("2026-01-01"), None,
         96, -25.0, -18.0, ("卫生证书",), False),
    # 德国：鲜品需三证，干制放宽时限
    Rule("DE-FRESH-1", "德国", "鲜品", _d("2026-01-01"), None,
         48, 0.0, 4.0, ("植物检疫证书", "卫生证书", "原产地证书"), True),
    Rule("DE-DRIED-1", "德国", "干制", _d("2026-01-01"), None,
         240, -5.0, 25.0, ("卫生证书", "原产地证书"), False),
    # 越南：陆路近程，时限短但单证要求少
    Rule("VN-FRESH-1", "越南", "鲜品", _d("2026-01-01"), None,
         24, 0.0, 4.0, ("植物检疫证书",), False),
    Rule("VN-DRIED-1", "越南", "干制", _d("2026-01-01"), None,
         120, -5.0, 25.0, ("植物检疫证书",), False),
]


def select_rule(rules, destination, form, departure):
    """按计划出运时间选择当时生效的目的地规则版本。"""
    day = departure.date() if isinstance(departure, datetime) else departure
    for rule in rules:
        if rule.destination == destination and rule.form == form and rule.covers(day):
            return rule
    return None


@dataclass
class Flight:
    """每日一班的航线时刻。"""
    flight_no: str
    port: str
    destination: str
    depart_hhmm: str
    transit_hours: float

    def departure_on(self, day):
        hh, mm = (int(x) for x in self.depart_hhmm.split(":"))
        return datetime.combine(day, time(hh, mm), tzinfo=timezone.utc)


FLIGHTS = [
    Flight("MU261", "昆明长水", "日本", "10:30", 8),
    Flight("CA927", "昆明长水", "日本", "23:10", 9),
    Flight("3U8085", "成都天府", "日本", "13:20", 7),
    Flight("CA961", "昆明长水", "德国", "11:40", 15),
    Flight("LH797", "成都天府", "德国", "14:05", 13),
    Flight("CZ301", "昆明长水", "越南", "09:20", 3),
    Flight("VN593", "昆明长水", "越南", "18:45", 3),
]


def find_flights(destination, earliest, latest, port=None):
    """列出时间窗内可达目的地的航班时刻，供替代路线评估。"""
    out = []
    day = earliest.date()
    while day <= latest.date():
        for flight in FLIGHTS:
            if flight.destination != destination or (port and flight.port != port):
                continue
            dep = flight.departure_on(day)
            if earliest <= dep <= latest:
                out.append({"flight_no": flight.flight_no, "port": flight.port,
                            "departure": dep, "arrival": dep + timedelta(hours=flight.transit_hours),
                            "transit_hours": flight.transit_hours})
        day += timedelta(days=1)
    return sorted(out, key=lambda f: f["departure"])
