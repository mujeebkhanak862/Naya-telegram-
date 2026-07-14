"""
Telegram Connect - Backend
Ye Railway pe 24/7 chalta hai. Frontend (Netlify) isse baat karta hai.

Endpoints:
  POST /auth/send-code      -> phone number bhejo, Telegram OTP bhejega
  POST /auth/verify-code    -> OTP verify karo, login complete
  POST /auth/logout         -> session khatam
  GET  /me/chats            -> saare groups/channels ki list (Telegram jaisi)
  GET  /me/chats/{id}/messages -> ek chat ke messages
  POST /apikey/generate     -> naya API key banao (external use ke liye)
  POST /apikey/revoke       -> API key band karo
  GET  /apikey/status       -> abhi API key hai ki nahi
  GET  /v1/signals          -> EXTERNAL API (API key se) - saare naye signals
  WS   /ws/{user_id}        -> live push, jaise hi naya message aaye
"""
import os
import io
import asyncio
import logging
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError, FloodWaitError
from telethon.tl.types import Chat, Channel, User

from db import (
    get_db, User, ApiKey, ChatSettings, SignalMessage, SignalTrade,
    DemoAccount, DemoTrade, BinaryDemoAccount, BinaryDemoTrade,
    AiProviderKey, BrokerConnection,
    encrypt_session, decrypt_session, encrypt_text, decrypt_text, generate_api_key,
)
from signal_parser import parse_signal, parse_result_report, parse_result_from_sticker
from prices import get_live_price
from ai_analyzer import analyze_signal, test_provider_key, PROVIDER_CATALOG
from broker_catalog import BROKER_CATALOG, CONN_TYPE_FIELDS, test_broker_connection
from backtest_engine import fetch_binance_klines, fetch_twelvedata_candles, run_backtest, compare_all_strategies, STRATEGY_CATALOG
from credentials import get_credential_by_slot, pick_credential_for_new_login

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# ---------- In-memory state ----------
# Pending logins jinka OTP abhi verify nahi hua (phone -> temp client + phone_code_hash)
pending_logins: dict[str, dict] = {}
# Har logged-in user ka live Telethon client (background me chalne wala listener)
active_clients: dict[int, TelegramClient] = {}
# Websocket connections, taaki naya message aate hi turant push kar sakein
ws_connections: dict[int, list[WebSocket]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Server start hote hi, jitne bhi users pehle se logged-in hain (DB me session
    # save hai), unke liye background listener wapas start kar do.
    db = next(get_db())
    users = db.query(User).all()
    for user in users:
        try:
            await start_listener_for_user(user)
        except Exception as e:
            logger.error(f"User {user.id} ka listener start nahi ho paya: {e}")

    price_task = asyncio.create_task(price_checker_loop())
    yield
    price_task.cancel()
    for client in active_clients.values():
        await client.disconnect()


app = FastAPI(title="Telegram Connect API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "*")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# EVENT LISTENER - ye har user ke liye background me chalta hai
# ============================================================
async def start_listener_for_user(user: User):
    """User ke session se ek live Telethon client banata hai aur naye
    messages sunna shuru kar deta hai - koi polling nahi, pure event-based."""
    cred = get_credential_by_slot(user.credential_slot)
    session_str = decrypt_session(user.encrypted_session)
    client = TelegramClient(StringSession(session_str), cred["api_id"], cred["api_hash"])
    await client.connect()

    if not await client.is_user_authorized():
        logger.warning(f"User {user.id} ka session ab valid nahi hai, re-login chahiye.")
        return

    @client.on(events.NewMessage)
    async def on_new_message(event):
        await handle_incoming_message(user.id, event, is_edit=False)

    @client.on(events.MessageEdited)
    async def on_edit(event):
        # Signal ke follow-up parts (SL/TP baad me edit hoke aana) yahan pakde jaate hain
        await handle_incoming_message(user.id, event, is_edit=True)

    active_clients[user.id] = client
    asyncio.create_task(client.run_until_disconnected())
    asyncio.create_task(backfill_all_chats(user.id, client))
    logger.info(f"User {user.id} ({user.phone}) ke liye listener shuru ho gaya.")


async def get_active_client(user_id: int, db: Session):
    """active_clients me client dhoondta hai. Agar nahi mila (jaise Railway ne
    backend restart/redeploy kar diya - active_clients ek in-memory dict hai,
    restart pe khali ho jaata hai), to user ke DB me saved (encrypted) session
    se AUTOMATICALLY reconnect karne ki koshish karta hai - taaki user ko
    baar-baar phone-number se dobara login na karna pade sirf backend restart
    ki wajah se. Sirf tabhi 401 deta hai jab session sach me invalid ho gaya ho
    (jaise Telegram se kahin aur logout kar diya ho)."""
    client = active_clients.get(user_id)
    if client:
        return client

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(401, "Login nahi hai ya session expire ho gayi")

    logger.info(f"User {user_id}: active_clients me client nahi mila, saved session se reconnect kar raha hoon.")
    await start_listener_for_user(user)
    client = active_clients.get(user_id)
    if not client:
        raise HTTPException(401, "Session ab valid nahi hai - dobara phone number se login karo")
    return client


async def backfill_all_chats(user_id: int, client: TelegramClient):
    """Login hote hi background me chalta hai - taaki koi bhi channel manually
    khole bina, sabka recent message-cache pehle se taiyaar ho jaaye (fridge
    bhar do pehle se, bajaye ki user khud dukaan jaaye). Sirf channels/groups
    cover karta hai (personal DMs skip - unki zaroorat nahi), aur jis chat ka
    cache already hai use dobara fetch nahi karta - taaki restart/re-login pe
    baar-baar Telegram pe load na pade.

    SAFETY LIMITS (zaroori hai bahut saare channels wale accounts ke liye -
    jaise 200+ channels - taaki Telegram ka rate-limit/flood-wait trigger na
    ho aur poora app slow/stuck na pad jaaye):
      - Ek session me max MAX_BACKFILL_CHATS channels hi cover karta hai
      - Har channel fetch pe 15-second timeout hai (ek slow channel sabko block
        na kare)
      - FloodWaitError explicitly pakadta hai - agar Telegram bole ki lamba
        wait karo, to backfill turant rok deta hai (Telegram ko force nahi
        karta), baaki channels agli baar (agle login ya manual open) pe
        cache ho jaayenge."""
    MAX_BACKFILL_CHATS = 25
    await asyncio.sleep(2)  # pehle login/UI settle hone do
    db = next(get_db())
    covered = 0
    try:
        async for dialog in client.iter_dialogs():
            if covered >= MAX_BACKFILL_CHATS:
                logger.info(f"User {user_id}: backfill limit ({MAX_BACKFILL_CHATS}) pooch gaya, baaki channels manually khulne pe cache honge.")
                break
            if not (dialog.is_channel or dialog.is_group):
                continue  # personal chats skip - sirf channels/groups cache karo

            chat_id = str(dialog.id)
            already_cached = (
                db.query(SignalMessage)
                .filter(SignalMessage.user_id == user_id, SignalMessage.chat_id == chat_id)
                .first()
            )
            if already_cached:
                continue  # is chat ka cache pehle se hai, skip karo

            try:
                chat_title = dialog.title or chat_id

                async def _fetch_and_store():
                    async for msg in client.iter_messages(dialog.id, limit=20):
                        media_kind = None
                        if msg.photo: media_kind = "photo"
                        elif msg.video: media_kind = "video"
                        elif msg.voice: media_kind = "voice"
                        elif msg.audio: media_kind = "audio"
                        elif msg.sticker: media_kind = "sticker"
                        elif msg.gif: media_kind = "gif"
                        elif msg.document: media_kind = "document"

                        db.add(SignalMessage(
                            user_id=user_id, chat_id=chat_id, chat_title=chat_title,
                            message_id=str(msg.id), text=msg.raw_text,
                            has_media=msg.media is not None, media_type=media_kind,
                            is_forwarded=bool(msg.forward), received_at=msg.date,
                        ))
                    db.commit()

                await asyncio.wait_for(_fetch_and_store(), timeout=15)
                covered += 1
            except asyncio.TimeoutError:
                logger.warning(f"Backfill timeout chat {chat_id} ke liye, skip karke aage badhte hain.")
                db.rollback()
            except FloodWaitError as e:
                logger.warning(f"User {user_id}: Telegram flood-wait ({e.seconds}s) mila, backfill turant rok raha hoon (safety).")
                break  # Telegram ko force nahi karna - poora backfill yahin rok do
            except Exception as e:
                logger.warning(f"Backfill fail hua chat {chat_id} ke liye: {e}")
                db.rollback()

            await asyncio.sleep(1)  # Telegram rate-limit se bachne ke liye gap
    except Exception as e:
        logger.warning(f"Backfill loop rukk gaya user {user_id} ke liye: {e}")


def get_or_create_demo_account(db: Session, user_id: int) -> DemoAccount:
    """User ka demo/paper-trading account nikalta hai, na ho to default
    (balance=1000, lot=0.1) ke saath naya bana deta hai."""
    acc = db.query(DemoAccount).filter(DemoAccount.user_id == user_id).first()
    if not acc:
        acc = DemoAccount(user_id=user_id, starting_balance=1000.0, balance=1000.0, lot_size=0.1)
        db.add(acc)
        db.commit()
        db.refresh(acc)
    return acc


def calc_demo_pnl(pair: str, direction: str, entry: float, close: float, lot_size: float) -> float:
    """Simple/approximate demo P&L - asli broker jaisi exact nahi (spread/swap/margin
    ignore kiye hain), sirf practice/tracking ke liye. Standard forex lot = 100000 units,
    crypto pairs me lot ko seedha coin-quantity jaisa treat karte hain. JPY pairs me
    quote currency JPY hone se pip value alag hoti hai, isliye 100 se divide karte hain."""
    if entry is None or close is None:
        return 0.0
    is_crypto = "USDT" in pair.upper()
    is_jpy = "JPY" in pair.upper()
    contract_size = 1 if is_crypto else 100000
    diff = (close - entry) if direction == "BUY" else (entry - close)
    pnl = diff * lot_size * contract_size
    if is_jpy:
        pnl = pnl / 100
    return round(pnl, 2)


def close_demo_trade(db: Session, signal_trade_id: int, close_price: float | None, cancel: bool = False):
    """Signal ka real result aane par, usse linked open DemoTrade (agar hai) close
    karta hai aur user ke DemoAccount balance ko P&L se update karta hai.
    cancel=True -> 'skip' jaisa result, balance untouched, trade bas band ho jata hai."""
    demo_trade = (
        db.query(DemoTrade)
        .filter(DemoTrade.signal_trade_id == signal_trade_id, DemoTrade.status == "open")
        .first()
    )
    if not demo_trade:
        return
    demo_trade.closed_at = datetime.utcnow()
    if cancel or close_price is None:
        demo_trade.status = "skip"
        demo_trade.pnl = 0.0
        db.commit()
        return

    pnl = calc_demo_pnl(demo_trade.pair, demo_trade.direction, demo_trade.entry_price, close_price, demo_trade.lot_size)
    demo_trade.close_price = close_price
    demo_trade.pnl = pnl
    demo_trade.status = "win" if pnl >= 0 else "loss"

    acc = db.query(DemoAccount).filter(DemoAccount.user_id == demo_trade.user_id).first()
    if acc:
        acc.balance = round(acc.balance + pnl, 2)
        acc.updated_at = datetime.utcnow()
    db.commit()


def get_or_create_binary_demo_account(db: Session, user_id: int) -> BinaryDemoAccount:
    """User ka Binary/OTC demo account nikalta hai, na ho to default
    (balance=1000, stake=10, payout=85%) ke saath naya bana deta hai.
    Forex wale get_or_create_demo_account se bilkul independent."""
    acc = db.query(BinaryDemoAccount).filter(BinaryDemoAccount.user_id == user_id).first()
    if not acc:
        acc = BinaryDemoAccount(user_id=user_id, starting_balance=1000.0, balance=1000.0, stake=10.0, payout_pct=85.0)
        db.add(acc)
        db.commit()
        db.refresh(acc)
    return acc


def calc_binary_pnl(stake: float, payout_pct: float, status: str) -> float:
    """Binary options me price-difference nahi, sirf fixed payout hota hai:
    win -> stake ka payout_pct% profit, loss -> poora stake gaya, skip -> 0."""
    if status == "win":
        return round(stake * payout_pct / 100.0, 2)
    if status == "loss":
        return round(-stake, 2)
    return 0.0


def close_binary_demo_trade(db: Session, signal_trade_id: int, result: str):
    """Signal ka result (win/loss/skip) aane par usse linked open BinaryDemoTrade
    close karta hai aur user ke BinaryDemoAccount balance ko update karta hai."""
    demo_trade = (
        db.query(BinaryDemoTrade)
        .filter(BinaryDemoTrade.signal_trade_id == signal_trade_id, BinaryDemoTrade.status == "open")
        .first()
    )
    if not demo_trade:
        return
    demo_trade.closed_at = datetime.utcnow()
    demo_trade.status = result if result in ("win", "loss") else "skip"
    demo_trade.pnl = calc_binary_pnl(demo_trade.stake, demo_trade.payout_pct, demo_trade.status)

    if demo_trade.status != "skip":
        acc = db.query(BinaryDemoAccount).filter(BinaryDemoAccount.user_id == demo_trade.user_id).first()
        if acc:
            acc.balance = round(acc.balance + demo_trade.pnl, 2)
            acc.updated_at = datetime.utcnow()
    db.commit()


def _detect_media_kind(msg) -> str | None:
    """Message pe kis tarah ka media hai (photo/sticker/gif/waghera) - chat
    history endpoint (get_chat_messages) jaisi hi logic, taaki dono jagah
    same media_kind values aayein."""
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.voice:
        return "voice"
    if msg.audio:
        return "audio"
    if msg.sticker:
        return "sticker"
    if msg.gif:
        return "gif"
    if msg.document:
        return "document"
    return None


async def handle_incoming_message(user_id: int, event, is_edit: bool):
    """Har naye/edited Telegram message pe chalta hai:
      1. DB me save/update karta hai (chat history ke liye)
      2. Connected websockets pe live push karta hai
      3. Signal/result tracking ke liye process_signal_tracking() ko call karta hai,
         sticker ki emoji bhi bhejta hai - taaki binary/OTC channels jo result
         sirf ek sticker se dete hain (koi text nahi), wo bhi win/loss/skip ki
         tarah count ho sakein."""
    message = event.message
    if message is None:
        return

    chat_id = str(event.chat_id)
    try:
        chat = await event.get_chat()
        chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or chat_id
    except Exception:
        chat_title = chat_id

    text = event.raw_text or ""
    media_kind = _detect_media_kind(message)
    has_media = message.media is not None

    # Sticker ki emoji nikalo (binary options result stickers yahan se pakde jaate hain)
    sticker_emoji = None
    if message.sticker and message.file is not None:
        sticker_emoji = getattr(message.file, "emoji", None)

    db = next(get_db())
    existing = None
    if is_edit:
        existing = (
            db.query(SignalMessage)
            .filter(
                SignalMessage.user_id == user_id,
                SignalMessage.chat_id == chat_id,
                SignalMessage.message_id == str(message.id),
            )
            .first()
        )

    if existing:
        existing.text = text
        existing.has_media = has_media
        existing.media_type = media_kind
        existing.is_edited = True
        db.commit()
    else:
        db.add(SignalMessage(
            user_id=user_id, chat_id=chat_id, chat_title=chat_title,
            message_id=str(message.id), text=text,
            has_media=has_media, media_type=media_kind,
            is_edited=is_edit, is_forwarded=bool(message.forward),
        ))
        db.commit()

    # Live push jo bhi is user ke websockets khule hain unko
    for ws in list(ws_connections.get(user_id, [])):
        try:
            await ws.send_json({
                "type": "new_message",
                "chat_id": chat_id,
                "message_id": message.id,
                "text": text,
                "has_media": has_media,
                "media_kind": media_kind,
                "is_edited": is_edit,
                "received_at": datetime.utcnow().isoformat(),
            })
        except Exception:
            pass

    # Forward kiya hua message signal-tracking ke liye ignore karo - hum sirf
    # channel ke apne likhe signals monitor karna chahte hain, kisi aur jagah
    # se forward kiya gaya message nahi (chahe usme koi pair/direction jaisa
    # text ho). Message chat mein dikhega, bas auto signal/trade nahi banega.
    if message.forward:
        return

    await process_signal_tracking(user_id, chat_id, chat_title, text, sticker_emoji=sticker_emoji)


async def process_signal_tracking(user_id: int, chat_id: str, chat_title: str, text: str, sticker_emoji: str | None = None):
    """Naya signal aaya to parse+AI analyse karke track karna shuru karo.
    Agar sirf ek result-report hai (jaise OTC channel 'DIRECT WIN' bolta hai,
    ya sirf ek result-sticker bhej deta hai - text bilkul nahi), to isi
    user/chat ke sabse recent open signal ko close kar do.

    Binary options (OTC) channels aksar result sirf ek STICKER se dete hain
    (✅/❌ jaisi emoji wali), koi text nahi hota - is case me text-based
    parsing kuch nahi paayegi, isliye sticker_emoji fallback ke taur pe check
    hoti hai.

    Per-channel 'Track Signals' / 'Demo Trade' settings yahan check hoti hain:
      - track_signals=False -> channel ka koi signal track hi nahi hota (backward-compat
        ke liye default True hai, matlab existing behavior bilkul same rehta hai)
      - auto_trade=True     -> tracking ke saath-saath ek demo (paper) trade bhi khulta hai,
        user ke DemoAccount balance/lot size se"""
    if not text and not sticker_emoji:
        return
    db = next(get_db())

    setting = db.query(ChatSettings).filter(
        ChatSettings.user_id == user_id, ChatSettings.chat_id == chat_id
    ).first()
    track_signals = setting.track_signals if setting else True  # default ON = purana behavior
    auto_trade = setting.auto_trade if setting else False  # default OFF = koi naya side-effect nahi

    if not track_signals:
        return

    parsed = parse_signal(text)
    if parsed:
        ai_result = await analyze_signal(db, user_id, text, parsed)
        trade = SignalTrade(
            user_id=user_id, chat_id=chat_id, chat_title=chat_title,
            pair=parsed["pair"], direction=parsed["direction"],
            entry=parsed["entry"],
            tp=parsed["tp1"], tp1=parsed["tp1"], tp2=parsed["tp2"],
            tp3=parsed["tp3"], tp4=parsed["tp4"], tp5=parsed["tp5"], tp6=parsed.get("tp6"),
            tps_hit=0,
            sl=parsed["sl"], original_sl=parsed["sl"],
            is_otc=parsed["is_otc"], status="open", raw_text=text[:500],
            ai_confidence=ai_result.get("confidence") if ai_result else None,
            ai_verdict=ai_result.get("verdict") if ai_result else None,
            ai_risk_note=ai_result.get("risk_note") if ai_result else None,
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)

        # Demo Trade ON hai -> paper trade khol do.
        # Binary/OTC signals: price-movement nahi, sirf win/loss/skip result se
        # chalte hain, isliye entry price zaroori nahi - alag BinaryDemoAccount use hota hai.
        # Forex (non-OTC) signals: pehle jaisa hi - entry price zaroori hai, DemoAccount use hota hai.
        if auto_trade and parsed["is_otc"]:
            bin_acc = get_or_create_binary_demo_account(db, user_id)
            binary_demo_trade = BinaryDemoTrade(
                user_id=user_id, signal_trade_id=trade.id, chat_id=chat_id, chat_title=chat_title,
                pair=parsed["pair"], direction=parsed["direction"],
                stake=bin_acc.stake, payout_pct=bin_acc.payout_pct, status="open",
            )
            db.add(binary_demo_trade)
            db.commit()
        elif auto_trade and parsed["entry"] is not None:
            acc = get_or_create_demo_account(db, user_id)
            demo_trade = DemoTrade(
                user_id=user_id, signal_trade_id=trade.id, chat_id=chat_id, chat_title=chat_title,
                pair=parsed["pair"], direction=parsed["direction"],
                lot_size=acc.lot_size, entry_price=parsed["entry"], status="open",
            )
            db.add(demo_trade)
            db.commit()
        return

    result = parse_result_report(text)
    result_source = "text_reported"
    if not result:
        # Text se result nahi mila (jaise binary/OTC channel sirf sticker
        # bhejta hai) -> sticker ki emoji se try karo.
        result = parse_result_from_sticker(sticker_emoji)
        result_source = "sticker_reported"

    if result:
        open_trade = (
            db.query(SignalTrade)
            .filter(SignalTrade.user_id == user_id, SignalTrade.chat_id == chat_id, SignalTrade.status == "open")
            .order_by(SignalTrade.opened_at.desc())
            .first()
        )
        if open_trade:
            open_trade.status = result
            open_trade.result_source = result_source
            open_trade.closed_at = datetime.utcnow()
            db.commit()

            # Linked demo trade bhi isi result ke hisaab se band karo.
            if open_trade.is_otc:
                # Binary/OTC: fixed payout se close, koi price approx nahi chahiye
                close_binary_demo_trade(db, open_trade.id, result)
            else:
                # Forex: exact close price text-report me nahi milta, isliye
                # approx: win -> TP1, loss -> SL
                if result == "skip":
                    close_demo_trade(db, open_trade.id, None, cancel=True)
                elif result == "win":
                    close_demo_trade(db, open_trade.id, open_trade.tp1 or open_trade.entry)
                elif result == "loss":
                    close_demo_trade(db, open_trade.id, open_trade.sl or open_trade.entry)


async def price_checker_loop():
    """Har second: jo bhi non-OTC signals 'open' hain, unka live market price check
    karta hai (TP ladder + trailing SL):
      - TP1 hit  -> SL breakeven (entry) pe move ho jata hai
      - TP2 hit  -> SL TP1 pe move ho jata hai
      - TP3 hit  -> SL TP2 pe move ho jata hai  ... aage bhi isi tarah
      - Aakhri TP hit -> trade "win" (full target) band ho jata hai
      - Beech me kabhi bhi current (trailing) SL touch ho jaye -> trade band:
          agar koi TP hit ho chuka tha to "win" (locked-in profit), warna "loss"
    OTC signals yahan skip hote hain (unka result sirf channel ke text-report se aata hai)."""
    while True:
        try:
            db = next(get_db())
            open_trades = db.query(SignalTrade).filter(
                SignalTrade.status == "open", SignalTrade.is_otc == False,
                SignalTrade.tp1.isnot(None), SignalTrade.sl.isnot(None),
            ).all()
            for trade in open_trades:
                price = await get_live_price(trade.pair, trade.is_otc)
                if price is None:
                    continue

                tps = [t for t in (trade.tp1, trade.tp2, trade.tp3, trade.tp4, trade.tp5, trade.tp6) if t is not None]
                if not tps:
                    continue

                direction = trade.direction
                def _hit(level):
                    return (direction == "BUY" and price >= level) or (direction == "SELL" and price <= level)
                def _stopped(level):
                    return (direction == "BUY" and price <= level) or (direction == "SELL" and price >= level)

                next_idx = trade.tps_hit  # agla TP jo abhi hit nahi hua
                if next_idx < len(tps) and _hit(tps[next_idx]):
                    trade.tps_hit = next_idx + 1
                    # SL trail: TP1 hit -> breakeven, TP2+ hit -> previous TP
                    trade.sl = trade.entry if trade.tps_hit == 1 else tps[trade.tps_hit - 2]
                    trade.result_source = "live_price"
                    if trade.tps_hit >= len(tps):
                        # sab TP hit - full target complete
                        trade.status = "win"
                        trade.close_price = price
                        trade.closed_at = datetime.utcnow()
                        close_demo_trade(db, trade.id, price)
                    # warna trade open rehta hai, agla TP track hota rahega
                elif trade.sl is not None and _stopped(trade.sl):
                    trade.close_price = price
                    trade.result_source = "live_price"
                    trade.closed_at = datetime.utcnow()
                    trade.status = "win" if trade.tps_hit > 0 else "loss"
                    close_demo_trade(db, trade.id, price)
            db.commit()
        except Exception as e:
            logger.error(f"Price checker error: {e}")
        await asyncio.sleep(1)
    chat = await event.get_chat()
    chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", "Unknown")

    # Signal ke multi-part hone ki soorat me thoda wait karke poora capture karna
    if not is_edit:
        await asyncio.sleep(3)

    db = next(get_db())
    msg = SignalMessage(
        user_id=user_id,
        chat_id=str(event.chat_id),
        chat_title=chat_title,
        message_id=str(event.id),
        text=event.raw_text,
        has_media=event.media is not None,
        media_type=type(event.media).__name__ if event.media else None,
        is_edited=is_edit,
        is_forwarded=event.message.forward is not None,
        received_at=datetime.utcnow(),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    # Signal tracking: parse karo, AI se analyse karvao, ya purane open signal ka result close karo
    asyncio.create_task(process_signal_tracking(user_id, str(event.chat_id), chat_title, event.raw_text))

    # Websocket se turant push - agar user ki website khuli hai
    m = event.message
    media_kind = None
    if m.photo: media_kind = "photo"
    elif m.video: media_kind = "video"
    elif m.voice: media_kind = "voice"
    elif m.audio: media_kind = "audio"
    elif m.sticker: media_kind = "sticker"
    elif m.gif: media_kind = "gif"
    elif m.document: media_kind = "document"

    payload = {
        "type": "new_message",
        "chat_title": chat_title,
        "chat_id": str(event.chat_id),
        "message_id": event.id,
        "has_media": event.media is not None,
        "media_kind": media_kind,
        "text": event.raw_text,
        "is_edited": is_edit,
        "received_at": msg.received_at.isoformat(),
    }
    for ws in ws_connections.get(user_id, []):
        try:
            await ws.send_json(payload)
        except Exception:
            pass


# ============================================================
# AUTH - Phone + OTP login
# ============================================================
@app.post("/auth/send-code")
async def send_code(payload: dict):
    phone = payload.get("phone")
    if not phone:
        raise HTTPException(400, "phone number chahiye")

    cred = pick_credential_for_new_login()
    client = TelegramClient(StringSession(), cred["api_id"], cred["api_hash"])

    try:
        # 20-second timeout - Telegram/network slow ho to hamesha "Bhej rahe
        # hain..." pe atkne ki bajaye turant clear error do.
        await asyncio.wait_for(client.connect(), timeout=20)
        sent = await asyncio.wait_for(client.send_code_request(phone), timeout=20)
    except asyncio.TimeoutError:
        await client.disconnect()
        raise HTTPException(504, "Telegram se connect hone me bahut time lag raha hai - thodi der baad dobara try karo")
    except Exception as e:
        await client.disconnect()
        raise HTTPException(400, f"Code bhejne me error: {str(e)}")

    pending_logins[phone] = {
        "client": client,
        "phone_code_hash": sent.phone_code_hash,
        "credential_slot": cred["slot"],
    }
    return {"status": "code_sent", "phone": phone}


@app.post("/auth/verify-code")
async def verify_code(payload: dict, db: Session = Depends(get_db)):
    phone = payload.get("phone")
    code = payload.get("code")
    password = payload.get("password")  # agar 2FA on hai

    pending = pending_logins.get(phone)
    if not pending:
        raise HTTPException(400, "Pehle /auth/send-code call karo")

    client: TelegramClient = pending["client"]
    try:
        try:
            await client.sign_in(phone, code, phone_code_hash=pending["phone_code_hash"])
        except SessionPasswordNeededError:
            if not password:
                return {"status": "2fa_required"}
            await client.sign_in(password=password)
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            raise HTTPException(400, "OTP galat ya expire ho gaya, dobara try karo")

        me = await client.get_me()
        session_str = client.session.save()

        user = db.query(User).filter(User.phone == phone).first()
        if not user:
            user = User(phone=phone)
            db.add(user)

        user.telegram_user_id = str(me.id)
        user.display_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        user.username = me.username
        user.encrypted_session = encrypt_session(session_str)
        user.credential_slot = pending["credential_slot"]
        user.last_login = datetime.utcnow()
        db.commit()
        db.refresh(user)

        del pending_logins[phone]
        await start_listener_for_user(user)

        return {
            "status": "success",
            "user_id": user.id,
            "display_name": user.display_name,
            "username": user.username,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Login fail: {str(e)}")


@app.post("/auth/logout")
async def logout(payload: dict, db: Session = Depends(get_db)):
    user_id = payload.get("user_id")
    client = active_clients.pop(user_id, None)
    if client:
        await client.disconnect()
    ws_connections.pop(user_id, None)
    return {"status": "logged_out"}


# ============================================================
# DASHBOARD DATA - Telegram jaisa chat list + messages
# ============================================================
@app.get("/me/{user_id}/chats")
async def get_chats(user_id: int, db: Session = Depends(get_db)):
    client = await get_active_client(user_id, db)

    # User ki saari custom folder/archive settings ek baar me le lo
    settings_map = {
        s.chat_id: s for s in db.query(ChatSettings).filter(ChatSettings.user_id == user_id).all()
    }

    chats = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        is_group = isinstance(entity, Chat) or (isinstance(entity, Channel) and getattr(entity, "megagroup", False))
        is_channel = isinstance(entity, Channel) and not getattr(entity, "megagroup", False)

        last_msg = dialog.message
        chat_id_str = str(dialog.id)
        setting = settings_map.get(chat_id_str)

        chats.append({
            "id": chat_id_str,
            "title": dialog.name,
            "is_group": is_group,
            "is_channel": is_channel,
            "unread_count": dialog.unread_count,
            "last_message": last_msg.text if last_msg else None,
            "last_message_date": dialog.date.isoformat() if dialog.date else None,
            "last_message_id": last_msg.id if last_msg else None,
            "last_message_has_media": bool(last_msg.media) if last_msg else False,
            "folder": setting.folder if setting else None,
            "archived": setting.archived if setting else False,
            "track_signals": setting.track_signals if setting else True,
            "auto_trade": setting.auto_trade if setting else False,
        })
    return {"chats": chats}


@app.get("/me/{user_id}/folders")
async def get_folders(user_id: int, db: Session = Depends(get_db)):
    """User ne ab tak jitne bhi custom folder naam banaye hain, unki list."""
    rows = (
        db.query(ChatSettings.folder)
        .filter(ChatSettings.user_id == user_id, ChatSettings.folder.isnot(None))
        .distinct()
        .all()
    )
    return {"folders": [r[0] for r in rows]}


@app.post("/me/{user_id}/chats/{chat_id}/folder")
async def set_chat_folder(user_id: int, chat_id: str, payload: dict, db: Session = Depends(get_db)):
    """Chat ko kisi folder me daalo, ya folder=null bhejke 'All' me wapas le aao."""
    folder = payload.get("folder")  # None/empty allowed - matlab koi folder nahi
    setting = db.query(ChatSettings).filter(
        ChatSettings.user_id == user_id, ChatSettings.chat_id == chat_id
    ).first()
    if not setting:
        setting = ChatSettings(user_id=user_id, chat_id=chat_id)
        db.add(setting)
    setting.folder = folder or None
    db.commit()
    return {"status": "updated", "folder": setting.folder}


@app.post("/me/{user_id}/chats/{chat_id}/archive")
async def set_chat_archive(user_id: int, chat_id: str, payload: dict, db: Session = Depends(get_db)):
    """Chat ko archive karo ya wapas nikal lo."""
    archived = bool(payload.get("archived", True))
    setting = db.query(ChatSettings).filter(
        ChatSettings.user_id == user_id, ChatSettings.chat_id == chat_id
    ).first()
    if not setting:
        setting = ChatSettings(user_id=user_id, chat_id=chat_id)
        db.add(setting)
    setting.archived = archived
    db.commit()
    return {"status": "updated", "archived": setting.archived}


@app.post("/me/{user_id}/chats/{chat_id}/tracking")
async def set_chat_tracking(user_id: int, chat_id: str, payload: dict, db: Session = Depends(get_db)):
    """Is channel ke liye 'Track Signals' aur 'Demo Trade' ON/OFF karo.
    track_signals=False -> is channel ke signals bilkul track nahi honge.
    auto_trade=True -> track ke saath-saath demo (paper) trade bhi khulega."""
    setting = db.query(ChatSettings).filter(
        ChatSettings.user_id == user_id, ChatSettings.chat_id == chat_id
    ).first()
    if not setting:
        setting = ChatSettings(user_id=user_id, chat_id=chat_id)
        db.add(setting)
    if "track_signals" in payload:
        setting.track_signals = bool(payload["track_signals"])
    if "auto_trade" in payload:
        setting.auto_trade = bool(payload["auto_trade"])
        if setting.auto_trade:
            setting.track_signals = True  # demo trade ke liye tracking zaroori hai
    db.commit()
    return {"status": "updated", "track_signals": setting.track_signals, "auto_trade": setting.auto_trade}


@app.get("/me/{user_id}/chats/{chat_id}/messages")
async def get_chat_messages(user_id: int, chat_id: str, limit: int = 30, db: Session = Depends(get_db)):
    """Messages ko pehle apne DB cache (SignalMessage table) se serve karta hai -
    taaki channel dobara kholne pe turant load ho, Telegram ko dobara na poochna
    pade. Sirf pehli baar (jab kisi chat ka cache khali ho) live Telegram se
    fetch karta hai aur usi waqt cache mein save bhi kar deta hai, taaki agli
    baar se instant mile. Naye messages websocket handler se already cache mein
    aate rehte hain, isliye cache dheere-dheere khud-ba-khud up-to-date rehta hai."""
    cached = (
        db.query(SignalMessage)
        .filter(SignalMessage.user_id == user_id, SignalMessage.chat_id == chat_id)
        .order_by(SignalMessage.id.desc())
        .limit(limit)
        .all()
    )

    if len(cached) >= min(limit, 10):  # kaafi cache mila, Telegram call skip karo
        messages = [
            {
                "id": int(m.message_id) if m.message_id.isdigit() else m.message_id,
                "text": m.text,
                "date": m.received_at.isoformat() if m.received_at else None,
                "has_media": m.has_media,
                "media_kind": m.media_type,
                "file_name": None,
                "is_out": False,
            }
            for m in reversed(cached)
        ]
        return {"messages": messages, "source": "cache"}

    # Cache khali/kam hai (pehli baar ye chat khula hai) - Telegram se live fetch karo
    client = await get_active_client(user_id, db)

    try:
        async def _live_fetch():
            try:
                chat_entity = await client.get_entity(int(chat_id))
                chat_title = getattr(chat_entity, "title", None) or getattr(chat_entity, "first_name", None) or chat_id
            except Exception:
                chat_title = chat_id
            messages = []
            async for msg in client.iter_messages(int(chat_id), limit=limit):
                media_kind = None
                if msg.photo:
                    media_kind = "photo"
                elif msg.video:
                    media_kind = "video"
                elif msg.voice:
                    media_kind = "voice"
                elif msg.audio:
                    media_kind = "audio"
                elif msg.sticker:
                    media_kind = "sticker"
                elif msg.gif:
                    media_kind = "gif"
                elif msg.document:
                    media_kind = "document"

                messages.append({
                    "id": msg.id,
                    "text": msg.raw_text,
                    "date": msg.date.isoformat() if msg.date else None,
                    "has_media": msg.media is not None,
                    "media_kind": media_kind,
                    "file_name": getattr(msg.file, "name", None) if msg.file else None,
                    "is_out": msg.out,
                })

                # Cache mein backfill karo taaki agli baar isi chat ke liye Telegram
                # call na karni pade (duplicate check message_id se).
                exists = (
                    db.query(SignalMessage)
                    .filter(
                        SignalMessage.user_id == user_id, SignalMessage.chat_id == chat_id,
                        SignalMessage.message_id == str(msg.id),
                    ).first()
                )
                if not exists:
                    db.add(SignalMessage(
                        user_id=user_id, chat_id=chat_id, chat_title=chat_title,
                        message_id=str(msg.id), text=msg.raw_text,
                        has_media=msg.media is not None, media_type=media_kind,
                        is_forwarded=bool(msg.forward), received_at=msg.date,
                    ))
            db.commit()
            return messages

        # 12-second timeout - agar Telegram slow ho ya flood-wait me ho, hamesha
        # ke liye "Loading..." pe atke rehne ki bajaye turant cache fallback de do.
        messages = await asyncio.wait_for(_live_fetch(), timeout=12)
        return {"messages": list(reversed(messages)), "source": "live"}
    except Exception:
        # Live fetch fail ho jaye (rate-limit, network) to jo bhi thoda-bahut
        # cache tha wahi de do, khali screen se behtar hai.
        db.rollback()
        messages = [
            {
                "id": int(m.message_id) if m.message_id.isdigit() else m.message_id,
                "text": m.text, "date": m.received_at.isoformat() if m.received_at else None,
                "has_media": m.has_media, "media_kind": m.media_type,
                "file_name": None, "is_out": False,
            }
            for m in reversed(cached)
        ]
        return {"messages": messages, "source": "cache_fallback"}


@app.get("/me/{user_id}/chats/{chat_id}/media/{message_id}")
async def get_media(user_id: int, chat_id: str, message_id: int, db: Session = Depends(get_db)):
    """Ek message ki photo/sticker/media ki actual file serve karta hai."""
    client = await get_active_client(user_id, db)

    msg = await client.get_messages(int(chat_id), ids=message_id)
    if not msg or not msg.media:
        raise HTTPException(404, "Media nahi mila")

    buf = io.BytesIO()
    await client.download_media(msg, file=buf)
    buf.seek(0)

    mime = "image/jpeg"
    if msg.file and msg.file.mime_type:
        mime = msg.file.mime_type

    return StreamingResponse(buf, media_type=mime)


# ============================================================
# API KEY SYSTEM - user MULTIPLE API keys bana sakta hai, har ek
# apne filter mode ke saath ("all" ya "original_only")
# ============================================================
@app.post("/apikey/create")
async def create_key(payload: dict, db: Session = Depends(get_db)):
    user_id = payload.get("user_id")
    label = payload.get("label") or "Untitled Key"
    mode = payload.get("mode", "all")
    chat_id_filter = payload.get("chat_id")  # optional - agar diya to sirf isi chat ke messages
    chat_title_filter = payload.get("chat_title")
    if mode not in ("all", "original_only"):
        raise HTTPException(400, "mode 'all' ya 'original_only' hona chahiye")

    key = ApiKey(
        user_id=user_id, key=generate_api_key(), label=label, mode=mode,
        chat_id_filter=chat_id_filter, chat_title_filter=chat_title_filter, active=True,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return {
        "id": key.id, "key": key.key, "label": key.label,
        "mode": key.mode, "active": key.active,
        "chat_id_filter": key.chat_id_filter, "chat_title_filter": key.chat_title_filter,
    }


@app.get("/apikey/list/{user_id}")
async def list_keys(user_id: int, db: Session = Depends(get_db)):
    keys = db.query(ApiKey).filter(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc()).all()
    return {
        "keys": [
            {
                "id": k.id, "key": k.key, "label": k.label, "mode": k.mode, "active": k.active,
                "chat_id_filter": k.chat_id_filter, "chat_title_filter": k.chat_title_filter,
            }
            for k in keys
        ]
    }


@app.post("/apikey/revoke")
async def revoke_key(payload: dict, db: Session = Depends(get_db)):
    key_id = payload.get("key_id")
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key:
        raise HTTPException(404, "API key nahi mili")
    key.active = False
    db.commit()
    return {"status": "revoked"}


@app.delete("/apikey/{key_id}")
async def delete_key(key_id: int, db: Session = Depends(get_db)):
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key:
        raise HTTPException(404, "API key nahi mili")
    db.delete(key)
    db.commit()
    return {"status": "deleted"}


# ============================================================
# EXTERNAL API - koi bhi bot/app/website apna API key se signals le sake
# key ka "mode" decide karta hai: "all" (forward included) ya
# "original_only" (sirf khud type/post hue messages, forward exclude)
# ============================================================
@app.get("/v1/signals")
async def get_signals_via_api(limit: int = 20, x_api_key: str = Header(None), db: Session = Depends(get_db)):
    if not x_api_key:
        raise HTTPException(401, "X-API-Key header chahiye")

    key = db.query(ApiKey).filter(ApiKey.key == x_api_key, ApiKey.active == True).first()
    if not key:
        raise HTTPException(403, "Invalid ya inactive API key")

    query = db.query(SignalMessage).filter(SignalMessage.user_id == key.user_id)
    if key.mode == "original_only":
        query = query.filter(SignalMessage.is_forwarded == False)
    if key.chat_id_filter:
        query = query.filter(SignalMessage.chat_id == key.chat_id_filter)

    signals = query.order_by(SignalMessage.received_at.desc()).limit(limit).all()
    return {
        "mode": key.mode,
        "chat_filter": key.chat_title_filter,
        "signals": [
            {
                "chat_title": s.chat_title,
                "text": s.text,
                "is_forwarded": s.is_forwarded,
                "received_at": s.received_at.isoformat(),
                "is_edited": s.is_edited,
            }
            for s in signals
        ]
    }


# ============================================================
# WEBSOCKET - live push, jaise hi naya message aaye
# ============================================================
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    ws_connections.setdefault(user_id, []).append(websocket)
    try:
        while True:
            await websocket.receive_text()  # sirf connection zinda rakhne ke liye
    except WebSocketDisconnect:
        ws_connections[user_id].remove(websocket)


@app.get("/me/{user_id}/trades")
async def get_trades(user_id: int, status: str = None, chat_id: str = None, limit: int = 50, db: Session = Depends(get_db)):
    query = db.query(SignalTrade).filter(SignalTrade.user_id == user_id)
    if status:
        query = query.filter(SignalTrade.status == status)
    if chat_id:
        query = query.filter(SignalTrade.chat_id == chat_id)
    trades = query.order_by(SignalTrade.opened_at.desc()).limit(limit).all()
    return {
        "trades": [
            {
                "id": t.id, "chat_id": t.chat_id, "chat_title": t.chat_title, "pair": t.pair, "direction": t.direction,
                "raw_text": t.raw_text,
                "entry": t.entry, "tp": t.tp, "sl": t.sl, "is_otc": t.is_otc,
                "tp1": t.tp1, "tp2": t.tp2, "tp3": t.tp3, "tp4": t.tp4, "tp5": t.tp5,
                "tps_hit": t.tps_hit, "original_sl": t.original_sl,
                "status": t.status, "result_source": t.result_source, "close_price": t.close_price,
                "ai_confidence": t.ai_confidence, "ai_verdict": t.ai_verdict, "ai_risk_note": t.ai_risk_note,
                "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in trades
        ]
    }


@app.get("/me/{user_id}/stats")
async def get_stats(user_id: int, db: Session = Depends(get_db)):
    base = db.query(SignalTrade).filter(SignalTrade.user_id == user_id)
    total = base.filter(SignalTrade.status.in_(["win", "loss"])).count()
    wins = base.filter(SignalTrade.status == "win").count()
    losses = base.filter(SignalTrade.status == "loss").count()
    open_count = base.filter(SignalTrade.status == "open").count()
    accuracy = round((wins / total) * 100, 1) if total > 0 else 0
    return {"total_closed": total, "wins": wins, "losses": losses, "open_trades": open_count, "accuracy_percent": accuracy}


@app.get("/me/{user_id}/demo-account")
async def get_demo_account(user_id: int, db: Session = Depends(get_db)):
    """Demo/paper-trading account ki current state - balance, lot size, aur
    ab tak ke demo trades ka win/loss + total P&L."""
    acc = get_or_create_demo_account(db, user_id)
    closed = db.query(DemoTrade).filter(
        DemoTrade.user_id == user_id, DemoTrade.status.in_(["win", "loss"])
    )
    total = closed.count()
    wins = closed.filter(DemoTrade.status == "win").count()
    losses = closed.filter(DemoTrade.status == "loss").count()
    open_count = db.query(DemoTrade).filter(DemoTrade.user_id == user_id, DemoTrade.status == "open").count()
    win_rate = round((wins / total) * 100, 1) if total > 0 else 0
    return {
        "starting_balance": acc.starting_balance,
        "balance": acc.balance,
        "lot_size": acc.lot_size,
        "total_pnl": round(acc.balance - acc.starting_balance, 2),
        "total_closed": total, "wins": wins, "losses": losses,
        "open_trades": open_count, "win_rate_percent": win_rate,
    }


@app.post("/me/{user_id}/demo-account")
async def update_demo_account(user_id: int, payload: dict, db: Session = Depends(get_db)):
    """Lot size badlo, aur/ya balance ko naye starting_balance pe reset karo
    (user khud apna virtual paisa aur lot size set kar sake)."""
    acc = get_or_create_demo_account(db, user_id)
    if "lot_size" in payload and payload["lot_size"] is not None:
        acc.lot_size = float(payload["lot_size"])
    if "starting_balance" in payload and payload["starting_balance"] is not None:
        acc.starting_balance = float(payload["starting_balance"])
        acc.balance = acc.starting_balance  # fresh restart
    acc.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "updated", "starting_balance": acc.starting_balance, "balance": acc.balance, "lot_size": acc.lot_size}


@app.get("/me/{user_id}/demo-trades")
async def get_demo_trades(user_id: int, limit: int = 50, db: Session = Depends(get_db)):
    trades = (
        db.query(DemoTrade).filter(DemoTrade.user_id == user_id)
        .order_by(DemoTrade.opened_at.desc()).limit(limit).all()
    )
    return {
        "trades": [
            {
                "id": t.id, "chat_title": t.chat_title, "pair": t.pair, "direction": t.direction,
                "lot_size": t.lot_size, "entry_price": t.entry_price, "close_price": t.close_price,
                "pnl": t.pnl, "status": t.status,
                "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in trades
        ]
    }


@app.get("/me/{user_id}/binary-demo-account")
async def get_binary_demo_account(user_id: int, db: Session = Depends(get_db)):
    """Binary/OTC demo (paper-trading) account ki current state - balance, stake,
    payout %, aur ab tak ke binary demo trades ka win/loss + total P&L."""
    acc = get_or_create_binary_demo_account(db, user_id)
    closed = db.query(BinaryDemoTrade).filter(
        BinaryDemoTrade.user_id == user_id, BinaryDemoTrade.status.in_(["win", "loss"])
    )
    total = closed.count()
    wins = closed.filter(BinaryDemoTrade.status == "win").count()
    losses = closed.filter(BinaryDemoTrade.status == "loss").count()
    open_count = db.query(BinaryDemoTrade).filter(BinaryDemoTrade.user_id == user_id, BinaryDemoTrade.status == "open").count()
    win_rate = round((wins / total) * 100, 1) if total > 0 else 0
    return {
        "starting_balance": acc.starting_balance,
        "balance": acc.balance,
        "stake": acc.stake,
        "payout_pct": acc.payout_pct,
        "total_pnl": round(acc.balance - acc.starting_balance, 2),
        "total_closed": total, "wins": wins, "losses": losses,
        "open_trades": open_count, "win_rate_percent": win_rate,
    }


@app.post("/me/{user_id}/binary-demo-account")
async def update_binary_demo_account(user_id: int, payload: dict, db: Session = Depends(get_db)):
    """Stake aur/ya payout % badlo, aur/ya balance ko naye starting_balance pe
    reset karo (user khud apna virtual paisa, stake aur payout set kar sake)."""
    acc = get_or_create_binary_demo_account(db, user_id)
    if "stake" in payload and payload["stake"] is not None:
        acc.stake = float(payload["stake"])
    if "payout_pct" in payload and payload["payout_pct"] is not None:
        acc.payout_pct = float(payload["payout_pct"])
    if "starting_balance" in payload and payload["starting_balance"] is not None:
        acc.starting_balance = float(payload["starting_balance"])
        acc.balance = acc.starting_balance  # fresh restart
    acc.updated_at = datetime.utcnow()
    db.commit()
    return {
        "status": "updated", "starting_balance": acc.starting_balance,
        "balance": acc.balance, "stake": acc.stake, "payout_pct": acc.payout_pct,
    }


@app.get("/me/{user_id}/binary-demo-trades")
async def get_binary_demo_trades(user_id: int, limit: int = 50, db: Session = Depends(get_db)):
    trades = (
        db.query(BinaryDemoTrade).filter(BinaryDemoTrade.user_id == user_id)
        .order_by(BinaryDemoTrade.opened_at.desc()).limit(limit).all()
    )
    return {
        "trades": [
            {
                "id": t.id, "chat_title": t.chat_title, "pair": t.pair, "direction": t.direction,
                "stake": t.stake, "payout_pct": t.payout_pct,
                "pnl": t.pnl, "status": t.status,
                "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in trades
        ]
    }


@app.get("/price/{pair}")
async def get_price(pair: str):
    """Chart ke liye ek pair ka live price - crypto Binance se, forex Finnhub se."""
    is_otc = "OTC" in pair.upper()
    price = await get_live_price(pair, is_otc)
    return {"pair": pair, "price": price}


# ============================================================
# AI PROVIDERS - user apni khud ki AI API keys add/edit/delete/test kar
# sakta hai (Claude, Gemini, DeepSeek, Mistral, Grok, ...). Signal aane
# par saare enabled providers priority order me try hote hain (fallback).
# ============================================================
def _serialize_ai_provider(row: AiProviderKey) -> dict:
    key_plain = None
    try:
        key_plain = decrypt_text(row.encrypted_api_key)
    except Exception:
        pass
    masked = ("•" * 6 + key_plain[-4:]) if key_plain and len(key_plain) > 4 else "••••••"
    cfg = PROVIDER_CATALOG.get(row.provider, {})
    return {
        "id": row.id,
        "provider": row.provider,
        "provider_label": cfg.get("label", row.provider),
        "label": row.label,
        "masked_key": masked,
        "model": row.model or cfg.get("default_model"),
        "enabled": row.enabled,
        "priority": row.priority,
        "status": row.status,
        "last_error": row.last_error,
        "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    }


@app.get("/ai/catalog")
async def get_ai_catalog():
    """Saare supported AI providers ki list (UI me 'Add AI' dropdown ke liye)."""
    return {
        "providers": [
            {"key": k, "label": v["label"], "default_model": v["default_model"]}
            for k, v in PROVIDER_CATALOG.items()
        ]
    }


@app.get("/ai/providers/{user_id}")
async def list_ai_providers(user_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(AiProviderKey).filter(AiProviderKey.user_id == user_id)
        .order_by(AiProviderKey.priority.asc(), AiProviderKey.id.asc()).all()
    )
    return {"providers": [_serialize_ai_provider(r) for r in rows]}


@app.post("/ai/providers")
async def add_ai_provider(payload: dict, db: Session = Depends(get_db)):
    user_id = payload.get("user_id")
    provider = payload.get("provider")
    api_key = (payload.get("api_key") or "").strip()
    if not user_id or not provider or not api_key:
        raise HTTPException(400, "user_id, provider aur api_key zaroori hain")
    if provider not in PROVIDER_CATALOG:
        raise HTTPException(400, f"Unknown provider: {provider}")

    max_priority = (
        db.query(AiProviderKey).filter(AiProviderKey.user_id == user_id)
        .count()
    )
    row = AiProviderKey(
        user_id=user_id, provider=provider,
        label=payload.get("label") or None,
        encrypted_api_key=encrypt_text(api_key),
        model=(payload.get("model") or "").strip() or None,
        enabled=True, priority=max_priority, status="untested",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_ai_provider(row)


@app.put("/ai/providers/{provider_id}")
async def edit_ai_provider(provider_id: int, payload: dict, db: Session = Depends(get_db)):
    row = db.query(AiProviderKey).filter(AiProviderKey.id == provider_id).first()
    if not row:
        raise HTTPException(404, "Provider nahi mila")
    if "api_key" in payload and (payload["api_key"] or "").strip():
        row.encrypted_api_key = encrypt_text(payload["api_key"].strip())
        row.status = "untested"  # naya key hai, dobara test karna hoga
        row.last_error = None
    if "label" in payload:
        row.label = payload["label"] or None
    if "model" in payload:
        row.model = (payload["model"] or "").strip() or None
    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])
    if "priority" in payload:
        row.priority = int(payload["priority"])
    db.commit()
    db.refresh(row)
    return _serialize_ai_provider(row)


@app.delete("/ai/providers/{provider_id}")
async def delete_ai_provider(provider_id: int, db: Session = Depends(get_db)):
    row = db.query(AiProviderKey).filter(AiProviderKey.id == provider_id).first()
    if not row:
        raise HTTPException(404, "Provider nahi mila")
    db.delete(row)
    db.commit()
    return {"deleted": True}


@app.post("/ai/providers/{provider_id}/test")
async def test_ai_provider(provider_id: int, db: Session = Depends(get_db)):
    """AI page ke 'Test' button - key/model sahi hai ya nahi check karke
    status turant DB me update kar deta hai, taaki list turant refresh ho sake."""
    row = db.query(AiProviderKey).filter(AiProviderKey.id == provider_id).first()
    if not row:
        raise HTTPException(404, "Provider nahi mila")
    api_key = decrypt_text(row.encrypted_api_key)
    ok, err = await test_provider_key(row.provider, api_key, row.model)
    row.status = "ok" if ok else "failed"
    row.last_error = err
    row.last_tested_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _serialize_ai_provider(row)


# ============================================================
# BROKER CONNECTIONS - Binary (Quotex/Pocket Option/apni bridge),
# Forex/MT5 (MetaApi.cloud/apni bridge), Crypto (exchange keys/apni bridge).
# ============================================================
def _serialize_broker(row: BrokerConnection) -> dict:
    def masked(enc):
        if not enc:
            return None
        try:
            plain = decrypt_text(enc)
            return ("•" * 6 + plain[-4:]) if len(plain) > 4 else "••••••"
        except Exception:
            return "••••••"

    cfg = BROKER_CATALOG.get(row.broker, {})
    return {
        "id": row.id,
        "category": row.category,
        "broker": row.broker,
        "broker_label": cfg.get("label", row.broker),
        "conn_type": cfg.get("conn_type"),
        "label": row.label,
        "masked_session_id": masked(row.encrypted_session_id),
        "masked_api_key": masked(row.encrypted_api_key),
        "masked_api_secret": masked(row.encrypted_api_secret),
        "masked_passphrase": masked(row.encrypted_passphrase),
        "account_id": row.account_id,
        "bridge_url": row.bridge_url,
        "masked_bridge_token": masked(row.encrypted_bridge_token),
        "enabled": row.enabled,
        "status": row.status,
        "last_error": row.last_error,
        "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    }


@app.get("/broker/catalog")
async def get_broker_catalog():
    """Saare supported brokers, category-wise (UI ke 'Add Broker' list ke liye)."""
    return {
        "brokers": [
            {"key": k, "label": v["label"], "category": v["category"],
             "conn_type": v["conn_type"], "fields": CONN_TYPE_FIELDS[v["conn_type"]]}
            for k, v in BROKER_CATALOG.items()
        ]
    }


@app.get("/broker/connections/{user_id}")
async def list_broker_connections(user_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(BrokerConnection).filter(BrokerConnection.user_id == user_id)
        .order_by(BrokerConnection.category.asc(), BrokerConnection.id.asc()).all()
    )
    return {"connections": [_serialize_broker(r) for r in rows]}


@app.post("/broker/connections")
async def add_broker_connection(payload: dict, db: Session = Depends(get_db)):
    user_id = payload.get("user_id")
    broker = payload.get("broker")
    if not user_id or not broker:
        raise HTTPException(400, "user_id aur broker zaroori hain")
    cfg = BROKER_CATALOG.get(broker)
    if not cfg:
        raise HTTPException(400, f"Unknown broker: {broker}")

    row = BrokerConnection(
        user_id=user_id, category=cfg["category"], broker=broker,
        label=payload.get("label") or None,
        encrypted_session_id=encrypt_text(payload["session_id"]) if payload.get("session_id") else None,
        encrypted_api_key=encrypt_text(payload["api_key"]) if payload.get("api_key") else None,
        encrypted_api_secret=encrypt_text(payload["api_secret"]) if payload.get("api_secret") else None,
        encrypted_passphrase=encrypt_text(payload["passphrase"]) if payload.get("passphrase") else None,
        account_id=payload.get("account_id") or None,
        bridge_url=(payload.get("bridge_url") or "").strip() or None,
        encrypted_bridge_token=encrypt_text(payload["bridge_token"]) if payload.get("bridge_token") else None,
        enabled=True, status="untested",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_broker(row)


@app.put("/broker/connections/{conn_id}")
async def edit_broker_connection(conn_id: int, payload: dict, db: Session = Depends(get_db)):
    row = db.query(BrokerConnection).filter(BrokerConnection.id == conn_id).first()
    if not row:
        raise HTTPException(404, "Connection nahi mila")

    field_map = {
        "session_id": "encrypted_session_id", "api_key": "encrypted_api_key",
        "api_secret": "encrypted_api_secret", "passphrase": "encrypted_passphrase",
        "bridge_token": "encrypted_bridge_token",
    }
    changed_secret = False
    for key, col in field_map.items():
        if key in payload and (payload[key] or "").strip():
            setattr(row, col, encrypt_text(payload[key].strip()))
            changed_secret = True
    if "label" in payload:
        row.label = payload["label"] or None
    if "account_id" in payload:
        row.account_id = payload["account_id"] or None
    if "bridge_url" in payload:
        row.bridge_url = (payload["bridge_url"] or "").strip() or None
    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])
    if changed_secret:
        row.status = "untested"
        row.last_error = None
    db.commit()
    db.refresh(row)
    return _serialize_broker(row)


@app.delete("/broker/connections/{conn_id}")
async def delete_broker_connection(conn_id: int, db: Session = Depends(get_db)):
    row = db.query(BrokerConnection).filter(BrokerConnection.id == conn_id).first()
    if not row:
        raise HTTPException(404, "Connection nahi mila")
    db.delete(row)
    db.commit()
    return {"deleted": True}


@app.post("/broker/connections/{conn_id}/test")
async def test_broker_connection_endpoint(conn_id: int, db: Session = Depends(get_db)):
    row = db.query(BrokerConnection).filter(BrokerConnection.id == conn_id).first()
    if not row:
        raise HTTPException(404, "Connection nahi mila")
    cfg = BROKER_CATALOG.get(row.broker, {})
    fields = {
        "session_id": decrypt_text(row.encrypted_session_id) if row.encrypted_session_id else None,
        "api_key": decrypt_text(row.encrypted_api_key) if row.encrypted_api_key else None,
        "api_secret": decrypt_text(row.encrypted_api_secret) if row.encrypted_api_secret else None,
        "passphrase": decrypt_text(row.encrypted_passphrase) if row.encrypted_passphrase else None,
        "account_id": row.account_id,
        "bridge_url": row.bridge_url,
        "bridge_token": decrypt_text(row.encrypted_bridge_token) if row.encrypted_bridge_token else None,
    }
    ok, err = await test_broker_connection(row.broker, cfg.get("conn_type"), fields)
    row.status = "connected" if ok else "failed"
    row.last_error = err
    row.last_tested_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _serialize_broker(row)


@app.get("/backtest/catalog")
async def get_backtest_catalog():
    """Saari supported strategies (UI ke dropdown ke liye), category ke saath."""
    return {"strategies": [{"key": k, "label": v["label"], "category": v["category"]} for k, v in STRATEGY_CATALOG.items()]}


async def _fetch_backtest_candles(pair: str, source: str, interval: str, num_candles: int):
    if source == "binance":
        return await fetch_binance_klines(pair, interval, num_candles)
    elif source == "twelvedata":
        api_key = os.getenv("TWELVEDATA_API_KEY")
        if not api_key:
            raise HTTPException(400, "TWELVEDATA_API_KEY Railway Variables me set nahi hai - forex/OTC backtest ke liye zaroori hai")
        td_interval = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h"}.get(interval, "5min")
        return await fetch_twelvedata_candles(pair, td_interval, num_candles, api_key)
    else:
        raise HTTPException(400, f"Unknown source: {source}")


@app.post("/backtest/run")
async def run_backtest_endpoint(payload: dict):
    """Ek strategy ko historical candles pe chalata hai aur WIN/LOSS stats deta hai.
    payload: {pair, source ('binance'/'twelvedata'), interval, candles, strategy, expiry_candles}"""
    pair = (payload.get("pair") or "").strip()
    source = payload.get("source", "binance")
    interval = payload.get("interval", "5m")
    num_candles = min(int(payload.get("candles", 300)), 1000)
    strategy = payload.get("strategy", "ema_crossover")
    expiry_candles = max(1, min(int(payload.get("expiry_candles", 3)), 20))

    if not pair:
        raise HTTPException(400, "pair zaroori hai")
    if strategy not in STRATEGY_CATALOG:
        raise HTTPException(400, f"Unknown strategy: {strategy}")

    try:
        candles = await _fetch_backtest_candles(pair, source, interval, num_candles)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Candle data fetch nahi ho paya: {str(e)[:200]}")

    if len(candles) < 30:
        raise HTTPException(400, "Itna kam data mila ki backtest meaningful nahi hoga - pair/interval check karo")

    result = run_backtest(candles, strategy, expiry_candles)
    result["pair"] = pair
    result["source"] = source
    result["interval"] = interval
    result["candles_used"] = len(candles)
    return result


@app.post("/backtest/compare")
async def compare_backtest_endpoint(payload: dict):
    """Saari 15 strategies ko SAME data pe chalake best-se-worst compare karta hai."""
    pair = (payload.get("pair") or "").strip()
    source = payload.get("source", "binance")
    interval = payload.get("interval", "5m")
    num_candles = min(int(payload.get("candles", 300)), 1000)
    expiry_candles = max(1, min(int(payload.get("expiry_candles", 3)), 20))

    if not pair:
        raise HTTPException(400, "pair zaroori hai")

    try:
        candles = await _fetch_backtest_candles(pair, source, interval, num_candles)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Candle data fetch nahi ho paya: {str(e)[:200]}")

    if len(candles) < 30:
        raise HTTPException(400, "Itna kam data mila ki backtest meaningful nahi hoga - pair/interval check karo")

    results = compare_all_strategies(candles, expiry_candles)
    return {"pair": pair, "source": source, "interval": interval, "candles_used": len(candles), "results": results}


@app.get("/health")
async def health():
    return {"status": "ok", "active_sessions": len(active_clients)}
