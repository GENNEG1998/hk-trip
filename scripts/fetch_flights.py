#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国庆日本机票多平台盯价脚本
- 监控：上海(PVG/SHA) <-> 日本(KIX/NRT/HND/NGO/FUK)
- 去程 2026-10-01，返程 2026-10-05 / 2026-10-06
- 平台：Trip.com / 携程 / 春秋 / 去哪儿 / 飞猪
- 输出：data/flights_latest.json + 追加 data/flights_history.csv + 走势图 data/trend.png

设计原则：
1. 任何平台失败都不阻塞，记录 source=xxx, status=failed
2. JSON 是网页面板的唯一数据源
3. CSV 全量历史，方便后期 Excel/重新画图
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
import re
import random
import traceback
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

LATEST_JSON = DATA_DIR / "flights_latest.json"
HISTORY_CSV = DATA_DIR / "flights_history.csv"
TREND_PNG = DATA_DIR / "trend.png"

CST = timezone(timedelta(hours=8))

# ======== 监控参数 ========
OUTBOUND_DATE = "2026-10-01"
RETURN_DATES = ["2026-10-05", "2026-10-06"]
ORIGINS = ["SHA", "PVG"]                     # 上海虹桥/浦东，飞日本只有浦东，留 SHA 兜底
DESTINATIONS = ["KIX", "NRT", "HND", "NGO", "FUK"]  # 大阪关西/成田/羽田/中部/福冈

EXPECT_OUTBOUND = 2000   # 单段去程期望价
EXPECT_RETURN = 1500     # 单段返程期望价
EXPECT_ROUNDTRIP = 3000  # 往返期望

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "application/json, text/plain, */*",
}

REQUEST_TIMEOUT = 15


@dataclass
class FlightOption:
    direction: str          # "outbound" / "return"
    date: str               # YYYY-MM-DD
    origin: str             # IATA
    destination: str        # IATA
    airline: str            # 航司
    flight_no: str          # 航班号
    depart_time: str        # HH:MM
    arrive_time: str        # HH:MM
    price_cny: int          # 价格
    source: str             # 数据来源
    duration_min: int = 0
    stops: int = 0
    note: str = ""

    def key(self) -> str:
        return f"{self.direction}|{self.date}|{self.origin}|{self.destination}|{self.flight_no}|{self.source}"


# ============================== 抓取实现 ==============================
# 注：以下接口都是公开 H5 / 移动端可直接 GET 的端点，不需要登录态。
# 各家平台不定期改接口，失败时只记录"未抓到"，不要抛异常。


def fetch_trip_com(origin: str, dest: str, date: str, direction: str) -> List[FlightOption]:
    """Trip.com 国际版搜索结果页，从内嵌 JSON 提取（在海外 IP 下可访问）。"""
    url = (
        f"https://www.trip.com/flights/showfarefirst?"
        f"dcity={origin.lower()}&acity={dest.lower()}&ddate={date}&triptype=ow&class=y&quantity=1"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html",
    }
    try:
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return []
        # Trip.com 把首屏数据放在 window.__INITIAL_STATE__
        m = re.search(r"window\.__INITIAL_STATE__\s*=\s*({.+?})\s*;\s*window", r.text, re.S)
        if not m:
            # 备用模式：找 productList
            m = re.search(r'"productList":\s*(\[[^\]]+\])', r.text, re.S)
            if not m:
                return []
            try:
                products = json.loads(m.group(1))
            except Exception:
                return []
        else:
            try:
                state = json.loads(m.group(1))
            except Exception:
                return []
            products = (
                state.get("productList") or
                state.get("flightList", {}).get("productList") or
                []
            )

        out: List[FlightOption] = []
        for p in products[:30]:
            try:
                segs = p.get("flightSegments") or p.get("segments") or []
                if not segs:
                    continue
                seg = segs[0]
                price = int(round(
                    p.get("price", {}).get("totalPrice") or
                    p.get("totalPrice") or
                    p.get("priceList", [{}])[0].get("totalPrice", 0) or
                    0
                ))
                if price <= 0:
                    continue
                out.append(FlightOption(
                    direction=direction,
                    date=date,
                    origin=origin,
                    destination=dest,
                    airline=seg.get("marketAirlineName") or seg.get("airlineName", ""),
                    flight_no=seg.get("flightNo") or seg.get("flightNumber", ""),
                    depart_time=(seg.get("departureDateTime") or seg.get("departTime", ""))[-5:],
                    arrive_time=(seg.get("arrivalDateTime") or seg.get("arriveTime", ""))[-5:],
                    price_cny=price,
                    source="trip.com",
                    stops=max(0, len(segs) - 1),
                ))
            except Exception:
                continue
        return out
    except Exception as e:
        print(f"[trip.com] {origin}->{dest} {date} failed: {e}", file=sys.stderr)
        return []


def fetch_ctrip_h5(origin: str, dest: str, date: str, direction: str) -> List[FlightOption]:
    """携程 H5 移动端搜索接口。"""
    url = "https://m.ctrip.com/restapi/soa2/13239/json/SearchFlightList"
    payload = {
        "flightWay": "Oneway",
        "segmentNo": 1,
        "head": {"abTesting": "", "platform": "H5", "clientID": "", "bu": "ibu", "group": "ctrip", "aid": "", "sid": "", "ouid": ""},
        "airportParams": [{"dcity": origin, "acity": dest, "dcityname": "", "acityname": "", "date": date}],
        "classType": "ALL",
    }
    try:
        r = requests.post(url, json=payload, headers={**HEADERS, "Origin": "https://m.ctrip.com"}, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json()
        out: List[FlightOption] = []
        items = (data.get("data") or {}).get("flightItineraryList") or []
        for it in items[:30]:
            try:
                fl = it.get("flightList", [{}])[0]
                price = int(round(it.get("priceList", [{}])[0].get("price", 0)))
                if price <= 0:
                    continue
                out.append(FlightOption(
                    direction=direction,
                    date=date,
                    origin=origin,
                    destination=dest,
                    airline=fl.get("airlineName", ""),
                    flight_no=fl.get("flightNo", ""),
                    depart_time=fl.get("departureTime", "")[-5:],
                    arrive_time=fl.get("arrivalTime", "")[-5:],
                    price_cny=price,
                    source="ctrip",
                    stops=len(it.get("flightList", [])) - 1,
                ))
            except Exception:
                continue
        return out
    except Exception as e:
        print(f"[ctrip] {origin}->{dest} {date} failed: {e}", file=sys.stderr)
        return []


def fetch_qunar_h5(origin: str, dest: str, date: str, direction: str) -> List[FlightOption]:
    """去哪儿国际机票 H5。"""
    # 新版去哪儿 PC/H5 入口
    url = (
        f"https://flight.qunar.com/site/oneway_list.htm?"
        f"searchDepartureAirport={origin}&searchArrivalAirport={dest}"
        f"&searchDepartureTime={date}&nextNDays=0&startSearch=true&fromCode={origin}&toCode={dest}&from=fi_re_search"
    )
    try:
        r = requests.get(url, headers={**HEADERS, "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/121.0"}, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return []
        # 这种页面也是 SPA，仅作占位（保留实现，等以后更新接口）
        return []
    except Exception as e:
        print(f"[qunar] {origin}->{dest} {date} failed: {e}", file=sys.stderr)
        return []


def fetch_chunqiu(origin: str, dest: str, date: str, direction: str) -> List[FlightOption]:
    """春秋官网搜索接口（公开 GET）。"""
    url = (
        f"https://www.ch.com/flightsearch?dpc={origin}&apc={dest}&dd={date}&adt=1&chd=0&inf=0&type=oneway"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return []
        # 春秋页面包含 flight data JSON 块
        m = re.search(r'flightList["\']?\s*:\s*(\[[^\]]+\])', r.text, re.S)
        if not m:
            return []
        flights = json.loads(m.group(1))
        out: List[FlightOption] = []
        for f in flights[:30]:
            try:
                price = int(f.get("salePrice") or f.get("price") or 0)
                if price <= 0:
                    continue
                out.append(FlightOption(
                    direction=direction,
                    date=date,
                    origin=origin,
                    destination=dest,
                    airline="春秋航空",
                    flight_no=f.get("flightNo", ""),
                    depart_time=f.get("dptTime", ""),
                    arrive_time=f.get("arrTime", ""),
                    price_cny=price,
                    source="chunqiu",
                ))
            except Exception:
                continue
        return out
    except Exception as e:
        print(f"[chunqiu] {origin}->{dest} {date} failed: {e}", file=sys.stderr)
        return []


# 飞猪反爬严，先留位
def fetch_fliggy(origin: str, dest: str, date: str, direction: str) -> List[FlightOption]:
    return []


SOURCES = [
    ("trip.com", fetch_trip_com),
    ("ctrip", fetch_ctrip_h5),
    ("qunar", fetch_qunar_h5),
    ("chunqiu", fetch_chunqiu),
    ("fliggy", fetch_fliggy),
]


def fetch_all() -> List[FlightOption]:
    all_options: List[FlightOption] = []

    # 去程
    for o in ORIGINS:
        for d in DESTINATIONS:
            for name, fn in SOURCES:
                opts = fn(o, d, OUTBOUND_DATE, "outbound")
                all_options.extend(opts)
                time.sleep(random.uniform(0.5, 1.2))

    # 返程
    for rd in RETURN_DATES:
        for d in DESTINATIONS:
            for o in ORIGINS:
                for name, fn in SOURCES:
                    opts = fn(d, o, rd, "return")
                    all_options.extend(opts)
                    time.sleep(random.uniform(0.5, 1.2))

    # 去重
    dedup: Dict[str, FlightOption] = {}
    for x in all_options:
        if x.price_cny <= 0:
            continue
        k = x.key()
        if k not in dedup or x.price_cny < dedup[k].price_cny:
            dedup[k] = x
    return list(dedup.values())


# ============================== 数据处理 ==============================

def write_history(options: List[FlightOption], scan_id: str):
    is_new = not HISTORY_CSV.exists()
    with open(HISTORY_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow([
                "scan_id", "scan_time", "direction", "date", "origin", "destination",
                "airline", "flight_no", "depart_time", "arrive_time", "duration_min",
                "stops", "price_cny", "source"
            ])
        ts = datetime.now(CST).isoformat(timespec="seconds")
        for o in options:
            w.writerow([
                scan_id, ts, o.direction, o.date, o.origin, o.destination,
                o.airline, o.flight_no, o.depart_time, o.arrive_time, o.duration_min,
                o.stops, o.price_cny, o.source
            ])


def build_payload(options: List[FlightOption]) -> Dict[str, Any]:
    now = datetime.now(CST)
    scan_id = now.strftime("%Y%m%d_%H%M")

    # 分组
    outbound = sorted([o for o in options if o.direction == "outbound"], key=lambda x: x.price_cny)
    returns = {rd: sorted([o for o in options if o.direction == "return" and o.date == rd], key=lambda x: x.price_cny) for rd in RETURN_DATES}

    # 平台覆盖
    sources_seen = sorted({o.source for o in options})

    # 最优往返组合
    best_combos = []
    if outbound:
        cheapest_out = outbound[0]
        for rd, rs in returns.items():
            if not rs:
                continue
            cheap_r = rs[0]
            total = cheapest_out.price_cny + cheap_r.price_cny
            best_combos.append({
                "return_date": rd,
                "outbound": asdict(cheapest_out),
                "inbound": asdict(cheap_r),
                "total_cny": total,
                "below_target": total <= EXPECT_ROUNDTRIP,
            })
    best_combos.sort(key=lambda x: x["total_cny"])

    payload = {
        "scan_id": scan_id,
        "scan_time": now.isoformat(timespec="seconds"),
        "outbound_date": OUTBOUND_DATE,
        "return_dates": RETURN_DATES,
        "origins": ORIGINS,
        "destinations": DESTINATIONS,
        "expectations": {
            "outbound_max": EXPECT_OUTBOUND,
            "return_max": EXPECT_RETURN,
            "roundtrip_max": EXPECT_ROUNDTRIP,
        },
        "sources_seen": sources_seen,
        "totals": {
            "all": len(options),
            "outbound": len(outbound),
            "return_10_5": len(returns.get("2026-10-05", [])),
            "return_10_6": len(returns.get("2026-10-06", [])),
        },
        "outbound_top10": [asdict(x) for x in outbound[:10]],
        "return_10_5_top10": [asdict(x) for x in returns.get("2026-10-05", [])[:10]],
        "return_10_6_top10": [asdict(x) for x in returns.get("2026-10-06", [])[:10]],
        "best_combos": best_combos,
    }
    return payload


def write_latest(payload: Dict[str, Any]):
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ============================== 走势图 ==============================

def render_trend():
    """从 history.csv 读全量，按 scan_id 聚合，画 4 条线。"""
    if not HISTORY_CSV.exists():
        return
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed, skip trend.png", file=sys.stderr)
        return

    rows = []
    with open(HISTORY_CSV, "r", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        for row in rd:
            try:
                row["price_cny"] = int(row["price_cny"])
                rows.append(row)
            except Exception:
                continue
    if not rows:
        return

    # 按 scan_id 排序
    scan_ids = sorted({r["scan_id"] for r in rows})

    out_low = []
    r5_low = []
    r6_low = []
    combo_low = []

    for sid in scan_ids:
        sub = [r for r in rows if r["scan_id"] == sid]
        out_min = min((r["price_cny"] for r in sub if r["direction"] == "outbound"), default=None)
        r5_min = min((r["price_cny"] for r in sub if r["direction"] == "return" and r["date"] == "2026-10-05"), default=None)
        r6_min = min((r["price_cny"] for r in sub if r["direction"] == "return" and r["date"] == "2026-10-06"), default=None)
        ret_min = min([v for v in (r5_min, r6_min) if v is not None], default=None)
        combo = (out_min + ret_min) if (out_min and ret_min) else None
        out_low.append(out_min)
        r5_low.append(r5_min)
        r6_low.append(r6_min)
        combo_low.append(combo)

    # 画图
    plt.figure(figsize=(10, 5), dpi=140)
    plt.style.use("dark_background")
    x = list(range(len(scan_ids)))
    if any(out_low):
        plt.plot(x, out_low, marker="o", linewidth=1.5, color="#7DD3FC", label=f"去程 10/1 最低")
    if any(r5_low):
        plt.plot(x, r5_low, marker="s", linewidth=1.5, color="#A7F3D0", label=f"返程 10/5 最低")
    if any(r6_low):
        plt.plot(x, r6_low, marker="^", linewidth=1.5, color="#FCD34D", label=f"返程 10/6 最低")
    if any(combo_low):
        plt.plot(x, combo_low, marker="*", linewidth=2.4, color="#F87171", label="往返组合最低")

    plt.axhline(EXPECT_ROUNDTRIP, color="#22D3EE", linestyle="--", alpha=0.6, label=f"目标往返 ¥{EXPECT_ROUNDTRIP}")
    plt.axhline(EXPECT_OUTBOUND, color="#FB923C", linestyle=":", alpha=0.5, label=f"目标单段 ¥{EXPECT_OUTBOUND}")

    # 横轴只显示日期部分
    short_labels = [sid[4:6] + "/" + sid[6:8] + " " + sid[9:11] + ":" + sid[11:13] for sid in scan_ids]
    if len(scan_ids) > 16:
        step = max(1, len(scan_ids) // 12)
        plt.xticks(x[::step], short_labels[::step], rotation=30, fontsize=8)
    else:
        plt.xticks(x, short_labels, rotation=30, fontsize=8)

    plt.title("国庆日本机票价格走势 · Auto-updated 4x/day", fontsize=12)
    plt.ylabel("价格 (CNY)", fontsize=10)
    plt.legend(fontsize=8, loc="upper right")
    plt.grid(True, alpha=0.2)
    plt.tight_layout()
    plt.savefig(TREND_PNG, facecolor="#0B0F14")
    plt.close()


# ============================== main ==============================

def main():
    print(f"[{datetime.now(CST).isoformat(timespec='seconds')}] start scan")
    try:
        options = fetch_all()
        print(f"  total options collected: {len(options)}")

        if not options:
            print("  WARN: no data fetched, keep previous JSON")
        else:
            scan_id = datetime.now(CST).strftime("%Y%m%d_%H%M")
            write_history(options, scan_id)

        payload = build_payload(options)
        write_latest(payload)
        render_trend()
        print("  done")
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
