"""
Database models aur encryption utilities.
Session strings hamesha ENCRYPTED store hoti hain - kabhi plaintext nahi.
"""
import os
import secrets
from datetime import datetime
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Text, Float
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./telegram_connect.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    """Har Telegram account jo website se connect hua, ek row yahan."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, unique=True, index=True, nullable=False)
    telegram_user_id = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    encrypted_session = Column(Text, nullable=False)  # Telethon session string, ENCRYPTED
    credential_slot = Column(Integer, default=1)  # kaunsa api_id/hash pair use kiya
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, default=datetime.utcnow)


class ApiKey(Base):
    """Ek user ke multiple API keys ho sakte hain, har ek apne filter mode ke saath.
    mode='all'            -> saare messages (forward included)
    mode='original_only'  -> sirf wo messages jo group/channel me khud type/post hue,
                              forward kiye hue messages exclude
    chat_id_filter         -> agar set hai, to sirf usi ek chat ke messages milenge"""
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    key = Column(String, unique=True, index=True, nullable=False)
    label = Column(String, nullable=True)  # user ka diya naam, jaise "Bot ke liye", "Website ke liye"
    mode = Column(String, default="all")  # 'all' ya 'original_only'
    chat_id_filter = Column(String, nullable=True)  # None = sab chats, warna sirf ye ek chat
    chat_title_filter = Column(String, nullable=True)  # display ke liye chat ka naam
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ChatSettings(Base):
    """Har user ke har chat ki custom settings: folder aur archive status.
    Telegram khud chats store nahi karta (wo live aate hain), isliye
    yahan sirf 'user ne is chat ko kis folder me daala / archive kiya' save hota hai."""
    __tablename__ = "chat_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    chat_id = Column(String, index=True, nullable=False)
    folder = Column(String, nullable=True)  # None = koi custom folder nahi, sirf "All" me
    archived = Column(Boolean, default=False)
    # Per-channel signal tracking/demo-trading toggle (naya):
    # track_signals=True (default) -> pehle jaisa hi behavior, sab signals track hote hain
    # auto_trade=False (default)   -> demo trade tabhi khulega jab user isko ON kare
    track_signals = Column(Boolean, default=True)
    auto_trade = Column(Boolean, default=False)


class SignalMessage(Base):
    """Har channel/group se aaya har message yahan save hota hai."""
    __tablename__ = "signal_messages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)  # kis user ke account se aaya
    chat_id = Column(String, index=True, nullable=False)
    chat_title = Column(String, nullable=True)
    message_id = Column(String, nullable=False)
    text = Column(Text, nullable=True)
    has_media = Column(Boolean, default=False)
    media_type = Column(String, nullable=True)
    is_edited = Column(Boolean, default=False)
    is_forwarded = Column(Boolean, default=False)  # kisi doosre group/channel se forward hua tha?
    received_at = Column(DateTime, default=datetime.utcnow)


class SignalTrade(Base):
    """Har parse hua trading signal yahan track hota hai - AI analysis,
    live price se win/loss detection, accuracy stats ke liye.
    Multi-TP trailing: jaise jaise TP1 → TP2 → TP3 hit hote hain, SL
    upar (breakeven, phir previous TP) trail hota rehta hai."""
    __tablename__ = "signal_trades"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    chat_id = Column(String, index=True, nullable=False)
    chat_title = Column(String, nullable=True)
    pair = Column(String, nullable=False)
    direction = Column(String, nullable=True)
    entry = Column(Float, nullable=True)
    tp = Column(Float, nullable=True)  # backward-compat = tp1
    tp1 = Column(Float, nullable=True)
    tp2 = Column(Float, nullable=True)
    tp3 = Column(Float, nullable=True)
    tp4 = Column(Float, nullable=True)
    tp5 = Column(Float, nullable=True)
    tp6 = Column(Float, nullable=True)
    tps_hit = Column(Integer, default=0)  # abhi tak kitne TP hit ho chuke
    sl = Column(Float, nullable=True)  # CURRENT (trailing) stop-loss
    original_sl = Column(Float, nullable=True)  # signal me diya gaya asli SL
    is_otc = Column(Boolean, default=False)
    status = Column(String, default="open")  # open / win / loss / skip
    result_source = Column(String, nullable=True)  # 'live_price' ya 'text_reported'
    close_price = Column(Float, nullable=True)
    raw_text = Column(Text, nullable=True)
    ai_confidence = Column(Integer, nullable=True)
    ai_verdict = Column(String, nullable=True)
    ai_risk_note = Column(String, nullable=True)
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)


class DemoAccount(Base):
    """Har user ka virtual/paper-trading account - khud set kiya gaya starting
    balance aur lot size. Jab kisi channel pe 'Demo Trade' ON ho aur signal aaye,
    isi balance/lot se ek DemoTrade khulta hai (asli paisa involve nahi hota)."""
    __tablename__ = "demo_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, unique=True, index=True, nullable=False)
    starting_balance = Column(Float, default=1000.0)
    balance = Column(Float, default=1000.0)  # current running balance
    lot_size = Column(Float, default=0.1)  # default 0.1 lot, user change kar sakta hai
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class DemoTrade(Base):
    """Signal se auto-khula ek demo (paper) trade - real SignalTrade ke result
    ke saath sync hokar close hota hai, balance update karta hai."""
    __tablename__ = "demo_trades"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    signal_trade_id = Column(Integer, index=True, nullable=False)  # SignalTrade.id se link
    chat_id = Column(String, index=True, nullable=False)
    chat_title = Column(String, nullable=True)
    pair = Column(String, nullable=False)
    direction = Column(String, nullable=True)
    lot_size = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    status = Column(String, default="open")  # open / win / loss / skip
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)


class BinaryDemoAccount(Base):
    """Har user ka virtual/paper-trading account - Binary/OTC options ke liye
    ALAG se, Forex wale DemoAccount se independent. Yahan lot_size ki jagah
    'stake' (per-trade fixed amount) aur 'payout_pct' (win pe broker jitna %
    deta hai, jaise 85%) hote hain - binary options isi tarah kaam karte hain."""
    __tablename__ = "binary_demo_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, unique=True, index=True, nullable=False)
    starting_balance = Column(Float, default=1000.0)
    balance = Column(Float, default=1000.0)  # current running balance
    stake = Column(Float, default=10.0)  # per-trade fixed stake, user change kar sakta hai
    payout_pct = Column(Float, default=85.0)  # win pe stake ka kitna % profit milta hai
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class BinaryDemoTrade(Base):
    """Signal se auto-khula ek Binary demo (paper) trade. Price-movement based
    nahi hota (jaise forex DemoTrade) - sirf win/loss/skip result ke hisaab se
    stake win hota ya haarta hai, isliye entry/close price yahan nahi rakhte."""
    __tablename__ = "binary_demo_trades"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    signal_trade_id = Column(Integer, index=True, nullable=False)  # SignalTrade.id se link
    chat_id = Column(String, index=True, nullable=False)
    chat_title = Column(String, nullable=True)
    pair = Column(String, nullable=False)
    direction = Column(String, nullable=True)
    stake = Column(Float, nullable=False)
    payout_pct = Column(Float, nullable=False)
    pnl = Column(Float, nullable=True)
    status = Column(String, default="open")  # open / win / loss / skip
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)


class AiProviderKey(Base):
    """User ke multiple AI provider API keys (Claude/Gemini/DeepSeek/...).
    Signal analysis (ai_analyzer.py) inhe 'priority' ke order me try karta hai -
    pehla enabled provider jo successfully respond kare, uska result use hota hai
    (fallback chain), jaise LADLA extension me tha.
    status: 'untested' (kabhi test/use nahi hua), 'ok' (last call successful),
    'failed' (last call fail hui - last_error me wajah)"""
    __tablename__ = "ai_provider_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    provider = Column(String, nullable=False)  # catalog key, jaise 'claude', 'gemini', 'deepseek'
    label = Column(String, nullable=True)  # user ka diya naam (optional)
    encrypted_api_key = Column(Text, nullable=False)
    model = Column(String, nullable=True)  # blank = provider ka default model use hoga
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=0)  # kam number = fallback chain me pehle try hoga
    status = Column(String, default="untested")  # untested / ok / failed
    last_error = Column(Text, nullable=True)
    last_tested_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BrokerConnection(Base):
    """User ke broker/bridge connections - Binary (Quotex/Pocket Option/apni
    Playwright bridge), Forex/MT5 (MetaApi.cloud ya apni bridge), Crypto
    (exchange API keys ya apni bridge). Fields sab optional hain kyunki har
    broker ka connection method alag hai (broker_catalog.py me conn_type se
    pata chalta hai kaunse fields chahiye)."""
    __tablename__ = "broker_connections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    category = Column(String, nullable=False)  # binary / forex / crypto
    broker = Column(String, nullable=False)  # catalog key, jaise 'quotex', 'metaapi_mt5', 'binance'
    label = Column(String, nullable=True)

    encrypted_session_id = Column(Text, nullable=True)   # Quotex/Pocket Option/Binomo/IQ session
    encrypted_api_key = Column(Text, nullable=True)       # MetaApi token / exchange API key
    encrypted_api_secret = Column(Text, nullable=True)    # exchange API secret
    encrypted_passphrase = Column(Text, nullable=True)    # OKX jaise exchanges ke liye
    account_id = Column(String, nullable=True)            # MetaApi account id (secret nahi)
    bridge_url = Column(String, nullable=True)            # apni Playwright/Express bridge ka URL
    encrypted_bridge_token = Column(Text, nullable=True)  # bridge ka Bearer token

    enabled = Column(Boolean, default=True)
    status = Column(String, default="untested")  # untested / connected / failed
    last_error = Column(Text, nullable=True)
    last_tested_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


def _run_light_migrations():
    """Purani deployed DB me `chat_settings` table pehle se ho sakti hai (naye
    track_signals/auto_trade columns ke bina) - create_all() existing table me
    naye columns add nahi karta, isliye yahan check karke manually ALTER karte hain.
    Naye/fresh DB pe ye chup-chaap kuch nahi karega (columns already create_all se ban chuke)."""
    try:
        with engine.connect() as conn:
            from sqlalchemy import inspect, text
            cols = {c["name"] for c in inspect(engine).get_columns("chat_settings")}
            if "track_signals" not in cols:
                conn.execute(text("ALTER TABLE chat_settings ADD COLUMN track_signals BOOLEAN DEFAULT 1"))
            if "auto_trade" not in cols:
                conn.execute(text("ALTER TABLE chat_settings ADD COLUMN auto_trade BOOLEAN DEFAULT 0"))
            conn.commit()
    except Exception:
        pass  # non-sqlite DBs ya permission issues me chup rehna, app fir bhi chalega


_run_light_migrations()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- Encryption helpers (session strings ke liye) ----------
def _get_fernet() -> Fernet:
    key = os.getenv("SESSION_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "SESSION_ENCRYPTION_KEY set nahi hai. Railway Variables me daalo. "
            "Generate karne ke liye: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def encrypt_session(session_string: str) -> str:
    return _get_fernet().encrypt(session_string.encode()).decode()


def decrypt_session(encrypted: str) -> str:
    return _get_fernet().decrypt(encrypted.encode()).decode()


# ---------- Encryption helpers (AI provider API keys ke liye) ----------
# Same Fernet key reuse karte hain jo session strings ke liye use hoti hai -
# encrypted_at_rest, kabhi plaintext DB me nahi jaati.
def encrypt_text(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_text(encrypted: str) -> str:
    return _get_fernet().decrypt(encrypted.encode()).decode()


def generate_api_key() -> str:
    """User ke liye ek naya, random, unique API key banata hai."""
    return "tc_" + secrets.token_urlsafe(32)  # tc = "telegram connect"
