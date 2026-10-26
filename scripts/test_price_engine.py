#!/usr/bin/env python3
"""Offline Automated Test Suite & Shadow-Mode Simulation Harness (Phase 1 & Shadow Integration).

Covers:
  - All 18 failure scenarios in PriceEngine
  - All 10 side-by-side Legacy vs PriceEngine failure simulations (Phase E)
  - Backward compatibility verification for all existing market.json fields (Phase F)
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import price_engine as pe  # noqa: E402
import update_prices as up  # noqa: E402

VN_TZ = timezone(timedelta(hours=7))
UTC_TZ = timezone.utc

SAMPLE_PQ_HTML_STANDARD = """
<div class="silver-price-page">
  <div id="last-update-time">09:15</div>
  <div id="last-update-date">23/10/2026</div>
  <table class="silver-price-table">
    <tbody>
      <tr>
        <td class="silver-product-name">BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG</td>
        <td class="silver-unit">VNĐ/LƯỢNG</td>
        <td class="silver-price silver-buy-price">1,860,000</td>
        <td class="silver-price silver-sell-price">1,918,000</td>
      </tr>
      <tr>
        <td class="silver-product-name">BẠC THỎI PHÚ QUÝ 999 10 LƯỢNG, 5 LƯỢNG</td>
        <td class="silver-unit">VNĐ/LƯỢNG</td>
        <td class="silver-price silver-buy-price">1,860,000</td>
        <td class="silver-price silver-sell-price">1,918,000</td>
      </tr>
      <tr>
        <td class="silver-product-name">ĐỒNG BẠC MỸ NGHỆ PHÚ QUÝ 999</td>
        <td class="silver-unit">VNĐ/LƯỢNG</td>
        <td class="silver-price silver-buy-price">1,860,000</td>
        <td class="silver-price silver-sell-price">2,185,000</td>
      </tr>
      <tr>
        <td class="silver-product-name">BẠC THỎI PHÚ QUÝ 999 1KILO</td>
        <td class="silver-unit">VNĐ/KG</td>
        <td class="silver-price silver-buy-price">49,600,000</td>
        <td class="silver-price silver-sell-price">51,146,667</td>
      </tr>
    </tbody>
  </table>
  <section id="worldSilverPriceContainer">54.10 USD/OUNCE ≈ 45,250,000 VNĐ/KG</section>
</div>
"""

SAMPLE_PQ_DATETIME_JSON = json.dumps({"success": True, "lastTime": "09:15", "lastDate": "23/10/2026"})

SAMPLE_PQ_WORLD_JSON = json.dumps({
    "success": True,
    "worldSilverPrice": 54.10,
    "worldSilverPriceText": "54.10",
    "worldSilverPriceVndKg": 45250000,
    "vcbUsdRate": 26020.0,
    "serverTime": "/Date(1792721700000)/",
})

SAMPLE_GIABAC_CHART_JSON = json.dumps({
    "Type": "Giá vnd/Lượng",
    "Dates": ["2026-10-22", "2026-10-23"],
    "LastBuyPrices": [1845000.0, 1860000.0],
    "LastSellPrices": [1902000.0, 1918000.0],
})

SAMPLE_SWISSQUOTE_JSON = json.dumps([
    {
        "ts": 1792721700000,  # 2026-10-23T02:15:00Z (09:15 VN)
        "spreadProfilePrices": [
            {"spreadProfile": "prime", "bid": 54.12, "ask": 54.18}
        ],
    }
])

SAMPLE_YAHOO_JSON = json.dumps({
    "chart": {
        "result": [
            {
                "meta": {
                    "regularMarketPrice": 54.14,
                    "chartPreviousClose": 53.50,
                    "regularMarketTime": 1792721700,
                }
            }
        ]
    }
})

SAMPLE_KITCO_HTML = """
<html><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"quote":{"bid":54.11,"changePercentage":1.14,"timestamp":1792721690}}}}
</script>
<div>Bid 54.11 +1.14% Oct 22, 2026 - 10:15 PM</div>
</body></html>
"""

BASE_EXISTING_DOC = {
    "fetchedAt": "2026-10-23T02:00:00Z",
    "priceAsOf": "2026-10-23T09:00:00+07:00",
    "itemsAsOf": "2026-10-23T09:00:00+07:00",
    "source": "phuquy",
    "target": {"name": pe.TARGET_CANONICAL, "buy": 1855000, "sell": 1912000},
    "items": [{"type": pe.TARGET_CANONICAL, "unit": "VNĐ/LƯỢNG", "buy": 1855000, "sell": 1912000}],
    "world": {"usdPerOz": 54.00, "changePct": 0.93, "asOf": "2026-10-23T02:00:00Z", "vndPerKg": 45160000, "source": "Kitco (giá bid)"},
    "history": [{"at": "2026-10-23T09:00:00+07:00", "buy": 1855000, "sell": 1912000}],
    "worldHistory": [{"at": "2026-10-23T02:00:00Z", "usd": 54.00}],
    "errors": [],
}


def make_mock_fetcher(routes: Dict[str, Tuple[int, str, int]]):
    # Sort patterns longest-first so specific subpaths match before base URLs
    sorted_patterns = sorted(routes.keys(), key=len, reverse=True)

    def _engine_fetch(url: str) -> Tuple[int, str, int]:
        for pattern in sorted_patterns:
            if pattern in url:
                status, body, ms = routes[pattern]
                if status == -1:
                    raise TimeoutError("Simulated network timeout")
                return status, body, ms
        return 404, "Not Found", 50

    def _legacy_fetch(url: str) -> str:
        status, body, _ = _engine_fetch(url)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        return body

    return _legacy_fetch, _engine_fetch


class TestResilientPriceEngine(unittest.TestCase):
    """Comprehensive offline test suite covering all 18 failure scenarios and edge cases."""

    def setUp(self) -> None:
        self.open_friday_dt = datetime(2026, 10, 23, 2, 16, 0, tzinfo=UTC_TZ)

    def test_scenario_01_phuquy_changes_html_to_divs(self) -> None:
        """Scenario 1: Phu Quy changes website HTML (removes <table>, uses <div> cards)."""
        div_html = """
        <div class="board">
          <div class="time">Cập nhật: 23/10/2026 lúc 09:15</div>
          <div class="card-item">
            <h4>Bạc miếng Phú Quý 999 1 lượng</h4>
            <span>VNĐ/LƯỢNG</span>
            <div class="b">1.862.000</div>
            <div class="s">1.920.000</div>
          </div>
        </div>
        """
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (404, "", 20),
            pe.PQ_PARTIAL_URL: (200, div_html, 80),
            pe.PQ_BASE_URL: (200, div_html, 90),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 100),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        sel = res["phuQuy"]["selected"]
        self.assertEqual(sel["adapter"], "adapter_text_window")
        self.assertEqual(sel["target"]["buy"], 1862000)
        self.assertEqual(sel["target"]["sell"], 1920000)

    def test_scenario_02_phuquy_changes_css_classes(self) -> None:
        """Scenario 2: Phu Quy changes CSS classes (.silver-buy-price renamed)."""
        no_class_html = """
        <table>
          <tr><th>Tên</th><th>ĐV</th><th>Mua</th><th>Bán</th></tr>
          <tr>
            <td class="new-cls-1">BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG</td>
            <td class="new-cls-2">VNĐ/LƯỢNG</td>
            <td class="new-cls-3">1,865,000</td>
            <td class="new-cls-4">1,923,000</td>
          </tr>
        </table>
        """
        parsed = pe.parse_phuquy_html_multi_adapter(no_class_html)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["adapter"], "adapter_table_heuristic")
        self.assertEqual(parsed["target"]["buy"], 1865000)

    def test_scenario_03_phuquy_changes_product_name(self) -> None:
        """Scenario 3: Phu Quy changes product name ('BẠC MIẾNG 999 (01 LƯỢNG)' or omits 1L row)."""
        renamed_html = """
        <table>
          <tr><td>BẠC MIẾNG 999 (01 LƯỢNG)</td><td>VNĐ/LƯỢNG</td><td>1,861,000</td><td>1,919,000</td></tr>
        </table>
        """
        parsed1 = pe.parse_phuquy_html_multi_adapter(renamed_html)
        self.assertTrue(parsed1["ok"])
        self.assertEqual(parsed1["target"]["buy"], 1861000)

        only_1kg_html = """
        <table>
          <tr><td>BẠC THỎI PHÚ QUÝ 999 1KILO</td><td>VNĐ/KG</td><td>49,600,000</td><td>51,146,667</td></tr>
        </table>
        """
        parsed2 = pe.parse_phuquy_html_multi_adapter(only_1kg_html)
        self.assertTrue(parsed2["ok"])
        self.assertEqual(parsed2["adapter"], "adapter_kg_ratio_fallback:derived_from_1kg")
        self.assertEqual(parsed2["target"]["buy"], 1860000)
        self.assertEqual(parsed2["target"]["sell"], 1918000)

    def test_scenario_04_phuquy_moves_to_js_api_rendering(self) -> None:
        """Scenario 4: Phu Quy moves HTML to empty JS shell; fails over to structured JSON endpoint."""
        spa_shell = "<html><body><div id='app'>Loading...</div></body></html>"
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (200, spa_shell, 50),
            pe.PQ_PARTIAL_URL: (200, spa_shell, 50),
            pe.PQ_BASE_URL: (200, spa_shell, 50),
            pe.GIABAC_CHART_L_URL: (200, SAMPLE_GIABAC_CHART_JSON, 110),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 90),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertEqual(res["phuQuy"]["selected"]["sourceName"], "giabac_vn_json")
        self.assertEqual(res["phuQuy"]["freshnessStatus"], "FALLBACK_ACTIVE")

    def test_scenario_05_primary_endpoint_stops_working_failover_to_secondary_html(self) -> None:
        """Scenario 5: Primary AJAX/proxy route stops working; fails over to secondary route immediately."""
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (502, "Bad Gateway", 50),
            pe.PQ_PARTIAL_URL: (502, "Bad Gateway", 50),
            pe.PQ_BASE_URL: (200, SAMPLE_PQ_HTML_STANDARD, 85),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 90),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertEqual(res["phuQuy"]["selected"]["sourceName"], "pq_official_html")
        self.assertEqual(res["phuQuy"]["freshnessStatus"], "LIVE")

    def test_scenario_06_swissquote_temporarily_unavailable(self) -> None:
        """Scenario 6: Swissquote becomes temporarily unavailable; Yahoo Finance / Kitco takes over."""
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 50),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
            pe.SWISSQUOTE_URL: (-1, "", 0),
            pe.YAHOO_XAG_URL: (200, SAMPLE_YAHOO_JSON, 120),
            pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 180),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertEqual(res["xagUsd"]["selected"]["sourceName"], "yahoo_xag")
        self.assertEqual(res["xagUsd"]["selected"]["price"], 54.14)
        self.assertEqual(res["sourceHealth"]["sources"]["swissquote_bbo"]["failureReason"], "NETWORK_TIMEOUT")

    def test_scenario_07_kitco_changes_page_structure(self) -> None:
        """Scenario 7: Kitco changes HTML structure; __NEXT_DATA__ adapter or Swissquote/Yahoo handles it."""
        next_only_kitco = """
        <html><body><script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"quote":{"bid":54.15,"changePercentage":1.20,"timestamp":1792721700}}}}
        </script><div>No bid text here</div></body></html>
        """
        parsed = pe.parse_kitco_multi_adapter(next_only_kitco)
        self.assertEqual(parsed["adapter"], "kitco_next_data_json")
        self.assertEqual(parsed["price"], 54.15)

        broken_kitco = "<html><body><h1>Completely changed</h1></body></html>"
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 50),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 95),
            pe.KITCO_URL: (200, broken_kitco, 180),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertEqual(res["xagUsd"]["selected"]["sourceName"], "swissquote_bbo")
        self.assertEqual(res["sourceHealth"]["sources"]["kitco_silver"]["failureReason"], "PARSER_BROKEN")

    def test_scenario_08_giabac_vn_unavailable(self) -> None:
        """Scenario 8: giabac.vn becomes unavailable; authoritative Phu Quy unaffected."""
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 50),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
            pe.GIABAC_CHART_L_URL: (503, "Down", 50),
            pe.GIABAC_BASE_URL: (503, "Down", 50),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 90),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertEqual(res["phuQuy"]["selected"]["sourceName"], "pq_official_ajax")
        self.assertEqual(res["sourceHealth"]["sources"]["giabac_vn_json"]["failureReason"], "HTTP_ERROR")

    def test_scenario_09_source_returns_old_price(self) -> None:
        """Scenario 9: A source responds HTTP 200 but returns a stale/old timestamp."""
        stale_kitco = "<html><body><div>Bid 52.10 -1.20% Oct 22, 2026 - 05:00 PM</div></body></html>"
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 50),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 90),
            pe.YAHOO_XAG_URL: (200, SAMPLE_YAHOO_JSON, 110),
            pe.KITCO_URL: (200, stale_kitco, 150),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertEqual(res["xagUsd"]["selected"]["sourceName"], "swissquote_bbo")
        self.assertEqual(res["sourceHealth"]["sources"]["kitco_silver"]["failureReason"], "STALE_UPSTREAM")

    def test_scenario_10_source_returns_malformed_price(self) -> None:
        """Scenario 10: Source returns malformed JSON, zero/null price, or inverted sell < buy."""
        inverted_html = """
        <table>
          <tr>
            <td class="silver-product-name">BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG</td>
            <td class="silver-unit">VNĐ/LƯỢNG</td>
            <td class="silver-price silver-buy-price">1,950,000</td>
            <td class="silver-price silver-sell-price">1,850,000</td>
          </tr>
        </table>
        """
        zero_json = '{"chart":{"result":[{"meta":{"regularMarketPrice":0,"regularMarketTime":1792721700}}]}}'
        _, ef = make_mock_fetcher({
            pe.PQ_PARTIAL_URL: (200, inverted_html, 50),
            pe.PQ_BASE_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
            pe.SWISSQUOTE_URL: (200, "{corrupted_json", 40),
            pe.YAHOO_XAG_URL: (200, zero_json, 50),
            pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 120),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertEqual(res["phuQuy"]["selected"]["sourceName"], "pq_official_html")
        self.assertEqual(res["sourceHealth"]["sources"]["pq_official_ajax"]["failureReason"], "MALFORMED_VALUE")
        self.assertEqual(res["xagUsd"]["selected"]["sourceName"], "kitco_silver")
        self.assertEqual(res["sourceHealth"]["sources"]["swissquote_bbo"]["failureReason"], "MALFORMED_VALUE")
        self.assertEqual(res["sourceHealth"]["sources"]["yahoo_xag"]["failureReason"], "MALFORMED_VALUE")

    def test_scenario_11_two_sources_return_conflicting_prices(self) -> None:
        """Scenario 11: Sources return conflicting prices (e.g. $85.00 spike vs $54.14 normal)."""
        spiked_sq = json.dumps([{"ts": 1792721700000, "spreadProfilePrices": [{"spreadProfile": "prime", "bid": 85.00, "ask": 85.10}]}])
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 50),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
            pe.SWISSQUOTE_URL: (200, spiked_sq, 60),
            pe.YAHOO_XAG_URL: (200, SAMPLE_YAHOO_JSON, 90),
            pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 120),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertEqual(res["xagUsd"]["selected"]["sourceName"], "yahoo_xag")
        self.assertEqual(res["xagUsd"]["selected"]["price"], 54.14)
        self.assertEqual(res["sourceHealth"]["sources"]["swissquote_bbo"]["failureReason"], "SUSPICIOUS_PRICE")
        self.assertTrue(res["sourceHealth"]["disagreements"]["xagUsd"]["detected"])

    def test_scenario_12_missed_scheduled_runs_resumes_cleanly(self) -> None:
        """Scenario 12: GitHub Actions misses several runs (LKG is 3 hours old), then runs and recovers cleanly."""
        old_doc = json.loads(json.dumps(BASE_EXISTING_DOC))
        old_doc["fetchedAt"] = "2026-10-22T23:00:00Z"
        old_doc["priceAsOf"] = "2026-10-22T18:15:00+07:00"
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 50),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 90),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(old_doc)
        self.assertFalse(res["phuQuy"]["usedLastKnownGood"])
        self.assertEqual(res["phuQuy"]["freshnessStatus"], "LIVE")
        self.assertEqual(res["xagUsd"]["freshnessStatus"], "LIVE")

    def test_scenario_13_internet_connectivity_temporarily_fails(self) -> None:
        """Scenario 13: All network requests time out (-1); preserves LKG and records NETWORK_TIMEOUT."""
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (-1, "", 0),
            pe.PQ_PARTIAL_URL: (-1, "", 0),
            pe.PQ_BASE_URL: (-1, "", 0),
            pe.GIABAC_CHART_L_URL: (-1, "", 0),
            pe.GIABAC_BASE_URL: (-1, "", 0),
            pe.SWISSQUOTE_URL: (-1, "", 0),
            pe.YAHOO_XAG_URL: (-1, "", 0),
            pe.KITCO_URL: (-1, "", 0),
            pe.PQ_WORLD_URL: (-1, "", 0),
            pe.GIABAC_WORLD_URL: (-1, "", 0),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertTrue(res["phuQuy"]["usedLastKnownGood"])
        self.assertTrue(res["xagUsd"]["usedLastKnownGood"])
        self.assertEqual(res["sourceHealth"]["sources"]["pq_official_ajax"]["failureReason"], "NETWORK_TIMEOUT")
        self.assertEqual(res["sourceHealth"]["sources"]["swissquote_bbo"]["failureReason"], "NETWORK_TIMEOUT")

    def test_scenario_14_market_closes_for_weekend(self) -> None:
        """Scenario 14: Market closes for the weekend (Saturday evening / Sunday); status is MARKET_CLOSED."""
        sunday_dt = datetime(2026, 10, 25, 14, 0, 0, tzinfo=UTC_TZ)
        sq_friday = json.dumps([{"ts": 1792789140000, "spreadProfilePrices": [{"spreadProfile": "prime", "bid": 53.96, "ask": 54.02}]}])
        pq_sat_dt = json.dumps({"success": True, "lastTime": "08:30", "lastDate": "24/10/2026"})
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (200, pq_sat_dt, 50),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
            pe.SWISSQUOTE_URL: (200, sq_friday, 90),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: sunday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertEqual(res["xagUsd"]["freshnessStatus"], "MARKET_CLOSED")
        self.assertEqual(res["xagUsd"]["marketSchedule"]["marketStatus"], "MARKET_CLOSED_WEEKEND")
        self.assertEqual(res["phuQuy"]["freshnessStatus"], "MARKET_CLOSED")
        self.assertEqual(res["phuQuy"]["marketSchedule"]["marketStatus"], "MARKET_CLOSED_WEEKEND")

    def test_scenario_15_last_session_price_unchanged_for_many_hours(self) -> None:
        """Scenario 15: Last Friday/Saturday price remains unchanged for 42+ hours; never marked STALE."""
        monday_early_vn = datetime(2026, 10, 25, 21, 30, 0, tzinfo=UTC_TZ)
        weekend_doc = json.loads(json.dumps(BASE_EXISTING_DOC))
        weekend_doc["priceAsOf"] = "2026-10-24T08:30:00+07:00"
        weekend_doc["world"]["asOf"] = "2026-10-23T17:00:00-04:00"
        _, ef = make_mock_fetcher({})
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: monday_early_vn)
        res = engine.run_cycle(weekend_doc)
        self.assertEqual(res["xagUsd"]["freshnessStatus"], "MARKET_CLOSED")
        self.assertEqual(res["phuQuy"]["freshnessStatus"], "MARKET_CLOSED")
        self.assertEqual(res["phuQuy"]["selected"]["target"]["buy"], 1855000)

    def test_scenario_16_market_reopens_after_weekend(self) -> None:
        """Scenario 16: Market reopens after the weekend; transitions from MARKET_CLOSED to LIVE/REOPEN_GRACE_PERIOD."""
        monday_815_vn = datetime(2026, 10, 26, 1, 15, 0, tzinfo=UTC_TZ)
        pq_sched = pe.get_phuquy_market_schedule(monday_815_vn)
        xag_sched = pe.get_xag_market_schedule(monday_815_vn)
        self.assertTrue(pq_sched["isOpen"])
        self.assertEqual(pq_sched["marketStatus"], "REOPEN_GRACE_PERIOD")
        self.assertTrue(xag_sched["isOpen"])
        self.assertEqual(xag_sched["marketStatus"], "MARKET_OPEN")

    def test_scenario_17_one_source_resumes_before_others_after_reopen(self) -> None:
        """Scenario 17: One source resumes with Monday prices while others still serve Friday's cached close."""
        monday_open_dt = datetime(2026, 10, 26, 1, 30, 0, tzinfo=UTC_TZ)
        monday_sq = json.dumps([{"ts": int(monday_open_dt.timestamp() * 1000) - 15000, "spreadProfilePrices": [{"spreadProfile": "prime", "bid": 54.45, "ask": 54.52}]}])
        monday_pq_dt = json.dumps({"success": True, "lastTime": "08:28", "lastDate": "26/10/2026"})
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (200, monday_pq_dt, 50),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
            pe.SWISSQUOTE_URL: (200, monday_sq, 70),
            pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 120),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: monday_open_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertEqual(res["xagUsd"]["selected"]["sourceName"], "swissquote_bbo")
        self.assertEqual(res["xagUsd"]["selected"]["price"], 54.45)
        self.assertEqual(res["sourceHealth"]["sources"]["kitco_silver"]["failureReason"], "STALE_UPSTREAM")

    def test_scenario_18_all_live_sources_fail_temporarily_during_open_market(self) -> None:
        """Scenario 18: All live sources return HTTP 500 during open market hours; preserves LKG and marks SOURCE_FAILED."""
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (500, "", 20),
            pe.PQ_PARTIAL_URL: (500, "", 20),
            pe.PQ_BASE_URL: (500, "", 20),
            pe.GIABAC_CHART_L_URL: (500, "", 20),
            pe.GIABAC_BASE_URL: (500, "", 20),
            pe.SWISSQUOTE_URL: (500, "", 20),
            pe.YAHOO_XAG_URL: (500, "", 20),
            pe.KITCO_URL: (500, "", 20),
            pe.PQ_WORLD_URL: (500, "", 20),
            pe.GIABAC_WORLD_URL: (500, "", 20),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertTrue(res["phuQuy"]["usedLastKnownGood"])
        self.assertEqual(res["phuQuy"]["freshnessStatus"], "SOURCE_FAILED")
        self.assertEqual(res["phuQuy"]["selected"]["target"]["buy"], 1855000)
        self.assertTrue(res["xagUsd"]["usedLastKnownGood"])
        self.assertEqual(res["xagUsd"]["freshnessStatus"], "SOURCE_FAILED")
        self.assertEqual(res["xagUsd"]["selected"]["price"], 54.00)

    def test_timestamp_missing_or_format_changed_does_not_discard_valid_price(self) -> None:
        """Decoupled Timestamp Rule: If Phu Quy changes timestamp format completely, valid buy/sell prices are still accepted."""
        no_time_html = """
        <table>
          <tr>
            <td class="silver-product-name">BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG</td>
            <td class="silver-unit">VNĐ/LƯỢNG</td>
            <td class="silver-price silver-buy-price">1,870,000</td>
            <td class="silver-price silver-sell-price">1,928,000</td>
          </tr>
        </table>
        """
        _, ef = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (404, "", 20),
            pe.PQ_PARTIAL_URL: (200, no_time_html, 60),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 70),
        })
        engine = pe.PriceEngine(fetcher=ef, now_fn=lambda: self.open_friday_dt)
        res = engine.run_cycle(BASE_EXISTING_DOC)
        self.assertFalse(res["phuQuy"]["usedLastKnownGood"])
        self.assertEqual(res["phuQuy"]["selected"]["target"]["buy"], 1870000)

    def test_automatic_recovery_after_consecutive_failures(self) -> None:
        """Verifies consecutiveFailures increments on failure and resets to 0 on recovery."""
        _, ef_fail = make_mock_fetcher({pe.SWISSQUOTE_URL: (500, "Error", 50)})
        engine_fail = pe.PriceEngine(fetcher=ef_fail, now_fn=lambda: self.open_friday_dt)
        r1 = engine_fail.run_cycle(BASE_EXISTING_DOC)
        self.assertEqual(r1["sourceHealth"]["sources"]["swissquote_bbo"]["consecutiveFailures"], 1)

        doc2 = dict(BASE_EXISTING_DOC, sourceHealth=r1["sourceHealth"])
        r2 = engine_fail.run_cycle(doc2)
        self.assertEqual(r2["sourceHealth"]["sources"]["swissquote_bbo"]["consecutiveFailures"], 2)

        _, ef_ok = make_mock_fetcher({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 40),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 60),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 70),
        })
        doc3 = dict(BASE_EXISTING_DOC, sourceHealth=r2["sourceHealth"])
        engine_ok = pe.PriceEngine(fetcher=ef_ok, now_fn=lambda: self.open_friday_dt)
        r3 = engine_ok.run_cycle(doc3)
        sq_h = r3["sourceHealth"]["sources"]["swissquote_bbo"]
        self.assertEqual(sq_h["consecutiveFailures"], 0)
        self.assertTrue(sq_h["currentlySelected"])
        self.assertEqual(sq_h["failureReason"], "NONE")


# ============================================================================
# PHASE E & F: SIDE-BY-SIDE LEGACY VS PRICE_ENGINE FAILURE SIMULATIONS
# ============================================================================

def run_phase_e_simulations() -> List[Dict[str, Any]]:
    """Runs all 10 required Phase E failure simulations comparing Legacy vs PriceEngine."""
    friday_dt = datetime(2026, 10, 23, 2, 16, 0, tzinfo=UTC_TZ)
    results = []

    # 1. Swissquote unavailable
    lf1, ef1 = make_mock_fetcher({
        pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 40),
        pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 60),
        pe.PQ_BASE_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
        pe.SWISSQUOTE_URL: (-1, "", 0),
        pe.YAHOO_XAG_URL: (200, SAMPLE_YAHOO_JSON, 110),
        pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 180),
    })
    c1 = up.run_shadow_comparison(BASE_EXISTING_DOC, legacy_fetch_fn=lf1, engine_fetcher=ef1, now_fn=lambda: friday_dt, write_tmp=False)
    results.append({"case": "1. Swissquote unavailable", "comp": c1})

    # 2. Yahoo unavailable
    lf2, ef2 = make_mock_fetcher({
        pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 40),
        pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 60),
        pe.PQ_BASE_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
        pe.SWISSQUOTE_URL: (-1, "", 0),
        pe.YAHOO_XAG_URL: (503, "Service Unavailable", 50),
        pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 180),
    })
    c2 = up.run_shadow_comparison(BASE_EXISTING_DOC, legacy_fetch_fn=lf2, engine_fetcher=ef2, now_fn=lambda: friday_dt, write_tmp=False)
    results.append({"case": "2. Yahoo (& Swissquote) unavailable", "comp": c2})

    # 3. Kitco returns wrong ticker / wrong price (e.g. 60.50 (+12%) when PQ ref string is absent, or 94.95)
    pq_no_ref_html = SAMPLE_PQ_HTML_STANDARD.replace("54.10 USD/OUNCE ≈ 45,250,000 VNĐ/KG", "")
    bad_kitco_html = "<html><body><div>Bid 60.50 +12.0% Oct 22, 2026 - 10:15 PM</div></body></html>"
    lf3, ef3 = make_mock_fetcher({
        pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 40),
        pe.PQ_PARTIAL_URL: (200, pq_no_ref_html, 60),
        pe.PQ_BASE_URL: (200, pq_no_ref_html, 80),
        pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 80),
        pe.YAHOO_XAG_URL: (200, SAMPLE_YAHOO_JSON, 95),
        pe.KITCO_URL: (200, bad_kitco_html, 150),
    })
    c3 = up.run_shadow_comparison(BASE_EXISTING_DOC, legacy_fetch_fn=lf3, engine_fetcher=ef3, now_fn=lambda: friday_dt, write_tmp=False)
    results.append({"case": "3. Kitco returns wrong price (60.50 vs 54.12)", "comp": c3})

    # 4. Phu Quy AJAX endpoint unavailable
    lf4, ef4 = make_mock_fetcher({
        pe.PQ_DATETIME_URL: (500, "Error", 30),
        pe.PQ_PARTIAL_URL: (500, "Error", 30),
        pe.PQ_BASE_URL: (200, SAMPLE_PQ_HTML_STANDARD, 85),
        pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 80),
        pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 150),
    })
    c4 = up.run_shadow_comparison(BASE_EXISTING_DOC, legacy_fetch_fn=lf4, engine_fetcher=ef4, now_fn=lambda: friday_dt, write_tmp=False)
    results.append({"case": "4. Phu Quy AJAX endpoint unavailable", "comp": c4})

    # 5. Phu Quy HTML structure / product name / timestamp format changed
    mutated_pq_html = """
    <div class="new-grid">
      <span id="last-update-time">09:15</span><span id="last-update-date">23-10-2026</span>
      <table>
        <tr><td>BẠC MIẾNG PHÚ QUÝ 999 (01 LƯỢNG)</td><td>VNĐ/LƯỢNG</td><td>1,864,000</td><td>1,922,000</td></tr>
      </table>
    </div>
    """
    lf5, ef5 = make_mock_fetcher({
        pe.PQ_DATETIME_URL: (404, "", 20),
        pe.PQ_PARTIAL_URL: (200, mutated_pq_html, 60),
        pe.PQ_BASE_URL: (200, mutated_pq_html, 80),
        pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 80),
        pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 150),
    })
    c5 = up.run_shadow_comparison(BASE_EXISTING_DOC, legacy_fetch_fn=lf5, engine_fetcher=ef5, now_fn=lambda: friday_dt, write_tmp=False)
    results.append({"case": "5. Phu Quy HTML/product name/date format changed", "comp": c5})

    # 6. giabac.vn unavailable
    lf6, ef6 = make_mock_fetcher({
        pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 40),
        pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 60),
        pe.PQ_BASE_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
        pe.GIABAC_CHART_L_URL: (503, "Down", 40),
        pe.GIABAC_BASE_URL: (503, "Down", 40),
        pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 80),
        pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 150),
    })
    c6 = up.run_shadow_comparison(BASE_EXISTING_DOC, legacy_fetch_fn=lf6, engine_fetcher=ef6, now_fn=lambda: friday_dt, write_tmp=False)
    results.append({"case": "6. giabac.vn unavailable", "comp": c6})

    # 7. Malformed / corrupted spread price (e.g. sell=9,500,000 > buy=1,860,000 due to typo on main page while giabac.vn is normal)
    corrupt_spread_html = """
    <div>09:15 23/10/2026</div>
    <table>
      <tr><td>BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG</td><td>VNĐ/LƯỢNG</td><td>1,860,000</td><td>9,500,000</td></tr>
    </table>
    """
    lf7, ef7 = make_mock_fetcher({
        pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 40),
        pe.PQ_PARTIAL_URL: (200, corrupt_spread_html, 60),
        pe.PQ_BASE_URL: (200, corrupt_spread_html, 80),
        pe.GIABAC_CHART_L_URL: (200, SAMPLE_GIABAC_CHART_JSON, 90),
        pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 80),
        pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 150),
    })
    c7 = up.run_shadow_comparison(BASE_EXISTING_DOC, legacy_fetch_fn=lf7, engine_fetcher=ef7, now_fn=lambda: friday_dt, write_tmp=False)
    results.append({"case": "7. Malformed/corrupted sell price (9,500,000 spread typo)", "comp": c7})

    # 8. Conflicting sources (Phu Quy page has unconfirmed 35% spike to 2,500,000 / 2,580,000 while giabac.vn is at 1,860,000)
    spiked_pq_html = """
    <div>09:15 23/10/2026</div>
    <table>
      <tr><td>BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG</td><td>VNĐ/LƯỢNG</td><td>2,500,000</td><td>2,580,000</td></tr>
    </table>
    """
    lf8, ef8 = make_mock_fetcher({
        pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 40),
        pe.PQ_PARTIAL_URL: (500, "", 20),
        pe.PQ_BASE_URL: (200, spiked_pq_html, 80),
        pe.GIABAC_CHART_L_URL: (200, SAMPLE_GIABAC_CHART_JSON, 90),
        pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 80),
        pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 150),
    })
    c8 = up.run_shadow_comparison(BASE_EXISTING_DOC, legacy_fetch_fn=lf8, engine_fetcher=ef8, now_fn=lambda: friday_dt, write_tmp=False)
    results.append({"case": "8. Conflicting sources (2,500,000 spike vs 1,860,000 & LKG 1,855,000)", "comp": c8})

    # 9. All XAG live sources unavailable
    lf9, ef9 = make_mock_fetcher({
        pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 40),
        pe.PQ_PARTIAL_URL: (200, pq_no_ref_html, 60),
        pe.PQ_BASE_URL: (200, pq_no_ref_html, 80),
        pe.SWISSQUOTE_URL: (-1, "", 0),
        pe.YAHOO_XAG_URL: (503, "", 30),
        pe.KITCO_URL: (503, "", 30),
        pe.PQ_WORLD_URL: (503, "", 30),
        pe.GIABAC_WORLD_URL: (503, "", 30),
    })
    c9 = up.run_shadow_comparison(BASE_EXISTING_DOC, legacy_fetch_fn=lf9, engine_fetcher=ef9, now_fn=lambda: friday_dt, write_tmp=False)
    results.append({"case": "9. All XAG live sources unavailable", "comp": c9})

    # 10. All Phu Quy live sources unavailable
    lf10, ef10 = make_mock_fetcher({
        pe.PQ_DATETIME_URL: (503, "", 30),
        pe.PQ_PARTIAL_URL: (503, "", 30),
        pe.PQ_BASE_URL: (503, "", 30),
        pe.GIABAC_CHART_L_URL: (503, "", 30),
        pe.GIABAC_BASE_URL: (503, "", 30),
        pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 80),
        pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 150),
    })
    c10 = up.run_shadow_comparison(BASE_EXISTING_DOC, legacy_fetch_fn=lf10, engine_fetcher=ef10, now_fn=lambda: friday_dt, write_tmp=False)
    results.append({"case": "10. All Phu Quy live sources unavailable", "comp": c10})

    return results


class TestShadowComparisonAndSimulations(unittest.TestCase):
    """Tests Phase E failure simulations and Phase F backward compatibility."""

    def test_all_10_phase_e_simulations_and_backward_compatibility(self) -> None:
        sims = run_phase_e_simulations()
        self.assertEqual(len(sims), 10)
        for item in sims:
            comp = item["comp"]
            self.assertTrue(comp["backwardCompatibility"]["allPassed"], f"Failed compat in {item['case']}")

        # Case 3: Legacy accepts wrong 60.50 price when PQ ref is absent; Engine rejects 60.50 and selects 54.12
        c3 = sims[2]["comp"]
        self.assertEqual(c3["xagUsd"]["legacy"]["usdPerOz"], 60.50)
        self.assertEqual(c3["xagUsd"]["engine"]["price"], 54.12)

        # Case 5: Legacy fails completely on renamed product/date format; Engine succeeds with 1,864,000
        c5 = sims[4]["comp"]
        self.assertFalse(c5["phuQuy"]["legacy"]["freshFetchSucceeded"])
        self.assertEqual(c5["phuQuy"]["engine"]["buy"], 1864000)

        # Case 7: Legacy accepts corrupted 9,500,000 sell price (because 9,500,000 >= 1,860,000); Engine rejects abnormal 410% spread and falls back to giabac.vn (1,918,000)
        c7 = sims[6]["comp"]
        self.assertEqual(c7["phuQuy"]["legacy"]["sell"], 9500000)
        self.assertEqual(c7["phuQuy"]["engine"]["sell"], 1918000)

        # Case 8: Legacy blindly accepts unconfirmed 2,500,000 spike; Engine rejects unconfirmed jump and uses corroborated giabac_vn_json (1,860,000)
        c8 = sims[7]["comp"]
        self.assertEqual(c8["phuQuy"]["legacy"]["buy"], 2500000)
        self.assertEqual(c8["phuQuy"]["engine"]["buy"], 1860000)

        # Case 9 & 10: LKG preserved when all sources fail
        c9 = sims[8]["comp"]
        self.assertTrue(c9["xagUsd"]["engine"]["usedLastKnownGood"])
        self.assertEqual(c9["xagUsd"]["engine"]["price"], 54.00)
        c10 = sims[9]["comp"]
        self.assertTrue(c10["phuQuy"]["engine"]["usedLastKnownGood"])
        self.assertEqual(c10["phuQuy"]["engine"]["buy"], 1855000)


class TestAuthoritativeIntegration(unittest.TestCase):
    """Step 9: Authoritative end-to-end integration tests for update_prices.run_authoritative_update."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.data_file = self.tmp_path / "market.json"
        self.pq_csv = self.tmp_path / "phuquy_silver.csv"
        self.world_csv = self.tmp_path / "world_xag.csv"
        self.data_file.write_text(json.dumps(BASE_EXISTING_DOC, ensure_ascii=False, indent=1) + "\n", "utf-8")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _make_engine(self, mapping, now_dt=None):
        if now_dt is None:
            now_dt = datetime(2026, 10, 23, 2, 16, 0, tzinfo=pe.UTC_TZ)

        def _fetcher(url: str):
            for k, val in mapping.items():
                if url == k or url.startswith(k):
                    return val
            return 503, "Service Unavailable", 10

        return pe.PriceEngine(fetcher=_fetcher, now_fn=lambda: now_dt)

    def test_01_authoritative_phuquy_update(self) -> None:
        """1. Authoritative Phu Quy update writes target, items, priceAsOf, history, and PQ CSV."""
        eng = self._make_engine({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 30),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 50),
            pe.PQ_WORLD_URL: (200, SAMPLE_PQ_WORLD_JSON, 40),
            pe.PQ_BASE_URL: (200, SAMPLE_PQ_HTML_STANDARD, 80),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 60),
        })
        rc = up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng)
        self.assertEqual(rc, 0)
        doc = json.loads(self.data_file.read_text("utf-8"))
        self.assertEqual(doc["target"]["buy"], 1860000)
        self.assertEqual(doc["target"]["sell"], 1918000)
        self.assertEqual(doc["priceAsOf"], "2026-10-23T09:15:00+07:00")
        self.assertEqual(doc["source"], "phuquy")
        self.assertEqual(doc["history"][-1], {"at": "2026-10-23T09:15:00+07:00", "buy": 1860000, "sell": 1918000})
        self.assertTrue(self.pq_csv.exists())
        csv_text = self.pq_csv.read_text("utf-8")
        self.assertIn("2026-10-23T09:15:00+07:00", csv_text)
        self.assertIn("1860000", csv_text)

    def test_02_authoritative_xag_update(self) -> None:
        """2. Authoritative XAG/USD update writes world, worldHistory, and World CSV."""
        eng = self._make_engine({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 30),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 50),
            pe.PQ_WORLD_URL: (200, SAMPLE_PQ_WORLD_JSON, 40),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 60),
            pe.YAHOO_XAG_URL: (200, SAMPLE_YAHOO_JSON, 80),
        })
        rc = up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng)
        self.assertEqual(rc, 0)
        doc = json.loads(self.data_file.read_text("utf-8"))
        self.assertEqual(doc["world"]["usdPerOz"], 54.12)
        self.assertEqual(doc["world"]["source"], "swissquote_bbo")
        self.assertEqual(doc["worldHistory"][-1]["usd"], 54.12)
        self.assertTrue(self.world_csv.exists())
        w_csv = self.world_csv.read_text("utf-8")
        self.assertIn("54.12", w_csv)
        self.assertIn("swissquote_bbo", w_csv)

    def test_03_lkg_preservation(self) -> None:
        """3. LKG preservation when all live sources fail: keeps valid target & world, marks SOURCE_FAILED."""
        eng = self._make_engine({})  # All return 503
        rc = up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng)
        self.assertEqual(rc, 1)  # Open-market total failure returns non-zero exit code
        doc = json.loads(self.data_file.read_text("utf-8"))
        self.assertEqual(doc["target"]["buy"], 1855000)
        self.assertEqual(doc["target"]["sell"], 1912000)
        self.assertEqual(doc["world"]["usdPerOz"], 54.00)
        self.assertEqual(doc["freshnessStatus"], "SOURCE_FAILED")
        self.assertTrue(doc["engineMetadata"]["phuQuy"]["usedLastKnownGood"])
        self.assertTrue(doc["engineMetadata"]["xagUsd"]["usedLastKnownGood"])
        self.assertEqual(len(doc["errors"]), 2)

    def test_04_history_deduplication(self) -> None:
        """4. History deduplication: repeated reads of the same board/tick do not append duplicate rows."""
        eng = self._make_engine({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 30),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 50),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 60),
        })
        up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng)
        doc1 = json.loads(self.data_file.read_text("utf-8"))
        h_len_1 = len(doc1["history"])
        wh_len_1 = len(doc1["worldHistory"])
        pq_lines_1 = self.pq_csv.read_text("utf-8").splitlines()
        w_lines_1 = self.world_csv.read_text("utf-8").splitlines()

        # Run second time with identical board & tick
        up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng)
        doc2 = json.loads(self.data_file.read_text("utf-8"))
        self.assertEqual(len(doc2["history"]), h_len_1)
        self.assertEqual(len(doc2["worldHistory"]), wh_len_1)
        self.assertEqual(self.pq_csv.read_text("utf-8").splitlines(), pq_lines_1)
        self.assertEqual(self.world_csv.read_text("utf-8").splitlines(), w_lines_1)

    def test_05_csv_no_write_on_lkg(self) -> None:
        """5. CSV no-write on LKG: when usedLastKnownGood == True, neither history nor CSV files are appended."""
        eng = self._make_engine({})  # All fail -> LKG active
        up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng)
        doc = json.loads(self.data_file.read_text("utf-8"))
        self.assertEqual(len(doc["history"]), 1)
        self.assertEqual(len(doc["worldHistory"]), 1)
        self.assertFalse(self.pq_csv.exists())
        self.assertFalse(self.world_csv.exists())

    def test_06_malformed_source_rejection(self) -> None:
        """6. Malformed source rejection: inverted/corrupt primary rejected, clean secondary accepted."""
        corrupt_html = """
        <div>09:15 23/10/2026</div>
        <table><tr><td>BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG</td><td>VNĐ/LƯỢNG</td><td>1,860,000</td><td>9,500,000</td></tr></table>
        """
        eng = self._make_engine({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 30),
            pe.PQ_PARTIAL_URL: (200, corrupt_html, 40),
            pe.PQ_BASE_URL: (200, SAMPLE_PQ_HTML_STANDARD, 60),
            pe.SWISSQUOTE_URL: (200, '[{"broken": true}]', 40),
            pe.YAHOO_XAG_URL: (200, SAMPLE_YAHOO_JSON, 70),
        })
        rc = up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng)
        self.assertEqual(rc, 0)
        doc = json.loads(self.data_file.read_text("utf-8"))
        self.assertEqual(doc["target"]["sell"], 1918000)
        self.assertEqual(doc["engineMetadata"]["phuQuy"]["sourceName"], "pq_official_html")
        self.assertEqual(doc["world"]["usdPerOz"], 54.14)
        self.assertEqual(doc["engineMetadata"]["xagUsd"]["sourceName"], "yahoo_xag")

    def test_07_fallback_source_promotion(self) -> None:
        """7. Fallback source promotion: when official Phu Quy fails, giabac.vn fallback is promoted with FALLBACK_ACTIVE."""
        eng = self._make_engine({
            pe.PQ_DATETIME_URL: (503, "", 20),
            pe.PQ_PARTIAL_URL: (503, "", 20),
            pe.PQ_BASE_URL: (503, "", 20),
            pe.GIABAC_CHART_L_URL: (200, SAMPLE_GIABAC_CHART_JSON, 60),
            pe.SWISSQUOTE_URL: (503, "", 20),
            pe.YAHOO_XAG_URL: (503, "", 20),
            pe.KITCO_URL: (200, SAMPLE_KITCO_HTML, 90),
        })
        rc = up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng)
        self.assertEqual(rc, 0)
        doc = json.loads(self.data_file.read_text("utf-8"))
        self.assertEqual(doc["target"]["buy"], 1860000)
        self.assertEqual(doc["source"], "giabac_vn_json")
        self.assertEqual(doc["freshnessStatus"], "FALLBACK_ACTIVE")
        self.assertEqual(doc["engineMetadata"]["xagUsd"]["sourceName"], "kitco_silver")
        self.assertEqual(doc["engineMetadata"]["xagUsd"]["freshnessStatus"], "FALLBACK_ACTIVE")

    def test_08_primary_source_recovery(self) -> None:
        """8. Primary source recovery: transitions LIVE -> FALLBACK_ACTIVE -> LIVE and resets consecutiveFailures."""
        eng_fail = self._make_engine({
            pe.PQ_DATETIME_URL: (503, "", 20),
            pe.PQ_PARTIAL_URL: (503, "", 20),
            pe.PQ_BASE_URL: (503, "", 20),
            pe.GIABAC_CHART_L_URL: (200, SAMPLE_GIABAC_CHART_JSON, 60),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 50),
        })
        up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng_fail)
        doc_fb = json.loads(self.data_file.read_text("utf-8"))
        self.assertEqual(doc_fb["freshnessStatus"], "FALLBACK_ACTIVE")
        self.assertEqual(doc_fb["sourceHealth"]["sources"]["pq_official_ajax"]["consecutiveFailures"], 1)

        eng_recover = self._make_engine({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 30),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 50),
            pe.GIABAC_CHART_L_URL: (200, SAMPLE_GIABAC_CHART_JSON, 60),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 50),
        })
        up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng_recover)
        doc_rec = json.loads(self.data_file.read_text("utf-8"))
        self.assertEqual(doc_rec["freshnessStatus"], "LIVE")
        self.assertEqual(doc_rec["source"], "phuquy")
        self.assertEqual(doc_rec["engineMetadata"]["phuQuy"]["sourceName"], "pq_official_ajax")
        self.assertEqual(doc_rec["sourceHealth"]["sources"]["pq_official_ajax"]["consecutiveFailures"], 0)

    def test_09_weekend_market_closed_behavior(self) -> None:
        """9. Weekend MARKET_CLOSED behavior: reports MARKET_CLOSED_WEEKEND / MARKET_CLOSED without false stale error."""
        sunday_dt = datetime(2026, 10, 25, 12, 0, 0, tzinfo=pe.UTC_TZ)
        eng = self._make_engine({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 30),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 50),
            pe.YAHOO_XAG_URL: (200, SAMPLE_YAHOO_JSON, 70),
        }, now_dt=sunday_dt)
        rc = up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng)
        self.assertEqual(rc, 0)
        doc = json.loads(self.data_file.read_text("utf-8"))
        self.assertEqual(doc["marketStatus"], "MARKET_CLOSED_WEEKEND")
        self.assertEqual(doc["freshnessStatus"], "MARKET_CLOSED")
        self.assertEqual(doc["engineMetadata"]["xagUsd"]["marketStatus"], "MARKET_CLOSED_WEEKEND")
        self.assertEqual(doc["engineMetadata"]["xagUsd"]["freshnessStatus"], "MARKET_CLOSED")
        self.assertEqual(doc["errors"], [])

    def test_10_open_market_live_fresh_behavior(self) -> None:
        """10. Open-market LIVE/FRESH behavior during active weekday trading."""
        monday_dt = datetime(2026, 10, 26, 2, 16, 0, tzinfo=pe.UTC_TZ)  # Monday 09:16 VN
        monday_dt_json = json.dumps({"success": True, "lastTime": "09:15", "lastDate": "26/10/2026"})
        monday_yahoo = json.dumps({
            "chart": {"result": [{"meta": {"regularMarketPrice": 54.25, "chartPreviousClose": 53.95, "regularMarketTime": int(monday_dt.timestamp()) - 300}}]}
        })
        eng = self._make_engine({
            pe.PQ_DATETIME_URL: (200, monday_dt_json, 30),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 50),
            pe.YAHOO_XAG_URL: (200, monday_yahoo, 70),
        }, now_dt=monday_dt)
        rc = up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng)
        self.assertEqual(rc, 0)
        doc = json.loads(self.data_file.read_text("utf-8"))
        self.assertEqual(doc["marketStatus"], "MARKET_OPEN")
        self.assertEqual(doc["freshnessStatus"], "LIVE")
        self.assertEqual(doc["engineMetadata"]["xagUsd"]["marketStatus"], "MARKET_OPEN")
        self.assertEqual(doc["engineMetadata"]["xagUsd"]["freshnessStatus"], "FRESH")

    def test_11_backward_compatible_output_schema(self) -> None:
        """11. Backward-compatible output schema: verifies all legacy + additive keys exist with exact types."""
        eng = self._make_engine({
            pe.PQ_DATETIME_URL: (200, SAMPLE_PQ_DATETIME_JSON, 30),
            pe.PQ_PARTIAL_URL: (200, SAMPLE_PQ_HTML_STANDARD, 50),
            pe.SWISSQUOTE_URL: (200, SAMPLE_SWISSQUOTE_JSON, 50),
        })
        up.run_authoritative_update(self.data_file, self.pq_csv, self.world_csv, engine=eng)
        doc = json.loads(self.data_file.read_text("utf-8"))
        for key in ("target", "items", "priceAsOf", "itemsAsOf", "source", "world", "history", "worldHistory", "fetchedAt", "errors", "sourceHealth", "marketStatus", "freshnessStatus", "engineMetadata"):
            self.assertIn(key, doc, f"Missing required key: {key}")
        self.assertIsInstance(doc["target"]["buy"], int)
        self.assertIsInstance(doc["target"]["sell"], int)
        self.assertIsInstance(doc["world"]["usdPerOz"], float)
        self.assertIsInstance(doc["world"]["vndPerKg"], int)


if __name__ == "__main__":
    if "--report-simulations" in sys.argv:
        sims = run_phase_e_simulations()
        summary = []
        for s in sims:
            c = s["comp"]
            summary.append({
                "case": s["case"],
                "pq_legacy": {"ok": c["phuQuy"]["legacy"]["freshFetchSucceeded"], "buy": c["phuQuy"]["legacy"]["buy"], "sell": c["phuQuy"]["legacy"]["sell"]},
                "pq_engine": {"source": c["phuQuy"]["engine"]["sourceName"], "buy": c["phuQuy"]["engine"]["buy"], "sell": c["phuQuy"]["engine"]["sell"], "status": c["phuQuy"]["engine"]["freshnessStatus"], "lkg": c["phuQuy"]["engine"]["usedLastKnownGood"]},
                "xag_legacy": {"ok": c["xagUsd"]["legacy"]["freshFetchSucceeded"], "usd": c["xagUsd"]["legacy"]["usdPerOz"]},
                "xag_engine": {"source": c["xagUsd"]["engine"]["sourceName"], "usd": c["xagUsd"]["engine"]["price"], "status": c["xagUsd"]["engine"]["freshnessStatus"], "lkg": c["xagUsd"]["engine"]["usedLastKnownGood"]},
                "compatAllPassed": c["backwardCompatibility"]["allPassed"],
            })
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        sys.exit(0)
    unittest.main(verbosity=2)
