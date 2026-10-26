#!/usr/bin/env python3
"""Standalone Resilient Price Engine for Silver Tracker (Phase 1).

Provides:
  - Market-open / market-closed schedule & session state machine (XAG/USD & Phu Quy)
  - Multi-source XAG/USD fetching (Swissquote, Yahoo Finance, Kitco, Phu Quy World JSON, GiaBac World JSON)
  - Multi-source Phu Quy fetching (Official AJAX Partial + JSON, Official Full HTML, giabac.vn JSON, giabac.vn HTML)
  - 4 independent HTML parser adapters for Phu Quy (CSS classes, Fuzzy Table Heuristic, KG/Equivalent Ratio, Text Window)
  - 2 independent HTML/JSON parser adapters for Kitco (__NEXT_DATA__ JSON + Text Regex)
  - Multi-stage validation: hard sanity bounds, spread checks, cross-row consistency, jump detection, consensus/disagreement
  - Last-Known-Good (LKG) preservation & automatic recovery
  - Per-source health tracking (sourceHealth) & full selection diagnostics

Uses Python 3 standard library only. Does NOT overwrite data/market.json unless explicitly invoked to do so.
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone, tzinfo
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class _USNewYorkTz(tzinfo):
    """Pure-stdlib America/New_York tzinfo fallback for Windows Python without tzdata."""

    @staticmethod
    def _nth_sunday(year: int, month: int, n: int) -> int:
        first_wd = datetime(year, month, 1).weekday()  # Mon=0..Sun=6
        first_sun = 1 + ((6 - first_wd) % 7)
        return first_sun + (n - 1) * 7

    def _is_dst(self, dt: Optional[datetime]) -> bool:
        if dt is None:
            return False
        y = dt.year
        dst_start = datetime(y, 3, self._nth_sunday(y, 3, 2), 2, 0)
        dst_end = datetime(y, 11, self._nth_sunday(y, 11, 1), 2, 0)
        naive = dt.replace(tzinfo=None)
        return dst_start <= naive < dst_end

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        return timedelta(hours=-4 if self._is_dst(dt) else -5)

    def dst(self, dt: Optional[datetime]) -> timedelta:
        return timedelta(hours=1 if self._is_dst(dt) else 0)

    def tzname(self, dt: Optional[datetime]) -> str:
        return "EDT" if self._is_dst(dt) else "EST"


# --- Constants ---
TARGET_CANONICAL = "BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG"
OZ_PER_KG = 32.1507466
LUONG_PER_KG = 26.6666667
VN_TZ = timezone(timedelta(hours=7))
try:
    NY_TZ: tzinfo = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    NY_TZ = _USNewYorkTz()
UTC_TZ = timezone.utc

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Endpoints
SWISSQUOTE_URL = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAG/USD"
YAHOO_XAG_URL = "https://query1.finance.yahoo.com/v8/finance/chart/SI=F?interval=1m&range=1d"
KITCO_URL = "https://www.kitco.com/charts/silver"
PQ_BASE_URL = "https://giabac.phuquygroup.vn/"
PQ_PARTIAL_URL = "https://giabac.phuquygroup.vn/PhuQuyPrice/SilverPricePartial"
PQ_DATETIME_URL = "https://giabac.phuquygroup.vn/PhuQuyPrice/GetDateTimeUpdate"
PQ_WORLD_URL = "https://giabac.phuquygroup.vn/PhuQuyPrice/GetWorldSilverPriceUpdate"
GIABAC_BASE_URL = "https://giabac.vn/"
GIABAC_CHART_L_URL = "https://giabac.vn/SilverInfo/GetGoldPriceChartFromSQLData?days=7&type=L"
GIABAC_CHART_KG_URL = "https://giabac.vn/SilverInfo/GetGoldPriceChartFromSQLData?days=7&type=KG"
GIABAC_WORLD_URL = "https://giabac.vn/SilverInfo/GetWorldSilverPriceUpdate"

# Thresholds
XAG_MIN_USD = 10.0
XAG_MAX_USD = 250.0
XAG_MAX_SPREAD_PCT = 0.05  # 5% max bid-ask spread

PQ_MIN_BUY_VND = 500_000
PQ_MAX_BUY_VND = 20_000_000
PQ_MIN_SPREAD_PCT = 0.001  # 0.1%
PQ_MAX_SPREAD_PCT = 0.12   # 12% (filters out artisan/jewelry silver which has ~17.6% spread)

JUMP_INTRADAY_MAX_PCT = 0.045   # 4.5% unconfirmed single-source jump within 30 mins
JUMP_SESSION_MAX_PCT = 0.12     # 12% unconfirmed single-source jump across sessions
DISAGREEMENT_WARN_PCT = 0.015   # 1.5% divergence warning
DISAGREEMENT_OUTLIER_PCT = 0.03 # 3.0% outlier threshold


# ============================================================================
# 1. UTILITIES & HTTP FETCHER
# ============================================================================

def iso_utc(dt: Optional[datetime] = None) -> str:
    d = dt or datetime.now(UTC_TZ)
    return d.astimezone(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_dotnet_date(s: Optional[str]) -> Optional[str]:
    """Parse '/Date(1792837358475)/' into ISO 8601 UTC string."""
    if not s or not isinstance(s, str):
        return None
    m = re.search(r"/Date\((\d+)\)/", s)
    if not m:
        return None
    ms = int(m.group(1))
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_diacritics(s: str) -> str:
    s = s.replace("đ", "d").replace("Đ", "D")
    nfd = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")


def to_int_price(s: Any) -> Optional[int]:
    if isinstance(s, (int, float)):
        v = int(round(s))
        return v if v > 0 else None
    raw = str(s or "").strip()
    if not raw or raw in ("_", "-", "—", "–", "N/A", "null", "None"):
        return None
    is_neg = bool(re.match(r"^[-\u2212]\s*\d", raw))
    # Handle float string like "1840000.000"
    if re.match(r"^-?\d+\.\d+$", raw) and raw.count(".") == 1:
        parts = raw.lstrip("-").split(".")
        # Distinguish "1.840" (thousand separator if 3 digits and < 100) vs "1840000.000"
        if len(parts[0]) >= 6:
            val = int(round(float(raw)))
            return val
    d = re.sub(r"[^\d]", "", raw)
    if not d:
        return None
    return -int(d) if is_neg else int(d)


def default_http_fetch(url: str, timeout: float = 15.0, headers: Optional[Dict[str, str]] = None) -> Tuple[int, str, int]:
    """Returns (http_status, text_body, latency_ms). Raises exception on network/HTTP failure."""
    req_headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    if headers:
        req_headers.update(headers)
    t0 = time.perf_counter()
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", errors="replace")
        ms = int(round((time.perf_counter() - t0) * 1000))
        return r.status, body, ms


# ============================================================================
# 2. MARKET SCHEDULE & SESSION ENGINE (Requirement C & Step 5)
# ============================================================================

def get_xag_market_schedule(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Determines whether the international spot silver market (XAG/USD) is open or closed.

    International spot silver trades Sunday 18:00 NY to Friday 17:00 NY,
    with a daily 60-minute CME Globex maintenance pause 17:00-18:00 NY (Mon-Thu),
    plus major fixed holidays (Jan 1, Dec 25).
    """
    now_dt = (now or datetime.now(UTC_TZ)).astimezone(NY_TZ)
    wd = now_dt.weekday()  # Mon=0 .. Fri=4, Sat=5, Sun=6
    hm = now_dt.hour * 60 + now_dt.minute

    # Fixed major global market closures (New Year's Day, Christmas Day)
    if (now_dt.month == 1 and now_dt.day == 1 and hm < 18 * 60) or (now_dt.month == 12 and now_dt.day == 25 and hm < 18 * 60):
        return {
            "isOpen": False,
            "marketStatus": "MARKET_CLOSED_HOLIDAY",
            "reason": "Thị trường quốc tế nghỉ lễ — giữ giá chốt phiên gần nhất",
            "inReopenGrace": False,
        }

    # Weekend closure: Friday 17:00 NY -> Sunday 18:00 NY
    if (wd == 4 and hm >= 17 * 60) or (wd == 5) or (wd == 6 and hm < 18 * 60):
        return {
            "isOpen": False,
            "marketStatus": "MARKET_CLOSED_WEEKEND",
            "reason": "Thị trường bạc quốc tế nghỉ cuối tuần — giá chốt phiên thứ Sáu",
            "inReopenGrace": False,
        }

    # Daily Globex maintenance window: Mon-Thu 17:00 - 18:00 NY
    if wd in (0, 1, 2, 3) and (17 * 60 <= hm < 18 * 60):
        return {
            "isOpen": False,
            "marketStatus": "MARKET_CLOSED_MAINTENANCE",
            "reason": "Thị trường quốc tế nghỉ chuyển phiên hàng ngày (17:00–18:00 NY)",
            "inReopenGrace": False,
        }

    # Reopen grace period: first 20 minutes after Sunday 18:00 NY or daily 18:00 NY reopen
    in_grace = (18 * 60 <= hm <= 18 * 60 + 20)
    return {
        "isOpen": True,
        "marketStatus": "REOPEN_GRACE_PERIOD" if in_grace else "MARKET_OPEN",
        "reason": "Mới mở phiên giao dịch" if in_grace else "Thị trường quốc tế đang giao dịch",
        "inReopenGrace": in_grace,
    }


def get_phuquy_market_schedule(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Determines whether Phu Quy's silver price board is within normal update hours.

    Phu Quy updates its silver price board Monday-Saturday starting ~08:00-08:30 VN time
    until ~18:30-18:45 VN time (Saturday often finishes updates earlier in the afternoon).
    Sunday is closed all day.
    """
    now_vn = (now or datetime.now(UTC_TZ)).astimezone(VN_TZ)
    wd = now_vn.weekday()  # Mon=0 .. Sat=5, Sun=6
    hm = now_vn.hour * 60 + now_vn.minute

    # Sunday all day or Saturday after 18:45 or Monday before 08:00
    if wd == 6 or (wd == 5 and hm >= 18 * 60 + 45) or (wd == 0 and hm < 8 * 60):
        return {
            "isOpen": False,
            "marketStatus": "MARKET_CLOSED_WEEKEND",
            "reason": "Phú Quý ngoài giờ niêm yết cuối tuần — giữ giá chốt phiên gần nhất",
            "inReopenGrace": False,
        }

    # Morning reopen grace period: Mon-Sat 08:00 - 09:00 VN (board typically posts first update around 08:15-08:45)
    if 8 * 60 <= hm < 9 * 60:
        return {
            "isOpen": True,
            "marketStatus": "REOPEN_GRACE_PERIOD",
            "reason": "Đầu giờ sáng — chờ bảng giá đầu ngày của Phú Quý",
            "inReopenGrace": True,
        }

    # Weekday / Saturday overnight (18:45 - 08:00)
    if hm < 8 * 60 or hm >= 18 * 60 + 45:
        return {
            "isOpen": False,
            "marketStatus": "MARKET_CLOSED_AFTER_HOURS",
            "reason": "Ngoài giờ cập nhật bảng giá Phú Quý (08:00–18:45) — giá cuối ngày",
            "inReopenGrace": False,
        }

    # Saturday afternoon (12:30 - 18:45): Phu Quy sometimes updates, sometimes holds morning price
    if wd == 5 and hm >= 12 * 60 + 30:
        return {
            "isOpen": True,
            "marketStatus": "MARKET_OPEN_SATURDAY_AFTERNOON",
            "reason": "Chiều thứ Bảy — Phú Quý giữ giá niêm yết gần nhất trong ngày",
            "inReopenGrace": True,
        }

    return {
        "isOpen": True,
        "marketStatus": "MARKET_OPEN",
        "reason": "Trong giờ niêm yết Phú Quý (08:00–18:45)",
        "inReopenGrace": False,
    }


# ============================================================================
# 3. PHU QUY HTML PARSER ADAPTERS (4 Independent Adapters)
# ============================================================================

class TableAndLineExtractor(HTMLParser):
    """Extracts both structured table rows (with class/id metadata) and plain text lines."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: List[List[Dict[str, str]]] = []
        self._cur_row: Optional[List[Dict[str, str]]] = None
        self._cur_cell: Optional[Dict[str, Any]] = None
        self.lines: List[str] = []
        self._buf: List[str] = []
        self.by_id: Dict[str, str] = {}
        self._id_stack: List[Tuple[str, List[str]]] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript"):
            self.skip += 1
            return
        el_id = attr_dict.get("id", "").strip()
        if el_id:
            self._id_stack.append((el_id, []))
        if tag == "tr":
            self._cur_row = []
        elif tag in ("td", "th") and self._cur_row is not None:
            self._cur_cell = {"tag": tag, "class": attr_dict.get("class", ""), "text": []}
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "span", "section"):
            self._flush_line()

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
            return
        if self._id_stack:
            el_id, chars = self._id_stack[-1]
            # Pop if non-empty text was collected and tag closes
            text_val = " ".join("".join(chars).split())
            if text_val and el_id not in self.by_id:
                self.by_id[el_id] = text_val
            self._id_stack.pop()
        if tag in ("td", "th") and self._cur_row is not None and self._cur_cell is not None:
            self._cur_row.append({
                "tag": self._cur_cell["tag"],
                "class": self._cur_cell["class"],
                "text": " ".join("".join(self._cur_cell["text"]).split()),
            })
            self._cur_cell = None
        elif tag == "tr" and self._cur_row is not None:
            if any(c["text"] for c in self._cur_row):
                self.rows.append(self._cur_row)
            self._cur_row = None
        if tag in ("p", "div", "li", "td", "th", "tr", "h1", "h2", "h3", "h4", "span", "section"):
            self._flush_line()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self._cur_cell is not None:
            self._cur_cell["text"].append(data)
        for _, buf in self._id_stack:
            buf.append(data)
        self._buf.append(data)

    def _flush_line(self) -> None:
        t = " ".join("".join(self._buf).split())
        if t:
            self.lines.append(t)
        self._buf = []


UNIT_RE = re.compile(r"VN[ĐD]\s*/\s*(LƯỢNG|LUONG|KG|CHỈ|CHI|KILO|KILOGRAM)", re.I)


def norm_unit(u: str) -> str:
    m = UNIT_RE.search(u or "")
    if not m:
        return (u or "").strip()
    raw = strip_diacritics(m.group(1)).upper()
    if raw in ("LUONG",):
        return "VNĐ/LƯỢNG"
    if raw in ("KG", "KILO", "KILOGRAM"):
        return "VNĐ/KG"
    if raw in ("CHI",):
        return "VNĐ/CHỈ"
    return f"VNĐ/{m.group(1).upper()}"


def is_target_1_luong_name(name: str) -> bool:
    """Fuzzy product identity matcher for Phu Quy 999 1 Luong silver piece.
    Matches variations like:
      - 'BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG'
      - 'BẠC MIẾNG PHÚ QUÝ 999 01 LƯỢNG'
      - 'BẠC MIẾNG 999 1 LƯỢNG'
      - 'BAC PHU QUY 999 (1 LUONG)'
    While strictly rejecting:
      - '10 LƯỢNG', '5 LƯỢNG', '1 KILO', '1KG', 'MỸ NGHỆ', 'THƯƠNG HIỆU KHÁC'
    """
    norm = strip_diacritics(name or "").lower()
    norm = re.sub(r"[^a-z0-9]+", " ", norm).strip()
    tokens = set(norm.split())
    if "bac" not in tokens:
        return False
    # Reject other products
    forbidden = {"10", "5", "kilo", "kg", "kilogram", "nghe", "khac", "thanh"}
    if tokens & forbidden:
        return False
    if "1kilo" in norm or "1kg" in norm or "my nghe" in norm:
        return False
    # Require either ('1' or '01') and 'luong', OR ('mieng' and '999' and 'phu' and 'quy')
    has_one_luong = bool(re.search(r"\b0?1\s*luong\b", norm)) or ("1" in tokens and "luong" in tokens)
    has_mieng_999 = "mieng" in tokens and ("999" in tokens or "99" in tokens)
    return has_one_luong or has_mieng_999


def is_equivalent_bar_luong_name(name: str) -> bool:
    """Matches 'BẠC THỎI PHÚ QUÝ 999 10 LƯỢNG, 5 LƯỢNG' which trades at the exact same per-luong price."""
    norm = strip_diacritics(name or "").lower()
    norm = re.sub(r"[^a-z0-9]+", " ", norm).strip()
    tokens = set(norm.split())
    return "bac" in tokens and "thoi" in tokens and "999" in tokens and ("10" in tokens or "5" in tokens) and "kilo" not in tokens and "kg" not in tokens


def is_1kg_bar_name(name: str, unit: str = "") -> bool:
    """Matches 'BẠC THỎI PHÚ QUÝ 999 1KILO' / 1 KG row."""
    norm = strip_diacritics((name or "") + " " + (unit or "")).lower()
    return "bac" in norm and "999" in norm and ("1kilo" in norm or "1 kilo" in norm or "1kg" in norm or "1 kg" in norm or "vnd/kg" in norm)


def extract_timestamp_from_dom_or_text(dom: TableAndLineExtractor, full_text: str) -> Optional[str]:
    """Multi-pattern timestamp extractor from DOM IDs or free text."""
    # Pattern 1: DOM IDs (#last-update-time + #last-update-date)
    t_str = dom.by_id.get("last-update-time") or dom.by_id.get("last-update-time-mobile")
    d_str = dom.by_id.get("last-update-date") or dom.by_id.get("last-update-date-mobile")
    if t_str and d_str:
        m_t = re.search(r"(\d{1,2}):(\d{2})", t_str)
        m_d = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", d_str)
        if m_t and m_d:
            hh, mm = int(m_t.group(1)), int(m_t.group(2))
            dd, mo, yy = int(m_d.group(1)), int(m_d.group(2)), int(m_d.group(3))
            return datetime(yy, mo, dd, hh, mm, tzinfo=VN_TZ).isoformat()

    # Pattern 2: HH:MM [ngày] DD/MM/YYYY or DD-MM-YYYY
    m = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*(?:ngày\s*|-\s*)?(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", full_text, re.I)
    if m:
        hh, mm, dd, mo, yy = map(int, m.groups())
        if 1 <= mo <= 12 and 1 <= dd <= 31 and 0 <= hh <= 23:
            return datetime(yy, mo, dd, hh, mm, tzinfo=VN_TZ).isoformat()

    # Pattern 3: DD/MM/YYYY [lúc] HH:MM
    m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\s*(?:lúc\s*|-\s*)?(\d{1,2}):(\d{2})", full_text, re.I)
    if m:
        dd, mo, yy, hh, mm = map(int, m.groups())
        if 1 <= mo <= 12 and 1 <= dd <= 31 and 0 <= hh <= 23:
            return datetime(yy, mo, dd, hh, mm, tzinfo=VN_TZ).isoformat()

    # Pattern 4: ISO timestamp in text
    m = re.search(r"(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))", full_text)
    if m:
        return m.group(1)

    return None


def extract_world_ref_from_text(full_text: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"([\d.,]+)\s*USD\s*/\s*OUNCE\s*[≈~=]?\s*([\d.,]+)\s*VN[ĐD]\s*/\s*KG", full_text, re.I)
    if not m:
        return None
    raw_usd = m.group(1).replace(",", "")
    usd = float(raw_usd) if raw_usd.count(".") <= 1 else None
    vnd_kg = to_int_price(m.group(2))
    if usd and vnd_kg:
        return {"usd": usd, "vndPerKg": vnd_kg}
    return None


def adapter_css_classes(dom: TableAndLineExtractor) -> Optional[Dict[str, Any]]:
    """Adapter 1: Extracts items using semantic CSS classes (.silver-product-name, .silver-buy-price, .silver-sell-price)."""
    items = []
    for row in dom.rows:
        name_cell = next((c for c in row if "silver-product-name" in c["class"]), None)
        buy_cell = next((c for c in row if "silver-buy-price" in c["class"]), None)
        sell_cell = next((c for c in row if "silver-sell-price" in c["class"]), None)
        unit_cell = next((c for c in row if "silver-unit" in c["class"]), None)
        if not name_cell or not buy_cell:
            continue
        buy_val = to_int_price(buy_cell["text"])
        sell_val = to_int_price(sell_cell["text"]) if sell_cell else None
        if not buy_val:
            continue
        items.append({
            "type": name_cell["text"].strip().upper(),
            "unit": norm_unit(unit_cell["text"] if unit_cell else "VNĐ/LƯỢNG"),
            "buy": buy_val,
            "sell": sell_val,
        })
    target = next((it for it in items if is_target_1_luong_name(it["type"])), None)
    if not target:
        return None
    return {
        "adapter": "adapter_css_classes",
        "target": {"name": TARGET_CANONICAL, "rawName": target["type"], "buy": target["buy"], "sell": target["sell"]},
        "items": items,
    }


def adapter_table_heuristic(dom: TableAndLineExtractor) -> Optional[Dict[str, Any]]:
    """Adapter 2: Class-agnostic table row heuristic. Works even if all CSS classes are removed or renamed."""
    items = []
    for row in dom.rows:
        texts = [c["text"].strip() for c in row if c["text"].strip()]
        if len(texts) < 2:
            continue
        # Look for unit column or product name containing BẠC
        ui = next((i for i, t in enumerate(texts) if UNIT_RE.search(t)), None)
        if ui is not None and ui > 0:
            name = " ".join(texts[:ui]).strip().upper()
            unit_str = norm_unit(texts[ui])
            rest = texts[ui + 1:]
        else:
            # No explicit unit column (or unit merged): check if first column has BẠC
            if "BẠC" not in texts[0].upper() and "BAC" not in strip_diacritics(texts[0]).upper():
                continue
            name = texts[0].strip().upper()
            unit_str = "VNĐ/KG" if is_1kg_bar_name(name) else "VNĐ/LƯỢNG"
            rest = texts[1:]

        if "BẠC" not in name and "BAC" not in strip_diacritics(name).upper():
            continue
        nums = [to_int_price(c) for c in rest]
        nums = [n for n in nums if n is not None and n >= 100_000]
        if not nums:
            continue
        buy_val = nums[0]
        sell_val = nums[1] if len(nums) > 1 else None
        items.append({"type": name, "unit": unit_str, "buy": buy_val, "sell": sell_val})

    # Deduplicate preserving order
    seen, uniq = set(), []
    for it in items:
        if it["type"] not in seen:
            seen.add(it["type"])
            uniq.append(it)

    target = next((it for it in uniq if is_target_1_luong_name(it["type"])), None)
    if not target:
        return None
    return {
        "adapter": "adapter_table_heuristic",
        "target": {"name": TARGET_CANONICAL, "rawName": target["type"], "buy": target["buy"], "sell": target["sell"]},
        "items": uniq,
    }


def adapter_kg_ratio_fallback(dom: TableAndLineExtractor) -> Optional[Dict[str, Any]]:
    """Adapter 3: Fallback when the 1-luong product row is renamed beyond recognition or omitted,
    deriving the 1-luong price from the equivalent 5/10-luong bar row or 1-kilo bar row (÷ 26.6666667)."""
    # Reuse rows extracted from table heuristic or CSS
    candidates = []
    for row in dom.rows:
        texts = [c["text"].strip() for c in row if c["text"].strip()]
        if len(texts) < 2:
            continue
        name = texts[0].upper()
        nums = [to_int_price(c) for c in texts[1:]]
        nums = [n for n in nums if n is not None and n >= 500_000]
        if len(nums) >= 2:
            candidates.append({"type": name, "buy": nums[0], "sell": nums[1]})

    # 3A: Check 5/10 luong bar (exact 1:1 per-luong price on Phu Quy)
    bar_luong = next((c for c in candidates if is_equivalent_bar_luong_name(c["type"]) and 500_000 <= c["buy"] <= 20_000_000), None)
    if bar_luong:
        return {
            "adapter": "adapter_kg_ratio_fallback:bar_5_10_luong",
            "target": {"name": TARGET_CANONICAL, "rawName": bar_luong["type"], "buy": bar_luong["buy"], "sell": bar_luong["sell"]},
            "items": candidates,
        }

    # 3B: Check 1 KG row (~13M - 500M VND) and divide by 26.6666667 (rounded to nearest 1,000 VND)
    kg_row = next((c for c in candidates if is_1kg_bar_name(c["type"]) and c["buy"] >= 13_000_000), None)
    if kg_row and kg_row["sell"]:
        buy_luong = int(round((kg_row["buy"] / LUONG_PER_KG) / 1000.0) * 1000)
        sell_luong = int(round((kg_row["sell"] / LUONG_PER_KG) / 1000.0) * 1000)
        return {
            "adapter": "adapter_kg_ratio_fallback:derived_from_1kg",
            "target": {"name": TARGET_CANONICAL, "rawName": kg_row["type"], "buy": buy_luong, "sell": sell_luong},
            "items": candidates,
        }
    return None


def adapter_text_window(dom: TableAndLineExtractor) -> Optional[Dict[str, Any]]:
    """Adapter 4: Tag-stripped sliding text-window parser.
    Works if <table> is completely replaced by <div>/flexbox/grid cards."""
    lines = dom.lines
    items = []
    for i, ln in enumerate(lines):
        if ("BẠC" in ln.upper() or "BAC" in strip_diacritics(ln).upper()) and len(ln) < 90 and not UNIT_RE.search(ln):
            win = lines[i + 1: i + 8]
            nums = []
            unit_str = "VNĐ/LƯỢNG"
            for w in win:
                if UNIT_RE.search(w):
                    unit_str = norm_unit(w)
                    continue
                # Stop if we hit another product header
                if ("BẠC" in w.upper() or "BAC" in strip_diacritics(w).upper()) and len(w) > 10 and not re.search(r"\d{3}", w):
                    break
                val = to_int_price(w)
                if val is not None and 500_000 <= val <= 500_000_000:
                    nums.append(val)
                if len(nums) == 2:
                    break
            if nums:
                items.append({
                    "type": ln.strip().upper(),
                    "unit": unit_str,
                    "buy": nums[0],
                    "sell": nums[1] if len(nums) > 1 else None,
                })

    target = next((it for it in items if is_target_1_luong_name(it["type"])), None)
    if not target:
        return None
    return {
        "adapter": "adapter_text_window",
        "target": {"name": TARGET_CANONICAL, "rawName": target["type"], "buy": target["buy"], "sell": target["sell"]},
        "items": items,
    }


PHUQUY_HTML_ADAPTERS = [
    adapter_css_classes,
    adapter_table_heuristic,
    adapter_kg_ratio_fallback,
    adapter_text_window,
]


def parse_phuquy_html_multi_adapter(html_text: str) -> Dict[str, Any]:
    """Runs all 4 HTML parser adapters in order until one produces a valid structure."""
    dom = TableAndLineExtractor()
    dom.feed(html_text or "")
    dom._flush_line()
    full_text = "\n".join(dom.lines)

    as_of = extract_timestamp_from_dom_or_text(dom, full_text)
    ref = extract_world_ref_from_text(full_text)

    tried = []
    for adapter_fn in PHUQUY_HTML_ADAPTERS:
        res = adapter_fn(dom)
        if res and res.get("target"):
            t = res["target"]
            # Check basic structural validity before accepting adapter output
            if (
                isinstance(t.get("buy"), int)
                and isinstance(t.get("sell"), int)
                and PQ_MIN_BUY_VND <= t["buy"] <= PQ_MAX_BUY_VND
                and t["sell"] > t["buy"]
                and (t["sell"] - t["buy"]) / t["buy"] <= PQ_MAX_SPREAD_PCT
            ):
                return {
                    "ok": True,
                    "adapter": res["adapter"],
                    "triedAdapters": tried + [res["adapter"]],
                    "target": {"name": TARGET_CANONICAL, "buy": t["buy"], "sell": t["sell"]},
                    "items": res.get("items") or [],
                    "asOf": as_of,
                    "ref": ref,
                }
        tried.append(adapter_fn.__name__)

    # Determine if there were numbers that failed validation vs total parser failure
    any_rows = len(dom.rows) > 0 or len(dom.lines) > 5
    return {
        "ok": False,
        "adapter": None,
        "triedAdapters": tried,
        "failureReason": "PARSER_BROKEN" if any_rows else "EMPTY_RESPONSE",
        "asOf": as_of,
        "ref": ref,
    }


# ============================================================================
# 4. XAG/USD PARSERS & ADAPTERS
# ============================================================================

def parse_swissquote_json(raw: str) -> Dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, list) or not data:
        raise ValueError("Swissquote payload is not a non-empty list")
    feed = next((x for x in data if isinstance(x, dict) and x.get("spreadProfilePrices")), data[0])
    prices = feed.get("spreadProfilePrices") or []
    if not prices:
        raise ValueError("Swissquote missing spreadProfilePrices")
    pr = next((x for x in prices if x.get("spreadProfile") == "prime"), prices[0])
    bid = float(pr["bid"])
    ask = float(pr["ask"])
    ts = feed.get("ts")
    as_of = None
    if isinstance(ts, (int, float)) and ts > 0:
        as_of = datetime.fromtimestamp(ts / 1000.0, tz=UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "adapter": "swissquote_bbo_json",
        "price": round(bid, 2),
        "rawBid": bid,
        "ask": round(ask, 2),
        "changePct": None,
        "sourceTimestamp": as_of,
        "timestampKind": "market",
    }


def parse_yahoo_json(raw: str) -> Dict[str, Any]:
    data = json.loads(raw)
    result = ((data.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise ValueError("Yahoo chart.result missing")
    meta = result.get("meta") or {}
    price = float(meta.get("regularMarketPrice"))
    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    chg_pct = None
    if prev_close and float(prev_close) > 0:
        chg_pct = round(((price - float(prev_close)) / float(prev_close)) * 100.0, 2)
    ts = meta.get("regularMarketTime")
    as_of = None
    if isinstance(ts, (int, float)) and ts > 0:
        as_of = datetime.fromtimestamp(ts, tz=UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "adapter": "yahoo_chart_v8_json",
        "price": round(price, 2),
        "rawBid": price,
        "ask": None,
        "changePct": chg_pct,
        "sourceTimestamp": as_of,
        "timestampKind": "market",
    }


def parse_kitco_multi_adapter(page: str) -> Dict[str, Any]:
    """Tries Adapter 1 (Visible Silver Bid text/regex) then Adapter 2 (Scoped __NEXT_DATA__ JSON tree)."""
    # Adapter 1: Plain-text visible Bid adapter (specifically matches the main Silver quote header)
    text = html_mod.unescape(re.sub(r"<[^>]+>", " ", page))
    text = " ".join(text.split())
    bid, win = None, ""
    m = re.search(r"\bBid\b\s*\$?\s*([0-9]{1,3}\.[0-9]{2,3})", text)
    if m:
        bid = float(m.group(1))
        win = text[max(0, m.start() - 300): m.end() + 300]

    pct = None
    if win:
        m_pct = re.search(r"([+\-−]?[0-9]+\.[0-9]{1,2})\s*%", win)
        if m_pct:
            pct = float(m_pct.group(1).replace("−", "-"))

    as_of = None
    months = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    m_dt = re.search(
        rf"({months})[a-z]*\.?\s+(\d{{1,2}}),\s*(\d{{4}})\s*(?:-|at)\s*(\d{{1,2}}):(\d{{2}})\s*([ap])?\.?\s*m?\.?",
        text,
        re.I,
    )
    if m_dt:
        mon = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(
            m_dt.group(1)[:3].lower()
        ) + 1
        h, mi = int(m_dt.group(4)), int(m_dt.group(5))
        ap = (m_dt.group(6) or "").lower()
        if ap == "p" and h < 12:
            h += 12
        if ap == "a" and h == 12:
            h = 0
        as_of = datetime(int(m_dt.group(3)), mon, int(m_dt.group(2)), h, mi, tzinfo=NY_TZ).isoformat()

    if bid is not None and XAG_MIN_USD <= bid <= XAG_MAX_USD:
        return {
            "adapter": "kitco_text_regex",
            "price": round(bid, 2),
            "rawBid": bid,
            "ask": None,
            "changePct": pct,
            "sourceTimestamp": as_of,
            "timestampKind": "server_clock",
        }

    # Adapter 2: Next.js __NEXT_DATA__ JSON (scoped to silver / XAG quote block)
    m_next = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', page, re.S | re.I)
    if m_next:
        try:
            nd = json.loads(m_next.group(1))
            nd_str = json.dumps(nd)
            # Look for silver/XAG specific quote block first, then fallback to generic quote block
            m_scoped = re.search(
                r'(?:"symbol"\s*:\s*"(?:XAG|SILVER)"|"metal"\s*:\s*"Silver"|"quote"\s*:\s*\{)[^{}]{0,400}?"bid"\s*:\s*([0-9]{2,3}\.[0-9]{1,4})',
                nd_str,
                re.I,
            )
            if m_scoped:
                bid_nd = float(m_scoped.group(1))
                win_nd = nd_str[m_scoped.start(): m_scoped.end() + 300]
                m_pct = re.search(r'"changePercentage"\s*:\s*([+\-]?[0-9]+\.[0-9]{1,4})', win_nd)
                pct_nd = round(float(m_pct.group(1)), 2) if m_pct else pct
                m_ts = re.search(r'"timestamp"\s*:\s*(\d{10,13})', win_nd)
                as_of_nd = as_of
                if m_ts:
                    ts_val = int(m_ts.group(1))
                    if ts_val > 10_000_000_000:
                        ts_val = ts_val // 1000
                    as_of_nd = datetime.fromtimestamp(ts_val, tz=UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")
                if XAG_MIN_USD <= bid_nd <= XAG_MAX_USD:
                    return {
                        "adapter": "kitco_next_data_json",
                        "price": round(bid_nd, 2),
                        "rawBid": bid_nd,
                        "ask": None,
                        "changePct": pct_nd,
                        "sourceTimestamp": as_of_nd,
                        "timestampKind": "market",
                    }
        except Exception:
            pass

    raise ValueError("Kitco parser could not find valid silver bid price in text or __NEXT_DATA__")


def parse_world_silver_update_json(raw: str, source_label: str) -> Dict[str, Any]:
    """Parses /PhuQuyPrice/GetWorldSilverPriceUpdate or /SilverInfo/GetWorldSilverPriceUpdate."""
    data = json.loads(raw)
    if not isinstance(data, dict) or not data.get("success"):
        raise ValueError(f"{source_label} returned success=false")
    price = float(data.get("worldSilverPrice") or 0)
    vnd_kg = to_int_price(data.get("worldSilverPriceVndKg"))
    vcb_rate = float(data.get("vcbUsdRate") or 0) or None
    as_of = parse_dotnet_date(data.get("retrievedAt")) or parse_dotnet_date(data.get("serverTime"))
    return {
        "adapter": "dotnet_world_silver_json",
        "price": round(price, 2),
        "rawBid": price,
        "ask": None,
        "changePct": None,
        "sourceTimestamp": as_of,
        "timestampKind": "server_clock",
        "vndPerKg": vnd_kg,
        "vcbUsdRate": vcb_rate,
    }


# ============================================================================
# 5. VALIDATION, ABNORMAL JUMP & DISAGREEMENT ENGINE (Requirement A, B, E)
# ============================================================================

def validate_xag_quote(candidate: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
    """Single-source structural & sanity validation for XAG/USD."""
    price = candidate.get("price")
    if price is None or not isinstance(price, (int, float)) or price <= 0:
        return False, "MALFORMED_VALUE", f"Invalid or non-positive XAG price: {price}"
    if price < XAG_MIN_USD or price > XAG_MAX_USD:
        return False, "SUSPICIOUS_PRICE", f"XAG price {price} outside sanity bounds [{XAG_MIN_USD}, {XAG_MAX_USD}]"
    ask = candidate.get("ask")
    if ask is not None:
        if ask < price:
            return False, "MALFORMED_VALUE", f"Inverted XAG ask ({ask}) < bid ({price})"
        if (ask - price) / price > XAG_MAX_SPREAD_PCT:
            return False, "SUSPICIOUS_PRICE", f"XAG bid-ask spread {(ask - price)/price:.1%} exceeds {XAG_MAX_SPREAD_PCT:.0%}"
    return True, None, None


def validate_pq_quote(candidate: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
    """Single-source structural, spread, and cross-row consistency validation for Phu Quy."""
    tgt = candidate.get("target") or {}
    buy = tgt.get("buy")
    sell = tgt.get("sell")
    if buy is None or sell is None or not isinstance(buy, int) or not isinstance(sell, int):
        return False, "MALFORMED_VALUE", f"Missing or non-integer buy/sell: buy={buy}, sell={sell}"
    if buy <= 0 or sell <= 0:
        return False, "MALFORMED_VALUE", f"Zero or negative price: buy={buy}, sell={sell}"
    if buy < PQ_MIN_BUY_VND or buy > PQ_MAX_BUY_VND:
        return False, "SUSPICIOUS_PRICE", f"Buy price {buy} outside sanity bounds [{PQ_MIN_BUY_VND}, {PQ_MAX_BUY_VND}]"
    if sell < buy:
        return False, "MALFORMED_VALUE", f"Sell price ({sell}) < buy price ({buy})"
    spread_pct = (sell - buy) / buy
    if spread_pct < PQ_MIN_SPREAD_PCT or spread_pct > PQ_MAX_SPREAD_PCT:
        return False, "SUSPICIOUS_PRICE", f"Abnormal buy-sell spread {spread_pct:.2%} (expected 0.1% - 12%)"

    # Cross-row consistency check if 1 KG row is present in items
    items = candidate.get("items") or []
    kg_item = next((i for i in items if is_1kg_bar_name(i.get("type", ""), i.get("unit", "")) and (i.get("buy") or 0) > 10_000_000), None)
    if kg_item and kg_item.get("buy"):
        implied_luong = kg_item["buy"] / LUONG_PER_KG
        diff_pct = abs(implied_luong - buy) / buy
        if diff_pct > 0.025:  # > 2.5% internal discrepancy between 1 Luong and 1 KG on same board
            return False, "SUSPICIOUS_PRICE", f"Internal cross-row mismatch: 1L={buy} vs 1KG/26.67={implied_luong:.0f} ({diff_pct:.1%})"

    return True, None, None


def check_jump_and_consensus(
    candidates: List[Dict[str, Any]],
    lkg_value: Optional[float],
    lkg_timestamp: Optional[str],
    now_dt: datetime,
    value_getter: Callable[[Dict[str, Any]], float],
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Evaluates multi-source consensus, detects outliers, and gates abnormal jumps against Last-Known-Good.

    Returns (accepted_candidates, disagreement_report).
    """
    valid = [c for c in candidates if c.get("validationOk")]
    if not valid:
        return [], None

    disagreement_report: Optional[Dict[str, Any]] = None
    vals = [value_getter(c) for c in valid]

    # 1. Multi-source disagreement check
    if len(valid) >= 2:
        min_v, max_v = min(vals), max(vals)
        div_pct = (max_v - min_v) / min_v if min_v > 0 else 0.0
        if div_pct >= DISAGREEMENT_WARN_PCT:
            disagreement_report = {
                "detected": True,
                "divergencePct": round(div_pct * 100, 2),
                "sources": {c["sourceName"]: value_getter(c) for c in valid},
            }

        # If 3+ sources, use median to reject outliers (> 3.0% from median when at least 2 sources agree)
        if len(valid) >= 3 and div_pct >= DISAGREEMENT_OUTLIER_PCT:
            sorted_vals = sorted(vals)
            n_v = len(sorted_vals)
            if n_v % 2 == 1:
                median_val = sorted_vals[n_v // 2]
            else:
                low_mid, high_mid = sorted_vals[n_v // 2 - 1], sorted_vals[n_v // 2]
                if lkg_value and lkg_value > 0 and abs(high_mid - low_mid) / max(1.0, low_mid) >= DISAGREEMENT_OUTLIER_PCT:
                    median_val = low_mid if abs(low_mid - lkg_value) <= abs(high_mid - lkg_value) else high_mid
                else:
                    median_val = (low_mid + high_mid) / 2.0
            filtered = []
            for c in valid:
                v = value_getter(c)
                if abs(v - median_val) / median_val > DISAGREEMENT_OUTLIER_PCT:
                    c["validationOk"] = False
                    c["failureReason"] = "SUSPICIOUS_PRICE"
                    c["whyRejected"] = f"Rejected as outlier ({v}) vs multi-source median ({median_val}, divergence {abs(v - median_val)/median_val:.1%})"
                else:
                    filtered.append(c)
            if filtered:
                valid = filtered
        # If exactly 2 sources and they conflict sharply (> 3.0%)
        elif len(valid) == 2 and div_pct >= DISAGREEMENT_OUTLIER_PCT and lkg_value and lkg_value > 0:
            # Prefer the source closer to Last-Known-Good if one is within normal move (< 4%) and the other jumped wildly
            d0 = abs(value_getter(valid[0]) - lkg_value) / lkg_value
            d1 = abs(value_getter(valid[1]) - lkg_value) / lkg_value
            if d0 > JUMP_INTRADAY_MAX_PCT and d1 <= JUMP_INTRADAY_MAX_PCT:
                valid[0]["validationOk"] = False
                valid[0]["failureReason"] = "SUSPICIOUS_PRICE"
                valid[0]["whyRejected"] = f"Conflicted with {valid[1]['sourceName']} ({div_pct:.1%}) and jumped {d0:.1%} from LKG ({lkg_value})"
                valid = [valid[1]]
            elif d1 > JUMP_INTRADAY_MAX_PCT and d0 <= JUMP_INTRADAY_MAX_PCT:
                valid[1]["validationOk"] = False
                valid[1]["failureReason"] = "SUSPICIOUS_PRICE"
                valid[1]["whyRejected"] = f"Conflicted with {valid[0]['sourceName']} ({div_pct:.1%}) and jumped {d1:.1%} from LKG ({lkg_value})"
                valid = [valid[0]]

    # 2. Abnormal jump check against Last-Known-Good (when unconfirmed by a second source)
    if lkg_value and lkg_value > 0:
        lkg_dt = parse_iso(lkg_timestamp)
        elapsed_mins = (now_dt - lkg_dt).total_seconds() / 60.0 if lkg_dt else 999.0
        max_jump = JUMP_INTRADAY_MAX_PCT if elapsed_mins <= 30 else JUMP_SESSION_MAX_PCT

        confirmed_filtered = []
        for c in valid:
            v = value_getter(c)
            jump_pct = abs(v - lkg_value) / lkg_value
            if jump_pct > max_jump:
                # Check if at least one other independent source in `valid` confirms this new price level (within 2%)
                corroborated_by = [
                    other["sourceName"]
                    for other in valid
                    if other["sourceName"] != c["sourceName"] and abs(value_getter(other) - v) / v <= 0.02
                ]
                if corroborated_by:
                    c["confirmedBy"] = corroborated_by
                    confirmed_filtered.append(c)
                else:
                    c["validationOk"] = False
                    c["failureReason"] = "SUSPICIOUS_PRICE"
                    c["whyRejected"] = (
                        f"Unconfirmed abnormal jump of {jump_pct:.1%} from LKG ({lkg_value} -> {v}) "
                        f"exceeds threshold {max_jump:.1%} without independent source confirmation"
                    )
            else:
                # Normal range: also record any corroborating sources within 1.5%
                c["confirmedBy"] = [
                    other["sourceName"]
                    for other in valid
                    if other["sourceName"] != c["sourceName"] and abs(value_getter(other) - v) / v <= 0.015
                ]
                confirmed_filtered.append(c)
        valid = confirmed_filtered

    return valid, disagreement_report


# ============================================================================
# 6. SOURCE FETCHERS & SELECTION ENGINE
# ============================================================================

def update_health_record(
    prev_health: Dict[str, Any],
    source_name: str,
    attempt: Dict[str, Any],
    market_status: str,
    selected: bool,
) -> Dict[str, Any]:
    """Builds the updated sourceHealth entry for a single source (Requirement C)."""
    old = prev_health.get(source_name) or {}
    ok = bool(attempt.get("validationOk"))
    now_iso = attempt.get("fetchedTimestamp") or iso_utc()

    if ok:
        consec = 0
        last_success = now_iso
        last_failure = old.get("lastFailure")
        last_val = attempt.get("summaryValue")
        last_src_ts = attempt.get("sourceTimestamp") or old.get("lastSourceTimestamp")
        last_err = None
        fail_reason = "NONE"
        stale_status = attempt.get("freshnessStatus") or ("MARKET_CLOSED" if "CLOSED" in market_status else "FRESH")
    else:
        consec = int(old.get("consecutiveFailures") or 0) + 1
        last_success = old.get("lastSuccess")
        last_failure = now_iso
        last_val = old.get("lastValue")
        last_src_ts = old.get("lastSourceTimestamp")
        last_err = attempt.get("error") or attempt.get("whyRejected")
        fail_reason = attempt.get("failureReason") or "SOURCE_FAILED"
        stale_status = "SOURCE_FAILED" if consec >= 3 else "DEGRADED"

    return {
        "sourceName": source_name,
        "adapter": attempt.get("adapter") or old.get("adapter"),
        "currentlySelected": selected,
        "lastSuccess": last_success,
        "lastFailure": last_failure,
        "consecutiveFailures": consec,
        "lastLatencyMs": attempt.get("latencyMs"),
        "lastValue": last_val,
        "lastSourceTimestamp": last_src_ts,
        "lastFetchedTimestamp": now_iso,
        "staleStatus": stale_status,
        "marketStatus": market_status,
        "failureReason": fail_reason,
        "lastError": last_err,
        "whySelectedOrRejected": attempt.get("whySelected") if selected else attempt.get("whyRejected"),
    }


class PriceEngine:
    """Resilient Multi-Source Price Engine for XAG/USD and Phu Quy Silver."""

    def __init__(
        self,
        fetcher: Callable[[str], Tuple[int, str, int]] = default_http_fetch,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC_TZ),
    ) -> None:
        self.fetcher = fetcher
        self.now_fn = now_fn

    def _safe_fetch(self, url: str) -> Tuple[Optional[str], int, Optional[str], Optional[str]]:
        """Returns (body, latency_ms, failure_reason, error_message)."""
        t0 = time.perf_counter()
        try:
            status, body, ms = self.fetcher(url)
            if status != 200:
                return None, ms, "HTTP_ERROR", f"HTTP {status}"
            if not body or not body.strip():
                return None, ms, "EMPTY_RESPONSE", "Empty HTTP response body"
            return body, ms, None, None
        except urllib.error.HTTPError as e:
            ms = int(round((time.perf_counter() - t0) * 1000))
            return None, ms, "HTTP_ERROR", f"HTTPError {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            ms = int(round((time.perf_counter() - t0) * 1000))
            return None, ms, "NETWORK_TIMEOUT", f"URLError: {e.reason}"
        except TimeoutError as e:
            ms = int(round((time.perf_counter() - t0) * 1000))
            return None, ms, "NETWORK_TIMEOUT", f"TimeoutError: {e}"
        except Exception as e:  # noqa: BLE001
            ms = int(round((time.perf_counter() - t0) * 1000))
            return None, ms, "NETWORK_TIMEOUT", f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------------
    # PHU QUY MULTI-SOURCE PIPELINE
    # ------------------------------------------------------------------------
    def fetch_phuquy_candidates(self, now_dt: datetime) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        now_str = iso_utc(now_dt)

        # --- Source 1: Official Phu Quy JSON + Partial Endpoints ---
        t0 = time.perf_counter()
        dt_body, dt_ms, dt_fail, dt_err = self._safe_fetch(PQ_DATETIME_URL)
        part_body, part_ms, part_fail, part_err = self._safe_fetch(PQ_PARTIAL_URL)
        w_body, w_ms, _, _ = self._safe_fetch(PQ_WORLD_URL)
        total_ms = dt_ms + part_ms + w_ms

        s1: Dict[str, Any] = {
            "sourceName": "pq_official_ajax",
            "priority": 1,
            "isAuthoritative": True,
            "fetchedTimestamp": now_str,
            "latencyMs": total_ms or int(round((time.perf_counter() - t0) * 1000)),
            "validationOk": False,
        }
        if part_body:
            parsed = parse_phuquy_html_multi_adapter(part_body)
            json_as_of = None
            if dt_body:
                try:
                    dt_json = json.loads(dt_body)
                    if dt_json.get("success") and dt_json.get("lastTime") and dt_json.get("lastDate"):
                        m_t = re.search(r"(\d{1,2}):(\d{2})", str(dt_json["lastTime"]))
                        m_d = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(dt_json["lastDate"]))
                        if m_t and m_d:
                            json_as_of = datetime(
                                int(m_d.group(3)), int(m_d.group(2)), int(m_d.group(1)),
                                int(m_t.group(1)), int(m_t.group(2)), tzinfo=VN_TZ
                            ).isoformat()
                except Exception:
                    pass
            ref_world = parsed.get("ref")
            if w_body:
                try:
                    wj = parse_world_silver_update_json(w_body, "pq_world_json")
                    if wj.get("price") and wj.get("vndPerKg"):
                        ref_world = {"usd": wj["rawBid"], "vndPerKg": wj["vndPerKg"], "vcbUsdRate": wj.get("vcbUsdRate")}
                except Exception:
                    pass

            if parsed.get("ok"):
                s1.update({
                    "adapter": f"json_datetime+{parsed['adapter']}" if json_as_of else parsed["adapter"],
                    "target": parsed["target"],
                    "items": parsed["items"],
                    "sourceTimestamp": json_as_of or parsed.get("asOf"),
                    "ref": ref_world,
                    "summaryValue": {"buy": parsed["target"]["buy"], "sell": parsed["target"]["sell"]},
                })
                ok, reason, msg = validate_pq_quote(s1)
                s1["validationOk"] = ok
                s1["failureReason"] = reason
                s1["whyRejected"] = msg
            else:
                # Inspect if malformed value vs parser broken
                s1["adapter"] = None
                s1["failureReason"] = self._classify_broken_html(part_body)
                s1["whyRejected"] = f"Partial HTML failed all 4 adapters ({s1['failureReason']})"
        else:
            s1["failureReason"] = part_fail or dt_fail or "HTTP_ERROR"
            s1["error"] = part_err or dt_err
            s1["whyRejected"] = f"Fetch failed: {s1['error']}"
        candidates.append(s1)

        # --- Source 2: Official Phu Quy Full HTML Page ---
        page_body, page_ms, page_fail, page_err = self._safe_fetch(PQ_BASE_URL)
        s2: Dict[str, Any] = {
            "sourceName": "pq_official_html",
            "priority": 2,
            "isAuthoritative": True,
            "fetchedTimestamp": now_str,
            "latencyMs": page_ms,
            "validationOk": False,
        }
        if page_body:
            parsed = parse_phuquy_html_multi_adapter(page_body)
            if parsed.get("ok"):
                s2.update({
                    "adapter": parsed["adapter"],
                    "target": parsed["target"],
                    "items": parsed["items"],
                    "sourceTimestamp": parsed.get("asOf") or s1.get("sourceTimestamp"),
                    "ref": parsed.get("ref") or s1.get("ref"),
                    "summaryValue": {"buy": parsed["target"]["buy"], "sell": parsed["target"]["sell"]},
                })
                ok, reason, msg = validate_pq_quote(s2)
                s2["validationOk"] = ok
                s2["failureReason"] = reason
                s2["whyRejected"] = msg
            else:
                s2["failureReason"] = self._classify_broken_html(page_body)
                s2["whyRejected"] = f"Full HTML failed all 4 adapters ({s2['failureReason']})"
        else:
            s2["failureReason"] = page_fail or "HTTP_ERROR"
            s2["error"] = page_err
            s2["whyRejected"] = f"Fetch failed: {page_err}"
        candidates.append(s2)

        # --- Source 3: giabac.vn JSON API Fallback (Secondary, Non-Authoritative) ---
        gb_body, gb_ms, gb_fail, gb_err = self._safe_fetch(GIABAC_CHART_L_URL)
        s3: Dict[str, Any] = {
            "sourceName": "giabac_vn_json",
            "priority": 3,
            "isAuthoritative": False,
            "fetchedTimestamp": now_str,
            "latencyMs": gb_ms,
            "validationOk": False,
        }
        if gb_body:
            try:
                gb_data = json.loads(gb_body)
                dates = gb_data.get("Dates") or []
                buys = gb_data.get("LastBuyPrices") or []
                sells = gb_data.get("LastSellPrices") or []
                if dates and buys and sells and len(buys) == len(sells):
                    buy_v = int(round(float(buys[-1])))
                    sell_v = int(round(float(sells[-1])))
                    last_day = str(dates[-1])[:10]
                    src_ts = f"{last_day}T08:30:00+07:00" if re.match(r"^\d{4}-\d{2}-\d{2}$", last_day) else None
                    s3.update({
                        "adapter": "giabac_sql_chart_json",
                        "target": {"name": TARGET_CANONICAL, "buy": buy_v, "sell": sell_v},
                        "items": [{"type": TARGET_CANONICAL, "unit": "VNĐ/LƯỢNG", "buy": buy_v, "sell": sell_v}],
                        "sourceTimestamp": src_ts,
                        "timestampGranularity": "date",
                        "summaryValue": {"buy": buy_v, "sell": sell_v},
                    })
                    ok, reason, msg = validate_pq_quote(s3)
                    s3["validationOk"] = ok
                    s3["failureReason"] = reason
                    s3["whyRejected"] = msg
                else:
                    s3["failureReason"] = "PARSER_BROKEN"
                    s3["whyRejected"] = "giabac.vn JSON missing Dates/LastBuyPrices/LastSellPrices arrays"
            except Exception as e:
                s3["failureReason"] = "MALFORMED_VALUE"
                s3["whyRejected"] = f"Invalid JSON from giabac.vn: {e}"
        else:
            s3["failureReason"] = gb_fail or "HTTP_ERROR"
            s3["error"] = gb_err
            s3["whyRejected"] = f"Fetch failed: {gb_err}"
        candidates.append(s3)

        # --- Source 4: giabac.vn HTML Fallback (Secondary, Non-Authoritative) ---
        gbh_body, gbh_ms, gbh_fail, gbh_err = self._safe_fetch(GIABAC_BASE_URL)
        s4: Dict[str, Any] = {
            "sourceName": "giabac_vn_html",
            "priority": 4,
            "isAuthoritative": False,
            "fetchedTimestamp": now_str,
            "latencyMs": gbh_ms,
            "validationOk": False,
        }
        if gbh_body:
            parsed = parse_phuquy_html_multi_adapter(gbh_body)
            if parsed.get("ok"):
                s4.update({
                    "adapter": parsed["adapter"],
                    "target": parsed["target"],
                    "items": parsed["items"],
                    "sourceTimestamp": parsed.get("asOf"),
                    "summaryValue": {"buy": parsed["target"]["buy"], "sell": parsed["target"]["sell"]},
                })
                ok, reason, msg = validate_pq_quote(s4)
                s4["validationOk"] = ok
                s4["failureReason"] = reason
                s4["whyRejected"] = msg
            else:
                s4["failureReason"] = self._classify_broken_html(gbh_body)
                s4["whyRejected"] = f"giabac.vn HTML failed all 4 adapters ({s4['failureReason']})"
        else:
            s4["failureReason"] = gbh_fail or "HTTP_ERROR"
            s4["error"] = gbh_err
            s4["whyRejected"] = f"Fetch failed: {gbh_err}"
        candidates.append(s4)

        return candidates

    def _classify_broken_html(self, html_text: str) -> str:
        """Distinguishes PARSER_BROKEN vs MALFORMED_VALUE vs SUSPICIOUS_PRICE when HTML adapters reject a page."""
        dom = TableAndLineExtractor()
        dom.feed(html_text or "")
        dom._flush_line()
        for row in dom.rows:
            texts = [c["text"].strip() for c in row if c["text"].strip()]
            if any(is_target_1_luong_name(t) for t in texts):
                nums = [to_int_price(t) for t in texts[1:]]
                nums = [n for n in nums if n is not None]
                if not nums:
                    return "MALFORMED_VALUE"
                if len(nums) >= 2:
                    b, s = nums[0], nums[1]
                    if s < b:
                        return "MALFORMED_VALUE"
                    if b < PQ_MIN_BUY_VND or b > PQ_MAX_BUY_VND or (s - b) / b > PQ_MAX_SPREAD_PCT:
                        return "SUSPICIOUS_PRICE"
                return "MALFORMED_VALUE"
        return "PARSER_BROKEN"

    # ------------------------------------------------------------------------
    # XAG/USD MULTI-SOURCE PIPELINE
    # ------------------------------------------------------------------------
    def fetch_xag_candidates(self, now_dt: datetime) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        now_str = iso_utc(now_dt)

        specs = [
            ("swissquote_bbo", 1, True, SWISSQUOTE_URL, parse_swissquote_json),
            ("yahoo_xag", 2, True, YAHOO_XAG_URL, parse_yahoo_json),
            ("kitco_silver", 3, True, KITCO_URL, parse_kitco_multi_adapter),
            ("phuquy_world_json", 4, False, PQ_WORLD_URL, lambda b: parse_world_silver_update_json(b, "phuquy_world_json")),
            ("giabac_world_json", 5, False, GIABAC_WORLD_URL, lambda b: parse_world_silver_update_json(b, "giabac_world_json")),
        ]

        for name, prio, is_auth, url, parser_fn in specs:
            body, ms, fail_reason, err_msg = self._safe_fetch(url)
            cand: Dict[str, Any] = {
                "sourceName": name,
                "priority": prio,
                "isAuthoritative": is_auth,
                "fetchedTimestamp": now_str,
                "latencyMs": ms,
                "validationOk": False,
            }
            if body is not None:
                try:
                    parsed = parser_fn(body)
                    cand.update(parsed)
                    cand["summaryValue"] = {"usdPerOz": parsed.get("price")}
                    ok, v_reason, v_msg = validate_xag_quote(cand)
                    cand["validationOk"] = ok
                    cand["failureReason"] = v_reason
                    cand["whyRejected"] = v_msg
                except json.JSONDecodeError as e:
                    cand["failureReason"] = "MALFORMED_VALUE"
                    cand["error"] = f"Malformed JSON: {e}"
                    cand["whyRejected"] = cand["error"]
                except Exception as e:  # noqa: BLE001
                    cand["failureReason"] = "PARSER_BROKEN"
                    cand["error"] = str(e)
                    cand["whyRejected"] = f"Parser error: {e}"
            else:
                cand["failureReason"] = fail_reason or "HTTP_ERROR"
                cand["error"] = err_msg
                cand["whyRejected"] = f"Fetch failed: {err_msg}"
            candidates.append(cand)

        return candidates

    # ------------------------------------------------------------------------
    # SELECTION, MARKET-CLOSED & FRESHNESS RESOLUTION
    # ------------------------------------------------------------------------
    def select_best_phuquy(
        self,
        candidates: List[Dict[str, Any]],
        existing_doc: Dict[str, Any],
        now_dt: datetime,
    ) -> Dict[str, Any]:
        sched = get_phuquy_market_schedule(now_dt)
        lkg_target = existing_doc.get("target") or {}
        lkg_buy = float(lkg_target["buy"]) if lkg_target.get("buy") else None
        lkg_ts = existing_doc.get("priceAsOf")

        valid = [c for c in candidates if c.get("validationOk")]

        # 0. Sanitize future timestamps (> 5m ahead of now_dt) so they cannot poison newest_ts_dt or outrank valid quotes
        for c in valid:
            c_dt = parse_iso(c.get("sourceTimestamp"))
            if c_dt and (c_dt - now_dt) > timedelta(minutes=5):
                c["sourceTimestamp"] = None
                c["timestampGranularity"] = "missing"
                c["futureTimestampQuarantined"] = True

        # 1. Filter out candidates with stale upstream timestamp BEFORE consensus math
        if valid:
            newest_ts_dt = max(
                (
                    parse_iso(c.get("sourceTimestamp"))
                    for c in valid
                    if parse_iso(c.get("sourceTimestamp")) and c.get("timestampGranularity") != "date"
                ),
                default=None,
            )
            if newest_ts_dt is None:
                newest_ts_dt = max(
                    (parse_iso(c.get("sourceTimestamp")) for c in valid if parse_iso(c.get("sourceTimestamp"))),
                    default=None,
                )
            lkg_dt = parse_iso(lkg_ts)
            ref_newest = max([d for d in (newest_ts_dt, lkg_dt) if d is not None], default=None)

            non_stale = []
            for c in valid:
                c_dt = parse_iso(c.get("sourceTimestamp"))
                if c_dt and ref_newest:
                    if c.get("timestampGranularity") == "date":
                        is_stale = c_dt.astimezone(VN_TZ).date() < ref_newest.astimezone(VN_TZ).date()
                    else:
                        is_stale = (ref_newest - c_dt) > timedelta(minutes=30)
                    if is_stale:
                        c["validationOk"] = False
                        c["failureReason"] = "STALE_UPSTREAM"
                        c["whyRejected"] = f"Source timestamp {c.get('sourceTimestamp')} is older than known timestamp {ref_newest.isoformat()}"
                        continue
                non_stale.append(c)
            valid = non_stale

        # 2. Evaluate jump & multi-source consensus among non-stale candidates
        valid, disagreement = check_jump_and_consensus(
            valid, lkg_buy, lkg_ts, now_dt, lambda c: float(c["target"]["buy"])
        )

        selected: Optional[Dict[str, Any]] = None
        if valid:
            # Find newest intraday timestamp (non-date-only) and its corresponding buy/sell quote
            intraday_cands = [
                c for c in valid if parse_iso(c.get("sourceTimestamp")) and c.get("timestampGranularity") != "date"
            ]
            newest_intraday_cand = max(
                intraday_cands,
                key=lambda c: parse_iso(c.get("sourceTimestamp")).timestamp(),
                default=None,
            )
            newest_intraday_epoch = (
                parse_iso(newest_intraday_cand.get("sourceTimestamp")).timestamp() if newest_intraday_cand else 0.0
            )

            def sort_key(c: Dict[str, Any]) -> Tuple[int, int, float, int]:
                dt = parse_iso(c.get("sourceTimestamp"))
                is_intraday = dt is not None and c.get("timestampGranularity") != "date"
                ts_epoch = dt.timestamp() if is_intraday else 0.0
                if newest_intraday_cand and newest_intraday_epoch > 0:
                    if is_intraday and (newest_intraday_epoch - ts_epoch) <= 60:
                        in_freshest_tier = 1
                    elif (
                        c.get("timestampGranularity") == "date"
                        and dt is not None
                        and dt.astimezone(VN_TZ).date() == parse_iso(newest_intraday_cand["sourceTimestamp"]).astimezone(VN_TZ).date()
                        and c["target"]["buy"] == newest_intraday_cand["target"]["buy"]
                        and c["target"]["sell"] == newest_intraday_cand["target"]["sell"]
                    ):
                        in_freshest_tier = 1
                        ts_epoch = newest_intraday_epoch
                    else:
                        in_freshest_tier = 0
                else:
                    in_freshest_tier = 1 if not c.get("futureTimestampQuarantined") else 0
                return (
                    in_freshest_tier,
                    1 if c.get("isAuthoritative") else 0,
                    ts_epoch,
                    -int(c.get("priority", 99)),
                )

            valid.sort(key=sort_key, reverse=True)
            selected = valid[0]
            if (
                selected.get("timestampGranularity") == "date"
                and newest_intraday_cand
                and selected["target"]["buy"] == newest_intraday_cand["target"]["buy"]
            ):
                selected["sourceTimestamp"] = newest_intraday_cand["sourceTimestamp"]
            for other in valid[1:]:
                other["whyRejected"] = f"Valid, but superseded by higher-priority/fresher source {selected['sourceName']}"

        # Determine effective timestamp (Decoupled Timestamp Rule: never discard valid price if timestamp regex failed!)
        if selected:
            src_ts = selected.get("sourceTimestamp")
            if not src_ts:
                # If price is identical to LKG and LKG had a timestamp, preserve LKG timestamp; else use fetchedTimestamp
                if lkg_buy and selected["target"]["buy"] == int(lkg_buy) and lkg_ts:
                    src_ts = lkg_ts
                else:
                    src_ts = now_dt.astimezone(VN_TZ).replace(second=0, microsecond=0).isoformat()
                selected["sourceTimestamp"] = src_ts

            src_dt = parse_iso(src_ts)
            age_sec = int(max(0.0, (now_dt - src_dt).total_seconds())) if src_dt else 0
            if not sched["isOpen"]:
                freshness = "MARKET_CLOSED"
            elif not selected.get("isAuthoritative"):
                freshness = "FALLBACK_ACTIVE"
            else:
                freshness = "LIVE" if selected.get("latencyMs", 9999) < 5000 else "FRESH"

            selected["freshnessStatus"] = freshness
            selected["ageSeconds"] = age_sec
            selected["whySelected"] = (
                f"Selected {selected['sourceName']} via {selected['adapter']} "
                f"(status={freshness}, market={sched['marketStatus']})"
            )
            return {
                "selected": selected,
                "usedLastKnownGood": False,
                "freshnessStatus": freshness,
                "marketSchedule": sched,
                "disagreement": disagreement,
            }

        # All live sources failed -> Fallback to Last-Known-Good (LKG)
        if lkg_buy and lkg_target.get("sell"):
            src_dt = parse_iso(lkg_ts)
            age_sec = int(max(0.0, (now_dt - src_dt).total_seconds())) if src_dt else None
            freshness = "MARKET_CLOSED" if not sched["isOpen"] else "SOURCE_FAILED"
            lkg_record = {
                "sourceName": "last_known_good_pq",
                "adapter": "lkg_preservation",
                "target": lkg_target,
                "items": existing_doc.get("items") or [],
                "sourceTimestamp": lkg_ts,
                "fetchedTimestamp": iso_utc(now_dt),
                "ageSeconds": age_sec,
                "freshnessStatus": freshness,
                "marketStatus": sched["marketStatus"],
                "whySelected": f"All live Phu Quy sources failed; preserved Last-Known-Good from {lkg_ts} ({freshness})",
            }
            return {
                "selected": lkg_record,
                "usedLastKnownGood": True,
                "freshnessStatus": freshness,
                "marketSchedule": sched,
                "disagreement": disagreement,
            }

        return {
            "selected": None,
            "usedLastKnownGood": False,
            "freshnessStatus": "SOURCE_FAILED",
            "marketSchedule": sched,
            "disagreement": disagreement,
        }

    def select_best_xag(
        self,
        candidates: List[Dict[str, Any]],
        existing_doc: Dict[str, Any],
        pq_result: Dict[str, Any],
        now_dt: datetime,
    ) -> Dict[str, Any]:
        sched = get_xag_market_schedule(now_dt)
        lkg_world = existing_doc.get("world") or {}
        lkg_usd = float(lkg_world["usdPerOz"]) if lkg_world.get("usdPerOz") else None
        lkg_ts = lkg_world.get("asOf")

        valid = [c for c in candidates if c.get("validationOk")]

        # 0. Sanitize future timestamps (> 5m ahead of now_dt) so they cannot poison newest_dt or outrank valid quotes
        for c in valid:
            c_dt = parse_iso(c.get("sourceTimestamp"))
            if c_dt and (c_dt - now_dt) > timedelta(minutes=5):
                c["sourceTimestamp"] = None
                c["timestampKind"] = "missing"
                c["futureTimestampQuarantined"] = True

        # 1. Check for stale upstream timestamps during open market hours BEFORE consensus math
        if valid and sched["isOpen"]:
            newest_dt = max(
                (
                    parse_iso(c.get("sourceTimestamp"))
                    for c in valid
                    if parse_iso(c.get("sourceTimestamp")) and c.get("timestampKind") == "market"
                ),
                default=None,
            )
            if newest_dt is None:
                newest_dt = max(
                    (parse_iso(c.get("sourceTimestamp")) for c in valid if parse_iso(c.get("sourceTimestamp"))),
                    default=None,
                )
            non_stale = []
            for c in valid:
                c_dt = parse_iso(c.get("sourceTimestamp"))
                # If a source's timestamp lags the freshest valid source by > 45 minutes during open market, mark STALE_UPSTREAM
                if c_dt and newest_dt and (newest_dt - c_dt) > timedelta(minutes=45):
                    c["validationOk"] = False
                    c["failureReason"] = "STALE_UPSTREAM"
                    c["whyRejected"] = (
                        f"Source timestamp {c.get('sourceTimestamp')} lags freshest source "
                        f"({newest_dt.isoformat()}) by {(newest_dt - c_dt).total_seconds()/60:.0f}m"
                    )
                else:
                    non_stale.append(c)
            valid = non_stale

        # 2. Evaluate jump & multi-source consensus among non-stale candidates
        valid, disagreement = check_jump_and_consensus(
            valid, lkg_usd, lkg_ts, now_dt, lambda c: float(c["price"])
        )

        selected: Optional[Dict[str, Any]] = None
        if valid:
            # Compute newest_market_epoch from genuine exchange market timestamps first
            newest_market_epoch = max(
                (
                    parse_iso(c.get("sourceTimestamp")).timestamp()
                    for c in valid
                    if parse_iso(c.get("sourceTimestamp")) and c.get("timestampKind") == "market"
                ),
                default=0.0,
            )
            newest_any_epoch = max(
                (parse_iso(c.get("sourceTimestamp")).timestamp() for c in valid if parse_iso(c.get("sourceTimestamp"))),
                default=now_dt.timestamp(),
            )

            def xag_sort_key(c: Dict[str, Any]) -> Tuple[int, int, int, int, float]:
                dt = parse_iso(c.get("sourceTimestamp"))
                has_ts = dt is not None and not c.get("futureTimestampQuarantined")
                ts_epoch = dt.timestamp() if dt else (now_dt.timestamp() - 900)
                if not sched["isOpen"]:
                    is_fresh_bucket = 1 if has_ts else 0
                elif newest_market_epoch > 0 and c.get("timestampKind") == "market":
                    # Compare true market-timestamp sources within a tight 180s (3m) window
                    is_fresh_bucket = 1 if (newest_market_epoch - ts_epoch) <= 180 else 0
                else:
                    is_fresh_bucket = 1 if (has_ts and (newest_any_epoch - ts_epoch) <= 900) else 0
                return (
                    is_fresh_bucket,
                    1 if c.get("isAuthoritative") else 0,
                    1 if has_ts else 0,
                    -int(c.get("priority", 99)),
                    ts_epoch,
                )

            valid.sort(key=xag_sort_key, reverse=True)
            selected = valid[0]
            for other in valid[1:]:
                other["whyRejected"] = f"Valid, but superseded by fresher/higher-priority source {selected['sourceName']}"

        # Determine USD->VND conversion rate (prefer Phu Quy's official vcbUsdRate or reference rate)
        rate = None
        pq_sel = pq_result.get("selected") or {}
        pq_ref = pq_sel.get("ref") or {}
        if pq_ref.get("usd") and pq_ref.get("vndPerKg"):
            rate = pq_ref["vndPerKg"] / (pq_ref["usd"] * OZ_PER_KG)
        else:
            # Check if phuquy_world_json or giabac_world_json returned vndPerKg
            for c in candidates:
                if c.get("rawBid") and c.get("vndPerKg"):
                    rate = c["vndPerKg"] / (c["rawBid"] * OZ_PER_KG)
                    break
        if not rate and lkg_world.get("usdPerOz") and lkg_world.get("vndPerKg"):
            rate = float(lkg_world["vndPerKg"]) / (float(lkg_world["usdPerOz"]) * OZ_PER_KG)
        if not rate:
            rate = 26020.0  # Safe fallback rate

        # Borrow changePct from Yahoo or Kitco if selected is Swissquote (which doesn't include daily changePct)
        if selected:
            if selected.get("changePct") is None:
                for c in valid:
                    if c.get("changePct") is not None:
                        selected["changePct"] = c["changePct"]
                        break
                if selected.get("changePct") is None and lkg_world.get("usdPerOz") and lkg_world.get("changePct") is not None:
                    prev_u = float(lkg_world["usdPerOz"])
                    prev_chg = float(lkg_world["changePct"])
                    if prev_u > 0 and prev_chg > -99:
                        prev_close = prev_u / (1.0 + prev_chg / 100.0)
                        selected["changePct"] = round(((selected["price"] - prev_close) / prev_close) * 100.0, 2)

            src_ts = selected.get("sourceTimestamp") or iso_utc(now_dt)
            selected["sourceTimestamp"] = src_ts
            src_dt = parse_iso(src_ts)
            age_sec = int(max(0.0, (now_dt - src_dt).total_seconds())) if src_dt else 0

            if not sched["isOpen"]:
                freshness = "MARKET_CLOSED"
            elif not selected.get("isAuthoritative") or selected["priority"] >= 3:
                freshness = "FALLBACK_ACTIVE" if selected["priority"] > 1 else "LIVE"
            else:
                freshness = "LIVE" if age_sec < 180 else "FRESH"

            selected["freshnessStatus"] = freshness
            selected["ageSeconds"] = age_sec
            selected["vndPerKg"] = int(round(selected["price"] * OZ_PER_KG * rate))
            selected["rateVndUsd"] = int(round(rate / 10.0) * 10)
            selected["whySelected"] = (
                f"Selected {selected['sourceName']} ({selected['price']} USD/oz, "
                f"status={freshness}, market={sched['marketStatus']})"
            )
            return {
                "selected": selected,
                "usedLastKnownGood": False,
                "freshnessStatus": freshness,
                "marketSchedule": sched,
                "disagreement": disagreement,
            }

        # Fallback to Last-Known-Good XAG/USD
        if lkg_usd:
            src_dt = parse_iso(lkg_ts)
            age_sec = int(max(0.0, (now_dt - src_dt).total_seconds())) if src_dt else None
            freshness = "MARKET_CLOSED" if not sched["isOpen"] else "SOURCE_FAILED"
            lkg_rec = {
                "sourceName": "last_known_good_world",
                "adapter": "lkg_preservation",
                "price": lkg_usd,
                "changePct": lkg_world.get("changePct"),
                "sourceTimestamp": lkg_ts,
                "fetchedTimestamp": iso_utc(now_dt),
                "ageSeconds": age_sec,
                "freshnessStatus": freshness,
                "marketStatus": sched["marketStatus"],
                "vndPerKg": lkg_world.get("vndPerKg") or int(round(lkg_usd * OZ_PER_KG * rate)),
                "rateVndUsd": int(round(rate / 10.0) * 10),
                "whySelected": f"All live XAG/USD sources failed; preserved Last-Known-Good from {lkg_ts} ({freshness})",
            }
            return {
                "selected": lkg_rec,
                "usedLastKnownGood": True,
                "freshnessStatus": freshness,
                "marketSchedule": sched,
                "disagreement": disagreement,
            }

        return {
            "selected": None,
            "usedLastKnownGood": False,
            "freshnessStatus": "SOURCE_FAILED",
            "marketSchedule": sched,
            "disagreement": disagreement,
        }

    def run_cycle(self, existing_doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Executes a complete fetch, validation, selection, and health-tracking cycle."""
        now_dt = self.now_fn()
        doc = json.loads(json.dumps(existing_doc or {}))
        prev_health = ((doc.get("sourceHealth") or {}).get("sources")) or {}

        pq_candidates = self.fetch_phuquy_candidates(now_dt)
        pq_res = self.select_best_phuquy(pq_candidates, doc, now_dt)

        xag_candidates = self.fetch_xag_candidates(now_dt)
        xag_res = self.select_best_xag(xag_candidates, doc, pq_res, now_dt)

        # Build updated sourceHealth registry
        new_sources_health: Dict[str, Any] = {}
        pq_sel_name = (pq_res.get("selected") or {}).get("sourceName")
        xag_sel_name = (xag_res.get("selected") or {}).get("sourceName")

        for c in pq_candidates:
            sname = c["sourceName"]
            new_sources_health[sname] = update_health_record(
                prev_health, sname, c, pq_res["marketSchedule"]["marketStatus"], sname == pq_sel_name
            )
        for c in xag_candidates:
            sname = c["sourceName"]
            new_sources_health[sname] = update_health_record(
                prev_health, sname, c, xag_res["marketSchedule"]["marketStatus"], sname == xag_sel_name
            )

        return {
            "timestamp": iso_utc(now_dt),
            "phuQuy": pq_res,
            "xagUsd": xag_res,
            "pqCandidates": pq_candidates,
            "xagCandidates": xag_candidates,
            "sourceHealth": {
                "updatedAt": iso_utc(now_dt),
                "phuQuyMarketStatus": pq_res["marketSchedule"]["marketStatus"],
                "phuQuyFreshness": pq_res["freshnessStatus"],
                "xagMarketStatus": xag_res["marketSchedule"]["marketStatus"],
                "xagFreshness": xag_res["freshnessStatus"],
                "disagreements": {
                    "phuQuy": pq_res.get("disagreement"),
                    "xagUsd": xag_res.get("disagreement"),
                },
                "sources": new_sources_health,
            },
        }


# ============================================================================
# 7. ENDPOINT PROBE & DIAGNOSTICS CLI (Step 2 & Step 4)
# ============================================================================

def probe_all_endpoints() -> Dict[str, Any]:
    """Tests every candidate endpoint live and records detailed characteristics."""
    endpoints = [
        ("XAG/USD Primary: Swissquote BBO JSON", SWISSQUOTE_URL, "json"),
        ("XAG/USD Secondary: Yahoo Finance Chart v8 JSON", YAHOO_XAG_URL, "json"),
        ("XAG/USD Tertiary: Kitco Silver Page", KITCO_URL, "html"),
        ("Phu Quy AJAX 1: GetDateTimeUpdate JSON", PQ_DATETIME_URL, "json"),
        ("Phu Quy AJAX 2: SilverPricePartial HTML", PQ_PARTIAL_URL, "html"),
        ("Phu Quy AJAX 3: GetWorldSilverPriceUpdate JSON", PQ_WORLD_URL, "json"),
        ("Phu Quy Official Full Page", PQ_BASE_URL, "html"),
        ("GiaBac.vn Fallback 1: SQL Chart Lượng JSON", GIABAC_CHART_L_URL, "json"),
        ("GiaBac.vn Fallback 2: World Silver JSON", GIABAC_WORLD_URL, "json"),
        ("GiaBac.vn Fallback 3: Full Page HTML", GIABAC_BASE_URL, "html"),
        ("Rejected Candidate: phuquy.com.vn Main Site", "https://phuquy.com.vn/", "html"),
    ]
    results = []
    for label, url, expected_kind in endpoints:
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "vi,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode("utf-8", errors="replace")
                ms = int(round((time.perf_counter() - t0) * 1000))
                ctype = r.headers.get("Content-Type", "")
                results.append({
                    "label": label,
                    "url": url,
                    "status": r.status,
                    "contentType": ctype,
                    "bytes": len(raw),
                    "latencyMs": ms,
                    "ok": True,
                })
        except Exception as e:  # noqa: BLE001
            ms = int(round((time.perf_counter() - t0) * 1000))
            results.append({
                "label": label,
                "url": url,
                "status": getattr(e, "code", None),
                "error": f"{type(e).__name__}: {e}",
                "latencyMs": ms,
                "ok": False,
            })
    return {"probedAt": iso_utc(), "endpoints": results}


def main() -> int:
    ap = argparse.ArgumentParser(description="Standalone Resilient Price Engine (Phase 1)")
    ap.add_argument("--probe-endpoints", action="store_true", help="Probe all candidate endpoints and print report")
    ap.add_argument("--dry-run", action="store_true", help="Run a live cycle against data/market.json (read-only) and print diagnostics")
    args = ap.parse_args()

    if args.probe_endpoints:
        print(json.dumps(probe_all_endpoints(), ensure_ascii=False, indent=2))
        return 0

    data_path = Path(__file__).resolve().parent.parent / "data" / "market.json"
    existing = json.loads(data_path.read_text("utf-8")) if data_path.exists() else {}
    engine = PriceEngine()
    res = engine.run_cycle(existing)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
