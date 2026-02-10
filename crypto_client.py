"""Crypto client for fetching spot prices."""
import aiohttp
from typing import Dict, Optional

class CryptoClient:
    """Async client for fetching real-time crypto spot prices from Coinbase."""
    
    BASE_URL = "https://api.coinbase.com/v2/prices"
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
            
    async def get_spot_price(self, pair: str = "BTC-USD") -> Optional[float]:
        """Fetch current spot price for a pair (e.g., BTC-USD, ETH-USD, SOL-USD)."""
        url = f"{self.BASE_URL}/{pair}/spot"
        try:
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data['data']['amount'])
                return None
        except Exception as e:
            print(f"Error fetching {pair} price: {e}")
            return None

    async def get_all_prices(self) -> Dict[str, float]:
        """Fetch spot prices for BTC, ETH, and SOL."""
        results = {}
        for coin in ["BTC", "ETH", "SOL"]:
            price = await self.get_spot_price(f"{coin}-USD")
            if price:
                results[coin] = price
        return results
