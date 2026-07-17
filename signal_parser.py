"""
Telegram message ke text se trading signal ka structured data nikalta hai -
pair, direction (BUY/SELL), entry, multiple TPs (TP1/TP2/TP3...), SL. Agar signal nahi mila to None.
"""
import re

# Common label/boilerplate words jo signal templates me bahut aate hain
# (jaise "PAIR: USD/BDT", "RISK - 3%") - extra safety ke liye inhe bhi
# explicitly exclude karte hain (whitelist ke upar defense-in-depth).
_NON_CURRENCY_WORDS = r'PAIR\b|RISK\b|TIME\b|ALERT\b|ENTRY\b|TRADE\b|EXPIRY\b|SIGNAL\b|TARGET\b'

# Majors ke alawa OTC binary channels bahut saari "exotic" currencies bhi
# use karte hain (jaise USD/BDT, USD/INR, USD/PKR) - unhe bhi quote-side
# list me shaamil kiya hai taaki asli pair sahi pakda jaaye.
_EXOTIC_CCY = (
    "BDT|INR|PKR|NGN|EGP|ZAR|BRL|IDR|PHP|VND|THB|MXN|RUB|KES|DZD|COP|"
    "ARS|LKR|MMK|NPR|BND|MYR|SGD|HKD|CNH|CNY|KRW|SAR|AED|QAR|KWD|BHD|OMR|"
    "JOD|LBP|UAH|PLN|CZK|HUF|BGN|UGX|GHS|TZS|ETB|TND|AZN|GEL|KZT|"
    "CLP|PEN|UYU|PYG|VES|DOP|JMD|TTD|XOF|XAF|IQD|AFN|UZS|TJS|KGS|MNT|"
    "TWD|LAK|KHR|MDL|RSD|MKD|ISK|ZMW|MWK|MZN|BWP|NAD|SZL|LSL|SCR|MUR|"
    "XPT|XPD"
)
# NOTE: TRY (Turkish Lira), ALL (Albanian Lek), MAD (Moroccan Dirham), RON
# (Romanian Leu), BOB (Bolivian Boliviano) jaan-boojh kar exclude kiye hain -
# ye sab common English words/names bhi hain ("try", "for all", "so mad",
# "Ron", "Bob") aur random prose ko galti se currency pair samajh lete the
# (jaise "highly profitable FOR ALL members" -> galat pair "FORALL" ban raha tha).

# Crypto tickers jo OTC/crypto channels "base" currency ki tarah likhte hain
# (jaise BTCUSD, ETHUSDT). Pehle [A-Z]{3,6} se KOI bhi word match ho jaata
# tha base ki tarah (jaise "Secret USD" -> galat pair "SECRETUSD") - ab
# sirf inhi whitelisted codes ko hi base maana jaata hai.
_CRYPTO_TICKERS = (
    "BTC|ETH|SOL|XRP|DOGE|ADA|LTC|DOT|LINK|AVAX|MATIC|UNI|ATOM|NEAR|APT|"
    "ARB|OP|SUI|TON|TRX|XLM|ALGO|FIL|SHIB|PEPE|WIF|INJ|TIA|RNDR|FET|SEI|"
    "STX|IMX|GRT|AAVE|MKR|SAND|MANA|AXS|EOS|ICP|ETC|BCH|VET|THETA|FLOW|"
    "EGLD|KAS|HBAR|BONK|JUP|JTO|PYTH|RAY|BNB|USDC"
)
# Base-currency whitelist: fiat majors + exotic fiat + crypto tickers.
# Quote-currency whitelist: fiat majors + exotic fiat (crypto shayad hi kabhi quote hota hai).
_BASE_CCY = f"USDT?|JPY|GBP|EUR|CAD|CHF|AUD|NZD|XAU|XAG|{_EXOTIC_CCY}|{_CRYPTO_TICKERS}"
_QUOTE_CCY = f"USDT?|JPY|GBP|EUR|CAD|CHF|AUD|NZD|XAU|XAG|{_EXOTIC_CCY}"

PAIR_PATTERN = re.compile(
    rf'\b(?!{_NON_CURRENCY_WORDS})((?:{_BASE_CCY}))[ \t]*[/_-]?[ \t]*'
    rf'((?:{_QUOTE_CCY}))'
    rf'[ \t]*[/_\-\(\[]*[ \t]*((?:OTC))?[\)\]]*\b',
    re.IGNORECASE,
)
# BUY/SELL/CALL/PUT/LONG/SHORT ke alawa channels ye bhi bahut use karte hain:
# UP/DOWN, HIGHER/LOWER, HIGH/LOW. Emojis (⬆️⬇️🔼🔽📈📉🟢🔴) ALAG pattern me
# rakhe hain kyunki bahut se channels inhe sirf DECORATIVE BULLET ki tarah use
# karte hain (jaise "🔴XAUUSD BUY 4055"), signal ki direction ke liye nahi -
# isliye explicit word hamesha priority leta hai, emoji sirf tabhi use hota
# hai jab koi explicit BUY/SELL/CALL/PUT word text me bilkul na ho.
WORD_DIRECTION_PATTERN = re.compile(
    r'\b(BUY|SELL|CALL|PUT|LONG|SHORT|UP|DOWN|HIGHER|LOWER|HIGH|LOW)\b',
    re.IGNORECASE,
)
EMOJI_DIRECTION_PATTERN = re.compile(r'([⬆🔼📈🟢])|([⬇🔽📉🔴])')
ENTRY_PATTERN = re.compile(r'(?:ENTRY|EP)[:\s]+([\d.]+)', re.IGNORECASE)
# Numbered TPs: "TP1: 1.2345", "TP2 1.2350" waghera
# Kuch channels TP number superscript Unicode digits me likhte hain (TP¹, TP²,
# TP³...) stylish formatting ke liye - inhe bhi normal 1-6 ki tarah match karte
# hain, phir translate kar dete hain.
_SUPERSCRIPT_DIGITS = "¹²³⁴⁵⁶⁷⁸⁹"
_SUPERSCRIPT_MAP = str.maketrans(_SUPERSCRIPT_DIGITS, "123456789")
TP_NUMBERED_PATTERN = re.compile(rf'TP\s*([1-9{_SUPERSCRIPT_DIGITS}])[:\s]+([\d.]+)', re.IGNORECASE)
# Plain TPs: "TP: 1.2345" (bina number ke) - kayi lines me repeat ho sakta hai
TP_PLAIN_PATTERN = re.compile(r'(?:TP|TAKE\s*PROFIT)[:\s]+([\d.]+)', re.IGNORECASE)
SL_PATTERN = re.compile(r'(?:SL|STOP\s*LOSS)[:\s]+([\d.]+)', re.IGNORECASE)

# Text-report ke liye - channels bahut alag-alag words use karte hain result
# batane ke liye. Sabse specific (jaise "direct win"/"mtg win") pehle check
# hote hain taaki generic "win"/"loss" unhe overwrite na kare.
RESULT_KEYWORDS = {
    "direct win": "win", "mtg win": "win", "martingale win": "win",
    "tp hit": "win", "target hit": "win", "profit": "win",
    "won": "win", "win": "win", "green": "win",
    "direct loss": "loss", "sl hit": "loss", "stop hit": "loss",
    "lost": "loss", "loss": "loss", "red candle": "loss",
    "skip": "skip", "skipped": "skip", "no trade": "skip", "cancelled": "skip",
}

# Binary options (OTC) channels result ka elaan aksar text ki jagah sirf ek
# Telegram STICKER se karte hain (koi caption/text nahi hota). Telethon me
# har sticker ke saath ek emoji attached hoti hai (msg.file.emoji) - usi se
# match karte hain. Naye emoji mile to yahan add kar sakte ho.
STICKER_WIN_EMOJIS = {"✅", "💚", "🟢", "👍", "🎯", "💰", "🥳", "🔥", "💵", "✔️", "☑️"}
STICKER_LOSS_EMOJIS = {"❌", "🔴", "👎", "💔", "🚫", "😢", "😭", "🥀", "⛔", "✖️"}
STICKER_SKIP_EMOJIS = {"⏭️", "🔁", "⚪", "🤍", "➖", "⏸️"}

MAX_TPS = 6


def is_otc_pair(pair: str) -> bool:
    return "OTC" in pair.upper()


def _extract_tps(text: str) -> list[float]:
    """TP1/TP2/TP3... order me nikalta hai (max 6, superscript TP¹TP²... bhi
    chalta hai). Agar numbered TP na milein to plain 'TP: x' occurrences ko
    unke order me use karta hai."""
    numbered = TP_NUMBERED_PATTERN.findall(text)
    if numbered:
        numbered = [(num.translate(_SUPERSCRIPT_MAP), val) for num, val in numbered]
        numbered.sort(key=lambda pair: int(pair[0]))
        seen = {}
        for num, val in numbered:
            seen.setdefault(int(num), float(val))
        ordered = [seen[k] for k in sorted(seen.keys())]
        return ordered[:MAX_TPS]

    plain = TP_PLAIN_PATTERN.findall(text)
    return [float(v) for v in plain[:MAX_TPS]]


def parse_signal(text: str) -> dict | None:
    if not text:
        return None
    pair_match = PAIR_PATTERN.search(text)
    word_match = WORD_DIRECTION_PATTERN.search(text)
    emoji_match = EMOJI_DIRECTION_PATTERN.search(text) if not word_match else None
    if not pair_match or (not word_match and not emoji_match):
        return None

    pair = (pair_match.group(1) + pair_match.group(2) + (pair_match.group(3) or "")).upper()
    if word_match:
        direction = word_match.group(1).upper()
        is_buy = direction in ("BUY", "CALL", "LONG", "UP", "HIGHER", "HIGH")
    elif emoji_match.group(1):  # up-arrow/green emoji
        is_buy = True
    else:  # down-arrow/red emoji (group 2)
        is_buy = False
    entry = ENTRY_PATTERN.search(text)
    tps = _extract_tps(text)
    sl = SL_PATTERN.search(text)

    result = {
        "pair": pair,
        "direction": "BUY" if is_buy else "SELL",
        "entry": float(entry.group(1)) if entry else None,
        "sl": float(sl.group(1)) if sl else None,
        "is_otc": is_otc_pair(pair),
    }
    for i in range(MAX_TPS):
        result[f"tp{i+1}"] = tps[i] if i < len(tps) else None
    # backward-compat field (pehla TP)
    result["tp"] = tps[0] if tps else None
    return result


def parse_result_report(text: str) -> str | None:
    """Text-based result report se WIN/LOSS/SKIP nikalta hai. Sabse lambe/specific
    keyword (jaise 'direct win') pehle check hote hain taaki generic 'win' word
    unhe galat overwrite na kare. Word-boundary use karte hain taaki 'windows'
    jaise words galti se 'win' na match ho jayein. Agar koi word-based keyword
    na mile, to emoji check karte hain (jaise channel ne sirf '✅' bheja ho)."""
    if not text:
        return None
    lower = text.lower()
    for keyword in sorted(RESULT_KEYWORDS, key=len, reverse=True):
        if re.search(rf'\b{re.escape(keyword)}\b', lower):
            return RESULT_KEYWORDS[keyword]
    for ch in text:
        if ch in STICKER_WIN_EMOJIS:
            return "win"
        if ch in STICKER_LOSS_EMOJIS:
            return "loss"
        if ch in STICKER_SKIP_EMOJIS:
            return "skip"
    return None


def parse_result_from_sticker(emoji: str | None) -> str | None:
    """Sticker ki emoji se win/loss/skip nikalta hai (binary options channels
    jo result sirf sticker se bhejte hain, text nahi)."""
    if not emoji:
        return None
    if emoji in STICKER_WIN_EMOJIS:
        return "win"
    if emoji in STICKER_LOSS_EMOJIS:
        return "loss"
    if emoji in STICKER_SKIP_EMOJIS:
        return "skip"
    return None
