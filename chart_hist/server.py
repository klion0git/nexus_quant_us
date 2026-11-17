"""Async aiohttp web server for the US Historical Chart Workstation (default port 8081)."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional, Union
from urllib.parse import urlsplit

from aiohttp import web

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.math_utils import safe_int
from chart_hist.config import (
    CHART_HIST_DIR,
    DB_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_SYMBOL,
    DEFAULT_TIMEFRAME,
    PRESET_SYMBOLS,
    SUPPORTED_TIMEFRAMES,
    normalize_chart_hist_timeframe,
)
from chart_hist.database import (
    delete_candles,
    get_available_symbols,
    get_candles,
    init_db,
    save_candles,
)
from chart_hist.fetchers import get_fetcher
from chart_hist.strategies import get_strategy, list_strategies
from us_core.symbol import UsSymbol

logger = logging.getLogger("nexus_quant_us.chart_hist.server")

HTML_TEMPLATE_PATH: Path = CHART_HIST_DIR / "templates" / "chart_view.html"
DB_PATH_KEY = web.AppKey("db_path", Optional[Union[str, Path]])


def get_app_db_path(app: web.Application) -> Optional[Union[str, Path]]:
    return app.get(DB_PATH_KEY)


@web.middleware
async def local_security_middleware(request: web.Request, handler):
    """Guard workstation routes against DNS rebinding and cross-origin mutation."""
    host_header = request.headers.get("Host", "")
    try:
        hostname = urlsplit("//" + host_header).hostname or ""
    except ValueError:
        hostname = ""

    host_ok = hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "")
    if not host_ok:
        try:
            ip = ipaddress.ip_address(hostname)
            host_ok = ip.is_loopback or ip.is_private
        except ValueError:
            host_ok = False
    if not host_ok:
        return web.json_response({"success": False, "error": "Disallowed Host header"}, status=403)

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin")
        if origin is not None:
            if origin.strip().lower() == "null":
                return web.json_response({"success": False, "error": "Disallowed null Origin"}, status=403)
            try:
                origin_netloc = urlsplit(origin).netloc
            except ValueError:
                origin_netloc = ""
            if not origin_netloc or (host_header and origin_netloc != host_header):
                return web.json_response({"success": False, "error": "Cross-origin request rejected"}, status=403)

    return await handler(request)


async def handle_index(request: web.Request) -> web.Response:
    if not HTML_TEMPLATE_PATH.exists():
        return web.Response(text="Template chart_view.html not found.", status=404)
    html = HTML_TEMPLATE_PATH.read_text(encoding="utf-8")
    return web.Response(
        text=html,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "success": True,
            "status": "ok",
            "market": "US",
            "timezone": "America/New_York",
            "default_port": DEFAULT_PORT,
        }
    )


async def handle_get_symbols(request: web.Request) -> web.Response:
    db_path = get_app_db_path(request.app)
    loop = asyncio.get_running_loop()
    db_summary = await loop.run_in_executor(None, get_available_symbols, db_path)

    presets = [dict(item) for item in PRESET_SYMBOLS]
    known = {item["symbol"] for item in presets}
    for row in db_summary:
        sym = row["symbol"]
        if sym not in known:
            presets.append(
                {
                    "symbol": sym,
                    "name": f"{sym} (Saved US Symbol)",
                    "category": "Custom US",
                    "exchange": "US",
                }
            )
    return web.json_response(
        {
            "success": True,
            "default_symbol": DEFAULT_SYMBOL,
            "symbols": presets,
            "db_summary": db_summary,
        }
    )


async def handle_get_timeframes(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "success": True,
            "default_timeframe": DEFAULT_TIMEFRAME,
            "timeframes": SUPPORTED_TIMEFRAMES,
        }
    )


async def handle_get_strategies(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "success": True,
            "strategies": list_strategies(),
        }
    )


async def handle_get_candles(request: web.Request) -> web.Response:
    raw_sym = request.query.get("symbol", DEFAULT_SYMBOL)
    raw_tf = request.query.get("timeframe", DEFAULT_TIMEFRAME)
    raw_strat = request.query.get("strategy", "stock_us_fir")
    raw_limit = request.query.get("limit", "2500")
    auto_mock = request.query.get("auto_mock", "0").strip().lower() in ("1", "true", "yes")

    try:
        clean_sym = UsSymbol.parse(raw_sym).code
    except ValueError as exc:
        return web.json_response({"success": False, "error": str(exc)}, status=400)

    try:
        norm_tf = normalize_chart_hist_timeframe(raw_tf)
    except ValueError as exc:
        return web.json_response({"success": False, "error": str(exc)}, status=400)

    try:
        strategy_fn = get_strategy(raw_strat)
    except ValueError as exc:
        return web.json_response({"success": False, "error": str(exc)}, status=400)

    limit = max(10, min(10000, safe_int(raw_limit, 2500)))
    db_path = get_app_db_path(request.app)
    loop = asyncio.get_running_loop()

    candles = await loop.run_in_executor(
        None,
        lambda: get_candles(clean_sym, norm_tf, limit=limit, db_path=db_path),
    )

    if not candles and auto_mock:
        fetcher = get_fetcher("mock")
        fetched = await fetcher.fetch(clean_sym, norm_tf, days=75)
        await loop.run_in_executor(None, lambda: save_candles(fetched, db_path=db_path))
        candles = await loop.run_in_executor(
            None,
            lambda: get_candles(clean_sym, norm_tf, limit=limit, db_path=db_path),
        )

    candle_dicts = [c.to_dict() for c in candles]
    strat_output = await loop.run_in_executor(
        None,
        lambda: strategy_fn(candle_dicts, symbol=clean_sym, timeframe=norm_tf),
    )

    return web.json_response(
        {
            "success": True,
            "symbol": clean_sym,
            "timeframe": norm_tf,
            "strategy": strat_output["strategy"],
            "count": len(candle_dicts),
            "candles": candle_dicts,
            "overlays": strat_output.get("overlays", {}),
            "markers": strat_output.get("markers", []),
            "elliott_waves": strat_output.get("elliott_waves", {}),
            "volume_profile": strat_output.get("volume_profile", {}),
        }
    )


async def handle_post_fetch(request: web.Request) -> web.Response:
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON body"}, status=400)

    raw_sym = body.get("symbol", DEFAULT_SYMBOL)
    raw_tf = body.get("timeframe", DEFAULT_TIMEFRAME)
    source = str(body.get("source", "mock") or "mock").strip().lower()
    days = max(5, min(3650, safe_int(body.get("days", 75), 75)))

    try:
        clean_sym = UsSymbol.parse(raw_sym).code
        norm_tf = normalize_chart_hist_timeframe(raw_tf)
        fetcher = get_fetcher(source)
    except ValueError as exc:
        return web.json_response({"success": False, "error": str(exc)}, status=400)

    try:
        bars = await fetcher.fetch(clean_sym, norm_tf, days=days)
        db_path = get_app_db_path(request.app)
        loop = asyncio.get_running_loop()
        saved = await loop.run_in_executor(None, lambda: save_candles(bars, db_path=db_path))
    except Exception as exc:
        logger.error("Fetch failed for %s %s (%s): %s", clean_sym, norm_tf, source, exc)
        return web.json_response({"success": False, "error": str(exc)}, status=500)

    return web.json_response(
        {
            "success": True,
            "symbol": clean_sym,
            "timeframe": norm_tf,
            "source": source,
            "saved_count": saved,
        }
    )


async def handle_delete_candles(request: web.Request) -> web.Response:
    body: Dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    raw_sym = body.get("symbol") or request.query.get("symbol")
    raw_tf = body.get("timeframe") or request.query.get("timeframe")
    confirm_raw = body.get("confirm_all", request.query.get("confirm_all", "false"))
    confirm_all = str(confirm_raw).strip().lower() in ("1", "true", "yes")

    try:
        clean_sym = UsSymbol.parse(raw_sym).code if raw_sym else None
        norm_tf = normalize_chart_hist_timeframe(raw_tf) if raw_tf else None
        db_path = get_app_db_path(request.app)
        loop = asyncio.get_running_loop()
        deleted = await loop.run_in_executor(
            None,
            lambda: delete_candles(
                symbol=clean_sym,
                timeframe=norm_tf,
                confirm_all=confirm_all,
                db_path=db_path,
            ),
        )
    except ValueError as exc:
        return web.json_response({"success": False, "error": str(exc)}, status=400)

    return web.json_response(
        {
            "success": True,
            "deleted_count": deleted,
            "symbol": clean_sym,
            "timeframe": norm_tf,
        }
    )


def create_app(db_path: Optional[Union[str, Path]] = None) -> web.Application:
    init_db(db_path)
    app = web.Application(middlewares=[local_security_middleware])
    app[DB_PATH_KEY] = db_path
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/symbols", handle_get_symbols)
    app.router.add_get("/api/timeframes", handle_get_timeframes)
    app.router.add_get("/api/strategies", handle_get_strategies)
    app.router.add_get("/api/candles", handle_get_candles)
    app.router.add_post("/api/fetch", handle_post_fetch)
    app.router.add_delete("/api/candles", handle_delete_candles)
    app.router.add_post("/api/candles/delete", handle_delete_candles)
    return app


def start_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    open_browser: bool = False,
    db_path: Optional[Union[str, Path]] = None,
) -> None:
    app = create_app(db_path=db_path)
    target_db = Path(db_path) if db_path else DB_PATH
    url = f"http://{host}:{port}"
    print("=" * 60)
    print("[Nexus US] Historical Chart & Strategy Workstation")
    print(f"[URL]      {url}")
    print(f"[TIMEZONE] America/New_York (EDT UTC-4 / EST UTC-5)")
    print(f"[DB]       {target_db}")
    print("=" * 60)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    web.run_app(app, host=host, port=int(port))
