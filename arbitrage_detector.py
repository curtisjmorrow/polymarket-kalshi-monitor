"""Core arbitrage detection logic."""
import csv
import asyncio
from datetime import datetime
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ArbitrageOpportunity:
    """Represents a detected arbitrage opportunity."""
    timestamp: str
    market_pair: str
    polymarket_market: str
    kalshi_market: str
    strategy: str  # "poly_yes_kalshi_no" or "kalshi_yes_poly_no"
    poly_price: float
    kalshi_price: float
    total_cost: float
    profit_cents: float
    poly_market_id: str
    kalshi_ticker: str


class ArbitrageDetector:
    """Detects and logs arbitrage opportunities between Polymarket and Kalshi."""
    
    def __init__(self, min_profit_cents: float, log_file: str):
        self.min_profit_cents = min_profit_cents
        self.log_file = Path(log_file)
        self.opportunities: List[ArbitrageOpportunity] = []
        self._init_csv()
    
    def _init_csv(self):
        """Initialize CSV file with headers if it doesn't exist."""
        if not self.log_file.exists():
            with open(self.log_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp',
                    'market_pair',
                    'polymarket_market',
                    'kalshi_market',
                    'strategy',
                    'poly_price',
                    'kalshi_price',
                    'total_cost',
                    'profit_cents',
                    'poly_market_id',
                    'kalshi_ticker'
                ])
    
    async def log_opportunity(self, opp: ArbitrageOpportunity):
        """Log opportunity to CSV file."""
        with open(self.log_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                opp.timestamp,
                opp.market_pair,
                opp.polymarket_market,
                opp.kalshi_market,
                opp.strategy,
                f"{opp.poly_price:.4f}",
                f"{opp.kalshi_price:.4f}",
                f"{opp.total_cost:.4f}",
                f"{opp.profit_cents:.2f}",
                opp.poly_market_id,
                opp.kalshi_ticker
            ])
        
        # Store in memory for dashboard
        self.opportunities.append(opp)
        if len(self.opportunities) > 100:
            self.opportunities = self.opportunities[-100:]  # Keep last 100
    
    def detect_arbitrage(
        self,
        poly_markets: List[Dict[str, Any]],
        kalshi_markets: List[Dict[str, Any]],
        kalshi_orderbooks: Dict[str, Dict[str, Any]]
    ) -> List[ArbitrageOpportunity]:
        """
        Detect arbitrage opportunities between matched markets.
        
        Strategy 1: Buy YES on Poly + Buy NO on Kalshi
        Strategy 2: Buy YES on Kalshi + Buy NO on Poly
        
        Arb exists when: YES_ask + NO_ask < $1.00
        """
        opportunities = []
        
        # Match markets by title similarity
        matched_pairs = self._match_markets(poly_markets, kalshi_markets)
        
        for poly_market, kalshi_market in matched_pairs:
            # Get prices
            poly_yes_ask, poly_no_ask = self._get_poly_prices(poly_market)
            kalshi_yes_ask, kalshi_no_ask = self._get_kalshi_prices(
                kalshi_market, 
                kalshi_orderbooks.get(kalshi_market.get('ticker', ''), {})
            )
            
            if not all([poly_yes_ask, poly_no_ask, kalshi_yes_ask, kalshi_no_ask]):
                continue
            
            # Strategy 1: Poly YES + Kalshi NO
            cost1 = poly_yes_ask + kalshi_no_ask
            profit1 = (100 - cost1 * 100)
            
            # Strategy 2: Kalshi YES + Poly NO
            cost2 = kalshi_yes_ask + poly_no_ask
            profit2 = (100 - cost2 * 100)
            
            # Check if either strategy is profitable
            if profit1 >= self.min_profit_cents:
                opp = ArbitrageOpportunity(
                    timestamp=datetime.utcnow().isoformat(),
                    market_pair=f"{poly_market.get('question', 'Unknown')} / {kalshi_market.get('title', 'Unknown')}",
                    polymarket_market=poly_market.get('question', 'Unknown'),
                    kalshi_market=kalshi_market.get('title', 'Unknown'),
                    strategy="poly_yes_kalshi_no",
                    poly_price=poly_yes_ask,
                    kalshi_price=kalshi_no_ask,
                    total_cost=cost1,
                    profit_cents=profit1,
                    poly_market_id=poly_market.get('condition_id', ''),
                    kalshi_ticker=kalshi_market.get('ticker', '')
                )
                opportunities.append(opp)
            
            if profit2 >= self.min_profit_cents:
                opp = ArbitrageOpportunity(
                    timestamp=datetime.utcnow().isoformat(),
                    market_pair=f"{poly_market.get('question', 'Unknown')} / {kalshi_market.get('title', 'Unknown')}",
                    polymarket_market=poly_market.get('question', 'Unknown'),
                    kalshi_market=kalshi_market.get('title', 'Unknown'),
                    strategy="kalshi_yes_poly_no",
                    poly_price=poly_no_ask,
                    kalshi_price=kalshi_yes_ask,
                    total_cost=cost2,
                    profit_cents=profit2,
                    poly_market_id=poly_market.get('condition_id', ''),
                    kalshi_ticker=kalshi_market.get('ticker', '')
                )
                opportunities.append(opp)
        
        return opportunities
    
    def _match_markets(
        self,
        poly_markets: List[Dict[str, Any]],
        kalshi_markets: List[Dict[str, Any]]
    ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Match Polymarket and Kalshi markets by title similarity."""
        # Simple keyword matching for now
        # In production, would use fuzzy matching (Levenshtein distance, etc.)
        matches = []
        
        for poly in poly_markets:
            poly_title = poly.get('question', '').lower()
            poly_keywords = set(poly_title.split())
            
            for kalshi in kalshi_markets:
                kalshi_title = kalshi.get('title', '').lower()
                kalshi_keywords = set(kalshi_title.split())
                
                # Calculate Jaccard similarity
                intersection = poly_keywords & kalshi_keywords
                union = poly_keywords | kalshi_keywords
                
                if union and len(intersection) / len(union) > 0.3:  # 30% overlap threshold
                    matches.append((poly, kalshi))
                    break
        
        return matches
    
    def _get_poly_prices(self, market: Dict[str, Any]) -> Tuple[float, float]:
        """Extract YES ask and NO ask from Polymarket market data."""
        # Polymarket Gamma API provides outcome prices
        # For binary markets, there are usually 2 outcomes
        outcomes = market.get('outcomes', [])
        
        if len(outcomes) != 2:
            return None, None
        
        # Assuming first outcome is YES, second is NO
        # Price is typically given as the current market price (mid)
        # For asks, we'd need orderbook data, but using mid as approximation for now
        yes_price = float(outcomes[0].get('price', 0))
        no_price = float(outcomes[1].get('price', 0))
        
        # Add small spread assumption (1 cent on each side)
        yes_ask = yes_price + 0.01
        no_ask = no_price + 0.01
        
        return yes_ask, no_ask
    
    def _get_kalshi_prices(
        self,
        market: Dict[str, Any],
        orderbook: Dict[str, Any]
    ) -> Tuple[float, float]:
        """Extract YES ask and NO ask from Kalshi orderbook."""
        if not orderbook:
            return None, None
        
        yes_bids = orderbook.get('yes', [])
        no_bids = orderbook.get('no', [])
        
        if not yes_bids or not no_bids:
            return None, None
        
        # In Kalshi, best bid is last element
        # Ask = 100 - opposing best bid (in cents, converted to dollars)
        best_yes_bid = yes_bids[-1][0] if yes_bids else 0
        best_no_bid = no_bids[-1][0] if no_bids else 0
        
        yes_ask = (100 - best_no_bid) / 100.0
        no_ask = (100 - best_yes_bid) / 100.0
        
        return yes_ask, no_ask
