"""
Broker/Bridge catalog. Teen categories: binary, forex, crypto. Har broker ka
apna connection_type hota hai jo decide karta hai UI me kaunse fields dikhne
chahiye aur "Test" button kaise verify karega:

  session_bridge   - Binary platforms (Quotex/Pocket Option/Binomo/IQ Option).
                      Ye platforms ka koi official public API nahi hai, isliye
                      session-id + apni khud ki bridge (Playwright/Express
                      server, jaisa Ibrahim pehle bana chuka hai) ke through
                      jaate hain. Test = bridge ke /status endpoint ko ping.
  custom_bridge    - Apni khud ki bridge API (kisi bhi category ke liye) -
                      bridge_url + Bearer token. Test = GET {url}/status.
  metaapi          - MT5 (Exness waghera) MetaApi.cloud ke through, terminal
                      install kiye bina. Test = MetaApi provisioning API call.
  exchange_keys    - Crypto exchange API key+secret (Binance). Test = signed
                      account-info request.
  exchange_keys_passphrase - OKX jaise exchanges jinhe passphrase bhi chahiye.
                      (Abhi live test implement nahi hai - sirf save/verify
                      format; asli verify apne bridge/terminal se karna hoga.)
"""
import hashlib
import hmac
import time
import httpx

BROKER_CATALOG = {
    # ---- Binary options ----
    "quotex":       {"label": "Quotex", "category": "binary", "conn_type": "session_bridge"},
    "pocketoption": {"label": "Pocket Option", "category": "binary", "conn_type": "session_bridge"},
    "binomo":       {"label": "Binomo", "category": "binary", "conn_type": "session_bridge"},
    "iqoption":     {"label": "IQ Option", "category": "binary", "conn_type": "session_bridge"},
    "custom_bridge_binary": {"label": "Apni Bridge API (Binary)", "category": "binary", "conn_type": "custom_bridge"},

    # ---- Forex / MT5 ----
    "metaapi_mt5":  {"label": "MT5 / Exness (MetaApi.cloud)", "category": "forex", "conn_type": "metaapi"},
    "custom_bridge_forex": {"label": "Apni Bridge API (Forex/MT5)", "category": "forex", "conn_type": "custom_bridge"},

    # ---- Crypto ----
    "binance":      {"label": "Binance", "category": "crypto", "conn_type": "exchange_keys"},
    "bybit":        {"label": "Bybit", "category": "crypto", "conn_type": "exchange_keys"},
    "okx":          {"label": "OKX", "category": "crypto", "conn_type": "exchange_keys_passphrase"},
    "custom_bridge_crypto": {"label": "Apni Bridge API (Crypto)", "category": "crypto", "conn_type": "custom_bridge"},
}

# Har conn_type ko kaunse fields chahiye - frontend isse form dynamically banata hai
CONN_TYPE_FIELDS = {
    "session_bridge": ["session_id", "bridge_url", "bridge_token"],
    "custom_bridge":  ["bridge_url", "bridge_token"],
    "metaapi":        ["api_key", "account_id"],
    "exchange_keys":  ["api_key", "api_secret"],
    "exchange_keys_passphrase": ["api_key", "api_secret", "passphrase"],
}


async def _test_bridge(bridge_url: str, bridge_token: str | None) -> tuple[bool, str | None]:
    """Apni khud ki Playwright/Express bridge (jo /status, /trade, /reconnect
    endpoints deti hai) - /status ping karke check karta hai zinda hai ya nahi."""
    if not bridge_url:
        return False, "Bridge URL nahi diya"
    url = bridge_url.rstrip("/") + "/status"
    headers = {"Authorization": f"Bearer {bridge_token}"} if bridge_token else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                return True, None
            return False, f"Bridge ne HTTP {r.status_code} diya"
    except Exception as e:
        return False, str(e)[:200]


async def _test_metaapi(api_key: str, account_id: str | None) -> tuple[bool, str | None]:
    """MetaApi.cloud provisioning API se account status check karta hai."""
    if not account_id:
        return False, "MetaApi account_id nahi diya"
    url = f"https://mt-provisioning-api-v1.agiliumtrade.ai/users/current/accounts/{account_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers={"auth-token": api_key})
            if r.status_code == 200:
                return True, None
            return False, f"MetaApi ne HTTP {r.status_code} diya: {r.text[:150]}"
    except Exception as e:
        return False, str(e)[:200]


async def _test_binance(api_key: str, api_secret: str) -> tuple[bool, str | None]:
    """Binance signed account-info request se key/secret verify karta hai."""
    try:
        ts = int(time.time() * 1000)
        query = f"timestamp={ts}"
        sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"https://api.binance.com/api/v3/account?{query}&signature={sig}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers={"X-MBX-APIKEY": api_key})
            if r.status_code == 200:
                return True, None
            return False, f"Binance ne HTTP {r.status_code} diya: {r.text[:150]}"
    except Exception as e:
        return False, str(e)[:200]


async def test_broker_connection(broker: str, conn_type: str, fields: dict) -> tuple[bool, str | None]:
    """UI ke 'Test' button ke liye - broker/conn_type ke hisaab se sahi
    tareeke se connection verify karta hai. Kuch exchanges (Bybit/OKX) ke
    liye abhi live test implement nahi hai - unke liye keys save ho jaati
    hain lekin status 'untested' hi rahega jab tak khud bridge se verify na ho."""
    if conn_type in ("session_bridge", "custom_bridge"):
        return await _test_bridge(fields.get("bridge_url"), fields.get("bridge_token"))
    if conn_type == "metaapi":
        return await _test_metaapi(fields.get("api_key"), fields.get("account_id"))
    if conn_type == "exchange_keys" and broker == "binance":
        return await _test_binance(fields.get("api_key"), fields.get("api_secret"))
    return False, "Is broker ke liye live test abhi supported nahi hai - keys save ho gayi hain, apni bridge/terminal se manually verify karo"
