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


def atr(candles: list[dict], period: int = 14) -> list[float | None]:
    """Average True Range - volatility measure, Supertrend ke liye chahiye."""
    trs = []
    for i in range(len(candles)):
        h, l, c = candles[i]["high"], candles[i]["low"], candles[i]["close"]
        if i == 0:
            trs.append(h - l)
        else:
            prev_c = candles[i - 1]["close"]
            trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    out: list[float | None] = [None] * (period - 1) if len(trs) >= period else [None] * len(trs)
    if len(trs) < period:
        return out
    seed = sum(trs[:period]) / period
    out.append(seed)
    prev = seed
    for tr in trs[period:]:
        prev = (prev * (period - 1) + tr) / period
        out.append(prev)
    return out


def stochastic(candles: list[dict], period: int = 14, smooth: int = 3):
    """%K aur %D lines."""
    k_raw: list[float | None] = []
    for i in range(len(candles)):
        if i < period - 1:
            k_raw.append(None)
            continue
        window = candles[i - period + 1:i + 1]
        hh = max(c["high"] for c in window)
        ll = min(c["low"] for c in window)
        c = candles[i]["close"]
        k_raw.append(0.0 if hh == ll else (c - ll) / (hh - ll) * 100)
    k_clean = [v for v in k_raw if v is not None]
    d_clean = []
    for i in range(len(k_clean)):
        if i < smooth - 1:
            d_clean.append(None)
        else:
            window = [v for v in k_clean[i - smooth + 1:i + 1]]
            d_clean.append(sum(window) / smooth)
    d: list[float | None] = [None] * (len(k_raw) - len(d_clean)) + d_clean
    return k_raw, d


def adx(candles: list[dict], period: int = 14) -> list[float | None]:
    """Average Directional Index - trend ki strength batata hai (direction nahi)."""
    if len(candles) < period + 1:
        return [None] * len(candles)
    plus_dm, minus_dm, trs = [0.0], [0.0], [candles[0]["high"] - candles[0]["low"]]
    for i in range(1, len(candles)):
        up = candles[i]["high"] - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    def _smooth(vals):
        out = [None] * (period - 1)
        seed = sum(vals[:period])
        out.append(seed)
        prev = seed
        for v in vals[period:]:
            prev = prev - (prev / period) + v
            out.append(prev)
        return out

    smoothed_tr = _smooth(trs)
    smoothed_plus = _smooth(plus_dm)
    smoothed_minus = _smooth(minus_dm)
    dx: list[float | None] = []
    for tr, pdm, mdm in zip(smoothed_tr, smoothed_plus, smoothed_minus):
        if tr is None or tr == 0:
            dx.append(None)
            continue
        pdi = pdm / tr * 100
        mdi = mdm / tr * 100
        dx.append(0.0 if (pdi + mdi) == 0 else abs(pdi - mdi) / (pdi + mdi) * 100)
    clean = [v for v in dx if v is not None]
    if len(clean) < period:
        return [None] * len(candles)
    adx_seed = sum(clean[:period]) / period
    adx_vals = [adx_seed]
    for v in clean[period:]:
        adx_vals.append((adx_vals[-1] * (period - 1) + v) / period)
    return [None] * (len(candles) - len(adx_vals)) + adx_vals


def vwap(candles: list[dict]) -> list[float]:
    """Volume-Weighted Average Price - hamare candles me volume nahi hai, isliye
    typical-price ka running average use karte hain (approximation)."""
    out = []
    cum_tp, count = 0.0, 0
    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        cum_tp += tp
        count += 1
        out.append(cum_tp / count)
    return out


def ichimoku(candles: list[dict], tenkan_period: int = 9, kijun_period: int = 26):
    """Tenkan-sen (conversion) aur Kijun-sen (base) lines."""
    def _mid(period, i):
        if i < period - 1:
            return None
        window = candles[i - period + 1:i + 1]
        return (max(c["high"] for c in window) + min(c["low"] for c in window)) / 2

    tenkan = [_mid(tenkan_period, i) for i in range(len(candles))]
    kijun = [_mid(kijun_period, i) for i in range(len(candles))]
    return tenkan, kijun



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


def _signals_supertrend(candles: list[dict], period: int = 10, multiplier: float = 3.0) -> list[str | None]:
    """Supertrend - trend-following, ATR-based. Trend flip pe signal deta hai.
    Standard textbook formula use karta hai (final bands sirf tighten hote hain
    jab tak price cross na kare, decision PREVIOUS close se hota hai)."""
    a = atr(candles, period)
    n = len(candles)
    final_upper: list[float | None] = [None] * n
    final_lower: list[float | None] = [None] * n
    st_is_upper: list[bool | None] = [None] * n  # True = supertrend line abhi upper-band pe hai (downtrend)

    for i in range(n):
        if a[i] is None:
            continue
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2
        basic_upper = hl2 + multiplier * a[i]
        basic_lower = hl2 - multiplier * a[i]

        prev_final_upper = final_upper[i - 1] if i > 0 else None
        prev_final_lower = final_lower[i - 1] if i > 0 else None
        prev_close = candles[i - 1]["close"] if i > 0 else None

        if prev_final_upper is None:
            final_upper[i] = basic_upper
        else:
            final_upper[i] = basic_upper if (basic_upper < prev_final_upper or prev_close > prev_final_upper) else prev_final_upper

        if prev_final_lower is None:
            final_lower[i] = basic_lower
        else:
            final_lower[i] = basic_lower if (basic_lower > prev_final_lower or prev_close < prev_final_lower) else prev_final_lower

        close = candles[i]["close"]
        prev_is_upper = st_is_upper[i - 1] if i > 0 else None
        if prev_is_upper is None:
            st_is_upper[i] = close <= final_upper[i]
        elif prev_is_upper:  # abhi downtrend (upper band active)
            st_is_upper[i] = close <= final_upper[i]
        else:  # abhi uptrend (lower band active)
            st_is_upper[i] = not (close >= final_lower[i])

    signals: list[str | None] = [None] * n
    for i in range(1, n):
        if st_is_upper[i] is None or st_is_upper[i - 1] is None:
            continue
        if st_is_upper[i - 1] and not st_is_upper[i]:
            signals[i] = "BUY"   # downtrend se uptrend me flip
        elif not st_is_upper[i - 1] and st_is_upper[i]:
            signals[i] = "SELL"  # uptrend se downtrend me flip
    return signals


def _signals_vwap_bounce(candles: list[dict]) -> list[str | None]:
    """Price VWAP se door jaake wapas cross kare to signal (mean-reversion)."""
    v = vwap(candles)
    signals: list[str | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        prev_c, cur_c = candles[i - 1]["close"], candles[i]["close"]
        crossed_up = prev_c <= v[i - 1] and cur_c > v[i]
        crossed_down = prev_c >= v[i - 1] and cur_c < v[i]
        if crossed_up:
            signals[i] = "BUY"
        elif crossed_down:
            signals[i] = "SELL"
    return signals


def _signals_stochastic(candles: list[dict], period: int = 14, oversold: int = 20, overbought: int = 80) -> list[str | None]:
    k, d = stochastic(candles, period)
    signals: list[str | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        if k[i] is None or k[i - 1] is None:
            continue
        if k[i - 1] < oversold <= k[i]:
            signals[i] = "BUY"
        elif k[i - 1] > overbought >= k[i]:
            signals[i] = "SELL"
    return signals


def _signals_adx_trend(candles: list[dict], period: int = 14, threshold: float = 25.0) -> list[str | None]:
    """ADX strong-trend confirm karta hai, direction EMA(9) se leta hai (ADX khud direction nahi deta)."""
    a = adx(candles, period)
    closes = [c["close"] for c in candles]
    fast_ema = ema(closes, 9)
    signals: list[str | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        if a[i] is None or a[i - 1] is None or fast_ema[i] is None:
            continue
        crossing_strong = a[i - 1] < threshold <= a[i]
        if crossing_strong:
            signals[i] = "BUY" if closes[i] > fast_ema[i] else "SELL"
    return signals


def _signals_support_resistance(candles: list[dict], lookback: int = 20) -> list[str | None]:
    """Recent swing high/low (support/resistance) touch pe bounce signal."""
    signals: list[str | None] = [None] * len(candles)
    for i in range(lookback, len(candles)):
        window = candles[i - lookback:i]
        resistance = max(c["high"] for c in window)
        support = min(c["low"] for c in window)
        c = candles[i]
        if c["low"] <= support and c["close"] > c["open"]:
            signals[i] = "BUY"
        elif c["high"] >= resistance and c["close"] < c["open"]:
            signals[i] = "SELL"
    return signals


def _signals_ichimoku_cross(candles: list[dict]) -> list[str | None]:
    """Tenkan-sen/Kijun-sen cross (TK Cross) - Ichimoku ka sabse basic signal."""
    tenkan, kijun = ichimoku(candles)
    signals: list[str | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        if None in (tenkan[i], kijun[i], tenkan[i - 1], kijun[i - 1]):
            continue
        crossed_up = tenkan[i - 1] <= kijun[i - 1] and tenkan[i] > kijun[i]
        crossed_down = tenkan[i - 1] >= kijun[i - 1] and tenkan[i] < kijun[i]
        if crossed_up:
            signals[i] = "BUY"
        elif crossed_down:
            signals[i] = "SELL"
    return signals


# ---- ICT / Smart Money Concepts (simplified, pure price-action based) ----

def _signals_order_block(candles: list[dict], impulse_pct: float = 0.15) -> list[str | None]:
    """ICT Order Block: ek strong (impulsive) move se pehle wali opposite-color
    candle "order block" hoti hai - institution ka entry zone maana jaata hai.
    Simplified: agla candle current se impulse_pct% se zyada move kare to
    is candle ko order block maan ke usi direction ka signal dete hain."""
    signals: list[str | None] = [None] * len(candles)
    for i in range(len(candles) - 1):
        cur, nxt = candles[i], candles[i + 1]
        cur_bearish = cur["close"] < cur["open"]
        cur_bullish = cur["close"] > cur["open"]
        move_pct = abs(nxt["close"] - nxt["open"]) / nxt["open"] * 100 if nxt["open"] else 0
        if move_pct < impulse_pct:
            continue
        nxt_bullish = nxt["close"] > nxt["open"]
        if cur_bearish and nxt_bullish:
            signals[i + 1] = "BUY"   # bullish order block confirm hua
        elif cur_bullish and not nxt_bullish:
            signals[i + 1] = "SELL"  # bearish order block confirm hua
    return signals


def _signals_fair_value_gap(candles: list[dict]) -> list[str | None]:
    """ICT Fair Value Gap (FVG): 3-candle imbalance - candle 1 ki high/low aur
    candle 3 ki low/high ke beech gap ho (candle 2 use nahi chhoo paati) -
    price is gap ko "fill" karne wapas aata hai, isliye gap ki direction
    opposite side ka signal deta hai (fill/retest trade)."""
    signals: list[str | None] = [None] * len(candles)
    for i in range(2, len(candles)):
        c1, c3 = candles[i - 2], candles[i]
        if c1["high"] < c3["low"]:
            signals[i] = "BUY"   # bullish FVG - upar gap, retest se buy
        elif c1["low"] > c3["high"]:
            signals[i] = "SELL"  # bearish FVG - neeche gap, retest se sell
    return signals


def _signals_liquidity_sweep(candles: list[dict], lookback: int = 10) -> list[str | None]:
    """ICT Liquidity Sweep: price recent high/low se thoda upar/neeche wick
    banata hai (stop-loss hunt / liquidity grab) phir ulta band hota hai -
    ye reversal ka strong signal माना jaata hai."""
    signals: list[str | None] = [None] * len(candles)
    for i in range(lookback, len(candles)):
        window = candles[i - lookback:i]
        recent_high = max(c["high"] for c in window)
        recent_low = min(c["low"] for c in window)
        c = candles[i]
        swept_high = c["high"] > recent_high and c["close"] < recent_high
        swept_low = c["low"] < recent_low and c["close"] > recent_low
        if swept_low:
            signals[i] = "BUY"   # neeche liquidity sweep, wapas upar close - bullish
        elif swept_high:
            signals[i] = "SELL"  # upar liquidity sweep, wapas neeche close - bearish
    return signals


def _signals_break_of_structure(candles: list[dict], lookback: int = 15) -> list[str | None]:
    """ICT Break of Structure (BOS): price recent swing high/low ko clearly
    break kar de (close se, sirf wick se nahi) - trend continuation/shift
    confirm karta hai."""
    signals: list[str | None] = [None] * len(candles)
    for i in range(lookback, len(candles)):
        window = candles[i - lookback:i]
        swing_high = max(c["high"] for c in window)
        swing_low = min(c["low"] for c in window)
        c = candles[i]
        if c["close"] > swing_high:
            signals[i] = "BUY"   # bullish structure break
        elif c["close"] < swing_low:
            signals[i] = "SELL"  # bearish structure break
    return signals


def _signals_change_of_character(candles: list[dict], lookback: int = 10) -> list[str | None]:
    """ICT Change of Character (CHoCH): jab market lower-lows bana raha ho
    (downtrend structure) aur achanak ek higher-high bana de, ya vice-versa -
    ye trend ke "character" ke badalne ka pehla ishara hai (BOS se halka,
    early-warning signal)."""
    signals: list[str | None] = [None] * len(candles)
    for i in range(lookback * 2, len(candles)):
        prev_window = candles[i - lookback * 2:i - lookback]
        cur_window = candles[i - lookback:i]
        prev_high, prev_low = max(c["high"] for c in prev_window), min(c["low"] for c in prev_window)
        cur_high, cur_low = max(c["high"] for c in cur_window), min(c["low"] for c in cur_window)
        was_downtrend = cur_high < prev_high and cur_low < prev_low
        was_uptrend = cur_high > prev_high and cur_low > prev_low
        c = candles[i]
        if was_downtrend and c["close"] > cur_high:
            signals[i] = "BUY"   # downtrend tha, ab higher-high - character change bullish
        elif was_uptrend and c["close"] < cur_low:
            signals[i] = "SELL"  # uptrend tha, ab lower-low - character change bearish
    return signals


def _signals_equal_highs_lows(candles: list[dict], lookback: int = 20, tolerance_pct: float = 0.1) -> list[str | None]:
    """ICT Equal Highs/Equal Lows (Liquidity Pool): jab 2+ swing highs/lows
    almost same level pe ho, wahan bahut logon ka stop-loss/pending order
    jama hota hai ("liquidity pool") - price aksar wahan jaake reverse
    hoti hai (liquidity sweep se pehle ka setup)."""
    signals: list[str | None] = [None] * len(candles)
    for i in range(lookback, len(candles)):
        window = candles[i - lookback:i]
        highs = [c["high"] for c in window]
        lows = [c["low"] for c in window]
        c = candles[i]
        top = max(highs)
        near_equal_highs = sum(1 for h in highs if abs(h - top) / top * 100 <= tolerance_pct) >= 2
        bottom = min(lows)
        near_equal_lows = sum(1 for l in lows if abs(l - bottom) / bottom * 100 <= tolerance_pct) >= 2
        if near_equal_lows and c["low"] <= bottom * 1.001 and c["close"] > c["open"]:
            signals[i] = "BUY"   # equal-lows liquidity pool sweep, bullish reversal
        elif near_equal_highs and c["high"] >= top * 0.999 and c["close"] < c["open"]:
            signals[i] = "SELL"  # equal-highs liquidity pool sweep, bearish reversal
    return signals


def _signals_breaker_block(candles: list[dict], lookback: int = 15) -> list[str | None]:
    """ICT Breaker Block: ek failed order block jo structure break hone ke
    baad polarity switch kar leta hai (jo pehle resistance tha, ab support
    ban jaata hai, ya vice-versa) - price wapas us zone pe retest karke
    continuation deta hai."""
    signals: list[str | None] = [None] * len(candles)
    for i in range(lookback, len(candles) - 1):
        window = candles[i - lookback:i]
        swing_high = max(c["high"] for c in window)
        swing_low = min(c["low"] for c in window)
        c, nxt = candles[i], candles[i + 1]
        # Structure break hua ho, phir agla candle wapas us level pe retest kare
        if c["close"] > swing_high and nxt["low"] <= swing_high:
            signals[i + 1] = "BUY"   # bullish breaker retest
        elif c["close"] < swing_low and nxt["high"] >= swing_low:
            signals[i + 1] = "SELL"  # bearish breaker retest
    return signals


def _signals_premium_discount(candles: list[dict], lookback: int = 30) -> list[str | None]:
    """ICT Premium/Discount Zone (Optimal Trade Entry): recent swing range ko
    Fibonacci se 3 zones me baantate hain - Discount (neeche 30%, buy zone),
    Equilibrium (beech), Premium (upar 30%, sell zone). Price discount me
    jaaye to buy, premium me jaaye to sell (institutional entry theory)."""
    signals: list[str | None] = [None] * len(candles)
    for i in range(lookback, len(candles)):
        window = candles[i - lookback:i]
        swing_high = max(c["high"] for c in window)
        swing_low = min(c["low"] for c in window)
        rng = swing_high - swing_low
        if rng <= 0:
            continue
        c = candles[i]
        discount_level = swing_low + rng * 0.3
        premium_level = swing_low + rng * 0.7
        if c["close"] <= discount_level and c["close"] > c["open"]:
            signals[i] = "BUY"   # discount zone me bullish candle
        elif c["close"] >= premium_level and c["close"] < c["open"]:
            signals[i] = "SELL"  # premium zone me bearish candle
    return signals


def _signals_mitigation_block(candles: list[dict], lookback: int = 15) -> list[str | None]:
    """ICT Mitigation Block: ek impulsive move ke baad, price wapas us candle
    ke "origin" (open price) tak aata hai jahan se move shuru hua tha -
    institutions apna baaki hua position wahan "mitigate"/complete karte
    hain, isliye wahan se continuation expected hoti hai."""
    signals: list[str | None] = [None] * len(candles)
    for i in range(lookback, len(candles)):
        # Pichhle lookback candles me sabse bada single-candle impulsive move dhoondo
        window = candles[i - lookback:i]
        best_idx, best_move = None, 0
        for j, c in enumerate(window):
            move = abs(c["close"] - c["open"])
            if move > best_move:
                best_move, best_idx = move, j
        if best_idx is None or best_move == 0:
            continue
        origin_candle = window[best_idx]
        was_bullish_impulse = origin_candle["close"] > origin_candle["open"]
        c = candles[i]
        # Price wapas origin candle ke open ke paas aaya
        near_origin = abs(c["close"] - origin_candle["open"]) / origin_candle["open"] * 100 <= 0.15
        if near_origin and was_bullish_impulse and c["close"] > c["open"]:
            signals[i] = "BUY"
        elif near_origin and not was_bullish_impulse and c["close"] < c["open"]:
            signals[i] = "SELL"
    return signals


STRATEGY_CATALOG = {
    "ema_crossover": {"label": "EMA Crossover (9/21)", "category": "Classic", "fn": _signals_ema_crossover},
    "rsi_reversal": {"label": "RSI Reversal (14, 30/70)", "category": "Classic", "fn": _signals_rsi_reversal},
    "macd_cross": {"label": "MACD Cross", "category": "Classic", "fn": _signals_macd_cross},
    "bollinger_bounce": {"label": "Bollinger Band Bounce", "category": "Classic", "fn": _signals_bollinger_bounce},
    "candle_pattern": {"label": "Candle Pattern (Engulfing)", "category": "Classic", "fn": _signals_candle_pattern},
    "supertrend": {"label": "Supertrend", "category": "Classic", "fn": _signals_supertrend},
    "vwap_bounce": {"label": "VWAP Bounce", "category": "Classic", "fn": _signals_vwap_bounce},
    "stochastic": {"label": "Stochastic (14, 20/80)", "category": "Classic", "fn": _signals_stochastic},
    "adx_trend": {"label": "ADX Trend Strength", "category": "Classic", "fn": _signals_adx_trend},
    "support_resistance": {"label": "Support/Resistance Bounce", "category": "Classic", "fn": _signals_support_resistance},
    "ichimoku_cross": {"label": "Ichimoku TK Cross", "category": "Classic", "fn": _signals_ichimoku_cross},
    "order_block": {"label": "ICT Order Block", "category": "ICT / Smart Money", "fn": _signals_order_block},
    "fair_value_gap": {"label": "ICT Fair Value Gap (FVG)", "category": "ICT / Smart Money", "fn": _signals_fair_value_gap},
    "liquidity_sweep": {"label": "ICT Liquidity Sweep", "category": "ICT / Smart Money", "fn": _signals_liquidity_sweep},
    "break_of_structure": {"label": "ICT Break of Structure (BOS)", "category": "ICT / Smart Money", "fn": _signals_break_of_structure},
    "change_of_character": {"label": "ICT Change of Character (CHoCH)", "category": "ICT / Smart Money", "fn": _signals_change_of_character},
    "equal_highs_lows": {"label": "ICT Equal Highs/Lows (Liquidity Pool)", "category": "ICT / Smart Money", "fn": _signals_equal_highs_lows},
    "breaker_block": {"label": "ICT Breaker Block", "category": "ICT / Smart Money", "fn": _signals_breaker_block},
    "premium_discount": {"label": "ICT Premium/Discount (OTE)", "category": "ICT / Smart Money", "fn": _signals_premium_discount},
    "mitigation_block": {"label": "ICT Mitigation Block", "category": "ICT / Smart Money", "fn": _signals_mitigation_block},
}


def _simulate(candles: list[dict], signals: list[str | None], expiry_candles: int) -> dict:
    """Har signal ke expiry_candles baad price check karta hai (binary-style):
    BUY signal ke baad price upar gaya to WIN, neeche gaya to LOSS. Same
    ulta SELL ke liye. Ye ek simplified fixed-expiry model hai (OTC binary
    jaisa), pip-target wala forex-style nahi. Catalog strategies aur custom
    (user-defined) strategies dono isi ek simulator se guzarte hain."""
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
        "total_signals": total, "wins": wins, "losses": losses, "draws": draws,
        "win_rate": win_rate, "trades": trades[-100:],  # UI ko zyada bhaari na karein
    }


def run_backtest(candles: list[dict], strategy: str, expiry_candles: int = 3) -> dict:
    cfg = STRATEGY_CATALOG.get(strategy)
    if not cfg:
        raise ValueError(f"Unknown strategy: {strategy}")
    signals = cfg["fn"](candles)
    result = _simulate(candles, signals, expiry_candles)
    result["strategy"] = strategy
    result["strategy_label"] = cfg["label"]
    result["category"] = cfg["category"]
    return result


# ============================================================
# CUSTOM STRATEGY BUILDER - apni khud ki strategy banao (parameters set
# karke), koi coding nahi chahiye. Har rule ek chhota building-block hai;
# 1-3 rules ko AND se combine kar sakte ho (sab rules same direction ka
# signal dein tabhi final signal banega).
# ============================================================
def _rule_ema_cross(candles: list[dict], fast: int, slow: int) -> list[str | None]:
    return _signals_ema_crossover(candles, fast, slow)


def _rule_rsi(candles: list[dict], period: int, oversold: int, overbought: int) -> list[str | None]:
    return _signals_rsi_reversal(candles, period, oversold, overbought)


def _rule_price_vs_ema(candles: list[dict], period: int) -> list[str | None]:
    """Price EMA(period) ko cross kare - simple trend-following rule."""
    closes = [c["close"] for c in candles]
    e = ema(closes, period)
    signals: list[str | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        if e[i] is None or e[i - 1] is None:
            continue
        prev_c, cur_c = closes[i - 1], closes[i]
        if prev_c <= e[i - 1] and cur_c > e[i]:
            signals[i] = "BUY"
        elif prev_c >= e[i - 1] and cur_c < e[i]:
            signals[i] = "SELL"
    return signals


def _rule_bollinger(candles: list[dict], period: int, std_dev: float) -> list[str | None]:
    return _signals_bollinger_bounce(candles, period, std_dev)


def _rule_stochastic(candles: list[dict], period: int, oversold: int, overbought: int) -> list[str | None]:
    return _signals_stochastic(candles, period, oversold, overbought)


CUSTOM_RULE_TYPES = {
    "ema_cross": {"label": "EMA Crossover", "fn": _rule_ema_cross,
                  "params": {"fast": 9, "slow": 21}},
    "rsi": {"label": "RSI Threshold", "fn": _rule_rsi,
            "params": {"period": 14, "oversold": 30, "overbought": 70}},
    "price_vs_ema": {"label": "Price vs EMA Cross", "fn": _rule_price_vs_ema,
                      "params": {"period": 50}},
    "bollinger": {"label": "Bollinger Band Bounce", "fn": _rule_bollinger,
                  "params": {"period": 20, "std_dev": 2.0}},
    "stochastic": {"label": "Stochastic Threshold", "fn": _rule_stochastic,
                   "params": {"period": 14, "oversold": 20, "overbought": 80}},
}


def build_custom_signals(candles: list[dict], rules: list[dict]) -> list[str | None]:
    """rules: [{"type": "ema_cross", "params": {"fast": 5, "slow": 20}}, ...]
    Har rule apna BUY/SELL/None signal deta hai; sirf usi candle pe final
    signal banega jahan SAARE rules EK HI direction bolein (AND logic) -
    isse zyada confident, kam-noise wale signals milte hain jab multiple
    rules combine karte ho."""
    if not rules:
        return []
    per_rule_signals = []
    for rule in rules:
        rtype = rule.get("type")
        cfg = CUSTOM_RULE_TYPES.get(rtype)
        if not cfg:
            raise ValueError(f"Unknown rule type: {rtype}")
        params = {**cfg["params"], **(rule.get("params") or {})}
        per_rule_signals.append(cfg["fn"](candles, **params))

    n = len(candles)
    final: list[str | None] = [None] * n
    for i in range(n):
        votes = [s[i] for s in per_rule_signals]
        if all(v == "BUY" for v in votes):
            final[i] = "BUY"
        elif all(v == "SELL" for v in votes):
            final[i] = "SELL"
    return final


def run_custom_backtest(candles: list[dict], rules: list[dict], expiry_candles: int = 3) -> dict:
    signals = build_custom_signals(candles, rules)
    result = _simulate(candles, signals, expiry_candles)
    result["strategy"] = "custom"
    result["strategy_label"] = "Custom Strategy (" + " + ".join(
        CUSTOM_RULE_TYPES.get(r.get("type"), {}).get("label", r.get("type")) for r in rules
    ) + ")"
    result["category"] = "Custom"
    return result


def compare_all_strategies(candles: list[dict], expiry_candles: int = 3) -> list[dict]:
    """Saari strategies ko SAME candles pe chalata hai aur best-se-worst (win_rate
    ke hisaab se) sorted list deta hai - taaki compare kar sako kaunsi strategy
    is pair/timeframe pe sabse zyada profitable hai."""
    results = []
    for key in STRATEGY_CATALOG:
        r = run_backtest(candles, key, expiry_candles)
        r.pop("trades", None)  # compare view me poori trade-list nahi chahiye, halka rakho
        results.append(r)
    results.sort(key=lambda r: (r["win_rate"], r["total_signals"]), reverse=True)
    return results
