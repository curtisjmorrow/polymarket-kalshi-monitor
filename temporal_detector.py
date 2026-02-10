"""Temporal Arbitrage Detector: Identifies lag between spot prices and prediction markets."""
import re
from datetime import datetime
from typing import List, Dict, Any, Optional

class TemporalArbDetector:
    def __init__(self, log_file: str = "temporal_arb.csv"):
        self.log_file = log_file
        self.opportunities = []
        
    def extract_threshold(self, market_title: str) -> Optional[float]:
        """Extract the price threshold from market titles like 'Will BTC be above $100,000?'."""
        # Clean string: remove commas
        title = market_title.replace(",", "")
        # Look for dollar amounts: $100000 or 100000
        match = re.search(r'\$?(\d+(\.\d+)?)', title)
        if match:
            return float(match.group(1))
        return None

    def detect_spot_lag(self, 
                       market: Dict[str, Any], 
                       current_spot: float, 
                       prediction_price: float, 
                       source: str) -> Optional[Dict[str, Any]]:
        """
        Detects if spot price has moved across a threshold but prediction market hasn't caught up.
        prediction_price: 0.0 to 1.0 (probability/cents)
        """
        title = market.get('title', market.get('question', ''))
        threshold = self.extract_threshold(title)
        
        if threshold is None:
            return None
            
        # Strategy 1: Spot is ABOVE threshold, but market is still CHEAP (Low YES)
        # Prediction market says < 40% chance, but spot is already there.
        if current_spot > threshold and prediction_price < 0.40:
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "market": title,
                "source": source,
                "spot": current_spot,
                "threshold": threshold,
                "prediction": prediction_price,
                "side": "BUY YES",
                "reason": f"Spot ${current_spot:.2f} > Threshold ${threshold:.2f}, but market only {prediction_price*100:.1f}%"
            }
            
        # Strategy 2: Spot is BELOW threshold, but market is still EXPENSIVE (High YES)
        # Prediction market says > 60% chance, but spot has dropped below.
        if current_spot < threshold and prediction_price > 0.60:
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "market": title,
                "source": source,
                "spot": current_spot,
                "threshold": threshold,
                "prediction": prediction_price,
                "side": "BUY NO",
                "reason": f"Spot ${current_spot:.2f} < Threshold ${threshold:.2f}, but market still {prediction_price*100:.1f}%"
            }
            
        return None

    async def log_opportunity(self, opp: Dict[str, Any]):
        """Append opportunity to CSV and internal list."""
        self.opportunities.append(opp)
        line = f"{opp['timestamp']},{opp['source']},{opp['market']},{opp['spot']},{opp['threshold']},{opp['prediction']},{opp['side']},{opp['reason']}\n"
        with open(self.log_file, "a") as f:
            if f.tell() == 0:
                f.write("timestamp,source,market,spot,threshold,prediction,side,reason\n")
            f.write(line)
