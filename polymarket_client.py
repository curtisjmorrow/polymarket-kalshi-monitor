"""Polymarket Gamma API client (public data, no auth required)."""
import aiohttp
from typing import List, Dict, Any


class PolymarketClient:
    """Async Polymarket Gamma API client for market data."""
    
    GAMMA_API = "https://gamma-api.polymarket.com"
    
    def __init__(self):
        self.session: aiohttp.ClientSession = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    async def get_markets(self, limit: int = 100, active: bool = True) -> List[Dict[str, Any]]:
        """Fetch active markets from Polymarket Gamma API."""
        params = {
            "limit": limit,
            "active": str(active).lower()
        }
        
        async with self.session.get(f"{self.GAMMA_API}/markets", params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"Polymarket API error: {resp.status} - {text}")
            
            return await resp.json()
    
    async def get_market(self, condition_id: str) -> Dict[str, Any]:
        """Fetch specific market details."""
        async with self.session.get(f"{self.GAMMA_API}/markets/{condition_id}") as resp:
            if resp.status != 200:
                return {}
            return await resp.json()
    
    async def get_prices(self, token_ids: List[str]) -> Dict[str, Dict[str, float]]:
        """Fetch current prices for token IDs.
        
        Returns dict mapping token_id -> {"bid": float, "ask": float, "mid": float}
        """
        if not token_ids:
            return {}
        
        # Gamma API provides prices in market response
        # For real-time pricing, we'd use CLOB API, but for now we'll parse from market data
        prices = {}
        
        # Fetch prices from simplified endpoint
        # Note: This is a simplified approach - full orderbook would give better data
        for token_id in token_ids:
            # For now, return empty - will populate from market data in main loop
            prices[token_id] = {"bid": 0.0, "ask": 0.0, "mid": 0.0}
        
        return prices
