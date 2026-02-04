"""Main monitoring loop and web dashboard."""
import asyncio
import os
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse
import uvicorn
import json

from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient
from arbitrage_detector import ArbitrageDetector

load_dotenv()

# Configuration
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi.pem")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))
MIN_PROFIT_CENTS = float(os.getenv("MIN_PROFIT_CENTS", "1.0"))
LOG_FILE = os.getenv("LOG_FILE", "arb_opportunities.csv")

# Global state
detector = ArbitrageDetector(MIN_PROFIT_CENTS, LOG_FILE)
latest_stats = {
    "last_scan": None,
    "poly_markets_count": 0,
    "kalshi_markets_count": 0,
    "matched_pairs": 0,
    "opportunities_found": 0,
    "total_logged": 0
}

app = FastAPI(title="Polymarket-Kalshi Arbitrage Monitor")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the main dashboard HTML."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Arbitrage Monitor</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'SF Mono', Monaco, monospace;
                background: #0a0a0a;
                color: #00ff41;
                padding: 20px;
            }
            .header {
                border-bottom: 2px solid #00ff41;
                padding-bottom: 20px;
                margin-bottom: 20px;
            }
            h1 {
                font-size: 24px;
                font-weight: 600;
                letter-spacing: -0.5px;
            }
            .stats {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 15px;
                margin-bottom: 30px;
            }
            .stat-card {
                background: #111;
                border: 1px solid #333;
                padding: 15px;
                border-radius: 4px;
            }
            .stat-label {
                color: #666;
                font-size: 12px;
                margin-bottom: 5px;
            }
            .stat-value {
                font-size: 24px;
                font-weight: 700;
            }
            .opportunities {
                background: #111;
                border: 1px solid #333;
                border-radius: 4px;
                padding: 20px;
            }
            .opp-header {
                font-size: 18px;
                margin-bottom: 15px;
                padding-bottom: 10px;
                border-bottom: 1px solid #333;
            }
            .opp-item {
                background: #0a0a0a;
                border: 1px solid #222;
                padding: 15px;
                margin-bottom: 10px;
                border-radius: 4px;
                border-left: 3px solid #00ff41;
            }
            .opp-time {
                color: #666;
                font-size: 11px;
                margin-bottom: 8px;
            }
            .opp-markets {
                font-size: 13px;
                margin-bottom: 8px;
                color: #00ff41;
            }
            .opp-details {
                display: grid;
                grid-template-columns: repeat(4, 1fr);
                gap: 10px;
                font-size: 12px;
                color: #888;
            }
            .opp-detail-value {
                color: #00ff41;
                font-weight: 600;
            }
            .profit-highlight {
                color: #ffaa00;
                font-size: 14px;
                font-weight: 700;
            }
            .no-opps {
                color: #666;
                text-align: center;
                padding: 40px;
                font-size: 14px;
            }
            .status-online {
                display: inline-block;
                width: 8px;
                height: 8px;
                background: #00ff41;
                border-radius: 50%;
                margin-right: 8px;
                animation: pulse 2s infinite;
            }
            @keyframes pulse {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.5; }
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>⚙️ Arbitrage Monitor <span class="status-online"></span></h1>
        </div>
        
        <div class="stats" id="stats">
            <div class="stat-card">
                <div class="stat-label">Last Scan</div>
                <div class="stat-value" id="last-scan">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Polymarket Markets</div>
                <div class="stat-value" id="poly-count">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Kalshi Markets</div>
                <div class="stat-value" id="kalshi-count">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Matched Pairs</div>
                <div class="stat-value" id="matched-count">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Opportunities (Session)</div>
                <div class="stat-value" id="opps-count">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Logged (CSV)</div>
                <div class="stat-value" id="total-logged">0</div>
            </div>
        </div>
        
        <div class="opportunities">
            <div class="opp-header">Live Opportunities</div>
            <div id="opportunities-list">
                <div class="no-opps">Waiting for data...</div>
            </div>
        </div>
        
        <script>
            const evtSource = new EventSource("/stream");
            
            evtSource.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                // Update stats
                if (data.stats) {
                    document.getElementById('last-scan').textContent = data.stats.last_scan || '--';
                    document.getElementById('poly-count').textContent = data.stats.poly_markets_count || 0;
                    document.getElementById('kalshi-count').textContent = data.stats.kalshi_markets_count || 0;
                    document.getElementById('matched-count').textContent = data.stats.matched_pairs || 0;
                    document.getElementById('opps-count').textContent = data.stats.opportunities_found || 0;
                    document.getElementById('total-logged').textContent = data.stats.total_logged || 0;
                }
                
                // Update opportunities list
                if (data.opportunities) {
                    const list = document.getElementById('opportunities-list');
                    
                    if (data.opportunities.length === 0) {
                        list.innerHTML = '<div class="no-opps">No arbitrage opportunities detected yet.</div>';
                    } else {
                        list.innerHTML = data.opportunities.map(opp => `
                            <div class="opp-item">
                                <div class="opp-time">${new Date(opp.timestamp).toLocaleString()} • Type: ${opp.arb_type || 'cross_exchange'}</div>
                                <div class="opp-markets">${opp.market_pair}</div>
                                <div class="opp-details">
                                    <div>Strategy: <span class="opp-detail-value">${opp.strategy}</span></div>
                                    <div>Poly: <span class="opp-detail-value">$${opp.poly_price.toFixed(4)}</span></div>
                                    <div>Kalshi: <span class="opp-detail-value">$${opp.kalshi_price.toFixed(4)}</span></div>
                                    <div>Profit: <span class="profit-highlight">+${opp.profit_cents.toFixed(2)}¢</span></div>
                                </div>
                            </div>
                        `).join('');
                    }
                }
            };
            
            evtSource.onerror = () => {
                console.error('SSE connection error');
            };
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/stream")
async def stream_updates():
    """SSE endpoint for live updates."""
    async def event_generator():
        while True:
            # Send current state
            data = {
                "stats": latest_stats,
                "opportunities": [
                    {
                        "timestamp": opp.timestamp,
                        "market_pair": opp.market_pair,
                        "strategy": opp.strategy,
                        "poly_price": opp.poly_price,
                        "kalshi_price": opp.kalshi_price,
                        "profit_cents": opp.profit_cents,
                        "arb_type": opp.arb_type
                    }
                    for opp in detector.opportunities[-20:]  # Last 20 opportunities
                ]
            }
            
            yield {
                "event": "message",
                "data": json.dumps(data)
            }
            
            await asyncio.sleep(2)  # Update every 2 seconds
    
    return EventSourceResponse(event_generator())


async def monitoring_loop():
    """Main monitoring loop with real orderbook data from both platforms."""
    print("🚀 Starting arbitrage monitoring loop...")
    print(f"   Polling interval: {POLL_INTERVAL}s")
    print(f"   Min profit threshold: {MIN_PROFIT_CENTS}¢")
    print(f"   Log file: {LOG_FILE}")
    print("   Using REAL orderbook asks (CLOB API for Polymarket)")
    print()
    
    async with KalshiClient(KALSHI_API_KEY, KALSHI_PRIVATE_KEY_PATH) as kalshi, \
               PolymarketClient() as poly:
        
        iteration = 0
        
        while True:
            iteration += 1
            scan_start = datetime.utcnow()
            
            try:
                print(f"[{scan_start.strftime('%H:%M:%S')}] Scan #{iteration}")
                
                # Fetch markets concurrently (Kalshi: non-sports only)
                # Polymarket: 300 req/10s limit on /markets, using 250 (83% capacity)
                # Kalshi: ~25 req/s peak load, on edge but with backoff safety
                poly_markets, kalshi_markets = await asyncio.gather(
                    poly.get_markets(limit=250),
                    kalshi.get_non_sports_markets(limit=250)
                )
                
                print(f"  ├─ Polymarket: {len(poly_markets)} markets")
                print(f"  ├─ Kalshi: {len(kalshi_markets)} markets")
                
                # Get matched pairs first
                matched_pairs = detector.get_matched_pairs(poly_markets, kalshi_markets)
                print(f"  ├─ Matched pairs: {len(matched_pairs)}")
                
                if not matched_pairs:
                    print(f"  └─ No matched markets to check")
                    latest_stats.update({
                        "last_scan": scan_start.strftime('%H:%M:%S'),
                        "poly_markets_count": len(poly_markets),
                        "kalshi_markets_count": len(kalshi_markets),
                        "matched_pairs": 0,
                        "opportunities_found": len(detector.opportunities),
                        "total_logged": len(detector.opportunities)
                    })
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                
                # Fetch orderbooks for matched pairs + check multi-outcome arbs
                all_opportunities = []
                orderbooks_fetched = 0
                
                # 1. Check matched pairs (cross-exchange + intra-exchange binary)
                for poly_market, kalshi_market in matched_pairs:
                    kalshi_ticker = kalshi_market.get('ticker', '')
                    
                    # Fetch both orderbooks concurrently
                    poly_orderbook, kalshi_orderbook = await asyncio.gather(
                        poly.get_market_orderbooks(poly_market),
                        kalshi.get_orderbook(kalshi_ticker)
                    )
                    
                    if poly_orderbook:
                        orderbooks_fetched += 1
                    
                    # Detect arbitrage with real prices (includes intra-exchange YES/NO spread)
                    opportunities = detector.detect_arbitrage_with_orderbooks(
                        poly_market,
                        kalshi_market,
                        poly_orderbook,
                        kalshi_orderbook
                    )
                    
                    all_opportunities.extend(opportunities)
                
                # 2. Check Polymarket multi-outcome markets (unmatched markets with 3+ outcomes)
                # CLOB /price limit: 1500 req/10s, using 100 for safety (leaves room for matched pairs)
                for poly_market in poly_markets[:100]:
                    tokens = poly_market.get('tokens', [])
                    if len(tokens) >= 3:  # Multi-outcome market
                        outcome_prices = await poly.get_multi_outcome_prices(poly_market)
                        if len(outcome_prices) >= 3:
                            multi_opp = detector.detect_multi_outcome_arbitrage(
                                poly_market,
                                outcome_prices,
                                "polymarket"
                            )
                            if multi_opp:
                                all_opportunities.append(multi_opp)
                
                # 3. Check for logical constraint violations (temporal superset, etc.)
                # Build price maps
                # CLOB /price limit: 1500 req/10s, can check ALL markets if needed
                poly_prices = {}
                for poly_market in poly_markets[:250]:  # Check all fetched markets
                    poly_id = poly_market.get('condition_id', '')
                    tokens = poly_market.get('tokens', [])
                    if len(tokens) >= 1:
                        # Use first token's price as market price (typically YES)
                        token_id = tokens[0].get('token_id', '')
                        if token_id:
                            _, ask = await poly.get_best_prices(token_id)
                            if ask:
                                poly_prices[poly_id] = ask
                
                kalshi_prices = {}
                for kalshi_market in kalshi_markets[:250]:  # Check all fetched markets
                    ticker = kalshi_market.get('ticker', '')
                    orderbook = await kalshi.get_orderbook(ticker)
                    if orderbook:
                        yes_bids = orderbook.get('yes', [])
                        no_bids = orderbook.get('no', [])
                        if yes_bids and no_bids:
                            best_no_bid = no_bids[-1][0] if no_bids else 0
                            yes_ask = (100 - best_no_bid) / 100.0
                            kalshi_prices[ticker] = yes_ask
                
                # Scan for temporal arbitrage on each platform
                poly_violations = detector.logical_detector.scan_for_temporal_arbitrage(
                    poly_markets[:250],  # All fetched markets
                    poly_prices,
                    "polymarket"
                )
                
                kalshi_violations = detector.logical_detector.scan_for_temporal_arbitrage(
                    kalshi_markets[:250],  # All fetched markets
                    kalshi_prices,
                    "kalshi"
                )
                
                # Convert violations to opportunities
                for violation in poly_violations:
                    opp = detector.convert_violation_to_opportunity(violation, "polymarket")
                    all_opportunities.append(opp)
                
                for violation in kalshi_violations:
                    opp = detector.convert_violation_to_opportunity(violation, "kalshi")
                    all_opportunities.append(opp)
                
                print(f"  ├─ Fetched {orderbooks_fetched} Poly + {len(matched_pairs)} Kalshi orderbooks")
                print(f"  ├─ Found {len(poly_violations)} Poly + {len(kalshi_violations)} Kalshi logical violations")
                
                # Log opportunities
                for opp in all_opportunities:
                    await detector.log_opportunity(opp)
                    print(f"  └─ 🎯 ARB FOUND: {opp.market_pair[:60]}...")
                    print(f"     Strategy: {opp.strategy}")
                    print(f"     Profit: +{opp.profit_cents:.2f}¢")
                
                if not all_opportunities:
                    print(f"  └─ No arbitrage opportunities detected")
                
                # Update global stats
                latest_stats.update({
                    "last_scan": scan_start.strftime('%H:%M:%S'),
                    "poly_markets_count": len(poly_markets),
                    "kalshi_markets_count": len(kalshi_markets),
                    "matched_pairs": len(matched_pairs),
                    "opportunities_found": len(detector.opportunities),
                    "total_logged": len(detector.opportunities)
                })
                
                print()
                
            except Exception as e:
                print(f"  └─ ❌ Error: {e}")
                import traceback
                traceback.print_exc()
                print()
            
            # Wait for next iteration
            await asyncio.sleep(POLL_INTERVAL)


@app.on_event("startup")
async def startup_event():
    """Start the monitoring loop when the server starts."""
    asyncio.create_task(monitoring_loop())


if __name__ == "__main__":
    import sys
    sys.stdout = sys.stderr  # Force all output to stderr for nohup
    
    print("=" * 80, flush=True)
    print("⚙️  POLYMARKET-KALSHI ARBITRAGE MONITOR", flush=True)
    print("=" * 80, flush=True)
    print(flush=True)
    print("Configuration:", flush=True)
    print(f"  • Kalshi API Key: {KALSHI_API_KEY[:20]}...", flush=True)
    print(f"  • Poll Interval: {POLL_INTERVAL}s", flush=True)
    print(f"  • Min Profit: {MIN_PROFIT_CENTS}¢", flush=True)
    print(f"  • Log File: {LOG_FILE}", flush=True)
    print(flush=True)
    print("Dashboard will be available at: http://localhost:8080", flush=True)
    print("=" * 80, flush=True)
    print(flush=True)
    
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
