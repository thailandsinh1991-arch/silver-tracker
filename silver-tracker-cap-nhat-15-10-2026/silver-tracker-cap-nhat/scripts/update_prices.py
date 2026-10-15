#!/usr/bin/env python3
"""Cập nhật data/market.json từ bảng giá Phú Quý và giá bạc thế giới (Kitco).

Chạy bởi GitHub Actions. Chỉ dùng thư viện chuẩn của Python.
Nếu một nguồn lỗi, dữ liệu cũ của nguồn đó được giữ nguyên.
"""
import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

DATA = Path(__file__).resolve().parent.parent / "data" / "market.json"
PQ_URL = "https://giabac.phuquygroup.vn/"
KITCO_URL = "https://www.kitco.com/charts/silver"
TARGET = "BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG"
OZ_PER_KG = 32.1507466
VN = timezone(timedelta(hours=7))
NY = ZoneInfo("America/New_York")
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


def main():
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
            # Sổ vĩnh viễn: ghi cả bảng giá mỗi khi Phú Quý đăng bảng mới (đổi giờ niêm yết hoặc đổi giá).
            rows = [[price_as_of, i["type"], i.get("unit") or "", i.get("buy") or "", i.get("sell") or ""] for i in items]
            # Chép bù các mức giá có trong market.json nhưng chưa có trong sổ (vd. đổi giá trước khi sổ được tạo).
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
        # Giá không đổi: chỉ ghi lại giờ kiểm tra mỗi 2 tiếng để repo không bị commit dày đặc.
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
    # Lỗi cả hai nguồn → báo đỏ trong tab Actions.
    return 0 if (pq_ok or world) else 1


if __name__ == "__main__":
    sys.exit(main())
