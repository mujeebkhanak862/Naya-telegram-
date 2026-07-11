"""
Multiple api_id/api_hash pairs ke beech fallback.
Ek account/session hamesha USI credential slot se bandha rehta hai jisse pehli
baar login hua tha - taaki Telegram ko suspicious na lage (session switching).
Naye logins round-robin/health-check se best available slot choose karte hain.
"""
import os
import logging

logger = logging.getLogger("credentials")


def load_credentials() -> list[dict]:
    """
    .env / Railway Variables se TELEGRAM_APP_1_ID, TELEGRAM_APP_1_HASH, 
    TELEGRAM_APP_2_ID... jitne bhi mile, sabko load karta hai.
    """
    creds = []
    slot = 1
    while True:
        api_id = os.getenv(f"TELEGRAM_APP_{slot}_ID")
        api_hash = os.getenv(f"TELEGRAM_APP_{slot}_HASH")
        if not api_id or not api_hash:
            break
        creds.append({"slot": slot, "api_id": int(api_id), "api_hash": api_hash})
        slot += 1

    if not creds:
        raise RuntimeError(
            "Koi TELEGRAM_APP_1_ID / TELEGRAM_APP_1_HASH nahi mila. "
            "Railway Variables me kam se kam ek pair set karo."
        )
    logger.info(f"{len(creds)} Telegram credential slot(s) load hue.")
    return creds


CREDENTIALS = None  # lazy-loaded first time it's needed


def get_credentials() -> list[dict]:
    global CREDENTIALS
    if CREDENTIALS is None:
        CREDENTIALS = load_credentials()
    return CREDENTIALS


def get_credential_by_slot(slot: int) -> dict:
    for c in get_credentials():
        if c["slot"] == slot:
            return c
    # slot na mile to pehla wala de do (fallback)
    return get_credentials()[0]


def pick_credential_for_new_login() -> dict:
    """
    Naye login ke liye kaunsa slot use karna hai.
    Simple round-robin - production me ismein rate-limit/health tracking bhi
    add ki ja sakti hai (jo slot abhi flood-wait me hai use skip karna).
    """
    creds = get_credentials()
    # abhi simplest: sabse pehla slot. Users badhne pe yahan load-balancing
    # logic add karna (jaise DB me count rakh ke round-robin karna).
    return creds[0]
