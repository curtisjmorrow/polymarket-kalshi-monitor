"""Logical constraint detection for correlated market arbitrage."""
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from enum import Enum


class ConstraintType(Enum):
    """Types of logical constraints between markets."""
    SUPERSET = "superset"  # Temporal: later date >= earlier date
    IMPLICATION = "implication"  # A implies B: P(A) <= P(B)
    MUTUAL_EXCLUSION = "mutual_exclusion"  # A and B can't both happen: P(A) + P(B) <= 1.0
    PARTITION = "partition"  # B is union of sub-events: P(B) = sum(P(A_i))


@dataclass
class LogicalConstraint:
    """Represents a logical relationship between markets."""
    constraint_type: ConstraintType
    market_ids: List[str]  # IDs involved in constraint
    operator: str  # "<=", ">=", "==", "sum_equals"
    threshold: float = 0.02  # Minimum violation to trigger (2 cents default)
    description: str = ""  # Human-readable explanation


@dataclass
class ConstraintViolation:
    """Represents a detected violation of a logical constraint."""
    constraint: LogicalConstraint
    markets: List[Dict[str, Any]]  # Market metadata
    prices: Dict[str, float]  # {market_id: price}
    violation_amount: float  # How much constraint is violated (in dollars)
    arbitrage_strategy: str  # What to do
    profit_estimate: float  # Expected profit in cents


class LogicalConstraintDetector:
    """
    Detects logical inconsistencies between correlated markets.
    Focus on mechanical patterns that don't require heavy NLP.
    """
    
    def __init__(self, min_profit_cents: float = 1.0):
        self.min_profit_cents = min_profit_cents
        self.constraints: List[LogicalConstraint] = []
        self._date_cache: Dict[str, Optional[datetime]] = {}
    
    def extract_date_from_title(self, title: str) -> Optional[datetime]:
        """
        Extract date from market title using pattern matching.
        
        Patterns supported:
        - "by March 2026"
        - "before June 1"
        - "in 2026"
        - "by Q2 2026"
        """
        if title in self._date_cache:
            return self._date_cache[title]
        
        title_lower = title.lower()
        
        # Pattern 1: "by [Month] [Year]"
        month_match = re.search(r'by\s+(\w+)\s+(\d{4})', title_lower)
        if month_match:
            month_str, year_str = month_match.groups()
            try:
                date = datetime.strptime(f"{month_str} {year_str}", "%B %Y")
                self._date_cache[title] = date
                return date
            except ValueError:
                try:
                    date = datetime.strptime(f"{month_str} {year_str}", "%b %Y")
                    self._date_cache[title] = date
                    return date
                except ValueError:
                    pass
        
        # Pattern 2: "by [Month] [Day]" (assume current or next year)
        day_match = re.search(r'by\s+(\w+)\s+(\d{1,2})', title_lower)
        if day_match:
            month_str, day_str = day_match.groups()
            current_year = datetime.now().year
            try:
                date = datetime.strptime(f"{month_str} {day_str} {current_year}", "%B %d %Y")
                if date < datetime.now():
                    date = date.replace(year=current_year + 1)
                self._date_cache[title] = date
                return date
            except ValueError:
                pass
        
        # Pattern 3: "in [Year]"
        year_match = re.search(r'in\s+(\d{4})', title_lower)
        if year_match:
            year = int(year_match.group(1))
            date = datetime(year, 12, 31)  # End of year
            self._date_cache[title] = date
            return date
        
        # Pattern 4: "by Q[1-4] [Year]"
        quarter_match = re.search(r'by\s+q([1-4])\s+(\d{4})', title_lower)
        if quarter_match:
            quarter, year = quarter_match.groups()
            quarter = int(quarter)
            year = int(year)
            month = quarter * 3  # Q1->3, Q2->6, Q3->9, Q4->12
            date = datetime(year, month, 1)  # Start of last month in quarter
            self._date_cache[title] = date
            return date
        
        self._date_cache[title] = None
        return None
    
    def find_temporal_supersets(
        self,
        markets: List[Dict[str, Any]],
        platform: str
    ) -> List[LogicalConstraint]:
        """
        Find markets with temporal superset relationships.
        E.g., "Rate cut by March" and "Rate cut by June" should have June >= March.
        
        Strategy:
        1. Group markets by base topic (using fuzzy title matching)
        2. Extract dates from titles
        3. Create superset constraints for earlier -> later
        """
        constraints = []
        
        # Extract dates for all markets
        market_dates: List[Tuple[Dict, Optional[datetime]]] = []
        for market in markets:
            title = market.get('question', '') if platform == 'polymarket' else market.get('title', '')
            date = self.extract_date_from_title(title)
            if date:
                market_dates.append((market, date))
        
        # Group by base topic (simple keyword matching)
        # Look for markets with similar base questions
        for i, (m1, d1) in enumerate(market_dates):
            for m2, d2 in market_dates[i+1:]:
                t1 = m1.get('question' if platform == 'polymarket' else 'title', '').lower()
                t2 = m2.get('question' if platform == 'polymarket' else 'title', '').lower()
                
                # Extract base question (remove date-related parts)
                base1 = re.sub(r'by\s+\w+\s+\d{1,4}', '', t1).strip()
                base1 = re.sub(r'in\s+\d{4}', '', base1).strip()
                base2 = re.sub(r'by\s+\w+\s+\d{1,4}', '', t2).strip()
                base2 = re.sub(r'in\s+\d{4}', '', base2).strip()
                
                # Check if bases are similar (simple substring match)
                similarity = len(set(base1.split()) & set(base2.split())) / max(len(base1.split()), len(base2.split()), 1)
                
                if similarity > 0.6:  # 60% word overlap
                    # Create constraint: earlier <= later
                    if d1 < d2:
                        earlier_id = (m1.get('condition_id') or m1.get('conditionId', '')) if platform == 'polymarket' else m1.get('ticker', '')
                        later_id = (m2.get('condition_id') or m2.get('conditionId', '')) if platform == 'polymarket' else m2.get('ticker', '')
                        
                        constraints.append(LogicalConstraint(
                            constraint_type=ConstraintType.SUPERSET,
                            market_ids=[earlier_id, later_id],
                            operator="<=",
                            threshold=0.02,
                            description=f"Earlier date ({d1.strftime('%b %Y')}) must be <= later date ({d2.strftime('%b %Y')})"
                        ))
                    elif d2 < d1:
                        earlier_id = (m2.get('condition_id') or m2.get('conditionId', '')) if platform == 'polymarket' else m2.get('ticker', '')
                        later_id = (m1.get('condition_id') or m1.get('conditionId', '')) if platform == 'polymarket' else m1.get('ticker', '')
                        
                        constraints.append(LogicalConstraint(
                            constraint_type=ConstraintType.SUPERSET,
                            market_ids=[earlier_id, later_id],
                            operator="<=",
                            threshold=0.02,
                            description=f"Earlier date ({d2.strftime('%b %Y')}) must be <= later date ({d1.strftime('%b %Y')})"
                        ))
        
        return constraints
    
    def detect_violations(
        self,
        constraints: List[LogicalConstraint],
        prices: Dict[str, float],  # {market_id: yes_ask}
        market_metadata: Dict[str, Dict[str, Any]]  # {market_id: market_data}
    ) -> List[ConstraintViolation]:
        """
        Check all constraints against current prices and return violations.
        """
        violations = []
        
        for constraint in constraints:
            if constraint.constraint_type == ConstraintType.SUPERSET:
                # market_ids[0] should be <= market_ids[1]
                if len(constraint.market_ids) != 2:
                    continue
                
                earlier_id, later_id = constraint.market_ids
                
                if earlier_id not in prices or later_id not in prices:
                    continue
                
                earlier_price = prices[earlier_id]
                later_price = prices[later_id]
                
                # Violation: earlier > later (impossible)
                if earlier_price > later_price + constraint.threshold:
                    violation_amount = earlier_price - later_price
                    
                    # Strategy: Buy later YES (underpriced), sell earlier YES (overpriced)
                    # Or: Buy later YES, buy earlier NO (equivalent)
                    profit_estimate = (violation_amount - 0.03) * 100  # 3 cent fee buffer
                    
                    if profit_estimate >= self.min_profit_cents:
                        violations.append(ConstraintViolation(
                            constraint=constraint,
                            markets=[
                                market_metadata.get(earlier_id, {}),
                                market_metadata.get(later_id, {})
                            ],
                            prices={earlier_id: earlier_price, later_id: later_price},
                            violation_amount=violation_amount,
                            arbitrage_strategy="buy_later_yes_buy_earlier_no",
                            profit_estimate=profit_estimate
                        ))
            
            elif constraint.constraint_type == ConstraintType.MUTUAL_EXCLUSION:
                # Sum of prices should be <= 1.0
                total = sum(prices.get(mid, 0.0) for mid in constraint.market_ids)
                
                if total > 1.0 + constraint.threshold:
                    violation_amount = total - 1.0
                    profit_estimate = (violation_amount - 0.03) * 100
                    
                    if profit_estimate >= self.min_profit_cents:
                        violations.append(ConstraintViolation(
                            constraint=constraint,
                            markets=[market_metadata.get(mid, {}) for mid in constraint.market_ids],
                            prices={mid: prices.get(mid, 0.0) for mid in constraint.market_ids},
                            violation_amount=violation_amount,
                            arbitrage_strategy="buy_all_no_positions",
                            profit_estimate=profit_estimate
                        ))
        
        return violations
    
    def scan_for_temporal_arbitrage(
        self,
        markets: List[Dict[str, Any]],
        prices: Dict[str, float],
        platform: str
    ) -> List[ConstraintViolation]:
        """
        High-level method: find temporal constraints and check for violations.
        """
        # Find constraints
        constraints = self.find_temporal_supersets(markets, platform)
        
        # Build metadata map
        if platform == 'polymarket':
            market_metadata = {(m.get('condition_id') or m.get('conditionId', '')): m for m in markets}
        else:
            market_metadata = {m.get('ticker', ''): m for m in markets}
        
        # Detect violations
        violations = self.detect_violations(constraints, prices, market_metadata)
        
        return violations
