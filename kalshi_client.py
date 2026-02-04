"""Kalshi API client with RSA signing support."""
import base64
import hashlib
import time
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
    
    async def get_markets(self, status: str = "open", limit: int = 200) -> List[Dict[str, Any]]:
        """Fetch active markets from Kalshi."""
        path = f"/markets?status={status}&limit={limit}"
        headers = self._get_headers("GET", path)
        
        async with self.session.get(f"{self.BASE_URL}{path}", headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"Kalshi API error: {resp.status} - {text}")
            data = await resp.json()
            return data.get("markets", [])
    
    async def get_orderbook(self, ticker: str) -> Dict[str, Any]:
        """Fetch orderbook for a specific market."""
        path = f"/markets/{ticker}/orderbook"
        headers = self._get_headers("GET", path)
        
        async with self.session.get(f"{self.BASE_URL}{path}", headers=headers) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            return data.get("orderbook", {})
