"""Kalshi API client with RSA signing support."""
import base64
import hashlib
import time
import asyncio
from typing import Optional, Dict, Any, List
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import aiohttp


class KalshiClient:
    """Async Kalshi API client with RSA request signing."""
    
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    
    def __init__(self, api_key: str, private_key_pem_or_path: str):
        self.api_key = api_key
        
        # Check if it's a file path or PEM content
        if private_key_pem_or_path.strip().startswith('-----BEGIN'):
            pem_data = private_key_pem_or_path.encode()
        else:
            # It's a file path
            with open(private_key_pem_or_path, 'rb') as f:
                pem_data = f.read()
        
        self.private_key = serialization.load_pem_private_key(
            pem_data,
            password=None
        )
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    def _sign_request(self, method: str, path: str, body: str = "") -> str:
        """Generate RSA signature for Kalshi API request."""
        timestamp = str(int(time.time() * 1000))
        msg = f"{timestamp}{method}{path}{body}"
        
        signature = self.private_key.sign(
            msg.encode(),
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        
        return base64.b64encode(signature).decode()
    
    def _get_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Generate signed headers for Kalshi request."""
        timestamp = str(int(time.time() * 1000))
        signature = self._sign_request(method, path, body)
        
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json"
        }
    
    async def _request_with_retry(self, method: str, url: str, headers: Dict[str, str], 
                                   max_retries: int = 3) -> Optional[Dict[str, Any]]:
        """Make HTTP request with exponential backoff on 429."""
        for attempt in range(max_retries):
            async with self.session.request(method, url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429:
                    if attempt < max_retries - 1:
                        wait_time = (2 ** attempt) * 1.5  # 1.5s, 3s, 6s
                        await asyncio.sleep(wait_time)
                        # Re-sign since timestamp changed
                        path = url.replace(self.BASE_URL, "")
                        headers = self._get_headers(method, path)
                        continue
                    else:
                        return None
                else:
                    text = await resp.text()
                    if resp.status != 404:
                        print(f"⚠️ Kalshi API {resp.status}: {text[:100]}")
                    return None
        return None
    
    async def get_markets(self, status: str = "open", limit: int = 200) -> List[Dict[str, Any]]:
        """Fetch active markets from Kalshi."""
        path = f"/markets?status={status}&limit={limit}"
        headers = self._get_headers("GET", path)
        
        data = await self._request_with_retry("GET", f"{self.BASE_URL}{path}", headers)
        return data.get("markets", []) if data else []
    
    async def get_orderbook(self, ticker: str) -> Dict[str, Any]:
        """Fetch orderbook for a specific market."""
        path = f"/markets/{ticker}/orderbook"
        headers = self._get_headers("GET", path)
        
        data = await self._request_with_retry("GET", f"{self.BASE_URL}{path}", headers)
        return data.get("orderbook", {}) if data else {}
    
    async def get_events(self, status: str = "open", limit: int = 100) -> List[Dict[str, Any]]:
        """Fetch events from Kalshi."""
        path = f"/events?status={status}&limit={limit}"
        headers = self._get_headers("GET", path)
        
        data = await self._request_with_retry("GET", f"{self.BASE_URL}{path}", headers)
        return data.get("events", []) if data else []
    
    async def get_markets_for_event(self, event_ticker: str) -> List[Dict[str, Any]]:
        """Fetch all markets for a specific event using series_ticker."""
        # Extract series ticker (remove the -XX suffix)
        series_ticker = event_ticker.rsplit('-', 1)[0] if '-' in event_ticker else event_ticker
        path = f"/markets?series_ticker={series_ticker}&limit=50"
        headers = self._get_headers("GET", path)
        
        data = await self._request_with_retry("GET", f"{self.BASE_URL}{path}", headers)
        return data.get("markets", []) if data else []
    
    async def get_non_sports_markets(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Fetch open markets excluding sports categories."""
        # Get events first
        events = await self.get_events(status="open", limit=100)
        
        # Filter to non-sports categories
        non_sports_categories = ['Politics', 'Financials', 'Science and Technology', 
                                  'Climate and Weather', 'Social', 'World', 'Entertainment']
        
        non_sports_events = [
            e for e in events 
            if e.get('category') in non_sports_categories
        ]
        
        # Get markets for each non-sports event (throttled)
        all_markets = []
        for event in non_sports_events[:15]:  # Cap to stay within rate limits
            markets = await self.get_markets_for_event(event.get('event_ticker', ''))
            for m in markets:
                m['event_title'] = event.get('title', '')
                m['category'] = event.get('category', '')
            all_markets.extend(markets)
            await asyncio.sleep(0.3)  # Throttle: ~3 req/s
            
            if len(all_markets) >= limit:
                break
        
        return all_markets[:limit]
