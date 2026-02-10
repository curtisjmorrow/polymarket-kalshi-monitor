"""Unified Arbitrage Engine v3.0

Combines all arbitrage detection modes into one scanner:
1. Cross-exchange (Polymarket vs Kalshi)
2. Intra-exchange YES/NO spread (Poly + Kalshi)
3. Multi-outcome (sum of YES < 1.0)
4. Temporal/logical constraint (date supersets, mutual exclusion)
5. Crypto spot-lag (BTC/ETH/SOL spot vs prediction market odds)

Single service, single dashboard at port 8000.
"""
import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse
import uvicorn

from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient
from crypto_client import CryptoClient
from arbitrage_detector import ArbitrageDetector, ArbitrageOpportunity
from logical_constraints import LogicalConstraintDetector

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi.pem")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))
MIN_PROFIT_CENTS = float(os.getenv("MIN_PROFIT_CENTS", "1.0"))
LOG_FILE = os.getenv("LOG_FILE", "arb_opportunities.csv")
DB_PATH = Path(__file__).parent / "unified_arb.db"
CRYPTO_MISPRICING = 0.15  # Market must be ≥15% away from correct value
CRYPTO_PROXIMITY = 0.05   # Spot must be within 5% of threshold (filters out obvious outcomes)

KALSHI_CRYPTO_SERIES = ["KXBTC", "KXETH", "KXSOL"]

# ─── Database ────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS opportunities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        market TEXT,
        arb_type TEXT,
        strategy TEXT,
        profit_cents REAL,
        poly_price REAL,
        kalshi_price REAL,
        source TEXT,
        url TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS scan_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        poly_count INTEGER,
        kalshi_count INTEGER,
        crypto_poly INTEGER,
        crypto_kalshi INTEGER,
        matched_pairs INTEGER,
        opportunities INTEGER
    )''')
    conn.commit()
    conn.close()

def log_opportunity(opp: dict):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('''INSERT INTO opportunities (timestamp, market, arb_type, strategy, profit_cents, poly_price, kalshi_price, source, url)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (time.time(), opp.get('market', '')[:200], opp.get('arb_type', ''), opp.get('strategy', ''),
               opp.get('profit_cents', 0), opp.get('poly_price', 0), opp.get('kalshi_price', 0),
               opp.get('source', ''), opp.get('url', '')))
    conn.commit()
    conn.close()

def log_scan_stats(poly, kalshi, crypto_p, crypto_k, matched, opps):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('''INSERT INTO scan_stats VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)''',
              (time.time(), poly, kalshi, crypto_p, crypto_k, matched, opps))
    conn.commit()
    conn.close()

def get_recent_opportunities(limit=30):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('SELECT * FROM opportunities ORDER BY timestamp DESC LIMIT ?', (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_period_stats(seconds):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    cutoff = time.time() - seconds
    c.execute('SELECT COUNT(*), COALESCE(SUM(profit_cents), 0) FROM opportunities WHERE timestamp > ?', (cutoff,))
    row = c.fetchone()
    conn.close()
    return {'count': row[0], 'profit': round(row[1], 2)}

# ─── Crypto Helpers ──────────────────────────────────────────────────────
def parse_kalshi_subtitle(subtitle):
    s = subtitle.replace(",", "")
    above = re.match(r'\$?([\d.]+)\s+or above', s)
    below = re.match(r'\$?([\d.]+)\s+or below', s)
    bracket = re.match(r'\$?([\d.]+)\s+to\s+([\d.]+)', s)
    if above:
        return {"direction": "above", "threshold": float(above.group(1))}
    elif below:
        return {"direction": "below", "threshold": float(below.group(1))}
    elif bracket:
        return {"direction": "bracket", "low": float(bracket.group(1)), "high": float(bracket.group(2))}
    return None

def coin_from_ticker(ticker):
    for prefix, coin in [("KXBTC", "BTC"), ("KXETH", "ETH"), ("KXSOL", "SOL")]:
        if ticker.startswith(prefix):
            return coin
    return None

def is_near_threshold(spot, threshold):
    """Only flag if spot is within CRYPTO_PROXIMITY of threshold — filters out obvious outcomes."""
    if threshold == 0:
        return False
    distance = abs(spot - threshold) / threshold
    return distance <= CRYPTO_PROXIMITY

def evaluate_crypto_kalshi(market, spot, yes_price):
    ticker = market.get("ticker", "")
    coin = coin_from_ticker(ticker)
    if not coin:
        return None
    
    subtitle = market.get("subtitle", "")
    parsed = parse_kalshi_subtitle(subtitle)
    if not parsed:
        return None
    
    title = market.get("title", "")
    direction = parsed["direction"]
    
    if direction == "above":
        threshold = parsed["threshold"]
        if not is_near_threshold(spot, threshold):
            return None
        if spot >= threshold and yes_price < CRYPTO_MISPRICING:
            return make_crypto_opp("kalshi", coin, f"{title} — {subtitle}", ticker, spot, threshold, yes_price, "BUY YES")
        if spot < threshold and yes_price > (1 - CRYPTO_MISPRICING):
            return make_crypto_opp("kalshi", coin, f"{title} — {subtitle}", ticker, spot, threshold, yes_price, "BUY NO")
    elif direction == "below":
        threshold = parsed["threshold"]
        if not is_near_threshold(spot, threshold):
            return None
        if spot <= threshold and yes_price < CRYPTO_MISPRICING:
            return make_crypto_opp("kalshi", coin, f"{title} — {subtitle}", ticker, spot, threshold, yes_price, "BUY YES")
        if spot > threshold and yes_price > (1 - CRYPTO_MISPRICING):
            return make_crypto_opp("kalshi", coin, f"{title} — {subtitle}", ticker, spot, threshold, yes_price, "BUY NO")
    elif direction == "bracket":
        low, high = parsed["low"], parsed["high"]
        # For brackets, check if spot is near either edge
        near_edge = is_near_threshold(spot, low) or is_near_threshold(spot, high)
        if not near_edge:
            return None
        in_bracket = low <= spot <= high
        if in_bracket and yes_price < CRYPTO_MISPRICING:
            return make_crypto_opp("kalshi", coin, f"{title} — {subtitle}", ticker, spot, low, yes_price, "BUY YES")
        if not in_bracket and yes_price > (1 - CRYPTO_MISPRICING):
            return make_crypto_opp("kalshi", coin, f"{title} — {subtitle}", ticker, spot, low, yes_price, "BUY NO")
    return None

def identify_poly_coin(title):
    t = title.upper()
    for coin, kws in {"BTC": ["BITCOIN","BTC"], "ETH": ["ETHEREUM","ETH"], "SOL": ["SOLANA","SOL"]}.items():
        for kw in kws:
            if re.search(r'\b' + kw + r'\b', t):
                return coin
    return None

def extract_threshold(title):
    matches = re.findall(r'\$(\d[\d,]*(?:\.\d+)?)', title)
    return max(float(m.replace(",", "")) for m in matches) if matches else None

def evaluate_crypto_poly(market, spot, yes_ask, coin):
    title = market.get("title", market.get("question", ""))
    slug = market.get("slug", "")
    cid = market.get("condition_id") or market.get("conditionId", "")
    threshold = extract_threshold(title)
    if not threshold:
        return None
    
    # Only flag near-threshold markets
    if not is_near_threshold(spot, threshold):
        return None
    
    t_upper = title.upper()
    is_above = any(w in t_upper for w in ["ABOVE", "OVER", "EXCEED", "REACH", "HIT", "CROSS"])
    
    if is_above or not any(w in t_upper for w in ["BELOW", "UNDER", "DROP", "FALL"]):
        if spot > threshold and yes_ask < CRYPTO_MISPRICING:
            return make_crypto_opp("polymarket", coin, title, cid, spot, threshold, yes_ask, "BUY YES", slug)
        if spot < threshold and yes_ask > (1 - CRYPTO_MISPRICING):
            return make_crypto_opp("polymarket", coin, title, cid, spot, threshold, yes_ask, "BUY NO", slug)
    else:
        if spot < threshold and yes_ask < CRYPTO_MISPRICING:
            return make_crypto_opp("polymarket", coin, title, cid, spot, threshold, yes_ask, "BUY YES", slug)
        if spot > threshold and yes_ask > (1 - CRYPTO_MISPRICING):
            return make_crypto_opp("polymarket", coin, title, cid, spot, threshold, yes_ask, "BUY NO", slug)
    return None

def make_crypto_opp(source, coin, market, ticker, spot, threshold, prediction, side, slug=""):
    url = f"https://kalshi.com/markets/{ticker}" if source == "kalshi" else f"https://polymarket.com/event/{slug}" if slug else ""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "arb_type": f"crypto_spot_lag",
        "source": source,
        "coin": coin,
        "market": market[:200],
        "ticker": ticker,
        "spot": spot,
        "threshold": threshold,
        "poly_price": prediction if source == "polymarket" else 0,
        "kalshi_price": prediction if source == "kalshi" else 0,
        "strategy": f"{side} ({coin} ${spot:,.0f} vs ${threshold:,.0f})",
        "profit_cents": abs(1.0 - prediction) * 100 if side == "BUY YES" else prediction * 100,
        "url": url,
    }

# ─── Global State ────────────────────────────────────────────────────────
state = {
    "status": "starting",
    "last_scan": None,
    "scan_count": 0,
    "spot_prices": {},
    "poly_count": 0,
    "kalshi_count": 0,
    "crypto_poly": 0,
    "crypto_kalshi": 0,
    "matched_pairs": 0,
    "opportunities": [],
    "errors": [],
    "uptime": datetime.now(timezone.utc).isoformat(),
}

app = FastAPI(title="Unified Arbitrage Engine v3.0")

# ─── Dashboard ───────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Unified Arb Engine v3.0</title>
    <meta http-equiv="refresh" content="15">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'SF Mono', monospace; background: #0d1117; color: #c9d1d9; padding: 20px; }
        .container { max-width: 1400px; margin: 0 auto; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #30363d; padding-bottom: 15px; margin-bottom: 25px; }
        .header h1 { font-size: 1.4em; color: #58a6ff; }
        .status { background: #238636; color: white; padding: 5px 12px; border-radius: 20px; font-size: 0.8em; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 25px; }
        .card { background: #161b22; padding: 14px; border-radius: 6px; border: 1px solid #30363d; }
        .card h3 { color: #8b949e; font-size: 0.7em; text-transform: uppercase; margin-bottom: 6px; }
        .number { font-size: 1.7em; font-weight: bold; color: #f0f6fc; }
        .sub { color: #3fb950; font-size: 0.8em; margin-top: 3px; }
        .section { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 15px; margin-bottom: 20px; }
        .section-title { color: #f0f6fc; border-bottom: 1px solid #21262d; padding-bottom: 8px; margin-bottom: 12px; font-size: 0.9em; }
        table { width: 100%; border-collapse: collapse; font-size: 0.8em; }
        th { text-align: left; padding: 8px; color: #8b949e; border-bottom: 1px solid #30363d; }
        td { padding: 8px; border-bottom: 1px solid #21262d; }
        tr:hover { background: #1c2128; }
        .tag { padding: 2px 6px; border-radius: 3px; font-size: 0.7em; }
        .tag-cross { background: #1f6feb; color: white; }
        .tag-internal { background: #8957e5; color: white; }
        .tag-crypto { background: #f0883e; color: white; }
        .tag-multi { background: #a371f7; color: white; }
        .tag-logical { background: #da3633; color: white; }
        .profit { color: #3fb950; font-weight: bold; }
        a { color: #58a6ff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .spot { display: flex; gap: 20px; font-size: 0.85em; margin-bottom: 15px; }
        .spot-item { background: #21262d; padding: 8px 12px; border-radius: 4px; }
        .spot-coin { color: #f0883e; font-weight: bold; }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>⚡ Unified Arb Engine <span style="font-size:0.6em;color:#8b949e">v3.0</span></h1>
        <span class="status" id="status">LOADING</span>
    </div>
    <div class="spot" id="spot-prices"></div>
    <div class="grid" id="stats"></div>
    <div class="section">
        <div class="section-title">OPPORTUNITY FEED</div>
        <table>
            <thead><tr><th>Time</th><th>Type</th><th>Market</th><th>Strategy</th><th>Profit</th><th>Link</th></tr></thead>
            <tbody id="opportunities"></tbody>
        </table>
    </div>
</div>
<script>
const evtSource = new EventSource("/stream");
evtSource.onmessage = (e) => {
    const d = JSON.parse(e.data);
    document.getElementById('status').textContent = d.status?.toUpperCase() || 'RUNNING';
    
    // Spot prices
    const sp = d.spot_prices || {};
    document.getElementById('spot-prices').innerHTML = Object.entries(sp).map(([c,p]) => 
        `<div class="spot-item"><span class="spot-coin">${c}</span> $${p.toLocaleString()}</div>`
    ).join('');
    
    // Stats grid
    document.getElementById('stats').innerHTML = `
        <div class="card"><h3>Polymarket</h3><div class="number">${d.poly_count}</div><div class="sub">Markets</div></div>
        <div class="card"><h3>Kalshi</h3><div class="number">${d.kalshi_count}</div><div class="sub">Markets</div></div>
        <div class="card"><h3>Cross-Matched</h3><div class="number">${d.matched_pairs}</div><div class="sub">Pairs</div></div>
        <div class="card"><h3>Crypto Markets</h3><div class="number">${(d.crypto_poly||0)+(d.crypto_kalshi||0)}</div><div class="sub">P:${d.crypto_poly||0} K:${d.crypto_kalshi||0}</div></div>
        <div class="card"><h3>Last Scan</h3><div class="number">${d.last_scan || '--'}</div><div class="sub">Scan #${d.scan_count}</div></div>
        <div class="card"><h3>Session Opps</h3><div class="number">${d.opportunities?.length || 0}</div><div class="sub">Detected</div></div>
    `;
    
    // Opportunities table
    const opps = d.opportunities?.slice(-30).reverse() || [];
    document.getElementById('opportunities').innerHTML = opps.map(o => {
        const t = o.arb_type || '';
        let tag = 'internal';
        if (t.includes('cross')) tag = 'cross';
        else if (t.includes('crypto')) tag = 'crypto';
        else if (t.includes('multi')) tag = 'multi';
        else if (t.includes('logical')) tag = 'logical';
        const ts = o.timestamp ? new Date(o.timestamp).toLocaleTimeString() : '--';
        return `<tr>
            <td>${ts}</td>
            <td><span class="tag tag-${tag}">${t}</span></td>
            <td>${(o.market||'').substring(0,60)}</td>
            <td>${(o.strategy||'').substring(0,50)}</td>
            <td class="profit">+${(o.profit_cents||0).toFixed(1)}¢</td>
            <td>${o.url ? `<a href="${o.url}" target="_blank">↗</a>` : ''}</td>
        </tr>`;
    }).join('');
};
</script>
</body>
</html>"""

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

@app.get("/api/state")
async def api_state():
    return state

# ─── Core Scanning Loop ──────────────────────────────────────────────────
async def scan_loop():
    p = lambda msg: print(msg, flush=True)
    p("🚀 Unified Arb Engine v3.0 starting...")
    p(f"   Poll: {POLL_INTERVAL}s | Min profit: {MIN_PROFIT_CENTS}¢")
    
    detector = ArbitrageDetector(MIN_PROFIT_CENTS, LOG_FILE)
    
    async with KalshiClient(KALSHI_API_KEY, KALSHI_PRIVATE_KEY_PATH) as kalshi, \
               PolymarketClient() as poly, \
               CryptoClient() as crypto:
        
        state["status"] = "running"
        
        while True:
            t0 = datetime.now(timezone.utc)
            state["scan_count"] += 1
            state["last_scan"] = t0.strftime("%H:%M:%S")
            found = []
            
            try:
                # 1. Fetch spot prices
                spot = await crypto.get_all_prices()
                state["spot_prices"] = spot
                p(f"[{t0.strftime('%H:%M:%S')}] Scan #{state['scan_count']} — BTC ${spot.get('BTC',0):,.0f} | ETH ${spot.get('ETH',0):,.0f} | SOL ${spot.get('SOL',0):,.0f}")
                
                # 2. Fetch Polymarket (non-sports, high volume)
                poly_markets = await poly.get_markets(limit=200)
                state["poly_count"] = len(poly_markets)
                
                # 3. Fetch Kalshi non-sports
                kalshi_markets = await kalshi.get_non_sports_markets(limit=200)
                state["kalshi_count"] = len(kalshi_markets)
                
                # 4. Fetch Kalshi crypto markets
                crypto_kalshi = []
                for series in KALSHI_CRYPTO_SERIES:
                    path = f"/markets?series_ticker={series}&status=open&limit=100"
                    headers = kalshi._get_headers("GET", path)
                    data = await kalshi._request_with_retry("GET", f"{kalshi.BASE_URL}{path}", headers)
                    if data:
                        crypto_kalshi.extend(data.get("markets", []))
                    await asyncio.sleep(0.2)
                state["crypto_kalshi"] = len(crypto_kalshi)
                
                # 5. Identify Polymarket crypto markets
                crypto_poly = [m for m in poly_markets if identify_poly_coin(m.get("title", ""))]
                state["crypto_poly"] = len(crypto_poly)
                
                p(f"  ├─ Markets: {len(poly_markets)} Poly | {len(kalshi_markets)} Kalshi | {len(crypto_poly)}+{len(crypto_kalshi)} crypto")
                
                # ═══════════════════════════════════════════════════════════
                # ARBITRAGE TYPE 1: Cross-exchange (Poly vs Kalshi)
                # ═══════════════════════════════════════════════════════════
                matched = detector.get_matched_pairs(poly_markets, kalshi_markets)
                state["matched_pairs"] = len(matched)
                
                for i, (poly_m, kalshi_m) in enumerate(matched[:30]):  # Cap API calls
                    poly_ob = await poly.get_market_orderbooks(poly_m)
                    await asyncio.sleep(0.15)  # Throttle Kalshi requests
                    kalshi_ob = await kalshi.get_orderbook(kalshi_m.get('ticker', ''))
                    
                    opps = detector.detect_arbitrage_with_orderbooks(poly_m, kalshi_m, poly_ob, kalshi_ob)
                    for o in opps:
                        opp_dict = {
                            "timestamp": o.timestamp,
                            "arb_type": o.arb_type,
                            "market": o.market_pair,
                            "strategy": o.strategy,
                            "profit_cents": o.profit_cents,
                            "poly_price": o.poly_price,
                            "kalshi_price": o.kalshi_price,
                            "url": "",
                        }
                        found.append(opp_dict)
                    await asyncio.sleep(0.15)  # ~3 req/s to Kalshi
                
                # ═══════════════════════════════════════════════════════════
                # ARBITRAGE TYPE 2: Multi-outcome (sum YES < 1.0)
                # ═══════════════════════════════════════════════════════════
                for poly_m in poly_markets[:80]:
                    tokens = poly_m.get('tokens', [])
                    if len(tokens) >= 3:
                        prices = await poly.get_multi_outcome_prices(poly_m)
                        if len(prices) >= 3:
                            mo = detector.detect_multi_outcome_arbitrage(poly_m, prices, "polymarket")
                            if mo:
                                found.append({
                                    "timestamp": mo.timestamp,
                                    "arb_type": mo.arb_type,
                                    "market": mo.market_pair,
                                    "strategy": mo.strategy,
                                    "profit_cents": mo.profit_cents,
                                    "poly_price": mo.poly_price,
                                    "kalshi_price": mo.kalshi_price,
                                    "url": "",
                                })
                
                # ═══════════════════════════════════════════════════════════
                # ARBITRAGE TYPE 3: Crypto spot-lag (Kalshi)
                # ═══════════════════════════════════════════════════════════
                crypto_checked = 0
                for market in crypto_kalshi:
                    ticker = market.get("ticker", "")
                    coin = coin_from_ticker(ticker)
                    if not coin or coin not in spot:
                        continue
                    
                    # Use yes_ask/no_ask from market data if available (avoids orderbook call)
                    yes_ask_raw = market.get("yes_ask")
                    if yes_ask_raw and yes_ask_raw > 0:
                        yes_ask = yes_ask_raw / 100.0
                    else:
                        # Fall back to orderbook (rate-limited)
                        if crypto_checked >= 50:  # Cap orderbook fetches
                            continue
                        ob = await kalshi.get_orderbook(ticker)
                        crypto_checked += 1
                        if not ob:
                            continue
                        no_bids = ob.get("no", [])
                        if no_bids:
                            best_no = no_bids[-1][0] if isinstance(no_bids[-1], list) else no_bids[-1]
                            yes_ask = (100 - best_no) / 100.0
                        else:
                            continue
                        await asyncio.sleep(0.2)  # Throttle orderbook calls
                    
                    opp = evaluate_crypto_kalshi(market, spot[coin], yes_ask)
                    if opp:
                        found.append(opp)
                        p(f"  🎯 [CRYPTO-K] {opp['strategy']}")
                
                # ═══════════════════════════════════════════════════════════
                # ARBITRAGE TYPE 4: Crypto spot-lag (Polymarket)
                # ═══════════════════════════════════════════════════════════
                for market in crypto_poly:
                    coin = identify_poly_coin(market.get("title", ""))
                    if not coin or coin not in spot:
                        continue
                    tokens = market.get("tokens", [])
                    if not tokens:
                        continue
                    
                    _, ask = await poly.get_best_prices(tokens[0].get("token_id", ""))
                    if not ask:
                        continue
                    
                    opp = evaluate_crypto_poly(market, spot[coin], ask, coin)
                    if opp:
                        found.append(opp)
                        p(f"  🎯 [CRYPTO-P] {opp['strategy']}")
                
                # ═══════════════════════════════════════════════════════════
                # Log + update state
                # ═══════════════════════════════════════════════════════════
                for opp in found:
                    log_opportunity(opp)
                    state["opportunities"].append(opp)
                    if len(state["opportunities"]) > 200:
                        state["opportunities"] = state["opportunities"][-200:]
                
                log_scan_stats(len(poly_markets), len(kalshi_markets), len(crypto_poly), len(crypto_kalshi), len(matched), len(found))
                
                if found:
                    p(f"  └─ Found {len(found)} opportunities")
                else:
                    p(f"  └─ No arb detected ({len(matched)} matched, {len(crypto_kalshi)+len(crypto_poly)} crypto)")
                
            except Exception as e:
                state["errors"].append(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {e}")
                if len(state["errors"]) > 20:
                    state["errors"] = state["errors"][-20:]
                p(f"  ❌ {e}")
                import traceback; traceback.print_exc()
            
            elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
            await asyncio.sleep(max(1, POLL_INTERVAL - elapsed))

@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(scan_loop())

if __name__ == "__main__":
    print("=" * 70, flush=True)
    print("⚡ UNIFIED ARBITRAGE ENGINE v3.0", flush=True)
    print("=" * 70, flush=True)
    print(f"  Dashboard: http://localhost:8000", flush=True)
    print(f"  Poll: {POLL_INTERVAL}s | Min profit: {MIN_PROFIT_CENTS}¢", flush=True)
    print("=" * 70, flush=True)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
