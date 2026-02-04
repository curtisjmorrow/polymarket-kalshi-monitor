"""Polymarket client with CLOB API for real orderbook data."""
import aiohttp
from typing import List, Dict, Any, Optional, Tuple


class PolymarketClient:
    """Async Polymarket client using Gamma API for markets + CLOB API for orderbooks."""
    
    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"
    
    def __init__(self):
        self.session: aiohttp.ClientSession = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    async def get_markets(self, limit: int = 100, active: bool = True) -> List[Dict[str, Any]]:
        """Fetch active, non-closed markets from Polymarket Gamma API."""
        params = {
            "limit": limit,
            "active": str(active).lower(),
            "closed": "false"  # Only open markets
        }
        
        async with self.session.get(f"{self.GAMMA_API}/markets", params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"Polymarket Gamma API error: {resp.status} - {text}")
            
            return await resp.json()
    
    async def get_orderbook(self, token_id: str) -> Dict[str, Any]:
        """Fetch orderbook from CLOB API for a specific token."""
        try:
            async with self.session.get(f"{self.CLOB_API}/book?token_id={token_id}") as resp:
                if resp.status != 200:
                    return {}
                return await resp.json()
        except Exception:
            return {}
    
    async def get_best_prices(self, token_id: str) -> Tuple[Optional[float], Optional[float]]:
        """Get best bid and ask from CLOB orderbook.
        
        Returns: (best_bid, best_ask) or (None, None) if unavailable
        """
        orderbook = await self.get_orderbook(token_id)
        
        if not orderbook:
            return None, None
        
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        
        best_bid = float(bids[0]['price']) if bids else None
        best_ask = float(asks[0]['price']) if asks else None
        
        return best_bid, best_ask
    
    async def get_market_orderbooks(self, market: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        """Get orderbook prices for all outcomes in a market.
        
        Returns: {outcome_name: {"bid": float, "ask": float}}
        """
        results = {}
        
        # Gamma API markets have 'tokens' array with clob_token_id
        tokens = market.get('tokens', [])
        
        for token in tokens:
            token_id = token.get('token_id', '')
            outcome = token.get('outcome', 'Unknown')
            
            if not token_id:
                continue
            
            bid, ask = await self.get_best_prices(token_id)
            
            if bid is not None or ask is not None:
                results[outcome] = {
                    "bid": bid or 0.0,
                    "ask": ask or 1.0,  # Default to 1.0 if no ask (worst case)
                    "token_id": token_id
                }
        
        return results
