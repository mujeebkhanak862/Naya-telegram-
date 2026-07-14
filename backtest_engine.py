"""
Backtest engine - purane signals ki tarah nahi, balki khud ke defined
strategies (EMA Crossover, RSI Reversal, MACD Cross, Bollinger Bounce) ko
historical candle data pe chala ke dekhta hai ki wo kitna accurate hota
(binary-options style: signal ke N candles baad price sahi direction me
gayi ya nahi, wahi WIN/LOSS decide karta hai - fixed payout wale OTC jaisa,
pip-target wala forex-style nahi, isliye simple aur consistent rehta hai).

Data sources:
  - Binance   : crypto pairs (BTCUSDT, ETHUSDT, ...), free public API
  - TwelveData: forex/OTC pairs (EURUSD, GBPUSD, ...), free-tier API key chahiye
"""
import httpx

# ============================================================
# DATA FETCHING
# ============================================================
async def fetch_binance_klines(symbol: str, interval: str, limit: int) -> list[dict]:
    """Binance se OHLC candles laata hai. symbol jaise 'BTCUSDT', interval jaise '1m','5m','15m','1h'."""
    url = "https://api.binance.com/api/v3/klines"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params={"symbol": symbol.upper(), "interval": interval, "limit": limit})
        r.raise_for_status()
        raw = r.json()
    return [
        {"time": row[0], "open": float(row[1]), "high": float(row[2]),
         "low": float(row[3]), "close": float(row[4])}
        for row in raw
    ]


async def fetch_twelvedata_candles(symbol: str, interval: str, outputsize: int, api_key: str) -> list[dict]:
    """TwelveData se OHLC candles laata hai (forex/OTC pairs ke liye).
    symbol jaise 'EUR/USD', interval jaise '1min','5min','15min','1h'."""
    url = "https://api.twelvedata.com/time_series"
    pair = symbol.upper().replace("OTC", "").strip("-/ ")
    if "/" not in pair and len(pair) == 6:
        pair = f"{pair[:3]}/{pair[3:]}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params={
            "symbol": pair, "interval": interval, "outputsize": outputsize, "apikey": api_key,
        })
        r.raise_for_status()
        data = r.json()
    if data.get("status") == "error":
        raise ValueError(data.get("message", "TwelveData error"))
    values = data.get("values", [])
    candles = [
        {"time": v["datetime"], "open": float(v["open"]), "high": float(v["high"]),
         "low": float(v["low"]), "close": float(v["close"])}
        for v in values
    ]
    candles.reverse()  # TwelveData naye-se-purane deta hai, humein purane-se-naye chahiye
    return candles


# ============================================================
# INDICATORS (pure Python, koi external TA library nahi chahiye)
# ============================================================
def ema(values: list[float], period: int) -> list[float | None]:
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    out: list[float | None] = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    if len(values) < period + 1:
        return [None] * len(values)
    out: list[float | None] = [None] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
    out.append(100 - (100 / (1 + rs)))
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        out.append(100 - (100 / (1 + rs)))
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    clean = [v for v in macd_line if v is not None]
    signal_clean = ema(clean, signal) if len(clean) >= signal else [None] * len(clean)
    signal_line: list[float | None] = [None] * (len(macd_line) - len(signal_clean)) + signal_clean
    return macd_line, signal_line


def bollinger_bands(values: list[float], period: int = 20, num_std: float = 2.0):
    upper: list[float | None] = []
    lower: list[float | None] = []
    mid: list[float | None] = []
    for i in range(len(values)):
        if i < period - 1:
            upper.append(None); lower.append(None); mid.append(None)
            continue
        window = values[i - period + 1:i + 1]
        m = sum(window) / period
        variance = sum((x - m) ** 2 for x in window) / period
        sd = variance ** 0.5
        mid.append(m)
        upper.append(m + num_std * sd)
        lower.append(m - num_std * sd)
    return upper, mid, lower


# ============================================================
# STRATEGIES - har ek candle-index pe "BUY"/"SELL"/None signal deta hai
# ============================================================
def _signals_ema_crossover(candles: list[dict], fast: int = 9, slow: int = 21) -> list[str | None]:
    closes = [c["close"] for c in candles]
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    signals: list[str | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        if None in (ema_fast[i], ema_slow[i], ema_fast[i - 1], ema_slow[i - 1]):
            continue
        crossed_up = ema_fast[i - 1] <= ema_slow[i - 1] and ema_fast[i] > ema_slow[i]
        crossed_down = ema_fast[i - 1] >= ema_slow[i - 1] and ema_fast[i] < ema_slow[i]
        if crossed_up:
            signals[i] = "BUY"
        elif crossed_down:
            signals[i] = "SELL"
    return signals


def _signals_rsi_reversal(candles: list[dict], period: int = 14, oversold: int = 30, overbought: int = 70) -> list[str | None]:
    closes = [c["close"] for c in candles]
    r = rsi(closes, period)
    signals: list[str | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        if r[i] is None or r[i - 1] is None:
            continue
        if r[i - 1] < oversold <= r[i]:
            signals[i] = "BUY"   # oversold se bahar nikal raha hai - reversal up
        elif r[i - 1] > overbought >= r[i]:
            signals[i] = "SELL"  # overbought se neeche aa raha hai - reversal down
    return signals


def _signals_macd_cross(candles: list[dict]) -> list[str | None]:
    closes = [c["close"] for c in candles]
    macd_line, signal_line = macd(closes)
    signals: list[str | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        if None in (macd_line[i], signal_line[i], macd_line[i - 1], signal_line[i - 1]):
            continue
        crossed_up = macd_line[i - 1] <= signal_line[i - 1] and macd_line[i] > signal_line[i]
        crossed_down = macd_line[i - 1] >= signal_line[i - 1] and macd_line[i] < signal_line[i]
        if crossed_up:
            signals[i] = "BUY"
        elif crossed_down:
            signals[i] = "SELL"
    return signals


def _signals_bollinger_bounce(candles: list[dict], period: int = 20, num_std: float = 2.0) -> list[str | None]:
    closes = [c["close"] for c in candles]
    upper, mid, lower = bollinger_bands(closes, period, num_std)
    signals: list[str | None] = [None] * len(candles)
    for i in range(len(candles)):
        if lower[i] is None or upper[i] is None:
            continue
        if closes[i] <= lower[i]:
            signals[i] = "BUY"   # lower band touch - bounce up expected
        elif closes[i] >= upper[i]:
            signals[i] = "SELL"  # upper band touch - bounce down expected
    return signals


def _signals_candle_pattern(candles: list[dict]) -> list[str | None]:
    """Simple bullish/bearish engulfing pattern detection."""
    signals: list[str | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        prev, cur = candles[i - 1], candles[i]
        prev_bearish = prev["close"] < prev["open"]
        prev_bullish = prev["close"] > prev["open"]
        cur_bullish = cur["close"] > cur["open"]
        cur_bearish = cur["close"] < cur["open"]
        engulf_up = prev_bearish and cur_bullish and cur["close"] > prev["open"] and cur["open"] < prev["close"]
        engulf_down = prev_bullish and cur_bearish and cur["close"] < prev["open"] and cur["open"] > prev["close"]
        if engulf_up:
            signals[i] = "BUY"
        elif engulf_down:
            signals[i] = "SELL"
    return signals


STRATEGY_CATALOG = {
    "ema_crossover": {"label": "EMA Crossover (9/21)", "fn": _signals_ema_crossover},
    "rsi_reversal": {"label": "RSI Reversal (14, 30/70)", "fn": _signals_rsi_reversal},
    "macd_cross": {"label": "MACD Cross", "fn": _signals_macd_cross},
    "bollinger_bounce": {"label": "Bollinger Band Bounce", "fn": _signals_bollinger_bounce},
    "candle_pattern": {"label": "Candle Pattern (Engulfing)", "fn": _signals_candle_pattern},
}


def run_backtest(candles: list[dict], strategy: str, expiry_candles: int = 3) -> dict:
    """Har signal ke expiry_candles baad price check karta hai (binary-style):
    BUY signal ke baad price upar gaya to WIN, neeche gaya to LOSS. Same
    ulta SELL ke liye. Ye ek simplified fixed-expiry model hai (OTC binary
    jaisa), pip-target wala forex-style nahi."""
    cfg = STRATEGY_CATALOG.get(strategy)
    if not cfg:
        raise ValueError(f"Unknown strategy: {strategy}")

    signals = cfg["fn"](candles)
    trades = []
    for i, sig in enumerate(signals):
        if sig is None:
            continue
        exit_idx = i + expiry_candles
        if exit_idx >= len(candles):
            continue  # backtest data khatam ho gaya, is signal ka result nahi mil sakta
        entry_price = candles[i]["close"]
        exit_price = candles[exit_idx]["close"]
        moved_up = exit_price > entry_price
        won = (sig == "BUY" and moved_up) or (sig == "SELL" and not moved_up)
        trades.append({
            "index": i, "time": candles[i]["time"], "direction": sig,
            "entry_price": entry_price, "exit_price": exit_price,
            "result": "win" if won else ("loss" if exit_price != entry_price else "draw"),
        })

    wins = sum(1 for t in trades if t["result"] == "win")
    losses = sum(1 for t in trades if t["result"] == "loss")
    draws = sum(1 for t in trades if t["result"] == "draw")
    total = len(trades)
    win_rate = round((wins / total) * 100, 1) if total else 0.0

    return {
        "strategy": strategy, "strategy_label": cfg["label"],
        "total_signals": total, "wins": wins, "losses": losses, "draws": draws,
        "win_rate": win_rate, "trades": trades[-100:],  # UI ko zyada bhaari na karein
    }
