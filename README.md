# Polymarket-Kalshi Arbitrage Monitor

Real-time arbitrage detection system for Polymarket and Kalshi prediction markets.

## Features

- **Real-time monitoring** of Polymarket (Gamma API) and Kalshi (REST API)
- **Web dashboard** with live stats and opportunity feed
- **CSV logging** of all detected arbitrage opportunities
- **Dual mode**: Monitor-only (current) or execution-enabled (future)

## Architecture

- **Python 3.12** with asyncio
- **FastAPI** web server + SSE for real-time updates
- **Kalshi API** with RSA request signing
- **Polymarket Gamma API** (public data)

## Quick Start

### 1. Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Credentials

Copy `.env.example` to `.env` and add your credentials:

```bash
KALSHI_API_KEY=your_api_key_here
KALSHI_PRIVATE_KEY_PATH=kalshi.pem
POLL_INTERVAL_SECONDS=10
MIN_PROFIT_CENTS=1.0
LOG_FILE=arb_opportunities.csv
```

Place your Kalshi RSA private key in `kalshi.pem`.

### 3. Run the Monitor

```bash
python main.py
```

Dashboard will be available at: **http://localhost:8080**

## How It Works

### Arbitrage Detection

The bot looks for price discrepancies between platforms:

**Strategy 1:** Buy YES on Polymarket + Buy NO on Kalshi  
**Strategy 2:** Buy YES on Kalshi + Buy NO on Polymarket

Arbitrage exists when: `YES_ask + NO_ask < $1.00`

### Example

- Polymarket YES ask: $0.42
- Kalshi NO ask: $0.56
- **Total cost:** $0.98
- **Guaranteed payout:** $1.00
- **Profit:** 2¢ per contract

### Market Matching

Markets are matched using keyword overlap (Jaccard similarity threshold: 30%).

## Dashboard

The web dashboard shows:

- **Live stats**: Market counts, matched pairs, opportunities found
- **Opportunity feed**: Last 20 detected arbitrage opportunities with:
  - Timestamp
  - Market pair
  - Strategy (poly_yes_kalshi_no or kalshi_yes_poly_no)
  - Prices and profit
- **Auto-updates** every 2 seconds via Server-Sent Events

## CSV Output

All detected opportunities are logged to `arb_opportunities.csv`:

```csv
timestamp,market_pair,polymarket_market,kalshi_market,strategy,poly_price,kalshi_price,total_cost,profit_cents,poly_market_id,kalshi_ticker
```

## Configuration

Edit `.env` to adjust:

- `POLL_INTERVAL_SECONDS` - How often to scan (default: 10)
- `MIN_PROFIT_CENTS` - Minimum profit threshold (default: 1.0¢)
- `LOG_FILE` - CSV output path
- `EXECUTION_ENABLED` - Enable trade execution (default: false)

## Future Enhancements

### Execution Mode

To enable automated trading:

1. Add your Polymarket private key to `.env`:
   ```
   POLYMARKET_PRIVATE_KEY=0xYOUR_PRIVATE_KEY
   ```

2. Set execution flag:
   ```
   EXECUTION_ENABLED=true
   ```

3. Restart the monitor

### Speed Optimizations

- WebSocket clients for real-time orderbook updates
- Parallel orderbook fetching
- Redis caching for market data

### Improved Matching

- Fuzzy string matching (Levenshtein distance)
- Manual market pair mapping file
- ML-based market similarity

## Project Structure

```
polymarket-kalshi-monitor/
├── main.py                  # FastAPI server + monitoring loop
├── kalshi_client.py         # Kalshi API client (RSA signing)
├── polymarket_client.py     # Polymarket Gamma API client
├── arbitrage_detector.py    # Core detection logic
├── requirements.txt         # Python dependencies
├── .env.example            # Configuration template
├── .gitignore              # Git ignore patterns
└── README.md               # This file
```

## License

MIT

## Disclaimer

This software is for educational purposes only. Arbitrage trading carries risk. Use at your own discretion.
