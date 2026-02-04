# API Rate Limits & Usage

## Polymarket Rate Limits (per 10 seconds)

| Endpoint | Limit | Usage | Headroom |
|----------|-------|-------|----------|
| GAMMA /markets | 300 req/10s | 1 req/scan | 299x |
| CLOB /book | 1500 req/10s | ~30 req/scan | 50x |
| CLOB /price | 1500 req/10s | ~300 req/scan | 5x |

**Total Polymarket:** ~330 requests per 10s scan = **22% of available capacity**

### Current Implementation:
- **1** call to `/markets` (fetch 200 markets)
- **~30** calls to `/book` (matched pairs orderbooks)
- **~300** calls to `/price` (multi-outcome + logical constraint price checks)

### Throttling Behavior:
- Cloudflare throttles (queues) requests over limit
- Requests are delayed, not dropped
- Burst allowances supported

---

## Kalshi Rate Limits

| Endpoint | Estimated Limit | Usage | Notes |
|----------|----------------|-------|-------|
| /markets | ~10-20 req/s | 1 req/scan | Conservative |
| /orderbook | ~10-20 req/s | ~30 req/scan | Conservative |

**Total Kalshi:** ~180 requests per 10s scan

### Current Implementation:
- **1** call to `get_non_sports_markets()` (150 markets via events API)
- **~30** calls to `/orderbook` (matched pairs)
- **~150** calls to `/orderbook` (logical constraint price checks)

### Known Constraints:
- No public rate limit documentation
- Tokens expire every 30 minutes (handled by client)
- Institutional API, generally conservative
- Can increase if no 429 errors observed

---

## Scan Breakdown (10-second interval)

### Phase 1: Market Fetch (~2 requests)
- 1x Polymarket GAMMA `/markets` (200 markets)
- 1x Kalshi markets via events API (150 markets)

### Phase 2: Matched Pairs (~30-60 requests)
- N matched pairs × 2 (Poly orderbook + Kalshi orderbook)
- Current: ~30 pairs → 60 requests

### Phase 3: Multi-Outcome (~100 requests)
- First 100 Polymarket markets with 3+ outcomes
- Each needs token price lookup via CLOB `/price`

### Phase 4: Logical Constraints (~300 requests)
- 200 Polymarket markets × 1 price lookup
- 150 Kalshi markets × 1 orderbook fetch

**Total:** ~460 requests per 10-second scan

---

## Headroom Analysis

### Can We Go Faster?

**Polymarket:**
- GAMMA /markets: Can fetch up to **300 markets** (currently 200)
- CLOB /price: Can make **1500 req/10s** (currently ~300)
- **Verdict:** 5x headroom available

**Kalshi:**
- Conservative limits observed
- **Verdict:** Can likely double market count if no 429 errors

### Bottleneck: Kalshi

Kalshi is the limiting factor due to:
1. No public rate limit docs
2. Conservative estimates (10-20 req/s)
3. Institutional API with stricter enforcement

---

## Optimization Strategies

### 1. Batch Requests
- Polymarket CLOB `/prices` endpoint (plural) supports batch queries
- Can reduce from 300 single calls to ~10-20 batch calls
- **Potential savings:** 90% reduction in CLOB calls

### 2. WebSocket Subscriptions
- Polymarket: `wss://ws-subscriptions-clob.polymarket.com/ws/`
- Kalshi: `wss://trading-api.kalshi.com/trade-api/ws/v2/`
- Real-time price updates without polling
- **Benefit:** Near-zero API calls for price data

### 3. Incremental Scans
- Alternate between full scan (all markets) and incremental scan (changed markets only)
- Track market state across scans
- **Benefit:** 50% reduction in average API calls

---

## Monitoring Recommendations

### HTTP 429 Rate Limit Detection
```python
if response.status == 429:
    retry_after = response.headers.get('Retry-After', 10)
    await asyncio.sleep(retry_after)
```

### Metrics to Track
- Requests per scan
- 429 error rate
- Average response time
- Queue depth (if throttled)

### Alerting Thresholds
- **Warning:** >80% of rate limit used
- **Critical:** Any 429 errors

---

## Current Configuration

**Environment Variables:**
```env
POLL_INTERVAL_SECONDS=10  # How often to scan
MIN_PROFIT_CENTS=1.0      # Minimum profit threshold

# Market fetch limits
POLY_MARKET_LIMIT=200     # Max markets to fetch from Polymarket
KALSHI_MARKET_LIMIT=150   # Max markets to fetch from Kalshi

# Detection limits
MULTI_OUTCOME_CHECK_LIMIT=100   # Max markets to check for multi-outcome arb
LOGICAL_CONSTRAINT_LIMIT=200    # Max markets to check for logical arb
```

**Actual Usage:**
- Polymarket: 330 requests per 10s (~22% capacity)
- Kalshi: 180 requests per 10s (~conservative)
- Can scale 2-5x without hitting limits

---

## Future: WebSocket Implementation

**Benefits:**
- Real-time price updates
- ~90% reduction in API calls
- Sub-second arbitrage detection

**Implementation:**
```python
# Polymarket WebSocket
ws = await websocket.connect("wss://ws-subscriptions-clob.polymarket.com/ws/")
await ws.send(json.dumps({
    "type": "subscribe",
    "channel": "book",
    "markets": [token_id_1, token_id_2, ...]
}))

# Kalshi WebSocket
ws = await websocket.connect("wss://trading-api.kalshi.com/trade-api/ws/v2/")
await ws.send(json.dumps({
    "type": "subscribe",
    "channels": ["orderbook_delta"],
    "tickers": [ticker_1, ticker_2, ...]
}))
```

**Priority:** Medium (current polling works fine at 10s interval)
