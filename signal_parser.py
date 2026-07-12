"""
Telegram message ke text se trading signal ka structured data nikalta hai -
pair, direction (BUY/SELL), entry, multiple TPs (TP1/TP2/TP3...), SL. Agar signal nahi mila to None.
"""
import re

PAIR_PATTERN = re.compile(
    r'\b([A-Z]{3,6})[\s/_-]?((?:USDT?|JPY|GBP|EUR|CAD|CHF|AUD|NZD|XAU|XAG))\s*[\s/_-]?\s*((?:OTC))?\b',
    re.IGNORECASE,
)
# BUY/SELL/CALL/PUT/LONG/SHORT ke alawa channels ye bhi bahut use karte hain:
# UP/DOWN, HIGHER/LOWER, HIGH/LOW, aur arrow emojis (⬆️⬇️🔼🔽📈📉🟢🔴)
DIRECTION_PATTERN = re.compile(
    r'(BUY|SELL|CALL|PUT|LONG|SHORT|UP|DOWN|HIGHER|LOWER|HIGH|LOW)\b|([⬆🔼📈🟢])|([⬇🔽📉🔴])',
    re.IGNORECASE,
)
ENTRY_PATTERN = re.compile(r'(?:ENTRY|EP)[:\s]+([\d.]+)', re.IGNORECASE)
# Numbered TPs: "TP1: 1.2345", "TP2 1.2350" waghera
TP_NUMBERED_PATTERN = re.compile(r'TP\s*([1-5])[:\s]+([\d.]+)', re.IGNORECASE)
# Plain TPs: "TP: 1.2345" (bina number ke) - kayi lines me repeat ho sakta hai
TP_PLAIN_PATTERN = re.compile(r'(?:TP|TAKE\s*PROFIT)[:\s]+([\d.]+)', re.IGNORECASE)
SL_PATTERN = re.compile(r'(?:SL|STOP\s*LOSS)[:\s]+([\d.]+)', re.IGNORECASE)

RESULT_KEYWORDS = {
    "direct win": "win", "mtg win": "win", "win": "win",
    "skip": "skip", "loss": "loss", "direct loss": "loss",
}

# Binary options (OTC) channels result ka elaan aksar text ki jagah sirf ek
# Telegram STICKER se karte hain (koi caption/text nahi hota). Telethon me
# har sticker ke saath ek emoji attached hoti hai (msg.file.emoji) - usi se
# match karte hain. Naye emoji mile to yahan add kar sakte ho.
STICKER_WIN_EMOJIS = {"✅", "💚", "🟢", "👍", "🎯", "💰", "🥳", "🔥", "💵", "✔️", "☑️"}
STICKER_LOSS_EMOJIS = {"❌", "🔴", "👎", "💔", "🚫", "😢", "😭", "🥀", "⛔", "✖️"}
STICKER_SKIP_EMOJIS = {"⏭️", "🔁", "⚪", "🤍", "➖", "⏸️"}

MAX_TPS = 5


def is_otc_pair(pair: str) -> bool:
    return "OTC" in pair.upper()


def _extract_tps(text: str) -> list[float]:
    """TP1/TP2/TP3... order me nikalta hai (max 5). Agar numbered TP na milein
    to plain 'TP: x' occurrences ko unke order me use karta hai."""
    numbered = TP_NUMBERED_PATTERN.findall(text)
    if numbered:
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
    dir_match = DIRECTION_PATTERN.search(text)
    if not pair_match or not dir_match:
        return None

    pair = (pair_match.group(1) + pair_match.group(2) + (pair_match.group(3) or "")).upper()
    if dir_match.group(1):
        direction = dir_match.group(1).upper()
        is_buy = direction in ("BUY", "CALL", "LONG", "UP", "HIGHER", "HIGH")
    elif dir_match.group(2):  # up-arrow emoji
        is_buy = True
    else:  # down-arrow emoji (group 3)
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
    if not text:
        return None
    lower = text.lower()
    for keyword, result in RESULT_KEYWORDS.items():
        if keyword in lower:
            return result
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
