# Polymarket-Kalshi Arbitrage Monitor

Real-time arbitrage opportunity detection between Polymarket and Kalshi prediction markets.

## Features

- **Live Dashboard** - Real-time web UI showing detected opportunities
- **CSV Logging** - Timestamped record of every arbitrage opportunity
- **Dual API Support** - Polymarket (Gamma API) + Kalshi (REST with RSA signing)
- **Fast Polling** - Configurable scan interval (default: 10 seconds)
- **Monitor Mode** - Detection-only by default (no execution)

## Requirements

- Python 3.9+
- Kalshi API credentials (API key + RSA private key)
- Polymarket private key (optional, for future execution mode)

## Installation

```bash
# Clone the repository
git clone https://github.com/curtisjmorrow/polymarket-kalshi-monitor.git
cd polymarket-kalshi-monitor

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Edit `.env`:

```
KALSHI_API_KEY=your_kalshi_api_key
KALSHI_PRIVATE_KEY_PATH=kalshi.pem

POLL_INTERVAL_SECONDS=10
MIN_PROFIT_CENTS=1.0
LOG_FILE=arb_opportunities.csv

# Optional - for future execution mode
POLYMARKET_PRIVATE_KEY=
EXECUTION_ENABLED=false
```

Place your Kalshi RSA private key in `kalshi.pem`.

## Usage

Start the monitor:

```bash
python main.py
```

Access the dashboard at: **http://localhost:8080**

## How It Works

1. **Market Fetching**: Polls Polymarket (Gamma API) and Kalshi (REST API) every N seconds
2. **Market Matching**: Fuzzy matches markets by title similarity (30% keyword overlap threshold)
3. **Arbitrage Detection**: For each matched pair, calculates:
   - Strategy 1: Buy YES on Polymarket + Buy NO on Kalshi
   - Strategy 2: Buy YES on Kalshi + Buy NO on Polymarket
4. **Logging**: Any opportunity where `total_cost < $1.00` is logged to CSV

### Arbitrage Formula

```
Arbitrage exists when: YES_ask + NO_ask < $1.00
Profit = $1.00 - (YES_ask + NO_ask)
```

## Output

### CSV Log (`arb_opportunities.csv`)

```csv
timestamp,market_pair,polymarket_market,kalshi_market,strategy,poly_price,kalshi_price,total_cost,profit_cents,poly_market_id,kalshi_ticker
2026-02-04T14:23:15,Fed Rate Cut / FED-RATE-CUT,Will Fed cut rates?,Fed Rate Decision,poly_yes_kalshi_no,0.4200,0.5700,0.9900,1.00,0x1a2b3c...,FED-26FEB04-T4.25
```

### Dashboard

- **Live Stats**: Market counts, matched pairs, opportunities found
- **Real-time Feed**: Last 20 opportunities with prices and profit
- **Auto-refresh**: Updates every 2 seconds via Server-Sent Events

## Project Structure

```
polymarket-kalshi-monitor/
├── main.py                 # Entry point, FastAPI server, monitoring loop
├── kalshi_client.py        # Kalshi API client with RSA signing
├── polymarket_client.py    # Polymarket Gamma API client
├── arbitrage_detector.py   # Core detection logic
├── requirements.txt        # Python dependencies
├── .env.example           # Configuration template
└── README.md              # This file
```

## Future Enhancements

- [ ] WebSocket support for real-time orderbook updates
- [ ] Automatic order execution (when `EXECUTION_ENABLED=true`)
- [ ] Multi-platform support (add PredictIt, Manifold, etc.)
- [ ] Advanced market matching (Levenshtein distance)
- [ ] Telegram/Discord alerts for opportunities

## Security

- **Never commit** `.env`, `*.pem`, or `*.csv` files
- Store private keys securely
- Use read-only API keys when possible

## License

MIT
