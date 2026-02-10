"""BTC Sector Arbitrage Bot

Focused scanner for Bitcoin prediction markets:
1. Spot-lag: BTC spot crosses threshold before market odds adjust
2. Cross-exchange: Same BTC market on Poly vs Kalshi with price discrepancy
3. Intra-exchange: YES + NO < $1.00 on same market
4. Multi-outcome: Sum of all outcomes < $1.00

Polls every 5 seconds. Uses targeted API queries (KXBTC series, BTC tag filter).
"""
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add parent dirs to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse
import uvicorn

from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient
from crypto_client import CryptoClient
from db import log_opportunity, update_bot_status, get_recent_opportunities, get_period_stats

load_dotenv(Path(__file__).parent.parent / ".env")

# ─── Config ──────────────────────────────────────────────────────────────
BOT_NAME = "btc"
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY")
KALSHI_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", str(Path(__file__).parent.parent / "kalshi.pem"))
POLL_INTERVAL = 5
SPOT_LAG_THRESHOLD = 0.15  # Market ≥15% mispriced vs spot
CROSS_MIN_PROFIT = 1.0     # ≥1¢ profit for cross-exchange
PROXIMITY = 0.05           # Spot must be within 5% of threshold
PORT = 8001

KALSHI_BTC_SERIES = ["KXBTC", "KXBTCD"]  # BTC price + BTC daily

# ─── Helpers ─────────────────────────────────────────────────────────────
def parse_kalshi_subtitle(subtitle):
    s = subtitle.replace(",", "")
    above = re.match(r'\$?([\d.]+)\s+or above', s)
    below = re.match(r'\$?([\d.]+)\s+or below', s)
    bracket = re.match(r'\$?([\d.]+)\s+to\s+([\d.]+)', s)
    if above: return {"dir": "above", "threshold": float(above.group(1))}
    if below: return {"dir": "below", "threshold": float(below.group(1))}
    if bracket: return {"dir": "bracket", "low": float(bracket.group(1)), "high": float(bracket.group(2))}
    return None

def is_btc_market(title):
    t = title.upper()
    return bool(re.search(r'\b(BITCOIN|BTC)\b', t))

def extract_threshold(title):
    matches = re.findall(r'\$(\d[\d,]*(?:\.\d+)?)', title)
    return max(float(m.replace(",", "")) for m in matches) if matches else None

def is_above_market(title):
    t = title.upper()
    return any(w in t for w in ["ABOVE", "OVER", "EXCEED", "REACH", "HIT", "CROSS", "HIGHER"])

# ─── State ───────────────────────────────────────────────────────────────
state = {
    "status": "starting",
    "scan_count": 0,
    "last_scan": None,
    "btc_spot": 0,
    "kalshi_markets": 0,
    "poly_markets": 0,
    "matched_pairs": 0,
    "session_opps": [],
    "errors": [],
}

app = FastAPI(title=f"Arb Bot: {BOT_NAME.upper()}")

@app.get("/api/state")
async def api_state():
    return state

@app.get("/api/opportunities")
async def api_opps():
    return get_recent_opportunities(limit=50, bot=BOT_NAME)

@app.get("/api/stats")
async def api_stats():
    return {
        "1h": get_period_stats(3600, BOT_NAME),
        "4h": get_period_stats(14400, BOT_NAME),
        "24h": get_period_stats(86400, BOT_NAME),
    }

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)

@app.get("/stream")
async def stream():
    async def gen():
        while True:
            yield {"event": "message", "data": json.dumps(state, default=str)}
            await asyncio.sleep(2)
    return EventSourceResponse(gen())

# ─── Core Scan Loop ─────────────────────────────────────────────────────
async def scan_loop():
    p = lambda msg: print(msg, flush=True)
    p(f"🟠 BTC Arb Bot starting (poll: {POLL_INTERVAL}s)")

    async with KalshiClient(KALSHI_API_KEY, KALSHI_KEY_PATH) as kalshi, \
               PolymarketClient() as poly, \
               CryptoClient() as crypto:

        state["status"] = "running"
        # Pre-build Poly BTC market cache (refreshed every 60s)
        poly_cache = []
        poly_cache_time = 0

        while True:
            t0 = datetime.now(timezone.utc)
            state["scan_count"] += 1
            state["last_scan"] = t0.strftime("%H:%M:%S")
            found = []

            try:
                # 1. BTC spot price
                btc = await crypto.get_spot_price("BTC-USD")
                if not btc:
                    p(f"  ⚠️ Failed to get BTC spot")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                state["btc_spot"] = btc

                # 2. Kalshi BTC markets (targeted series query)
                kalshi_markets = []
                for series in KALSHI_BTC_SERIES:
                    path = f"/markets?series_ticker={series}&status=open&limit=200"
                    headers = kalshi._get_headers("GET", path)
                    data = await kalshi._request_with_retry("GET", f"{kalshi.BASE_URL}{path}", headers)
                    if data:
                        kalshi_markets.extend(data.get("markets", []))
                    await asyncio.sleep(0.3)
                state["kalshi_markets"] = len(kalshi_markets)

                # 3. Polymarket BTC markets (cached, refreshed every 60s)
                now = time.time()
                if now - poly_cache_time > 60:
                    all_poly = await poly.get_markets(limit=200)
                    poly_cache = [m for m in all_poly if is_btc_market(m.get("question", "") or m.get("title", ""))]
                    poly_cache_time = now
                state["poly_markets"] = len(poly_cache)

                if state["scan_count"] % 20 == 1:
                    p(f"[{t0.strftime('%H:%M:%S')}] #{state['scan_count']} BTC ${btc:,.0f} | K:{len(kalshi_markets)} P:{len(poly_cache)}")

                # ═══════════════════════════════════════════════════
                # TYPE 1: Spot-Lag — Kalshi
                # ═══════════════════════════════════════════════════
                for market in kalshi_markets:
                    subtitle = market.get("subtitle", "")
                    parsed = parse_kalshi_subtitle(subtitle)
                    if not parsed:
                        continue

                    # Use market-level yes_ask if available
                    yes_ask_raw = market.get("yes_ask")
                    if yes_ask_raw and yes_ask_raw > 0:
                        yes_ask = yes_ask_raw / 100.0
                    else:
                        # Fetch orderbook (throttled)
                        ob = await kalshi.get_orderbook(market.get("ticker", ""))
                        if not ob:
                            continue
                        no_bids = ob.get("no", [])
                        if no_bids:
                            best_no = no_bids[-1][0] if isinstance(no_bids[-1], list) else no_bids[-1]
                            yes_ask = (100 - best_no) / 100.0
                        else:
                            continue
                        await asyncio.sleep(0.15)

                    opp = check_spot_lag(btc, yes_ask, parsed, market, "kalshi")
                    if opp:
                        found.append(opp)

                # ═══════════════════════════════════════════════════
                # TYPE 2: Spot-Lag — Polymarket
                # ═══════════════════════════════════════════════════
                for market in poly_cache:
                    title = market.get("question", "") or market.get("title", "")
                    threshold = extract_threshold(title)
                    if not threshold:
                        continue

                    tokens = market.get("tokens", [])
                    if not tokens:
                        continue

                    _, ask = await poly.get_best_prices(tokens[0].get("token_id", ""))
                    if ask is None:
                        continue

                    above = is_above_market(title)
                    opp = check_spot_lag_poly(btc, ask, threshold, above, market)
                    if opp:
                        found.append(opp)

                # ═══════════════════════════════════════════════════
                # TYPE 3: Cross-Exchange — BTC markets on both platforms
                # ═══════════════════════════════════════════════════
                cross_opps = await check_cross_exchange(btc, kalshi_markets, poly_cache, kalshi, poly)
                found.extend(cross_opps)

                # ═══════════════════════════════════════════════════
                # TYPE 4: Intra-Exchange YES/NO spread
                # ═══════════════════════════════════════════════════
                for market in kalshi_markets:
                    ya = market.get("yes_ask", 0)
                    na = market.get("no_ask", 0)
                    if ya and na:
                        cost = ya / 100.0 + na / 100.0
                        if cost < (1.0 - CROSS_MIN_PROFIT / 100):
                            profit = (1.0 - cost) * 100
                            found.append({
                                "market": f"{market.get('title','')} — {market.get('subtitle','')}",
                                "arb_type": "intra_kalshi",
                                "strategy": f"Buy YES@{ya}¢ + NO@{na}¢ = {int(ya+na)}¢",
                                "profit_cents": profit,
                                "price_a": ya / 100.0,
                                "price_b": na / 100.0,
                                "source_a": "kalshi",
                                "source_b": "kalshi",
                                "url": f"https://kalshi.com/markets/{market.get('ticker','')}",
                            })

                # ═══════════════════════════════════════════════════
                # Log results
                # ═══════════════════════════════════════════════════
                for opp in found:
                    log_opportunity(
                        bot=BOT_NAME, market=opp["market"], arb_type=opp["arb_type"],
                        strategy=opp["strategy"], profit_cents=opp["profit_cents"],
                        price_a=opp.get("price_a", 0), price_b=opp.get("price_b", 0),
                        source_a=opp.get("source_a", ""), source_b=opp.get("source_b", ""),
                        url=opp.get("url", ""),
                    )
                    state["session_opps"].append(opp)
                    if len(state["session_opps"]) > 200:
                        state["session_opps"] = state["session_opps"][-200:]

                state["matched_pairs"] = len(cross_opps)
                update_bot_status(BOT_NAME, "running", state["scan_count"],
                                  len(kalshi_markets) + len(poly_cache), 0, len(found))

                if found:
                    for o in found:
                        p(f"  🎯 [{o['arb_type']}] {o['strategy'][:60]}")

            except Exception as e:
                state["errors"].append(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {e}")
                if len(state["errors"]) > 20:
                    state["errors"] = state["errors"][-20:]
                p(f"  ❌ {e}")
                import traceback; traceback.print_exc()

            elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
            await asyncio.sleep(max(1, POLL_INTERVAL - elapsed))


def near_threshold(spot, th):
    """Only flag if spot is within PROXIMITY of threshold."""
    if th == 0: return False
    return abs(spot - th) / th <= PROXIMITY

def check_spot_lag(spot, yes_ask, parsed, market, source):
    """Check single Kalshi market for spot-lag."""
    title = market.get("title", "")
    subtitle = market.get("subtitle", "")
    ticker = market.get("ticker", "")
    d = parsed["dir"]

    if d == "above":
        th = parsed["threshold"]
        if not near_threshold(spot, th):
            return None
        if spot >= th and yes_ask < SPOT_LAG_THRESHOLD:
            return _make_opp(f"{title} — {subtitle}", "spot_lag", f"BUY YES (BTC ${spot:,.0f} ≥ ${th:,.0f}, mkt {yes_ask*100:.0f}¢)",
                             (1 - yes_ask) * 100, yes_ask, 0, source, "", f"https://kalshi.com/markets/{ticker}")
        if spot < th and yes_ask > (1 - SPOT_LAG_THRESHOLD):
            return _make_opp(f"{title} — {subtitle}", "spot_lag", f"BUY NO (BTC ${spot:,.0f} < ${th:,.0f}, mkt {yes_ask*100:.0f}¢)",
                             yes_ask * 100, yes_ask, 0, source, "", f"https://kalshi.com/markets/{ticker}")
    elif d == "below":
        th = parsed["threshold"]
        if not near_threshold(spot, th):
            return None
        if spot <= th and yes_ask < SPOT_LAG_THRESHOLD:
            return _make_opp(f"{title} — {subtitle}", "spot_lag", f"BUY YES (BTC ${spot:,.0f} ≤ ${th:,.0f}, mkt {yes_ask*100:.0f}¢)",
                             (1 - yes_ask) * 100, yes_ask, 0, source, "", f"https://kalshi.com/markets/{ticker}")
        if spot > th and yes_ask > (1 - SPOT_LAG_THRESHOLD):
            return _make_opp(f"{title} — {subtitle}", "spot_lag", f"BUY NO (BTC ${spot:,.0f} > ${th:,.0f}, mkt {yes_ask*100:.0f}¢)",
                             yes_ask * 100, yes_ask, 0, source, "", f"https://kalshi.com/markets/{ticker}")
    elif d == "bracket":
        lo, hi = parsed["low"], parsed["high"]
        if not (near_threshold(spot, lo) or near_threshold(spot, hi)):
            return None
        inside = lo <= spot <= hi
        if inside and yes_ask < SPOT_LAG_THRESHOLD:
            return _make_opp(f"{title} — {subtitle}", "spot_lag", f"BUY YES (BTC ${spot:,.0f} in ${lo:,.0f}-${hi:,.0f}, mkt {yes_ask*100:.0f}¢)",
                             (1 - yes_ask) * 100, yes_ask, 0, source, "", f"https://kalshi.com/markets/{ticker}")
        if not inside and yes_ask > (1 - SPOT_LAG_THRESHOLD):
            return _make_opp(f"{title} — {subtitle}", "spot_lag", f"BUY NO (BTC ${spot:,.0f} outside ${lo:,.0f}-${hi:,.0f}, mkt {yes_ask*100:.0f}¢)",
                             yes_ask * 100, yes_ask, 0, source, "", f"https://kalshi.com/markets/{ticker}")
    return None


def check_spot_lag_poly(spot, yes_ask, threshold, above, market):
    """Check Polymarket BTC market for spot-lag."""
    title = market.get("question", "") or market.get("title", "")
    slug = market.get("slug", "")
    
    if not near_threshold(spot, threshold):
        return None
    
    if above:
        if spot > threshold and yes_ask < SPOT_LAG_THRESHOLD:
            return _make_opp(title, "spot_lag", f"BUY YES (BTC ${spot:,.0f} > ${threshold:,.0f}, mkt {yes_ask*100:.0f}¢)",
                             (1 - yes_ask) * 100, yes_ask, 0, "polymarket", "", f"https://polymarket.com/event/{slug}")
        if spot < threshold and yes_ask > (1 - SPOT_LAG_THRESHOLD):
            return _make_opp(title, "spot_lag", f"BUY NO (BTC ${spot:,.0f} < ${threshold:,.0f}, mkt {yes_ask*100:.0f}¢)",
                             yes_ask * 100, yes_ask, 0, "polymarket", "", f"https://polymarket.com/event/{slug}")
    else:
        if spot < threshold and yes_ask < SPOT_LAG_THRESHOLD:
            return _make_opp(title, "spot_lag", f"BUY YES (BTC ${spot:,.0f} < ${threshold:,.0f}, mkt {yes_ask*100:.0f}¢)",
                             (1 - yes_ask) * 100, yes_ask, 0, "polymarket", "", f"https://polymarket.com/event/{slug}")
        if spot > threshold and yes_ask > (1 - SPOT_LAG_THRESHOLD):
            return _make_opp(title, "spot_lag", f"BUY NO (BTC ${spot:,.0f} > ${threshold:,.0f}, mkt {yes_ask*100:.0f}¢)",
                             yes_ask * 100, yes_ask, 0, "polymarket", "", f"https://polymarket.com/event/{slug}")
    return None


async def check_cross_exchange(spot, kalshi_markets, poly_markets, kalshi, poly):
    """Find same-topic BTC markets across exchanges and compare prices."""
    opps = []

    # Build Kalshi market map by threshold
    kalshi_by_threshold = {}
    for m in kalshi_markets:
        parsed = parse_kalshi_subtitle(m.get("subtitle", ""))
        if parsed and parsed["dir"] in ("above", "below"):
            key = (parsed["dir"], parsed["threshold"])
            ya = m.get("yes_ask", 0)
            na = m.get("no_ask", 0)
            if ya and na:
                kalshi_by_threshold[key] = {
                    "market": m,
                    "yes_ask": ya / 100.0,
                    "no_ask": na / 100.0,
                    "ticker": m.get("ticker", ""),
                }

    # Match Polymarket markets by threshold
    for pm in poly_markets:
        title = pm.get("question", "") or pm.get("title", "")
        threshold = extract_threshold(title)
        if not threshold:
            continue

        above = is_above_market(title)
        direction = "above" if above else "below"
        key = (direction, threshold)

        if key not in kalshi_by_threshold:
            continue

        km = kalshi_by_threshold[key]
        tokens = pm.get("tokens", [])
        if not tokens:
            continue

        # Get Poly orderbook price
        bid, ask = await poly.get_best_prices(tokens[0].get("token_id", ""))
        if ask is None:
            continue

        poly_yes = ask
        poly_no = 1.0 - bid if bid else None

        # Cross-exchange: Buy Poly YES + Kalshi NO
        cost1 = poly_yes + km["no_ask"]
        if cost1 < (1.0 - CROSS_MIN_PROFIT / 100):
            profit = (1.0 - cost1) * 100
            opps.append(_make_opp(
                f"BTC {direction} ${threshold:,.0f}", "cross_exchange",
                f"Poly YES@{poly_yes:.2f} + Kalshi NO@{km['no_ask']:.2f} = {cost1:.2f}",
                profit, poly_yes, km["no_ask"], "polymarket", "kalshi",
                f"https://kalshi.com/markets/{km['ticker']}"
            ))

        # Cross-exchange: Buy Kalshi YES + Poly NO
        if poly_no is not None:
            cost2 = km["yes_ask"] + poly_no
            if cost2 < (1.0 - CROSS_MIN_PROFIT / 100):
                profit = (1.0 - cost2) * 100
                opps.append(_make_opp(
                    f"BTC {direction} ${threshold:,.0f}", "cross_exchange",
                    f"Kalshi YES@{km['yes_ask']:.2f} + Poly NO@{poly_no:.2f} = {cost2:.2f}",
                    profit, km["yes_ask"], poly_no, "kalshi", "polymarket",
                    f"https://kalshi.com/markets/{km['ticker']}"
                ))

    return opps


def _make_opp(market, arb_type, strategy, profit_cents, price_a, price_b, source_a, source_b, url):
    return {
        "market": market, "arb_type": arb_type, "strategy": strategy,
        "profit_cents": profit_cents, "price_a": price_a, "price_b": price_b,
        "source_a": source_a, "source_b": source_b, "url": url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ─── Dashboard HTML ─────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html><head><title>BTC Arb Bot</title>
<style>
    body{font-family:'JetBrains Mono',monospace;background:#0d1117;color:#c9d1d9;padding:20px;margin:0}
    .hdr{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #30363d;padding-bottom:12px;margin-bottom:20px}
    .hdr h1{font-size:1.3em;color:#f0883e} .badge{background:#238636;color:#fff;padding:4px 10px;border-radius:15px;font-size:0.75em}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}
    .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px}
    .card h3{color:#8b949e;font-size:0.65em;text-transform:uppercase;margin-bottom:4px}
    .val{font-size:1.8em;font-weight:700;color:#f0f6fc} .sub{color:#3fb950;font-size:0.75em;margin-top:3px}
    table{width:100%;border-collapse:collapse;font-size:0.75em;margin-top:10px}
    th{text-align:left;padding:6px;color:#8b949e;border-bottom:1px solid #30363d}
    td{padding:6px;border-bottom:1px solid #21262d}
    .profit{color:#3fb950;font-weight:700} a{color:#58a6ff;text-decoration:none}
</style></head><body>
<div class="hdr"><h1>🟠 BTC Arb Bot</h1><span class="badge" id="status">LOADING</span></div>
<div class="grid">
    <div class="card"><h3>BTC Spot</h3><div class="val" id="spot">--</div></div>
    <div class="card"><h3>Kalshi</h3><div class="val" id="k-count">--</div><div class="sub">markets</div></div>
    <div class="card"><h3>Polymarket</h3><div class="val" id="p-count">--</div><div class="sub">markets</div></div>
    <div class="card"><h3>Scan</h3><div class="val" id="scan">--</div><div class="sub" id="last-scan">--</div></div>
    <div class="card"><h3>Session Opps</h3><div class="val" id="opps">--</div></div>
</div>
<table><thead><tr><th>Time</th><th>Type</th><th>Strategy</th><th>Profit</th><th>Link</th></tr></thead>
<tbody id="opp-body"><tr><td colspan="5" style="text-align:center;color:#8b949e">Waiting...</td></tr></tbody></table>
<script>
const es=new EventSource("/stream");
es.onmessage=e=>{
    const d=JSON.parse(e.data);
    document.getElementById('status').textContent=(d.status||'?').toUpperCase();
    document.getElementById('spot').textContent='$'+(d.btc_spot||0).toLocaleString();
    document.getElementById('k-count').textContent=d.kalshi_markets||0;
    document.getElementById('p-count').textContent=d.poly_markets||0;
    document.getElementById('scan').textContent='#'+(d.scan_count||0);
    document.getElementById('last-scan').textContent=d.last_scan||'--';
    const o=(d.session_opps||[]).slice(-15).reverse();
    document.getElementById('opps').textContent=d.session_opps?.length||0;
    document.getElementById('opp-body').innerHTML=o.length?o.map(r=>{
        const ts=r.timestamp?new Date(r.timestamp).toLocaleTimeString():'--';
        return `<tr><td>${ts}</td><td>${r.arb_type||''}</td><td>${(r.strategy||'').substring(0,60)}</td><td class="profit">+${(r.profit_cents||0).toFixed(1)}¢</td><td>${r.url?`<a href="${r.url}" target="_blank">↗</a>`:''}</td></tr>`;
    }).join(''):'<tr><td colspan="5" style="text-align:center;color:#8b949e">No opportunities yet</td></tr>';
};
</script></body></html>"""


@app.on_event("startup")
async def startup():
    asyncio.create_task(scan_loop())

if __name__ == "__main__":
    print(f"🟠 BTC Arb Bot | Port {PORT} | Poll {POLL_INTERVAL}s", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
