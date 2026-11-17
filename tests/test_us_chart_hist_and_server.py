"""Tests for US chart_hist 7-timeframe SQLite store and aiohttp port-8081 HTTP API."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from chart_hist.config import DEFAULT_PORT, ORDERED_TIMEFRAMES, normalize_chart_hist_timeframe
from chart_hist.database import get_candles, init_db, save_candles
from chart_hist.fetchers import get_fetcher
from chart_hist.server import create_app
from nexus_quant_core.contracts import CandleBar
from us_core.time_utils import et_to_utc_ts


class TestUsChartHistAndServer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_us_candles.db"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_default_port_is_8081_and_7_timeframe_whitelist(self) -> None:
        self.assertEqual(DEFAULT_PORT, 8081)
        self.assertEqual(ORDERED_TIMEFRAMES, ["15m", "1h", "4h", "1d", "1wk", "1mo", "3mo"])
        self.assertEqual(normalize_chart_hist_timeframe("1M"), "1mo")
        self.assertEqual(normalize_chart_hist_timeframe("3M"), "3mo")

        for forbidden_tf in ("1m", "m", "5m", "30m", "2h"):
            with self.subTest(tf=forbidden_tf):
                with self.assertRaises(ValueError):
                    normalize_chart_hist_timeframe(forbidden_tf)

    def test_dual_axis_sqlite_storage_across_all_7_timeframes(self) -> None:
        async def _seed_and_check() -> None:
            fetcher = get_fetcher("mock")
            for tf in ORDERED_TIMEFRAMES:
                bars = await fetcher.fetch("NVDA", tf, days=70)
                self.assertGreater(len(bars), 0, tf)
                saved = save_candles(bars, db_path=self.db_path)
                self.assertEqual(saved, len(bars))
                loaded = get_candles("NVDA", tf, db_path=self.db_path)
                self.assertEqual(len(loaded), len(bars))
                for row in loaded:
                    self.assertEqual(len(row.trading_date), 10)
                    self.assertGreater(row.timestamp, 0)

        asyncio.run(_seed_and_check())

        # Verify CME ES=F Sunday 18:00 ET bar auto-populates Monday trading_date
        sun_1800 = int(et_to_utc_ts(2026, 11, 15, 18, 0, 0))
        save_candles(
            [
                {
                    "symbol": "ES=F",
                    "timeframe": "15m",
                    "timestamp": sun_1800,
                    "open": 5980.0,
                    "high": 5985.0,
                    "low": 5978.0,
                    "close": 5983.0,
                    "volume": 10000.0,
                }
            ],
            db_path=self.db_path,
        )
        es_rows = get_candles("ES=F", "15m", db_path=self.db_path)
        self.assertEqual(len(es_rows), 1)
        self.assertEqual(es_rows[0].trading_date, "2026-11-16")

        # Verify rejecting 1m candle in save_candles
        with self.assertRaises(ValueError):
            save_candles(
                [
                    CandleBar(
                        symbol="NVDA",
                        timeframe="1m",
                        timestamp=sun_1800,
                        trading_date="2026-11-16",
                        open=140.0,
                        high=141.0,
                        low=139.0,
                        close=140.5,
                        volume=1000.0,
                    )
                ],
                db_path=self.db_path,
            )

    def test_http_api_routes_1M_to_1mo_and_1m_returns_400(self) -> None:
        async def _exercise_api() -> None:
            app = create_app(db_path=self.db_path)
            async with TestClient(TestServer(app)) as client:
                # 1. Index HTML
                r_idx = await client.get("/")
                self.assertEqual(r_idx.status, 200)
                html = await r_idx.text()
                self.assertIn("NVDA", html)
                self.assertIn("ES=F", html)
                self.assertIn("MA520", html)
                self.assertIn("MA1560", html)

                # 2. Health, symbols, timeframes, strategies
                r_health = await client.get("/api/health")
                self.assertEqual(r_health.status, 200)
                j_health = await r_health.json()
                self.assertEqual(j_health["default_port"], 8081)
                self.assertEqual(j_health["timezone"], "America/New_York")

                r_tf = await client.get("/api/timeframes")
                j_tf = await r_tf.json()
                self.assertEqual([x["code"] for x in j_tf["timeframes"]], ORDERED_TIMEFRAMES)

                # 3. POST /api/fetch with '1M' -> normalized to '1mo' (HTTP 200)
                r_fetch_1m_upper = await client.post(
                    "/api/fetch",
                    json={"symbol": "NVDA", "timeframe": "1M", "source": "mock", "days": 400},
                )
                self.assertEqual(r_fetch_1m_upper.status, 200)
                j_fetch = await r_fetch_1m_upper.json()
                self.assertTrue(j_fetch["success"])
                self.assertEqual(j_fetch["timeframe"], "1mo")
                self.assertGreater(j_fetch["saved_count"], 5)

                # 4. GET /api/candles with '1M' -> normalized to '1mo' (HTTP 200)
                r_candles_1mo = await client.get("/api/candles?symbol=NVDA&timeframe=1M&strategy=stock_us_fir")
                self.assertEqual(r_candles_1mo.status, 200)
                j_candles = await r_candles_1mo.json()
                self.assertEqual(j_candles["timeframe"], "1mo")
                self.assertIn("ma520", j_candles["overlays"])
                self.assertIn("ma1560", j_candles["overlays"])
                self.assertIn("poc", j_candles["overlays"])
                self.assertIn("elliott_waves", j_candles)

                # 5. GET /api/candles with '15m' & auto_mock=1 across masb and blank strategies
                for strat_name in ("stock_us_fir", "masb", "blank"):
                    r_st = await client.get(
                        f"/api/candles?symbol=AAPL&timeframe=15m&strategy={strat_name}&auto_mock=1"
                    )
                    self.assertEqual(r_st.status, 200)
                    j_st = await r_st.json()
                    self.assertTrue(j_st["success"])
                    self.assertGreater(j_st["count"], 100)

                # 6. Lowercase '1m' MUST return HTTP 400 on both GET /api/candles and POST /api/fetch!
                r_bad_get = await client.get("/api/candles?symbol=NVDA&timeframe=1m")
                self.assertEqual(r_bad_get.status, 400)

                r_bad_post = await client.post(
                    "/api/fetch",
                    json={"symbol": "NVDA", "timeframe": "1m", "source": "mock"},
                )
                self.assertEqual(r_bad_post.status, 400)

        asyncio.run(_exercise_api())


if __name__ == "__main__":
    unittest.main()
