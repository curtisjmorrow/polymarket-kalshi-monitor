"""Core arbitrage detection logic with smart matching and real orderbook prices."""
import csv
from datetime import datetime, timezone
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass
from pathlib import Path

from market_matcher import MarketMatcher
from logical_constraints import LogicalConstraintDetector, ConstraintViolation


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
    arb_type: str = "cross_exchange"  # cross_exchange, intra_poly, intra_kalshi, multi_outcome


class ArbitrageDetector:
    """Detects and logs arbitrage opportunities with real orderbook prices."""
    
    def __init__(self, min_profit_cents: float, log_file: str):
        self.min_profit_cents = min_profit_cents
        self.log_file = Path(log_file)
        self.opportunities: List[ArbitrageOpportunity] = []
        self.matcher = MarketMatcher()
        self.logical_detector = LogicalConstraintDetector(min_profit_cents)
        self._init_csv()
    
    def _init_csv(self):
        """Initialize CSV file with headers if it doesn't exist."""
        if not self.log_file.exists():
            with open(self.log_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'market_pair', 'polymarket_market',
                    'kalshi_market', 'strategy', 'poly_price', 'kalshi_price',
                    'total_cost', 'profit_cents', 'poly_market_id', 'kalshi_ticker', 'arb_type'
                ])
    
    async def log_opportunity(self, opp: ArbitrageOpportunity):
        """Log opportunity to CSV file."""
        with open(self.log_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                opp.timestamp, opp.market_pair, opp.polymarket_market,
                opp.kalshi_market, opp.strategy, f"{opp.poly_price:.4f}",
                f"{opp.kalshi_price:.4f}", f"{opp.total_cost:.4f}",
                f"{opp.profit_cents:.2f}", opp.poly_market_id, opp.kalshi_ticker, opp.arb_type
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
        
        # INTRA-EXCHANGE: Check Polymarket YES/NO spread arbitrage
        poly_spread_cost = poly_yes_ask + poly_no_ask
        poly_spread_profit = 100 - (poly_spread_cost * 100)
        
        if poly_spread_profit >= self.min_profit_cents:
            opportunities.append(ArbitrageOpportunity(
                timestamp=ts,
                market_pair=poly_title,
                polymarket_market=poly_title,
                kalshi_market="",
                strategy="buy_poly_yes_and_no",
                poly_price=poly_spread_cost, kalshi_price=0.0,
                total_cost=poly_spread_cost, profit_cents=poly_spread_profit,
                poly_market_id=poly_id, kalshi_ticker="",
                arb_type="intra_poly"
            ))
        
        # INTRA-EXCHANGE: Check Kalshi YES/NO spread arbitrage
        kalshi_spread_cost = kalshi_yes_ask + kalshi_no_ask
        kalshi_spread_profit = 100 - (kalshi_spread_cost * 100)
        
        if kalshi_spread_profit >= self.min_profit_cents:
            opportunities.append(ArbitrageOpportunity(
                timestamp=ts,
                market_pair=kalshi_title,
                polymarket_market="",
                kalshi_market=kalshi_title,
                strategy="buy_kalshi_yes_and_no",
                poly_price=0.0, kalshi_price=kalshi_spread_cost,
                total_cost=kalshi_spread_cost, profit_cents=kalshi_spread_profit,
                poly_market_id="", kalshi_ticker=kalshi_ticker,
                arb_type="intra_kalshi"
            ))
        
        # CROSS-EXCHANGE: Strategy 1: Buy YES on Poly + Buy NO on Kalshi
        cost1 = poly_yes_ask + kalshi_no_ask
        profit1 = 100 - (cost1 * 100)
        
        # CROSS-EXCHANGE: Strategy 2: Buy YES on Kalshi + Buy NO on Poly
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
                poly_market_id=poly_id, kalshi_ticker=kalshi_ticker,
                arb_type="cross_exchange"
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
                poly_market_id=poly_id, kalshi_ticker=kalshi_ticker,
                arb_type="cross_exchange"
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
    
    def detect_multi_outcome_arbitrage(
        self,
        market: Dict[str, Any],
        outcome_prices: List[Dict[str, Any]],  # [{"outcome": str, "yes_ask": float}, ...]
        platform: str  # "polymarket" or "kalshi"
    ) -> Optional[ArbitrageOpportunity]:
        """
        Detect multi-outcome arbitrage (sum of YES prices != 1.0).
        Per spec: If sum < 1.0, buy all YES positions for guaranteed profit.
        
        Args:
            market: Market metadata
            outcome_prices: List of outcome prices [{"outcome": str, "yes_ask": float}, ...]
            platform: "polymarket" or "kalshi"
        """
        if len(outcome_prices) < 3:  # Need at least 3 outcomes for multi-outcome
            return None
        
        # Calculate sum of all YES asks
        total_cost = sum(o.get('yes_ask', 1.0) for o in outcome_prices)
        
        # Fee threshold: 0.5 cents to account for fees/slippage
        FEE_THRESHOLD = 0.005
        
        if total_cost < (1.0 - FEE_THRESHOLD):
            # Opportunity: buy all YES positions
            profit_cents = (1.0 - total_cost) * 100
            
            if profit_cents >= self.min_profit_cents:
                ts = datetime.now(timezone.utc).isoformat()
                market_title = market.get('question' if platform == 'polymarket' else 'title', 'Unknown')
                market_id = market.get('condition_id' if platform == 'polymarket' else 'ticker', '')
                
                outcome_names = ", ".join(o.get('outcome', '?')[:15] for o in outcome_prices[:5])
                if len(outcome_prices) > 5:
                    outcome_names += f" (+{len(outcome_prices) - 5} more)"
                
                return ArbitrageOpportunity(
                    timestamp=ts,
                    market_pair=f"{market_title} [{len(outcome_prices)} outcomes]",
                    polymarket_market=market_title if platform == 'polymarket' else '',
                    kalshi_market=market_title if platform == 'kalshi' else '',
                    strategy=f"buy_all_{len(outcome_prices)}_yes_outcomes",
                    poly_price=total_cost if platform == 'polymarket' else 0.0,
                    kalshi_price=total_cost if platform == 'kalshi' else 0.0,
                    total_cost=total_cost,
                    profit_cents=profit_cents,
                    poly_market_id=market_id if platform == 'polymarket' else '',
                    kalshi_ticker=market_id if platform == 'kalshi' else '',
                    arb_type=f"multi_outcome_{platform}"
                )
        
        return None
    
    def convert_violation_to_opportunity(
        self,
        violation: ConstraintViolation,
        platform: str
    ) -> ArbitrageOpportunity:
        """Convert a logical constraint violation to an ArbitrageOpportunity."""
        ts = datetime.now(timezone.utc).isoformat()
        
        # Build market description
        market_titles = [m.get('question' if platform == 'polymarket' else 'title', 'Unknown')[:40] for m in violation.markets]
        market_pair = " vs ".join(market_titles)
        
        # Get market IDs
        id_field = 'condition_id' if platform == 'polymarket' else 'ticker'
        market_ids = [m.get(id_field, '') for m in violation.markets]
        
        # Total cost (sum of all involved prices)
        total_cost = sum(violation.prices.values())
        
        return ArbitrageOpportunity(
            timestamp=ts,
            market_pair=market_pair,
            polymarket_market=market_titles[0] if platform == 'polymarket' else '',
            kalshi_market=market_titles[0] if platform == 'kalshi' else '',
            strategy=violation.arbitrage_strategy,
            poly_price=total_cost if platform == 'polymarket' else 0.0,
            kalshi_price=total_cost if platform == 'kalshi' else 0.0,
            total_cost=total_cost,
            profit_cents=violation.profit_estimate,
            poly_market_id=market_ids[0] if platform == 'polymarket' else '',
            kalshi_ticker=market_ids[0] if platform == 'kalshi' else '',
            arb_type=f"logical_{violation.constraint.constraint_type.value}_{platform}"
        )
