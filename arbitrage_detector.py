"""Core arbitrage detection logic with smart matching and real orderbook prices."""
import csv
from datetime import datetime, timezone
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass
from pathlib import Path

from market_matcher import MarketMatcher


@dataclass
class ArbitrageOpportunity:
    """Represents a detected arbitrage opportunity."""
    timestamp: str
    market_pair: str
    polymarket_market: str
    kalshi_market: str
    strategy: str
    poly_price: float
    kalshi_price: float
    total_cost: float
    profit_cents: float
    poly_market_id: str
    kalshi_ticker: str


class ArbitrageDetector:
    """Detects and logs arbitrage opportunities with real orderbook prices."""
    
    def __init__(self, min_profit_cents: float, log_file: str):
        self.min_profit_cents = min_profit_cents
        self.log_file = Path(log_file)
        self.opportunities: List[ArbitrageOpportunity] = []
        self.matcher = MarketMatcher()
        self._init_csv()
    
    def _init_csv(self):
        """Initialize CSV file with headers if it doesn't exist."""
        if not self.log_file.exists():
            with open(self.log_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'market_pair', 'polymarket_market',
                    'kalshi_market', 'strategy', 'poly_price', 'kalshi_price',
                    'total_cost', 'profit_cents', 'poly_market_id', 'kalshi_ticker'
                ])
    
    async def log_opportunity(self, opp: ArbitrageOpportunity):
        """Log opportunity to CSV file."""
        with open(self.log_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                opp.timestamp, opp.market_pair, opp.polymarket_market,
                opp.kalshi_market, opp.strategy, f"{opp.poly_price:.4f}",
                f"{opp.kalshi_price:.4f}", f"{opp.total_cost:.4f}",
                f"{opp.profit_cents:.2f}", opp.poly_market_id, opp.kalshi_ticker
            ])
        
        self.opportunities.append(opp)
        if len(self.opportunities) > 100:
            self.opportunities = self.opportunities[-100:]
    
    def get_matched_pairs(
        self,
        poly_markets: List[Dict[str, Any]],
        kalshi_markets: List[Dict[str, Any]]
    ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Get list of matched market pairs for orderbook fetching."""
        self.matcher.update_markets(poly_markets, kalshi_markets)
        
        kalshi_by_ticker = {m.get('ticker'): m for m in kalshi_markets}
        matched = []
        
        for poly in poly_markets:
            poly_id = poly.get('condition_id', '')
            poly_title = poly.get('question', '')
            
            if not poly_id:
                continue
            
            kalshi_ticker = self.matcher.get_match(poly_id)
            if not kalshi_ticker:
                kalshi_ticker = self.matcher.match_new_market(poly_id, poly_title)
            
            if kalshi_ticker and kalshi_ticker in kalshi_by_ticker:
                matched.append((poly, kalshi_by_ticker[kalshi_ticker]))
        
        # Re-match unmatched every 5 min
        if self.matcher.should_rematch_unmatched():
            self.matcher.rematch_unmatched()
        
        return matched
    
    def detect_arbitrage_with_orderbooks(
        self,
        poly_market: Dict[str, Any],
        kalshi_market: Dict[str, Any],
        poly_orderbook: Dict[str, Dict[str, float]],  # {outcome: {bid, ask}}
        kalshi_orderbook: Dict[str, Any]
    ) -> List[ArbitrageOpportunity]:
        """Detect arbitrage for a single matched pair using real orderbook prices."""
        opportunities = []
        
        # Get Polymarket asks (real orderbook data)
        poly_yes_ask, poly_no_ask = self._get_poly_asks(poly_orderbook)
        
        # Get Kalshi asks
        kalshi_yes_ask, kalshi_no_ask = self._get_kalshi_asks(kalshi_orderbook)
        
        if not all([poly_yes_ask, poly_no_ask, kalshi_yes_ask, kalshi_no_ask]):
            return []
        
        poly_title = poly_market.get('question', 'Unknown')
        kalshi_title = kalshi_market.get('title', 'Unknown')
        poly_id = poly_market.get('condition_id', '')
        kalshi_ticker = kalshi_market.get('ticker', '')
        ts = datetime.now(timezone.utc).isoformat()
        
        # Strategy 1: Buy YES on Poly + Buy NO on Kalshi
        cost1 = poly_yes_ask + kalshi_no_ask
        profit1 = 100 - (cost1 * 100)
        
        # Strategy 2: Buy YES on Kalshi + Buy NO on Poly
        cost2 = kalshi_yes_ask + poly_no_ask
        profit2 = 100 - (cost2 * 100)
        
        if profit1 >= self.min_profit_cents:
            opportunities.append(ArbitrageOpportunity(
                timestamp=ts,
                market_pair=f"{poly_title} / {kalshi_title}",
                polymarket_market=poly_title,
                kalshi_market=kalshi_title,
                strategy="poly_yes_kalshi_no",
                poly_price=poly_yes_ask, kalshi_price=kalshi_no_ask,
                total_cost=cost1, profit_cents=profit1,
                poly_market_id=poly_id, kalshi_ticker=kalshi_ticker
            ))
        
        if profit2 >= self.min_profit_cents:
            opportunities.append(ArbitrageOpportunity(
                timestamp=ts,
                market_pair=f"{poly_title} / {kalshi_title}",
                polymarket_market=poly_title,
                kalshi_market=kalshi_title,
                strategy="kalshi_yes_poly_no",
                poly_price=poly_no_ask, kalshi_price=kalshi_yes_ask,
                total_cost=cost2, profit_cents=profit2,
                poly_market_id=poly_id, kalshi_ticker=kalshi_ticker
            ))
        
        return opportunities
    
    def _get_poly_asks(self, orderbook: Dict[str, Dict[str, float]]) -> Tuple[Optional[float], Optional[float]]:
        """Extract YES/NO ask from Polymarket CLOB orderbook."""
        if not orderbook:
            return None, None
        
        # Look for Yes/No outcomes
        yes_data = orderbook.get('Yes') or orderbook.get('YES') or orderbook.get('yes')
        no_data = orderbook.get('No') or orderbook.get('NO') or orderbook.get('no')
        
        if not yes_data or not no_data:
            return None, None
        
        yes_ask = yes_data.get('ask')
        no_ask = no_data.get('ask')
        
        return yes_ask, no_ask
    
    def _get_kalshi_asks(self, orderbook: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
        """Extract YES/NO ask from Kalshi orderbook."""
        if not orderbook:
            return None, None
        
        yes_bids = orderbook.get('yes', [])
        no_bids = orderbook.get('no', [])
        
        if not yes_bids or not no_bids:
            return None, None
        
        # Kalshi: best bid is last element, ask = 100 - opposing best bid
        best_yes_bid = yes_bids[-1][0] if yes_bids else 0
        best_no_bid = no_bids[-1][0] if no_bids else 0
        
        yes_ask = (100 - best_no_bid) / 100.0
        no_ask = (100 - best_yes_bid) / 100.0
        
        return yes_ask, no_ask
    
    def get_matcher_stats(self) -> dict:
        """Get matching engine stats for dashboard."""
        return self.matcher.stats()
