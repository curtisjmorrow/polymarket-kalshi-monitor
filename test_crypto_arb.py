import asyncio
from crypto_client import CryptoClient
from temporal_detector import TemporalArbDetector

async def test_detectors():
    print("Testing CryptoClient...")
    async with CryptoClient() as crypto:
        prices = await crypto.get_all_prices()
        print(f"Prices: {prices}")
        assert "BTC" in prices
        
    print("\nTesting TemporalArbDetector...")
    detector = TemporalArbDetector("test_arb.csv")
    
    # Test 1: Spot moved ABOVE threshold, market is too cheap
    market = {"title": "Will Bitcoin be above $100,000 by tonight?"}
    opp = detector.detect_spot_lag(market, 105000, 0.20, "test")
    print(f"Test 1 (Spot High, Market Low): {opp['side'] if opp else 'FAILED'}")
    assert opp and opp['side'] == "BUY YES"
    
    # Test 2: Spot moved BELOW threshold, market is too expensive
    market = {"title": "Will ETH be above $3,000?"}
    opp = detector.detect_spot_lag(market, 2500, 0.80, "test")
    print(f"Test 2 (Spot Low, Market High): {opp['side'] if opp else 'FAILED'}")
    assert opp and opp['side'] == "BUY NO"
    
    # Test 3: Normal conditions (no arb)
    market = {"title": "Will BTC be above $100,000?"}
    opp = detector.detect_spot_lag(market, 95000, 0.10, "test")
    print(f"Test 3 (Normal): {'FAILED' if opp else 'PASSED'}")
    assert opp is None
    
    print("\nAll tests passed!")

if __name__ == "__main__":
    asyncio.run(test_detectors())
