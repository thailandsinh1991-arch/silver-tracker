#!/usr/bin/env python3
"""Cập nhật data/market.json từ bảng giá Phú Quý và giá bạc thế giới (Kitco).

Chạy bởi GitHub Actions. Chỉ dùng thư viện chuẩn của Python.
Nếu một nguồn lỗi, dữ liệu cũ của nguồn đó được giữ nguyên.
Hỗ trợ chế độ SHADOW-MODE (--shadow-only) để chạy song song logic cũ (Legacy)
và PriceEngine mới mà không ghi đè data/market.json.
"""
import argparse
import html
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from price_engine import PriceEngine, _USNewYorkTz, parse_iso, iso_utc  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data" / "market.json"
SHADOW_OUT = Path(__file__).resolve().parent.parent / "tmp" / "shadow-price-comparison.json"
PQ_URL = "https://giabac.phuquygroup.vn/"
KITCO_URL = "https://www.kitco.com/charts/silver"
TARGET = "BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG"
OZ_PER_KG = 32.1507466
VN = timezone(timedelta(hours=7))
try:
    NY = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    NY = _USNewYorkTz()
KEEP = 240  # market.json chỉ giữ 240 điểm gần nhất cho biểu đồ nhỏ; bản đầy đủ nằm trong archive/
ARCHIVE = DATA.parent.parent / "archive"
PQ_CSV = ARCHIVE / "gia-phu-quy.csv"
WORLD_CSV = ARCHIVE / "gia-the-gioi.csv"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "vi,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def to_int(s):
    d = re.sub(r"[^\d]", "", s or "")
    return int(d) if d else None


class Rows(HTMLParser):
    """Gom bảng HTML thành danh sách hàng, mỗi hàng là danh sách ô (text)."""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.lines = [], None, None, []
        self.buf = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "span"):
            self._flush_line()

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
            self.row = None
        if tag in ("p", "div", "li", "td", "th", "tr", "h1", "h2", "h3", "h4", "span"):
            self._flush_line()

    def handle_data(self, data):
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
        self.buf.append(data)

    def _flush_line(self):
        t = " ".join("".join(self.buf).split())
        if t:
            self.lines.append(t)
        self.buf = []


UNIT_RE = re.compile(r"VN[ĐD]\s*/\s*(LƯỢNG|KG|CHỈ|KILO)", re.I)
PRICE_RE = re.compile(r"^\d{1,3}(?:[.,]\d{3})+$|^\d{5,}$")


def norm_unit(u):
    m = UNIT_RE.search(u or "")
    return f"VNĐ/{m.group(1).upper()}" if m else (u or "").strip()


def parse_phuquy(page):
    p = Rows()
    p.feed(page)
    p._flush_line()
    text = "\n".join(p.lines)

    items = []
    for row in p.rows:
        ui = next((i for i, c in enumerate(row) if UNIT_RE.search(c)), None)
        if ui is None or ui == 0:
            continue
        name = " ".join(row[:ui]).strip().upper()
        if "BẠC" not in name:
            continue
        rest = row[ui + 1:]
        nums = [to_int(c) if PRICE_RE.match(c.strip()) else None for c in rest[:2]]
        while len(nums) < 2:
            nums.append(None)
        if nums[0] is None:
            continue
        items.append({"type": name, "unit": norm_unit(row[ui]), "buy": nums[0], "sell": nums[1]})

    # Dự phòng: trang không dùng <table>, quét theo dòng chữ.
    if not items:
        lines = p.lines
        for i, ln in enumerate(lines):
            if "BẠC" in ln and ln == ln.upper() and len(ln) < 80 and not UNIT_RE.search(ln):
                win = lines[i + 1:i + 6]
                unit = next((w for w in win if UNIT_RE.fullmatch(w.strip()) or UNIT_RE.match(w.strip())), None)
                if not unit:
                    continue
                after = win[win.index(unit) + 1:]
                vals = []
                for w in after:
                    w = w.strip()
                    if PRICE_RE.match(w):
                        vals.append(to_int(w))
                    elif w in ("—", "-", "–"):
                        vals.append(None)
                    else:
                        break
                    if len(vals) == 2:
                        break
                if vals and vals[0]:
                    vals += [None] * (2 - len(vals))
                    items.append({"type": ln.strip(), "unit": norm_unit(unit), "buy": vals[0], "sell": vals[1]})

    # bỏ trùng, giữ thứ tự
    seen, uniq = set(), []
    for it in items:
        if it["type"] not in seen:
            seen.add(it["type"])
            uniq.append(it)
    items = uniq

    m = re.search(r"(\d{1,2}):(\d{2})\s+(\d{1,2})/(\d{1,2})/(\d{4})", text)
    as_of = None
    if m:
        hh, mm, dd, mo, yy = map(int, m.groups())
        as_of = datetime(yy, mo, dd, hh, mm, tzinfo=VN).isoformat()

    ref = None
    m = re.search(r"([\d.,]+)\s*USD\s*/\s*OUNCE\s*[≈~=]?\s*([\d.,]+)\s*VN[ĐD]\s*/\s*KG", text, re.I)
    if m:
        usd = float(m.group(1).replace(",", "")) if m.group(1).count(".") <= 1 else None
        ref = {"usd": usd, "vndPerKg": to_int(m.group(2))}

    return items, as_of, ref


def parse_kitco(page):
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    text = " ".join(text.split())
    bid, win = None, ""
    m = re.search(r"\bBid\b\s*\$?\s*([0-9]{1,3}\.[0-9]{2,3})", text)
    if m:
        bid = float(m.group(1))
        win = text[max(0, m.start() - 300):m.end() + 300]
    if bid is None:
        m = re.search(r'"bid"\s*:\s*"?([0-9]{1,3}\.[0-9]{1,3})', page)
        if m:
            bid = float(m.group(1))
    pct = None
    m = re.search(r"([+\-−]?[0-9]+\.[0-9]{1,2})\s*%", win)
    if m:
        pct = float(m.group(1).replace("−", "-"))

    as_of = None
    months = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    m = re.search(rf"({months})[a-z]*\.?\s+(\d{{1,2}}),\s*(\d{{4}})\s*(?:-|at)\s*(\d{{1,2}}):(\d{{2}})\s*([ap])?\.?\s*m?\.?", text, re.I)
    if m:
        mon = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(m.group(1)[:3].lower()) + 1
        h, mi = int(m.group(4)), int(m.group(5))
        ap = (m.group(6) or "").lower()
        if ap == "p" and h < 12:
            h += 12
        if ap == "a" and h == 12:
            h = 0
        as_of = datetime(int(m.group(3)), mon, int(m.group(2)), h, mi, tzinfo=NY).isoformat()
    return bid, pct, as_of


def csv_append(path, header, rows):
    """Ghi thêm dòng vào sổ lưu trữ vĩnh viễn. Không bao giờ xoá hay sửa dòng cũ."""
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def csv_last_key(path, ncols):
    """Khoá (các cột đầu) của dòng cuối cùng, để không ghi trùng."""
    if not path.exists():
        return None
    lines = [ln for ln in path.read_text("utf-8").splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    import csv
    last_time = next(csv.reader([lines[-1]]))[0]
    return [r for r in csv.reader(lines[1:]) if r and r[0] == last_time][: ncols or None]


def fmt_dots(n):
    return f"{n:,}".replace(",", ".")


# ============================================================================
# SHADOW-MODE COMPARISON & BACKWARD COMPATIBILITY VERIFICATION
# ============================================================================

def run_legacy_in_memory(base_doc, fetch_fn=fetch, now_fn=lambda: datetime.now(timezone.utc)):
    """Chạy đúng logic cũ (Legacy) trong bộ nhớ (không chạm vào ổ cứng hay CSV) để đối chiếu."""
    doc = json.loads(json.dumps(base_doc or {}))
    before = json.loads(json.dumps(doc))
    history = doc.get("history") or []
    world_hist = doc.get("worldHistory") or []
    errors = []

    t0_pq = time.perf_counter()
    pq_ok, ref, price_as_of = False, None, None
    try:
        items, price_as_of, ref = parse_phuquy(fetch_fn(PQ_URL))
        tgt = next((i for i in items if i["type"] == TARGET), None)
        if tgt and 500_000 <= tgt["buy"] <= 20_000_000 and tgt["sell"] and tgt["sell"] >= tgt["buy"] and price_as_of:
            pq_ok = True
            doc["target"] = {"name": TARGET, "buy": tgt["buy"], "sell": tgt["sell"]}
            doc["items"] = items
            doc["priceAsOf"] = doc["itemsAsOf"] = price_as_of
            doc["source"] = "phuquy"
            last = history[-1] if history else {}
            if (last.get("at"), last.get("buy"), last.get("sell")) != (price_as_of, tgt["buy"], tgt["sell"]):
                history.append({"at": price_as_of, "buy": tgt["buy"], "sell": tgt["sell"]})
            doc["history"] = history[-KEEP:]
        else:
            errors.append(f"phuquy: không đọc được dòng {TARGET} (tìm thấy {len(items)} dòng, giờ={price_as_of})")
    except Exception as e:  # noqa: BLE001
        errors.append(f"phuquy: {type(e).__name__}: {e}")
    pq_latency_ms = int(round((time.perf_counter() - t0_pq) * 1000))

    rate = None
    if ref and ref.get("usd") and ref.get("vndPerKg"):
        rate = ref["vndPerKg"] / (ref["usd"] * OZ_PER_KG)
    elif (doc.get("world") or {}).get("usdPerOz") and doc["world"].get("vndPerKg"):
        rate = doc["world"]["vndPerKg"] / (doc["world"]["usdPerOz"] * OZ_PER_KG)

    t0_w = time.perf_counter()
    world = None
    try:
        bid, pct, w_as_of = parse_kitco(fetch_fn(KITCO_URL))
        sane = bid is not None and 5 <= bid <= 500 and w_as_of
        if sane and ref and ref.get("usd"):
            sane = abs(bid - ref["usd"]) / ref["usd"] < 0.15
        if sane and rate:
            world = {
                "usdPerOz": round(bid, 2), "changePct": pct, "asOf": w_as_of, "source": "Kitco (giá bid)",
                "vndPerKg": round(bid * OZ_PER_KG * rate),
                "note": f"Quy đổi theo tỷ giá Phú Quý dùng cho giá tham chiếu (≈{fmt_dots(int(round(rate / 10) * 10))} ₫/USD), trước thuế và phí nhập khẩu",
            }
        else:
            errors.append(f"kitco: không đọc được giá (bid={bid}, giờ={w_as_of})")
    except Exception as e:  # noqa: BLE001
        errors.append(f"kitco: {type(e).__name__}: {e}")

    if world is None and ref and ref.get("usd") and pq_ok:
        world = {"usdPerOz": ref["usd"], "changePct": None, "asOf": price_as_of, "source": "Bảng giá Phú Quý",
                 "vndPerKg": ref["vndPerKg"], "note": "Trước thuế và phí nhập khẩu"}
    w_latency_ms = int(round((time.perf_counter() - t0_w) * 1000))

    if world:
        doc["world"] = world
        last = world_hist[-1] if world_hist else {}
        if (last.get("at"), last.get("usd")) != (world["asOf"], world["usdPerOz"]):
            world_hist.append({"at": world["asOf"], "usd": world["usdPerOz"]})
        doc["worldHistory"] = world_hist[-KEEP:]

    doc["errors"] = errors
    now = now_fn()
    if pq_ok or world:
        doc["fetchedAt"] = before.get("fetchedAt")
        try:
            age = now - datetime.fromisoformat(str(before.get("fetchedAt")).replace("Z", "+00:00"))
        except ValueError:
            age = timedelta(days=1)
        if doc != before or age > timedelta(hours=2):
            doc["fetchedAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "doc": doc,
        "pqOk": pq_ok,
        "worldOk": world is not None,
        "pqLatencyMs": pq_latency_ms,
        "worldLatencyMs": w_latency_ms,
        "errors": errors,
        "executedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _ts_diff_and_fresher(legacy_ts_str, engine_ts_str):
    dt_leg = parse_iso(legacy_ts_str)
    dt_eng = parse_iso(engine_ts_str)
    if not dt_leg and not dt_eng:
        return None, "neither"
    if dt_eng and not dt_leg:
        return None, "engine"
    if dt_leg and not dt_eng:
        return None, "legacy"
    diff_sec = int(round((dt_eng - dt_leg).total_seconds()))
    if abs(diff_sec) < 1:
        return 0, "equal"
    return diff_sec, ("engine" if diff_sec > 0 else "legacy")


def build_compatible_market_doc(base_doc, engine_cycle):
    """Builds the production-compatible market.json document from a PriceEngine cycle."""
    cand_doc = json.loads(json.dumps(base_doc or {}))
    before_core = {
        k: json.loads(json.dumps(cand_doc.get(k)))
        for k in ("target", "items", "priceAsOf", "itemsAsOf", "source", "world", "history", "worldHistory")
    }
    history = list(cand_doc.get("history") or [])
    world_hist = list(cand_doc.get("worldHistory") or [])
    errors = []

    # --- 1. Phú Quý ---
    pq_res = engine_cycle["phuQuy"]
    pq_sel = pq_res.get("selected")
    pq_used_lkg = bool(pq_res.get("usedLastKnownGood"))
    pq_live_ok = (pq_sel is not None) and (not pq_used_lkg)
    pq_history_appended = False

    if pq_sel:
        tgt = pq_sel["target"]
        price_as_of = pq_sel["sourceTimestamp"]
        cand_doc["target"] = {"name": TARGET, "buy": tgt["buy"], "sell": tgt["sell"]}
        if pq_sel.get("items"):
            cand_doc["items"] = pq_sel["items"]
        cand_doc["priceAsOf"] = cand_doc["itemsAsOf"] = price_as_of
        if not pq_used_lkg:
            cand_doc["source"] = "phuquy" if pq_sel.get("isAuthoritative", True) else pq_sel["sourceName"]
        else:
            cand_doc.setdefault("source", "phuquy")

        if pq_live_ok and price_as_of:
            last = history[-1] if history else {}
            if (last.get("at"), last.get("buy"), last.get("sell")) != (price_as_of, tgt["buy"], tgt["sell"]):
                if last.get("at") == price_as_of:
                    history[-1] = {"at": price_as_of, "buy": tgt["buy"], "sell": tgt["sell"]}
                else:
                    history.append({"at": price_as_of, "buy": tgt["buy"], "sell": tgt["sell"]})
                pq_history_appended = True
            cand_doc["history"] = history[-KEEP:]
        else:
            cand_doc["history"] = history[-KEEP:]
            errors.append(f"phuquy: all live sources failed; preserved Last-Known-Good ({tgt['buy']}/{tgt['sell']} @ {price_as_of})")
    else:
        cand_doc["history"] = history[-KEEP:]
        errors.append("phuquy: all live sources failed and no Last-Known-Good available")

    cand_doc["marketStatus"] = pq_res["marketSchedule"]["marketStatus"]
    cand_doc["freshnessStatus"] = pq_res["freshnessStatus"]

    # --- 2. XAG/USD (World) ---
    xag_res = engine_cycle["xagUsd"]
    xag_sel = xag_res.get("selected")
    xag_used_lkg = bool(xag_res.get("usedLastKnownGood"))
    xag_live_ok = (xag_sel is not None) and (not xag_used_lkg)
    xag_history_appended = False

    if xag_sel:
        prev_world = cand_doc.get("world") or {}
        if xag_used_lkg and prev_world.get("note"):
            note_str = prev_world["note"]
            src_str = prev_world.get("source") or xag_sel["sourceName"]
        else:
            note_str = f"Quy đổi theo tỷ giá Phú Quý dùng cho giá tham chiếu (≈{fmt_dots(xag_sel['rateVndUsd'])} ₫/USD), trước thuế và phí nhập khẩu"
            src_str = xag_sel["sourceName"]

        world = {
            "usdPerOz": xag_sel["price"],
            "changePct": xag_sel.get("changePct"),
            "asOf": xag_sel["sourceTimestamp"],
            "source": src_str,
            "vndPerKg": xag_sel["vndPerKg"],
            "note": note_str,
        }
        cand_doc["world"] = world

        if xag_live_ok and world.get("asOf"):
            last = world_hist[-1] if world_hist else {}
            if (last.get("at"), last.get("usd")) != (world["asOf"], world["usdPerOz"]):
                world_hist.append({"at": world["asOf"], "usd": world["usdPerOz"]})
                xag_history_appended = True
            cand_doc["worldHistory"] = world_hist[-KEEP:]
        else:
            cand_doc["worldHistory"] = world_hist[-KEEP:]
            errors.append(f"xagUsd: all live sources failed; preserved Last-Known-Good ({world['usdPerOz']} USD/oz @ {world['asOf']})")
    else:
        cand_doc["worldHistory"] = world_hist[-KEEP:]
        errors.append("xagUsd: all live sources failed and no Last-Known-Good available")

    cand_doc["errors"] = errors
    cand_doc["sourceHealth"] = engine_cycle["sourceHealth"]
    cand_doc["engineMetadata"] = {
        "phuQuy": {
            "sourceName": pq_sel.get("sourceName") if pq_sel else None,
            "adapter": pq_sel.get("adapter") if pq_sel else None,
            "freshnessStatus": pq_res.get("freshnessStatus"),
            "marketStatus": pq_res["marketSchedule"]["marketStatus"],
            "usedLastKnownGood": pq_used_lkg,
            "confirmedBy": (pq_sel.get("confirmedBy") or []) if pq_sel else [],
            "disagreement": pq_res.get("disagreement"),
        },
        "xagUsd": {
            "sourceName": xag_sel.get("sourceName") if xag_sel else None,
            "adapter": xag_sel.get("adapter") if xag_sel else None,
            "freshnessStatus": xag_res.get("freshnessStatus"),
            "marketStatus": xag_res["marketSchedule"]["marketStatus"],
            "usedLastKnownGood": xag_used_lkg,
            "confirmedBy": (xag_sel.get("confirmedBy") or []) if xag_sel else [],
            "disagreement": xag_res.get("disagreement"),
        },
    }

    now_iso = engine_cycle["timestamp"]
    now_dt = parse_iso(now_iso) or datetime.now(timezone.utc)
    after_core = {
        k: cand_doc.get(k)
        for k in ("target", "items", "priceAsOf", "itemsAsOf", "source", "world", "history", "worldHistory")
    }
    if pq_live_ok or xag_live_ok or pq_sel or xag_sel:
        prev_fetched = (base_doc or {}).get("fetchedAt")
        cand_doc["fetchedAt"] = prev_fetched or now_iso
        try:
            age = now_dt - datetime.fromisoformat(str(prev_fetched).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            age = timedelta(days=1)
        if after_core != before_core or age > timedelta(hours=2):
            cand_doc["fetchedAt"] = now_iso
    else:
        cand_doc.setdefault("fetchedAt", now_iso)

    return {
        "doc": cand_doc,
        "pqLiveOk": pq_live_ok,
        "pqUsedLkg": pq_used_lkg,
        "pqHistoryAppended": pq_history_appended,
        "xagLiveOk": xag_live_ok,
        "xagUsedLkg": xag_used_lkg,
        "xagHistoryAppended": xag_history_appended,
        "errors": errors,
    }


def verify_backward_compatibility(base_doc, engine_cycle):
    """Phase F: Kiểm tra nếu PriceEngine tạo ra doc thì mọi trường cũ vẫn giữ đúng kiểu dữ liệu."""
    built = build_compatible_market_doc(base_doc, engine_cycle)
    cand_doc = built["doc"]

    required_checks = {
        "target": isinstance(cand_doc.get("target"), dict) and isinstance(cand_doc["target"].get("buy"), int) and isinstance(cand_doc["target"].get("sell"), int),
        "items": isinstance(cand_doc.get("items"), list) and len(cand_doc["items"]) > 0,
        "priceAsOf": isinstance(cand_doc.get("priceAsOf"), str) and parse_iso(cand_doc["priceAsOf"]) is not None,
        "itemsAsOf": isinstance(cand_doc.get("itemsAsOf"), str) and parse_iso(cand_doc["itemsAsOf"]) is not None,
        "source": isinstance(cand_doc.get("source"), str) and len(cand_doc["source"]) > 0,
        "world": isinstance(cand_doc.get("world"), dict) and isinstance(cand_doc["world"].get("usdPerOz"), (int, float)) and isinstance(cand_doc["world"].get("vndPerKg"), int),
        "history": isinstance(cand_doc.get("history"), list),
        "worldHistory": isinstance(cand_doc.get("worldHistory"), list),
        "fetchedAt": isinstance(cand_doc.get("fetchedAt"), str) and parse_iso(cand_doc["fetchedAt"]) is not None,
        "errors": isinstance(cand_doc.get("errors"), list),
        "additive_sourceHealth": isinstance(cand_doc.get("sourceHealth"), dict),
        "additive_marketStatus": isinstance(cand_doc.get("marketStatus"), str),
        "additive_freshnessStatus": isinstance(cand_doc.get("freshnessStatus"), str),
        "additive_engineMetadata": isinstance(cand_doc.get("engineMetadata"), dict),
    }
    return {
        "allPassed": all(required_checks.values()),
        "fieldChecks": required_checks,
    }


def run_shadow_comparison(base_doc, legacy_fetch_fn=fetch, engine_fetcher=None, now_fn=lambda: datetime.now(timezone.utc), write_tmp=True):
    """Chạy song song Legacy và PriceEngine ở chế độ Shadow, trả về báo cáo đối chiếu chi tiết."""
    leg = run_legacy_in_memory(base_doc, fetch_fn=legacy_fetch_fn, now_fn=now_fn)
    leg_doc = leg["doc"]

    engine = PriceEngine(fetcher=engine_fetcher, now_fn=now_fn) if engine_fetcher else PriceEngine(now_fn=now_fn)
    eng_cycle = engine.run_cycle(base_doc)

    # --- 1. Phú Quý Comparison ---
    leg_tgt = leg_doc.get("target") or {}
    leg_pq = {
        "buy": leg_tgt.get("buy"),
        "sell": leg_tgt.get("sell"),
        "priceAsOf": leg_doc.get("priceAsOf"),
        "fetchedAt": leg_doc.get("fetchedAt"),
        "source": leg_doc.get("source"),
        "freshFetchSucceeded": leg["pqOk"],
        "latencyMs": leg["pqLatencyMs"],
    }

    eng_pq_res = eng_cycle["phuQuy"]
    eng_pq_sel = eng_pq_res.get("selected") or {}
    eng_pq_tgt = eng_pq_sel.get("target") or {}
    eng_pq = {
        "buy": eng_pq_tgt.get("buy"),
        "sell": eng_pq_tgt.get("sell"),
        "sourceTimestamp": eng_pq_sel.get("sourceTimestamp"),
        "fetchedTimestamp": eng_pq_sel.get("fetchedTimestamp"),
        "sourceName": eng_pq_sel.get("sourceName"),
        "adapter": eng_pq_sel.get("adapter"),
        "latencyMs": eng_pq_sel.get("latencyMs"),
        "freshnessStatus": eng_pq_res.get("freshnessStatus"),
        "marketStatus": eng_pq_res["marketSchedule"]["marketStatus"],
        "usedLastKnownGood": eng_pq_res.get("usedLastKnownGood"),
        "confirmedBy": eng_pq_sel.get("confirmedBy") or [],
        "disagreement": eng_pq_res.get("disagreement"),
    }

    buy_abs = (eng_pq["buy"] - leg_pq["buy"]) if (eng_pq["buy"] and leg_pq["buy"]) else None
    buy_pct = round((buy_abs / leg_pq["buy"]) * 100.0, 4) if (buy_abs is not None and leg_pq["buy"]) else None
    sell_abs = (eng_pq["sell"] - leg_pq["sell"]) if (eng_pq["sell"] and leg_pq["sell"]) else None
    sell_pct = round((sell_abs / leg_pq["sell"]) * 100.0, 4) if (sell_abs is not None and leg_pq["sell"]) else None
    pq_ts_diff, pq_fresher = _ts_diff_and_fresher(leg_pq["priceAsOf"], eng_pq["sourceTimestamp"])

    # --- 2. XAG/USD Comparison ---
    leg_w = leg_doc.get("world") or {}
    leg_xag = {
        "usdPerOz": leg_w.get("usdPerOz"),
        "asOf": leg_w.get("asOf"),
        "fetchedAt": leg_doc.get("fetchedAt"),
        "source": leg_w.get("source"),
        "freshFetchSucceeded": leg["worldOk"],
        "latencyMs": leg["worldLatencyMs"],
    }

    eng_xag_res = eng_cycle["xagUsd"]
    eng_xag_sel = eng_xag_res.get("selected") or {}
    eng_xag = {
        "price": eng_xag_sel.get("price"),
        "sourceTimestamp": eng_xag_sel.get("sourceTimestamp"),
        "fetchedTimestamp": eng_xag_sel.get("fetchedTimestamp"),
        "sourceName": eng_xag_sel.get("sourceName"),
        "adapter": eng_xag_sel.get("adapter"),
        "latencyMs": eng_xag_sel.get("latencyMs"),
        "freshnessStatus": eng_xag_res.get("freshnessStatus"),
        "marketStatus": eng_xag_res["marketSchedule"]["marketStatus"],
        "usedLastKnownGood": eng_xag_res.get("usedLastKnownGood"),
        "confirmedBy": eng_xag_sel.get("confirmedBy") or [],
        "disagreement": eng_xag_res.get("disagreement"),
    }

    xag_abs = round(eng_xag["price"] - leg_xag["usdPerOz"], 4) if (eng_xag["price"] and leg_xag["usdPerOz"]) else None
    xag_pct = round((xag_abs / leg_xag["usdPerOz"]) * 100.0, 4) if (xag_abs is not None and leg_xag["usdPerOz"]) else None
    xag_ts_diff, xag_fresher = _ts_diff_and_fresher(leg_xag["asOf"], eng_xag["sourceTimestamp"])

    compat = verify_backward_compatibility(base_doc, eng_cycle)

    comparison = {
        "comparedAt": eng_cycle["timestamp"],
        "phuQuy": {
            "legacy": leg_pq,
            "engine": eng_pq,
            "difference": {
                "buyAbsolute": buy_abs,
                "buyPercent": buy_pct,
                "sellAbsolute": sell_abs,
                "sellPercent": sell_pct,
                "timestampDifferenceSeconds": pq_ts_diff,
                "whichIsFresher": pq_fresher,
            },
        },
        "xagUsd": {
            "legacy": leg_xag,
            "engine": eng_xag,
            "difference": {
                "absolute": xag_abs,
                "percent": xag_pct,
                "timestampDifferenceSeconds": xag_ts_diff,
                "whichIsFresher": xag_fresher,
            },
        },
        "legacyErrors": leg["errors"],
        "backwardCompatibility": compat,
        "sourceHealth": eng_cycle["sourceHealth"],
    }

    if write_tmp:
        SHADOW_OUT.parent.mkdir(parents=True, exist_ok=True)
        SHADOW_OUT.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", "utf-8")

    return comparison


def run_authoritative_update(data_path=DATA, pq_csv_path=PQ_CSV, world_csv_path=WORLD_CSV, engine=None):
    """Authoritative PriceEngine execution path writing market.json and archive CSVs."""
    data_path = Path(data_path)
    pq_csv_path = Path(pq_csv_path)
    world_csv_path = Path(world_csv_path)

    existing_doc = json.loads(data_path.read_text("utf-8")) if data_path.exists() else {}
    active_engine = engine or PriceEngine()
    cycle = active_engine.run_cycle(existing_doc)
    built = build_compatible_market_doc(existing_doc, cycle)
    doc = built["doc"]

    pq_res = cycle["phuQuy"]
    pq_sel = pq_res.get("selected")
    if built["pqLiveOk"] and pq_sel:
        tgt = pq_sel["target"]
        items = doc.get("items") or []
        price_as_of = doc["priceAsOf"]
        history = doc.get("history") or []
        # Sổ vĩnh viễn: chỉ ghi khi dữ liệu là live/fresh (KHÔNG bao giờ ghi khi usedLastKnownGood == True)
        rows = [[price_as_of, i["type"], i.get("unit") or "", i.get("buy") or "", i.get("sell") or ""] for i in items]
        last_at = csv_last_key(pq_csv_path, 0)
        last_at = last_at[0][0] if last_at else ""
        gap = [h for h in history if h.get("at") and h["at"] > last_at and h["at"] != price_as_of]
        if gap:
            csv_append(
                pq_csv_path,
                ["thoi_diem_niem_yet", "san_pham", "don_vi", "gia_mua_vao", "gia_ban_ra"],
                [[h["at"], TARGET, "VNĐ/LƯỢNG", h["buy"], h["sell"]] for h in sorted(gap, key=lambda h: h["at"])],
            )
        prev = csv_last_key(pq_csv_path, 0)
        if prev != [[str(c) for c in r] for r in rows]:
            csv_append(pq_csv_path, ["thoi_diem_niem_yet", "san_pham", "don_vi", "gia_mua_vao", "gia_ban_ra"], rows)
        print(
            f"Phú Quý [{pq_sel['sourceName']}:{pq_sel['adapter']}]: "
            f"{tgt['buy']} / {tgt['sell']} @ {price_as_of} ({len(items)} dòng, status={pq_res['freshnessStatus']})"
        )

    xag_res = cycle["xagUsd"]
    xag_sel = xag_res.get("selected")
    if built["xagLiveOk"] and xag_sel:
        world = doc["world"]
        if built["xagHistoryAppended"]:
            csv_append(
                world_csv_path,
                ["thoi_diem", "xag_usd_oz", "thay_doi_pct", "quy_doi_vnd_kg", "nguon"],
                [
                    [
                        world["asOf"],
                        world["usdPerOz"],
                        "" if world.get("changePct") is None else world["changePct"],
                        world.get("vndPerKg") or "",
                        world.get("source") or "",
                    ]
                ],
            )
        print(
            f"XAG/USD [{xag_sel['sourceName']}:{xag_sel.get('adapter')}]: "
            f"{world['usdPerOz']} USD/oz ({world.get('changePct')}%) @ {world['asOf']} (status={xag_res['freshnessStatus']})"
        )

    for e in built["errors"]:
        print("CẢNH BÁO:", e, file=sys.stderr)

    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", "utf-8")

    # Exit 0 if at least one live source succeeded or if LKG preserved a valid state during market closure
    any_ok = built["pqLiveOk"] or built["xagLiveOk"] or (
        (pq_sel is not None or xag_sel is not None)
        and (not pq_res["marketSchedule"]["isOpen"] or not xag_res["marketSchedule"]["isOpen"])
    )
    return 0 if any_ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Update silver prices (Legacy production default; supports --shadow-only and --simulate-engine for testing)")
    ap.add_argument("--shadow-only", action="store_true", help="Run Legacy + PriceEngine in shadow mode without modifying data/market.json or archive/*.csv")
    ap.add_argument("--simulate-engine", action="store_true", help="Test-only flag: run PriceEngine against specified --data-file / --pq-csv / --world-csv")
    ap.add_argument("--cycles", type=int, default=1, help="Number of shadow cycles to run when --shadow-only is enabled")
    ap.add_argument("--interval", type=float, default=2.0, help="Seconds between shadow cycles")
    ap.add_argument("--data-file", type=str, default=str(DATA), help="Path to market.json (for isolated testing)")
    ap.add_argument("--pq-csv", type=str, default=str(PQ_CSV), help="Path to Phu Quy archive CSV (for isolated testing)")
    ap.add_argument("--world-csv", type=str, default=str(WORLD_CSV), help="Path to World XAG archive CSV (for isolated testing)")
    args = ap.parse_args(argv)

    if args.shadow_only:
        data_file = Path(args.data_file)
        base_doc = json.loads(data_file.read_text("utf-8")) if data_file.exists() else {}
        runs = []
        for idx in range(args.cycles):
            if idx > 0 and args.interval > 0:
                time.sleep(args.interval)
            comp = run_shadow_comparison(base_doc, write_tmp=True)
            runs.append(comp)
        if args.cycles > 1:
            multi_payload = {"cyclesRun": len(runs), "latest": runs[-1], "cycles": runs}
            SHADOW_OUT.write_text(json.dumps(multi_payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
            print(json.dumps(multi_payload, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(runs[0], ensure_ascii=False, indent=2))
        return 0

    if args.simulate_engine:
        return run_authoritative_update(
            data_path=Path(args.data_file),
            pq_csv_path=Path(args.pq_csv),
            world_csv_path=Path(args.world_csv),
        )

    # ========================================================================
    # PRODUCTION LEGACY PATH (100% UNCHANGED DEFAULT BEHAVIOR)
    # ========================================================================
    doc = json.loads(DATA.read_text("utf-8")) if DATA.exists() else {}
    before = json.loads(json.dumps(doc))
    history = doc.get("history") or []
    world_hist = doc.get("worldHistory") or []
    errors = []

    # --- Phú Quý ---
    pq_ok, ref, price_as_of = False, None, None
    try:
        items, price_as_of, ref = parse_phuquy(fetch(PQ_URL))
        tgt = next((i for i in items if i["type"] == TARGET), None)
        if tgt and 500_000 <= tgt["buy"] <= 20_000_000 and tgt["sell"] and tgt["sell"] >= tgt["buy"] and price_as_of:
            pq_ok = True
            doc["target"] = {"name": TARGET, "buy": tgt["buy"], "sell": tgt["sell"]}
            doc["items"] = items
            doc["priceAsOf"] = doc["itemsAsOf"] = price_as_of
            doc["source"] = "phuquy"
            last = history[-1] if history else {}
            if (last.get("at"), last.get("buy"), last.get("sell")) != (price_as_of, tgt["buy"], tgt["sell"]):
                history.append({"at": price_as_of, "buy": tgt["buy"], "sell": tgt["sell"]})
            doc["history"] = history[-KEEP:]
            rows = [[price_as_of, i["type"], i.get("unit") or "", i.get("buy") or "", i.get("sell") or ""] for i in items]
            last_at = csv_last_key(PQ_CSV, 0)
            last_at = last_at[0][0] if last_at else ""
            gap = [h for h in history if h.get("at") and h["at"] > last_at and h["at"] != price_as_of]
            if gap:
                csv_append(PQ_CSV, ["thoi_diem_niem_yet", "san_pham", "don_vi", "gia_mua_vao", "gia_ban_ra"],
                           [[h["at"], TARGET, "VNĐ/LƯỢNG", h["buy"], h["sell"]] for h in sorted(gap, key=lambda h: h["at"])])
            prev = csv_last_key(PQ_CSV, 0)
            if prev != [[str(c) for c in r] for r in rows]:
                csv_append(PQ_CSV, ["thoi_diem_niem_yet", "san_pham", "don_vi", "gia_mua_vao", "gia_ban_ra"], rows)
            print(f"Phú Quý: {tgt['buy']} / {tgt['sell']} @ {price_as_of} ({len(items)} dòng)")
        else:
            errors.append(f"phuquy: không đọc được dòng {TARGET} (tìm thấy {len(items)} dòng, giờ={price_as_of})")
    except Exception as e:  # noqa: BLE001
        errors.append(f"phuquy: {type(e).__name__}: {e}")

    # --- Tỷ giá quy đổi ---
    rate = None
    if ref and ref.get("usd") and ref.get("vndPerKg"):
        rate = ref["vndPerKg"] / (ref["usd"] * OZ_PER_KG)
    elif (doc.get("world") or {}).get("usdPerOz") and doc["world"].get("vndPerKg"):
        rate = doc["world"]["vndPerKg"] / (doc["world"]["usdPerOz"] * OZ_PER_KG)

    # --- Kitco ---
    world = None
    try:
        bid, pct, w_as_of = parse_kitco(fetch(KITCO_URL))
        sane = bid is not None and 5 <= bid <= 500 and w_as_of
        if sane and ref and ref.get("usd"):
            sane = abs(bid - ref["usd"]) / ref["usd"] < 0.15  # chặn số đọc nhầm
        if sane and rate:
            world = {
                "usdPerOz": round(bid, 2), "changePct": pct, "asOf": w_as_of, "source": "Kitco (giá bid)",
                "vndPerKg": round(bid * OZ_PER_KG * rate),
                "note": f"Quy đổi theo tỷ giá Phú Quý dùng cho giá tham chiếu (≈{fmt_dots(int(round(rate / 10) * 10))} ₫/USD), trước thuế và phí nhập khẩu",
            }
            print(f"Kitco: {bid} USD/oz ({pct}%) @ {w_as_of}")
        else:
            errors.append(f"kitco: không đọc được giá (bid={bid}, giờ={w_as_of})")
    except Exception as e:  # noqa: BLE001
        errors.append(f"kitco: {type(e).__name__}: {e}")

    if world is None and ref and ref.get("usd") and pq_ok:
        world = {"usdPerOz": ref["usd"], "changePct": None, "asOf": price_as_of, "source": "Bảng giá Phú Quý",
                 "vndPerKg": ref["vndPerKg"], "note": "Trước thuế và phí nhập khẩu"}
        print(f"Thế giới (theo Phú Quý): {ref['usd']} USD/oz")

    if world:
        doc["world"] = world
        last = world_hist[-1] if world_hist else {}
        if (last.get("at"), last.get("usd")) != (world["asOf"], world["usdPerOz"]):
            world_hist.append({"at": world["asOf"], "usd": world["usdPerOz"]})
            csv_append(WORLD_CSV, ["thoi_diem", "xag_usd_oz", "thay_doi_pct", "quy_doi_vnd_kg", "nguon"],
                       [[world["asOf"], world["usdPerOz"], "" if world.get("changePct") is None else world["changePct"],
                         world.get("vndPerKg") or "", world.get("source") or ""]])
        doc["worldHistory"] = world_hist[-KEEP:]

    doc["errors"] = errors
    if pq_ok or world:
        now = datetime.now(timezone.utc)
        doc["fetchedAt"] = before.get("fetchedAt")
        try:
            age = now - datetime.fromisoformat(str(before.get("fetchedAt")).replace("Z", "+00:00"))
        except ValueError:
            age = timedelta(days=1)
        if doc != before or age > timedelta(hours=2):
            doc["fetchedAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for e in errors:
        print("CẢNH BÁO:", e, file=sys.stderr)

    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", "utf-8")
    return 0 if (pq_ok or world) else 1


if __name__ == "__main__":
    sys.exit(main())
