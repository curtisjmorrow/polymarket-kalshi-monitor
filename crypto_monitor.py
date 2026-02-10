"""Crypto Temporal Arbitrage Monitor with Dashboard.

Detects spot-lag arbitrage: crypto spot price has moved past a prediction
market threshold but the market odds haven't caught up yet.

Polls every 5 seconds. Dashboard at http://localhost:8081
"""
import asyncio
import json
import os
import re
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

load_dotenv()

KALSHI_API_KEY = os.getenv("KALSHI_API_KEY")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi.pem")
POLL_INTERVAL = 5
MISPRICING_THRESHOLD = 0.30  # Market must be this far from correct

KALSHI_CRYPTO_SERIES = ["KXBTC", "KXETH", "KXSOL"]

# ─── Global state ────────────────────────────────────────────────────────
state = {
    "status": "starting",
    "last_scan": None,
    "scan_count": 0,
    "spot_prices": {},
    "poly_crypto_count": 0,
    "kalshi_crypto_count": 0,
    "total_markets_scanned": 0,
    "opportunities": [],
    "recent_markets": [],
    "errors": [],
    "uptime_start": datetime.now(timezone.utc).isoformat(),
}

app = FastAPI(title="Crypto Temporal Arbitrage Monitor")

# Load dashboard HTML from file
DASHBOARD_PATH = Path(__file__).parent / "dashboard_crypto.html"


# ─── Kalshi Crypto Helpers ───────────────────────────────────────────────
def parse_kalshi_subtitle(subtitle):
    """Parse '$77,500 or above' / '$58,499.99 or below' / '$76,500 to 76,999.99'."""
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
    if ticker.startswith("KXBTC"):
        return "BTC"
    elif ticker.startswith("KXETH"):
        return "ETH"
    elif ticker.startswith("KXSOL"):
        return "SOL"
    return None


def evaluate_kalshi_arb(market, spot, yes_price):
    """Check a Kalshi crypto market for spot-lag arbitrage."""
    ticker = market.get("ticker", "")
    coin = coin_from_ticker(ticker)
    if not coin:
        return None

    subtitle = market.get("subtitle", "")
    parsed = parse_kalshi_subtitle(subtitle)
    if not parsed:
        return None

    title = market.get("title", "")
    full_label = f"{title} — {subtitle}"
    direction = parsed["direction"]

    if direction == "above":
        threshold = parsed["threshold"]
        if spot >= threshold and yes_price < MISPRICING_THRESHOLD:
            return _make_opp("kalshi", coin, full_label, ticker, spot, threshold, yes_price, "BUY YES",
                f'Buy YES "{subtitle}" @ {yes_price*100:.1f}¢. {coin} spot ${spot:,.2f} ≥ ${threshold:,.0f}. Should be ~100¢.')
        if spot < threshold and yes_price > (1 - MISPRICING_THRESHOLD):
            return _make_opp("kalshi", coin, full_label, ticker, spot, threshold, yes_price, "BUY NO",
                f'Buy NO "{subtitle}" @ {(1-yes_price)*100:.1f}¢. {coin} spot ${spot:,.2f} < ${threshold:,.0f}. YES should be ~0¢.')

    elif direction == "below":
        threshold = parsed["threshold"]
        if spot <= threshold and yes_price < MISPRICING_THRESHOLD:
            return _make_opp("kalshi", coin, full_label, ticker, spot, threshold, yes_price, "BUY YES",
                f'Buy YES "{subtitle}" @ {yes_price*100:.1f}¢. {coin} spot ${spot:,.2f} ≤ ${threshold:,.0f}. Should be ~100¢.')
        if spot > threshold and yes_price > (1 - MISPRICING_THRESHOLD):
            return _make_opp("kalshi", coin, full_label, ticker, spot, threshold, yes_price, "BUY NO",
                f'Buy NO "{subtitle}" @ {(1-yes_price)*100:.1f}¢. {coin} spot ${spot:,.2f} > ${threshold:,.0f}. YES should be ~0¢.')

    elif direction == "bracket":
        low, high = parsed["low"], parsed["high"]
        in_bracket = low <= spot <= high
        if in_bracket and yes_price < MISPRICING_THRESHOLD:
            return _make_opp("kalshi", coin, full_label, ticker, spot, low, yes_price, "BUY YES",
                f'Buy YES "{subtitle}" @ {yes_price*100:.1f}¢. {coin} ${spot:,.2f} is inside ${low:,.0f}-${high:,.0f}.')
        if not in_bracket and yes_price > (1 - MISPRICING_THRESHOLD):
            return _make_opp("kalshi", coin, full_label, ticker, spot, low, yes_price, "BUY NO",
                f'Buy NO "{subtitle}" @ {(1-yes_price)*100:.1f}¢. {coin} ${spot:,.2f} is outside ${low:,.0f}-${high:,.0f}.')

    return None


def _make_opp(source, coin, market, ticker, spot, threshold, prediction, side, detail_text, slug=""):
    action_emoji = "📈" if side == "BUY YES" else "📉"
    if source == "kalshi":
        url = f"https://kalshi.com/markets/{ticker}"
    elif source == "polymarket" and slug:
        url = f"https://polymarket.com/event/{slug}"
    else:
        url = ""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "coin": coin,
        "market": market,
        "ticker": ticker,
        "spot": spot,
        "threshold": threshold,
        "prediction": prediction,
        "side": side,
        "trade_action": f"{action_emoji} {side} on {source.capitalize()}",
        "trade_detail": detail_text,
        "reason": f'{coin} ${spot:,.2f} vs threshold ${threshold:,.0f}, market at {prediction*100:.1f}%',
        "url": url,
    }


# ─── Polymarket Crypto Helpers ───────────────────────────────────────────
def identify_coin(title):
    t = title.upper()
    for coin, keywords in {"BTC": ["BITCOIN","BTC"], "ETH": ["ETHEREUM","ETH"], "SOL": ["SOLANA","SOL"]}.items():
        for kw in keywords:
            if re.search(r'\b' + kw + r'\b', t):
                return coin
    return None


def extract_threshold(title):
    matches = re.findall(r'\$(\d[\d,]*(?:\.\d+)?)', title)
    if matches:
        return max(float(m.replace(",", "")) for m in matches)
    return None


def check_poly_arb(market, spot, yes_ask, coin):
    title = market.get("title", market.get("question", ""))
    slug = market.get("slug", "")
    cid = market.get("conditionId", market.get("condition_id", ""))
    threshold = extract_threshold(title)
    if not threshold:
        return None

    t_upper = title.upper()
    is_above = any(w in t_upper for w in ["ABOVE", "OVER", "EXCEED", "REACH", "HIT", "CROSS"])

    if is_above or not any(w in t_upper for w in ["BELOW", "UNDER", "DROP", "FALL"]):
        if spot > threshold and yes_ask < MISPRICING_THRESHOLD:
            return _make_opp("polymarket", coin, title, cid, spot, threshold, yes_ask, "BUY YES",
                f'Buy YES on "{title}" @ {yes_ask*100:.1f}¢. {coin} spot ${spot:,.2f} > ${threshold:,.0f}. Should be ~100¢.', slug)
        if spot < threshold and yes_ask > (1 - MISPRICING_THRESHOLD):
            return _make_opp("polymarket", coin, title, cid, spot, threshold, yes_ask, "BUY NO",
                f'Buy NO on "{title}" @ {(1-yes_ask)*100:.1f}¢. {coin} spot ${spot:,.2f} < ${threshold:,.0f}. YES should be ~0¢.', slug)
    else:
        if spot < threshold and yes_ask < MISPRICING_THRESHOLD:
            return _make_opp("polymarket", coin, title, cid, spot, threshold, yes_ask, "BUY YES",
                f'Buy YES on "{title}" @ {yes_ask*100:.1f}¢. {coin} spot ${spot:,.2f} < ${threshold:,.0f}. Should be ~100¢.', slug)
        if spot > threshold and yes_ask > (1 - MISPRICING_THRESHOLD):
            return _make_opp("polymarket", coin, title, cid, spot, threshold, yes_ask, "BUY NO",
                f'Buy NO on "{title}" @ {(1-yes_ask)*100:.1f}¢. {coin} spot ${spot:,.2f} > ${threshold:,.0f}. YES should be ~0¢.', slug)

    return None


# ─── CSV Logger ──────────────────────────────────────────────────────────
LOG_FILE = "crypto_temporal_arb.csv"

def log_to_csv(opp):
    needs_header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a") as f:
        if needs_header:
            f.write("timestamp,source,coin,market,ticker,spot,threshold,prediction,side,trade_action,reason\n")
        f.write(f'{opp["timestamp"]},{opp["source"]},{opp["coin"]},"{opp["market"]}",{opp["ticker"]},{opp["spot"]},{opp["threshold"]},{opp["prediction"]},{opp["side"]},"{opp["trade_action"]}","{opp["reason"]}"\n')


# ─── FastAPI Routes ──────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_PATH.read_text())

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


# ─── Core Monitoring Loop ───────────────────────────────────────────────
async def monitoring_loop():
    detector_print = lambda msg: print(msg, flush=True)

    detector_print("🚀 Crypto Temporal Arbitrage Monitor starting...")
    detector_print(f"   Poll interval: {POLL_INTERVAL}s")
    detector_print(f"   Dashboard: http://localhost:8081")

    async with KalshiClient(KALSHI_API_KEY, KALSHI_PRIVATE_KEY_PATH) as kalshi, \
               PolymarketClient() as poly, \
               CryptoClient() as crypto:

        state["status"] = "running"

        while True:
            t0 = datetime.now(timezone.utc)
            state["scan_count"] += 1
            state["last_scan"] = t0.strftime("%H:%M:%S")

            try:
                # 1. Spot prices
                spot_prices = await crypto.get_all_prices()
                state["spot_prices"] = spot_prices
                detector_print(f"[{t0.strftime('%H:%M:%S')}] Scan #{state['scan_count']} — "
                    f"BTC ${spot_prices.get('BTC',0):,.0f} | ETH ${spot_prices.get('ETH',0):,.0f} | SOL ${spot_prices.get('SOL',0):,.0f}")

                # 2. Kalshi crypto markets (targeted by series ticker)
                kalshi_markets = []
                recent_markets = []
                for series in KALSHI_CRYPTO_SERIES:
                    path = f"/markets?series_ticker={series}&status=open&limit=100"
                    headers = kalshi._get_headers("GET", path)
                    data = await kalshi._request_with_retry("GET", f"{kalshi.BASE_URL}{path}", headers)
                    markets = data.get("markets", []) if data else []
                    kalshi_markets.extend(markets)
                    for m in markets[:5]:
                        recent_markets.append({"source": "kalshi", "title": m.get("subtitle",""), "ticker": m.get("ticker","")})
                    await asyncio.sleep(0.3)  # Rate limiting between series

                state["kalshi_crypto_count"] = len(kalshi_markets)

                # 3. Polymarket crypto markets
                all_poly = await poly.get_markets(limit=100)
                crypto_poly = [m for m in all_poly if identify_coin(m.get("title", ""))]
                state["poly_crypto_count"] = len(crypto_poly)
                for m in crypto_poly[:5]:
                    recent_markets.append({"source": "poly", "title": m.get("title","")[:80], "ticker": m.get("condition_id","")[:20]})

                state["total_markets_scanned"] = len(kalshi_markets) + len(crypto_poly)
                state["recent_markets"] = recent_markets[:20]

                found = 0

                # 4. Check Kalshi crypto markets
                for market in kalshi_markets:
                    ticker = market.get("ticker", "")
                    coin = coin_from_ticker(ticker)
                    if not coin or coin not in spot_prices:
                        continue

                    # Fetch orderbook to get YES price
                    orderbook = await kalshi.get_orderbook(ticker)
                    if not orderbook:
                        continue

                    # Calculate YES ask: 100 - best NO bid
                    no_bids = orderbook.get("no", [])
                    yes_bids = orderbook.get("yes", [])
                    if no_bids:
                        best_no_bid = no_bids[-1][0] if isinstance(no_bids[-1], list) else no_bids[-1]
                        yes_ask = (100 - best_no_bid) / 100.0
                    elif yes_bids:
                        best_yes_bid = yes_bids[0][0] if isinstance(yes_bids[0], list) else yes_bids[0]
                        yes_ask = best_yes_bid / 100.0
                    else:
                        continue

                    opp = evaluate_kalshi_arb(market, spot_prices[coin], yes_ask)
                    if opp:
                        log_to_csv(opp)
                        state["opportunities"].append(opp)
                        if len(state["opportunities"]) > 500:
                            state["opportunities"] = state["opportunities"][-500:]
                        found += 1
                        detector_print(f"  🎯 [KALSHI] {opp['trade_action']}: {opp['reason']}")

                    await asyncio.sleep(0.1)  # Rate limit orderbook calls

                # 5. Check Polymarket crypto markets
                for market in crypto_poly:
                    coin = identify_coin(market.get("title", ""))
                    if not coin or coin not in spot_prices:
                        continue

                    tokens = market.get("tokens", [])
                    if not tokens:
                        continue

                    _, ask = await poly.get_best_prices(tokens[0].get("token_id", ""))
                    if not ask:
                        continue

                    opp = check_poly_arb(market, spot_prices[coin], ask, coin)
                    if opp:
                        log_to_csv(opp)
                        state["opportunities"].append(opp)
                        if len(state["opportunities"]) > 500:
                            state["opportunities"] = state["opportunities"][-500:]
                        found += 1
                        detector_print(f"  🎯 [POLY] {opp['trade_action']}: {opp['reason']}")

                if not found:
                    detector_print(f"  No opportunities this scan ({len(kalshi_markets)} Kalshi + {len(crypto_poly)} Poly markets)")

            except Exception as e:
                err_msg = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {e}"
                state["errors"].append(err_msg)
                if len(state["errors"]) > 20:
                    state["errors"] = state["errors"][-20:]
                state["status"] = "error"
                detector_print(f"  ❌ {e}")
                import traceback; traceback.print_exc()

            elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
            await asyncio.sleep(max(0.5, POLL_INTERVAL - elapsed))
            if state["status"] == "error":
                state["status"] = "running"


@app.on_event("startup")
async def startup():
    asyncio.create_task(monitoring_loop())


if __name__ == "__main__":
    print("=" * 70, flush=True)
    print("⚙️  CRYPTO TEMPORAL ARBITRAGE MONITOR", flush=True)
    print("=" * 70, flush=True)
    print(f"  Dashboard: http://localhost:8081", flush=True)
    print(f"  Poll interval: {POLL_INTERVAL}s", flush=True)
    print(f"  Mispricing threshold: {MISPRICING_THRESHOLD*100:.0f}%", flush=True)
    print("=" * 70, flush=True)
    uvicorn.run(app, host="0.0.0.0", port=8081, log_level="warning")
