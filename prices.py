"""
Live price fetching. Crypto -> Binance (free). Forex -> Finnhub (free tier).
OTC pairs -> live price nahi milti, unka result Telegram message ke text se aata hai.
"""
import os
import httpx

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")


async def get_crypto_price(pair: str) -> float | None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://api.binance.com/api/v3/ticker/price?symbol={pair}")
            data = r.json()
            return float(data["price"]) if "price" in data else None
    except Exception:
        return None


async def get_forex_price(pair: str) -> float | None:
    if not FINNHUB_API_KEY:
        return None
    try:
        symbol = f"OANDA:{pair[:3]}_{pair[3:6]}"
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": symbol, "token": FINNHUB_API_KEY},
            )
            data = r.json()
            return float(data["c"]) if data.get("c") else None
    except Exception:
        return None


async def get_live_price(pair: str, is_otc: bool) -> float | None:
    if is_otc:
        return None
    clean_pair = pair.replace("-OTC", "").upper()
    if clean_pair.startswith(("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE")):
        symbol = clean_pair if clean_pair.endswith("USDT") else clean_pair.replace("USD", "") + "USDT"
        return await get_crypto_price(symbol)
    return await get_forex_price(clean_pair)
