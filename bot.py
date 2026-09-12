import os
import logging
import asyncio
import time
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeChat,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    MessageHandler, filters,
)

# ============================================================
# BRANDING — Winning Wave (Ads-Optimized Edition)
# ============================================================

BRAND = "Winning Wave"
OFFICIAL_CHANNEL = "@winningwaveofficial"
USERS_TABLE = "winning_wave_users"
TOPICS_TABLE = "winning_wave_topics"
STAFF_TABLE = "winning_wave_staff"
CAMPAIGNS_TABLE = "winning_wave_campaigns"   # NEW: ad campaign registry
EVENTS_TABLE = "winning_wave_events"          # NEW: conversion funnel events

# ============================================================
# ENVIRONMENT
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPPORT_GROUP_ID = int(os.getenv("SUPPORT_GROUP_ID", "0") or 0)

# Optional tuning (all have safe defaults)
NUDGE_DELAY_SECONDS = int(os.getenv("NUDGE_DELAY_SECONDS", "90"))
NUDGE_ENABLED = os.getenv("NUDGE_ENABLED", "true").lower() != "false"
SUSPICIOUS_START_THRESHOLD = int(os.getenv("SUSPICIOUS_START_THRESHOLD", "3"))
SUSPICIOUS_START_WINDOW = int(os.getenv("SUSPICIOUS_START_WINDOW", "60"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "0") or 0)  # 0 = disabled

# Staff IDs from env
_staff_env = os.getenv("AUTHORIZED_STAFF_IDS", "")
AUTHORIZED_STAFF: set[int] = {
    int(v.strip()) for v in _staff_env.split(",")
    if v.strip().lstrip("-").isdigit()
}

# ============================================================
# STATIC CONFIG — Games, Sources, Bonuses
# ============================================================

DEFAULT_GAMES = [
    "Orionstars", "Firekirin", "Ultrapanda", "Juwa", "GameVault",
    "Riversweeps", "Milkyway", "Vblink", "Gameroom",
]
_allowed_env = os.getenv("ALLOWED_GAMES", "").strip()
GAMES = [g.strip() for g in _allowed_env.split(",") if g.strip()] or DEFAULT_GAMES

# Legacy / direct traffic sources (for non-ad links like ?start=website)
TRAFFIC_SOURCES = {
    "website":   "🌐 Website",
    "facebook":  "📘 Facebook",
    "tiktok":    "🎵 TikTok",
    "instagram": "📷 Instagram",
    "youtube":   "▶️ YouTube",
    "twitter":   "🐦 Twitter",
    "reddit":    "🟠 Reddit",
    "direct":    "🔗 Direct",
}

# Bonus options — includes the ad-matched "$5 free play" promise
BONUS_OPTIONS = {
    "signup120": "💰 120% Signup Bonus",
    "freeplay":  "🎁 Redeemable Freeplay",
    "f5free":    "💵 $5 Free Play (First 100)",
}

# Conversion funnel events tracked in winning_wave_events
EVENTS = {
    "bot_start":          "User pressed Start (with attribution)",
    "bonus_selected":     "User selected a bonus option",
    "game_selected":      "User selected a game",
    "first_message":      "User sent their first message",
    "nudge_sent":         "Auto-nudge sent after inactivity",
    "nudge_acted":        "User messaged after receiving nudge",
    "topic_closed":       "Support topic closed by staff",
    "broadcast_received": "User received a broadcast message",
}

# ============================================================
# TUNABLES
# ============================================================

MIN_GAP_SECONDS = 0.4
MAX_QUEUE_SIZE = 30
WORKER_IDLE_TIMEOUT = 5

# ============================================================
# RUNTIME STATE
# ============================================================

_pool: pg_pool.ThreadedConnectionPool | None = None
_user_queues: dict[int, asyncio.Queue] = {}
_user_workers: dict[int, asyncio.Task] = {}
_background_tasks: set[asyncio.Task] = set()
_topic_locks: dict[int, asyncio.Lock] = {}
_start_timestamps: defaultdict[int, list[float]] = defaultdict(list)
_users_with_first_message: set[int] = set()
_nudges_sent: set[int] = set()  # users who already received a nudge

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Silence noisy httpx logs so our structured ad-event logs stay readable
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ============================================================
# DATABASE POOL
# ============================================================

def _pool_connection() -> pg_pool.ThreadedConnectionPool:
    global _pool
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set.")
    if _pool is None:
        _pool = pg_pool.ThreadedConnectionPool(
            minconn=1, maxconn=10, dsn=database_url,
            cursor_factory=RealDictCursor,
        )
        logger.info("PostgreSQL pool created for %s", BRAND)
    return _pool


def _run(query_function, *args):
    pool = _pool_connection()
    conn = pool.getconn()
    try:
        with conn.cursor() as cursor:
            result = query_function(cursor, *args)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ============================================================
# DATABASE INITIALIZATION (idempotent — safe to run repeatedly)
# ============================================================

def init_db() -> None:
    def q(cur):
        # Users table
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {USERS_TABLE} (
                user_id BIGINT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                topic_id BIGINT,
                topic_status TEXT NOT NULL DEFAULT 'open',
                game TEXT,
                source TEXT,
                bonus TEXT,
                first_touch_source TEXT,
                last_touch_source TEXT,
                first_touch_campaign TEXT,
                last_touch_campaign TEXT,
                first_touch_at TIMESTAMPTZ,
                last_touch_at TIMESTAMPTZ,
                is_suspicious BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # Topics table
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TOPICS_TABLE} (
                topic_id BIGINT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES {USERS_TABLE}(user_id)
                    ON DELETE CASCADE
            )
        """)
        # Staff table
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {STAFF_TABLE} (
                user_id BIGINT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # NEW: Campaigns registry
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {CAMPAIGNS_TABLE} (
                token TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                campaign_name TEXT NOT NULL,
                creative TEXT,
                target_channel TEXT,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ended_at TIMESTAMPTZ,
                expected_clicks INTEGER,
                actual_clicks INTEGER,
                notes TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        # NEW: Events log (conversion funnel)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                event_name TEXT NOT NULL,
                campaign_token TEXT,
                event_data JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{EVENTS_TABLE}_user ON {EVENTS_TABLE}(user_id)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{EVENTS_TABLE}_name ON {EVENTS_TABLE}(event_name)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{EVENTS_TABLE}_campaign ON {EVENTS_TABLE}(campaign_token)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{EVENTS_TABLE}_created ON {EVENTS_TABLE}(created_at)")

        # Backwards-compat: add new columns to existing tables
        for col, decl in [
            ("first_touch_source",   "TEXT"),
            ("last_touch_source",    "TEXT"),
            ("first_touch_campaign", "TEXT"),
            ("last_touch_campaign",  "TEXT"),
            ("first_touch_at",       "TIMESTAMPTZ"),
            ("last_touch_at",        "TIMESTAMPTZ"),
            ("is_suspicious",        "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("source",               "TEXT"),
            ("bonus",                "TEXT"),
        ]:
            cur.execute(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS {col} {decl}")

    _run(q)
    logger.info("Database initialized: campaigns + events tables ready.")


def load_staff() -> None:
    def q(cur):
        cur.execute(f"SELECT user_id FROM {STAFF_TABLE}")
        return {row["user_id"] for row in cur.fetchall()}
    AUTHORIZED_STAFF.update(_run(q))


# ============================================================
# DB CRUD — USERS
# ============================================================

async def db_user(user_id: int):
    def q(cur):
        cur.execute(f"SELECT * FROM {USERS_TABLE} WHERE user_id = %s", (user_id,))
        return cur.fetchone()
    return await asyncio.to_thread(_run, q)


async def db_user_by_topic(topic_id: int):
    def q(cur):
        cur.execute(f"SELECT * FROM {USERS_TABLE} WHERE topic_id = %s", (topic_id,))
        return cur.fetchone()
    return await asyncio.to_thread(_run, q)


async def db_upsert(
    user_id: int, name: str, username: str,
    topic_id: int | None = None,
    game: str | None = None,
    status: str | None = None,
    source: str | None = None,
    bonus: str | None = None,
    first_touch_source: str | None = None,
    last_touch_source: str | None = None,
    first_touch_campaign: str | None = None,
    last_touch_campaign: str | None = None,
    is_suspicious: bool | None = None,
) -> None:
    """Upsert user with first-touch / last-touch attribution.

    first_touch_* fields are only set on INSERT (never overwritten on update).
    last_touch_* fields update on every touch.
    """
    def q(cur):
        cur.execute(f"""
            INSERT INTO {USERS_TABLE}
                (user_id, name, username, topic_id, game, topic_status, source, bonus,
                 first_touch_source, last_touch_source,
                 first_touch_campaign, last_touch_campaign,
                 first_touch_at, last_touch_at, is_suspicious)
            VALUES (%s, %s, %s, %s, %s, COALESCE(%s, 'open'), %s, %s,
                    %s, %s, %s, %s, NOW(), NOW(), COALESCE(%s, FALSE))
            ON CONFLICT(user_id) DO UPDATE SET
                name = EXCLUDED.name,
                username = EXCLUDED.username,
                topic_id = COALESCE(EXCLUDED.topic_id, {USERS_TABLE}.topic_id),
                game = COALESCE(EXCLUDED.game, {USERS_TABLE}.game),
                topic_status = COALESCE(%s, {USERS_TABLE}.topic_status),
                source = COALESCE(EXCLUDED.source, {USERS_TABLE}.source),
                bonus = COALESCE(EXCLUDED.bonus, {USERS_TABLE}.bonus),
                last_touch_source = COALESCE(EXCLUDED.last_touch_source, {USERS_TABLE}.last_touch_source),
                last_touch_campaign = COALESCE(EXCLUDED.last_touch_campaign, {USERS_TABLE}.last_touch_campaign),
                last_touch_at = NOW(),
                is_suspicious = COALESCE(EXCLUDED.is_suspicious, {USERS_TABLE}.is_suspicious),
                last_seen = NOW()
        """, (
            user_id, name, username, topic_id, game, status, source, bonus,
            first_touch_source, last_touch_source,
            first_touch_campaign, last_touch_campaign,
            is_suspicious,
            status,
        ))
    await asyncio.to_thread(_run, q)


async def db_all_users():
    def q(cur):
        cur.execute(f"SELECT * FROM {USERS_TABLE} ORDER BY created_at DESC")
        return cur.fetchall()
    return await asyncio.to_thread(_run, q)


async def db_status(user_id: int, status: str) -> None:
    def q(cur):
        cur.execute(
            f"UPDATE {USERS_TABLE} SET topic_status = %s WHERE user_id = %s",
            (status, user_id),
        )
    await asyncio.to_thread(_run, q)


async def db_clear_topic(user_id: int, topic_id: int) -> None:
    def q(cur):
        cur.execute(
            f"UPDATE {USERS_TABLE} SET topic_id = NULL, topic_status = 'open' "
            "WHERE user_id = %s", (user_id,)
        )
        cur.execute(f"DELETE FROM {TOPICS_TABLE} WHERE topic_id = %s", (topic_id,))
    await asyncio.to_thread(_run, q)


# ============================================================
# DB CRUD — TOPICS
# ============================================================

async def db_topic(topic_id: int, user_id: int) -> None:
    def q(cur):
        cur.execute(f"""
            INSERT INTO {TOPICS_TABLE} (topic_id, user_id) VALUES (%s, %s)
            ON CONFLICT(topic_id) DO UPDATE SET user_id = EXCLUDED.user_id
        """, (topic_id, user_id))
    await asyncio.to_thread(_run, q)


# ============================================================
# DB CRUD — STAFF
# ============================================================

async def db_add_staff(user_id: int, name: str) -> None:
    def q(cur):
        cur.execute(f"""
            INSERT INTO {STAFF_TABLE} (user_id, name) VALUES (%s, %s)
            ON CONFLICT(user_id) DO UPDATE SET name = EXCLUDED.name
        """, (user_id, name))
    await asyncio.to_thread(_run, q)


async def db_remove_staff(user_id: int) -> None:
    def q(cur):
        cur.execute(f"DELETE FROM {STAFF_TABLE} WHERE user_id = %s", (user_id,))
    await asyncio.to_thread(_run, q)


# ============================================================
# DB CRUD — CAMPAIGNS (NEW)
# ============================================================

async def db_get_campaign(token: str):
    def q(cur):
        cur.execute(
            f"SELECT * FROM {CAMPAIGNS_TABLE} WHERE token = %s", (token,)
        )
        return cur.fetchone()
    return await asyncio.to_thread(_run, q)


async def db_list_campaigns(active_only: bool = False):
    def q(cur):
        if active_only:
            cur.execute(
                f"SELECT * FROM {CAMPAIGNS_TABLE} WHERE is_active = TRUE "
                "ORDER BY started_at DESC"
            )
        else:
            cur.execute(
                f"SELECT * FROM {CAMPAIGNS_TABLE} ORDER BY started_at DESC"
            )
        return cur.fetchall()
    return await asyncio.to_thread(_run, q)


async def db_add_campaign(
    token: str, platform: str, name: str,
    creative: str | None = None,
    target_channel: str | None = None,
    expected_clicks: int | None = None,
    notes: str | None = None,
) -> None:
    def q(cur):
        cur.execute(f"""
            INSERT INTO {CAMPAIGNS_TABLE}
                (token, platform, campaign_name, creative, target_channel,
                 expected_clicks, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(token) DO UPDATE SET
                platform = EXCLUDED.platform,
                campaign_name = EXCLUDED.campaign_name,
                creative = COALESCE(EXCLUDED.creative, {CAMPAIGNS_TABLE}.creative),
                target_channel = COALESCE(EXCLUDED.target_channel, {CAMPAIGNS_TABLE}.target_channel),
                expected_clicks = COALESCE(EXCLUDED.expected_clicks, {CAMPAIGNS_TABLE}.expected_clicks),
                notes = COALESCE(EXCLUDED.notes, {CAMPAIGNS_TABLE}.notes),
                is_active = TRUE,
                ended_at = NULL
        """, (token, platform, name, creative, target_channel, expected_clicks, notes))
    await asyncio.to_thread(_run, q)


async def db_end_campaign(token: str) -> bool:
    def q(cur):
        cur.execute(
            f"UPDATE {CAMPAIGNS_TABLE} SET is_active = FALSE, ended_at = NOW() "
            "WHERE token = %s", (token,)
        )
        return cur.rowcount > 0
    return await asyncio.to_thread(_run, q)


async def db_update_campaign_clicks(token: str, actual_clicks: int) -> bool:
    """Staff can manually update actual_clicks from Telegram Ads dashboard."""
    def q(cur):
        cur.execute(
            f"UPDATE {CAMPAIGNS_TABLE} SET actual_clicks = %s WHERE token = %s",
            (actual_clicks, token)
        )
        return cur.rowcount > 0
    return await asyncio.to_thread(_run, q)


# ============================================================
# DB CRUD — EVENTS (NEW — conversion funnel)
# ============================================================

async def db_log_event(
    user_id: int,
    event_name: str,
    campaign_token: str | None = None,
    event_data: dict | None = None,
) -> None:
    """Log a conversion funnel event. Never raises — failures are logged only."""
    try:
        def q(cur):
            cur.execute(f"""
                INSERT INTO {EVENTS_TABLE}
                    (user_id, event_name, campaign_token, event_data)
                VALUES (%s, %s, %s, %s)
            """, (
                user_id, event_name, campaign_token,
                json.dumps(event_data) if event_data else None,
            ))
        await asyncio.to_thread(_run, q)
    except Exception:
        logger.exception("Failed to log event %s for user %s", event_name, user_id)


# ============================================================
# SHARED HELPERS
# ============================================================

def fire_and_forget(coroutine) -> None:
    """Run a coroutine in background, keep a strong reference so it can't be GC'd mid-flight."""
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def is_staff(message) -> bool:
    return bool(message.from_user and message.from_user.id in AUTHORIZED_STAFF)


def user_label(user) -> str:
    name = (user.full_name or user.first_name or "Unknown Player").replace("\n", " ").strip()
    return f"{name} (@{user.username})" if user.username else f"{name} [ID: {user.id}]"


# ============================================================
# DEEP-LINK PARSERS — Campaign-Aware
# ============================================================

def parse_game(args: list[str]) -> str | None:
    if not args:
        return None
    value = args[0].strip().lower()
    return next((g for g in GAMES if g.lower() == value), None)


def parse_source(args: list[str]) -> tuple[str | None, str | None]:
    """Parse deep-link parameter.

    Returns (source_label, campaign_token).

    Supported forms:
      ?start=website                 -> ("🌐 Website", None)
      ?start=src_website             -> ("🌐 Website", None)
      ?start=src_tgads_v1            -> ("📣 Telegram Ads", "tgads_v1")
      ?start=src_mangoads_q4         -> ("🟣 MangoAds", "mangoads_q4")
      ?start=src_chan_casinofans     -> ("📢 Channel Buy", "chan_casinofans")
      ?start=juwa                    -> (None, None)  # game deep-link
    """
    if not args:
        return None, None

    value = args[0].strip().lower()

    # Strip optional src_ prefix
    if value.startswith("src_"):
        value = value[4:]

    # Check if it's a campaign token (tokens contain underscore + custom suffix)
    # Campaign tokens are dynamic, so we check against the campaigns DB later.
    # For now, return a heuristic source label based on prefix.
    if value in TRAFFIC_SOURCES:
        return TRAFFIC_SOURCES[value], None

    # Heuristic: known ad-platform prefixes
    PLATFORM_PREFIXES = {
        "tgads_":   "📣 Telegram Ads",
        "mangoads_":"🟣 MangoAds",
        "richads_": "🟠 RichAds",
        "chan_":    "📢 Channel Buy",
        "search_":  "🔍 TG Search Ad",
        "fb_":      "📘 Facebook",
        "ig_":      "📷 Instagram",
        "tt_":      "🎵 TikTok",
        "yt_":      "▶️ YouTube",
        "tw_":      "🐦 Twitter",
        "rd_":      "🟠 Reddit",
    }
    for prefix, label in PLATFORM_PREFIXES.items():
        if value.startswith(prefix):
            return label, value  # value (without src_) is the campaign token

    return None, None


def closed_topic(error: TelegramError) -> bool:
    text = str(error).lower()
    return any(item in text for item in (
        "topic_closed", "thread_not_found", "message thread not found",
    ))


# ============================================================
# KEYBOARDS
# ============================================================

def game_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(GAMES), 2):
        row = [InlineKeyboardButton(f"🎮 {GAMES[i]}", callback_data=f"game:{GAMES[i]}")]
        if i + 1 < len(GAMES):
            row.append(InlineKeyboardButton(
                f"🎮 {GAMES[i + 1]}", callback_data=f"game:{GAMES[i + 1]}"
            ))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def bonus_keyboard() -> InlineKeyboardMarkup:
    """All 3 bonus options including the $5 free play promised in ads."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BONUS_OPTIONS["f5free"], callback_data="bonus:f5free")],
        [InlineKeyboardButton(BONUS_OPTIONS["signup120"], callback_data="bonus:signup120")],
        [InlineKeyboardButton(BONUS_OPTIONS["freeplay"], callback_data="bonus:freeplay")],
    ])


# ============================================================
# ANTI-FRAUD — Suspicious /start detection
# ============================================================

def is_suspicious_start(user_id: int) -> bool:
    """Returns True if user has pressed /start >= THRESHOLD times in WINDOW seconds."""
    now = time.time()
    window_start = now - SUSPICIOUS_START_WINDOW
    recent = [t for t in _start_timestamps[user_id] if t > window_start]
    recent.append(now)
    _start_timestamps[user_id] = recent
    # Clean up old entries to prevent unbounded growth
    if len(_start_timestamps) > 10000:
        for uid in list(_start_timestamps.keys()):
            _start_timestamps[uid] = [t for t in _start_timestamps[uid] if t > window_start]
            if not _start_timestamps[uid]:
                _start_timestamps.pop(uid, None)
    return len(recent) >= SUSPICIOUS_START_THRESHOLD


# ============================================================
# AUTO-NUDGE — If user starts but doesn't message within delay
# ============================================================

async def schedule_nudge(user_id: int, campaign_token: str | None, context) -> None:
    """After NUDGE_DELAY_SECONDS, if user hasn't messaged, send a nudge."""
    if not NUDGE_ENABLED:
        return
    await asyncio.sleep(NUDGE_DELAY_SECONDS)
    # Check if user has sent any message by now
    if user_id in _users_with_first_message:
        return
    if user_id in _nudges_sent:
        return
    _nudges_sent.add(user_id)
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"👋 Hey! Still there?\n\n"
                f"🎁 Your $5 Free Play is waiting — just send a message "
                f"and our team will hook you up!\n\n"
                f"💬 You can also pick a bonus below 👇"
            ),
            reply_markup=bonus_keyboard(),
        )
        await db_log_event(
            user_id, "nudge_sent",
            campaign_token=campaign_token,
            event_data={"delay_seconds": NUDGE_DELAY_SECONDS},
        )
        logger.info(
            "NUDGE_SENT | user_id=%s | campaign=%s | delay=%ss",
            user_id, campaign_token or "none", NUDGE_DELAY_SECONDS,
        )
    except TelegramError as e:
        logger.warning("Nudge failed for user %s: %s", user_id, e)


# ============================================================
# FORUM TOPIC OPERATIONS
# ============================================================

def topic_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _topic_locks:
        _topic_locks[user_id] = asyncio.Lock()
    return _topic_locks[user_id]


async def create_topic(user, context, game=None, source=None, campaign_token=None) -> int | None:
    try:
        forum_topic = await context.bot.create_forum_topic(
            chat_id=SUPPORT_GROUP_ID, name=user_label(user)[:120]
        )
        topic_id = forum_topic.message_thread_id

        old = await db_user(user.id)
        selected_game = old["game"] if old and old["game"] else game
        saved_source = (old.get("source") if old else None) or source

        await db_upsert(
            user.id, user.full_name or user.first_name or "Unknown", user.username or "",
            topic_id=topic_id, game=selected_game, status="open", source=saved_source,
            last_touch_source=saved_source, last_touch_campaign=campaign_token,
        )
        await db_topic(topic_id, user.id)

        game_line = f"🎮 Game     : {selected_game}" if selected_game else "🎮 Game     : Not selected yet"
        source_line = f"📊 Source   : {saved_source}" if saved_source else "📊 Source   : Direct/Unknown"
        campaign_line = f"🎯 Campaign : {campaign_token}" if campaign_token else "🎯 Campaign : (none)"
        entry_line = "🔗 Entry    : Deep-link" if game or source else "🔗 Entry    : Direct"

        await context.bot.send_message(
            chat_id=SUPPORT_GROUP_ID, message_thread_id=topic_id,
            text=(
                f"📣 NEW {BRAND.upper()} PLAYER CONNECTED\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Name     : {user.full_name or user.first_name or 'Unknown'}\n"
                f"Username : @{user.username or 'No username'}\n"
                f"ID       : {user.id}\n"
                f"{game_line}\n{source_line}\n{campaign_line}\n{entry_line}\n\n"
                "💬 Customer messages below.\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
        )
        return topic_id
    except TelegramError as error:
        logger.error("Topic creation failed: %s", error)
        return None


async def reopen_or_recreate(user, topic_id: int, context) -> int | None:
    try:
        await context.bot.reopen_forum_topic(
            chat_id=SUPPORT_GROUP_ID, message_thread_id=topic_id
        )
        await db_status(user.id, "open")
        await context.bot.send_message(
            chat_id=SUPPORT_GROUP_ID, message_thread_id=topic_id,
            text="🔓 Customer wapas aa gaya — topic reopened.",
        )
        return topic_id
    except TelegramError as error:
        logger.warning("Could not reopen topic %s: %s", topic_id, error)
        await db_clear_topic(user.id, topic_id)
        return await create_topic(user, context)


async def get_topic(user, context, game=None, source=None, campaign_token=None) -> int | None:
    lock = topic_lock(user.id)
    try:
        async with lock:
            row = await db_user(user.id)
            if row and row["topic_id"]:
                topic_id = (
                    await reopen_or_recreate(user, row["topic_id"], context)
                    if row.get("topic_status") == "closed" else row["topic_id"]
                )
                if topic_id and (game or source or campaign_token):
                    await db_upsert(
                        user.id, user.full_name or user.first_name or "Unknown",
                        user.username or "", game=game, source=source,
                        last_touch_source=source, last_touch_campaign=campaign_token,
                    )
                return topic_id
            return await create_topic(user, context, game, source, campaign_token)
    finally:
        if not lock.locked():
            _topic_locks.pop(user.id, None)


async def group_message(user, context, topic_id: int, text: str) -> int:
    try:
        await context.bot.send_message(
            chat_id=SUPPORT_GROUP_ID, message_thread_id=topic_id, text=text
        )
        return topic_id
    except TelegramError as error:
        if closed_topic(error):
            new_topic_id = await reopen_or_recreate(user, topic_id, context)
            if new_topic_id:
                try:
                    await context.bot.send_message(
                        chat_id=SUPPORT_GROUP_ID, message_thread_id=new_topic_id, text=text
                    )
                    return new_topic_id
                except TelegramError as retry_error:
                    logger.error("Message failed after topic recovery: %s", retry_error)
        else:
            logger.error("Group message failed: %s", error)
    return topic_id


async def forward_customer(user, update, context, topic_id: int, retry=False) -> bool:
    try:
        await context.bot.copy_message(
            chat_id=SUPPORT_GROUP_ID, from_chat_id=user.id,
            message_id=update.message.message_id, message_thread_id=topic_id,
        )
        return True
    except TelegramError as error:
        if closed_topic(error) and not retry:
            new_topic_id = await reopen_or_recreate(user, topic_id, context)
            if new_topic_id:
                return await forward_customer(user, update, context, new_topic_id, True)
        logger.error("Customer forward failed: %s", error)
        return False


# ============================================================
# PER-USER MESSAGE QUEUE (spam protection)
# ============================================================

def start_worker(user_id: int, context, queue: asyncio.Queue) -> None:
    _user_workers[user_id] = asyncio.create_task(queue_worker(user_id, context, queue))


async def queue_worker(user_id: int, context, queue: asyncio.Queue) -> None:
    try:
        while True:
            try:
                user, update, topic_id = await asyncio.wait_for(
                    queue.get(), timeout=WORKER_IDLE_TIMEOUT
                )
            except asyncio.TimeoutError:
                if queue.empty():
                    return
                continue
            if not await forward_customer(user, update, context, topic_id):
                try:
                    await update.message.reply_text(
                        "⚠️ Could not send your message. Please try again."
                    )
                except TelegramError:
                    pass
            await asyncio.sleep(MIN_GAP_SECONDS)
    finally:
        if _user_workers.get(user_id) is asyncio.current_task():
            _user_workers.pop(user_id, None)
        if _user_queues.get(user_id) is queue:
            if queue.empty():
                _user_queues.pop(user_id, None)
            elif user_id not in _user_workers:
                start_worker(user_id, context, queue)


async def enqueue(user, update, context, topic_id: int) -> None:
    queue = _user_queues.setdefault(user.id, asyncio.Queue(maxsize=MAX_QUEUE_SIZE))
    try:
        queue.put_nowait((user, update, topic_id))
    except asyncio.QueueFull:
        await update.message.reply_text(
            "⚠️ Bohot zyada messages ek sath aa rahe hain. Thoda ruk ke bhejein."
        )
        return
    worker = _user_workers.get(user.id)
    if worker is None or worker.done():
        start_worker(user.id, context, queue)


# ============================================================
# CUSTOMER ACTIONS — /start, /help, bonus, games, support, message
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User pressed /start. Reply FIRST, then do background work.

    Critical: Every /start is logged with full attribution so Railway logs
    can be grepped to verify ad campaigns are driving real users.
    """
    if not update.message or not update.effective_user:
        return
    user = update.effective_user
    game, source_label, campaign_token = (
        parse_game(context.args or []),
        *parse_source(context.args or []),
    )

    # Anti-fraud check
    suspicious = is_suspicious_start(user.id)

    # STRUCTURED LOG — grep-friendly format for Railway
    logger.info(
        "START_EVENT | user_id=%s | username=@%s | args=%s | game=%s | source=%s | campaign=%s | suspicious=%s",
        user.id, user.username or "none", context.args,
        game or "none", source_label or "none", campaign_token or "none", suspicious,
    )

    # Conversion-optimized welcome — mirrors what the ad promised
    if game:
        await update.message.reply_text(
            f"🌊 WELCOME TO {BRAND.upper()}!\n\n"
            f"🎮 Game selected: {game}\n"
            f"🎁 FIRST 100 PLAYERS: $5 FREE PLAY\n"
            f"💸 CashApp • Crypto • Chime payouts\n\n"
            f"💬 Send your message now — our team replies in ~2 minutes!\n\n"
            f"📢 Official updates: {OFFICIAL_CHANNEL}"
        )
    else:
        await update.message.reply_text(
            f"🌊 WELCOME TO {BRAND.upper()}!\n\n"
            f"🎁 FIRST 100 PLAYERS: $5 FREE PLAY\n"
            f"💸 CashApp • Crypto • Chime payouts\n"
            f"🎮 9 games: Fire Kirin, Orion Stars, Juwa + 6 more\n\n"
            f"👇 CLAIM YOUR BONUS BELOW 👇",
            reply_markup=bonus_keyboard(),
        )

    # Background: log event, set attribution, create topic, schedule nudge
    fire_and_forget(finalize_start(
        user, context, game, source_label, campaign_token, suspicious
    ))


async def finalize_start(
    user, context, game, source, campaign_token, suspicious
) -> None:
    """Background work for /start — never blocks user reply."""
    try:
        # Log event FIRST (even if topic creation fails, we keep attribution)
        old = await db_user(user.id)
        is_new_user = not (old and old.get("topic_id"))

        await db_log_event(
            user.id, "bot_start",
            campaign_token=campaign_token,
            event_data={
                "game": game,
                "source": source,
                "is_new_user": is_new_user,
                "is_suspicious": suspicious,
                "args": [str(a) for a in []],
                "username": user.username,
            },
        )

        # Upsert with attribution
        await db_upsert(
            user.id, user.full_name or user.first_name or "Unknown", user.username or "",
            game=game, source=source,
            first_touch_source=source if is_new_user else None,
            last_touch_source=source,
            first_touch_campaign=campaign_token if is_new_user else None,
            last_touch_campaign=campaign_token,
            is_suspicious=suspicious,
        )

        # Create / reopen topic
        topic_id = await get_topic(
            user, context, game=game, source=source, campaign_token=campaign_token,
        )

        if topic_id and not is_new_user:
            # Returning customer: notify staff (don't re-post full card)
            details = [
                f"👤 Name     : {user.full_name or user.first_name or 'Unknown'}",
                f"🔗 Username : @{user.username or 'No username'}",
                f"🆔 ID       : {user.id}",
            ]
            if game:
                details.append(f"🎮 Game     : {game}")
            if source:
                details.append(f"📊 Source   : {source}")
            if campaign_token:
                details.append(f"🎯 Campaign : {campaign_token}")
            await group_message(
                user, context, topic_id,
                f"🔁 CUSTOMER STARTED BOT AGAIN\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                + "\n".join(details) + "\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━",
            )

        # Schedule nudge for new users (and returning users without messages)
        if topic_id and user.id not in _users_with_first_message:
            fire_and_forget(schedule_nudge(user.id, campaign_token, context))

    except Exception:
        logger.exception("/start finalization failed for user %s", user.id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        f"ℹ️ {BRAND.upper()} SUPPORT — HELP\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "/start   — Begin or restart your support chat\n"
        "/games   — Choose or change your game\n"
        "/support — Reach our support team\n"
        "/help    — Show this message\n\n"
        "💬 You can also just send a message anytime — our team will reply here.\n\n"
        f"📢 Official updates: {OFFICIAL_CHANNEL}"
    )


async def bonus_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user = query.from_user
    bonus_key = query.data.replace("bonus:", "", 1)
    if bonus_key not in BONUS_OPTIONS:
        await query.message.reply_text("⚠️ Invalid bonus selection.")
        return
    bonus_label = BONUS_OPTIONS[bonus_key]

    logger.info(
        "BONUS_SELECTED | user_id=%s | bonus=%s",
        user.id, bonus_key,
    )

    await query.message.reply_text(
        f"✅ {bonus_label} selected!\n\n"
        f"🎮 Now choose your game:",
        reply_markup=game_keyboard(),
    )
    fire_and_forget(finalize_bonus(user, context, bonus_key, bonus_label))


async def finalize_bonus(user, context, bonus_key: str, bonus_label: str) -> None:
    try:
        # Get user's campaign for attribution
        row = await db_user(user.id)
        campaign_token = row.get("last_touch_campaign") if row else None

        topic_id = await get_topic(user, context, campaign_token=campaign_token)
        if topic_id:
            await db_upsert(
                user.id, user.full_name or user.first_name or "Unknown", user.username or "",
                topic_id=topic_id, bonus=bonus_label,
            )
            await db_log_event(
                user.id, "bonus_selected",
                campaign_token=campaign_token,
                event_data={"bonus_key": bonus_key, "bonus_label": bonus_label},
            )
            await group_message(user, context, topic_id, f"🎁 BONUS SELECTED: {bonus_label}")
    except Exception:
        logger.exception("Bonus finalization failed for %s", user.id)


async def games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    await update.message.reply_text("🎮 Choose your game:", reply_markup=game_keyboard())
    fire_and_forget(command_touch(update.effective_user, context, "games"))


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    await update.message.reply_text(
        "💬 Select your game (optional):", reply_markup=game_keyboard()
    )
    fire_and_forget(command_touch(update.effective_user, context, "support"))


async def command_touch(user, context, command: str) -> None:
    try:
        old = await db_user(user.id)
        is_new_user = not (old and old.get("topic_id"))
        campaign_token = old.get("last_touch_campaign") if old else None
        topic_id = await get_topic(user, context, campaign_token=campaign_token)
        if topic_id and not is_new_user:
            await group_message(
                user, context, topic_id,
                f"🔁 CUSTOMER USED /{command}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Name     : {user.full_name or user.first_name or 'Unknown'}\n"
                f"🔗 Username : @{user.username or 'No username'}\n"
                f"🆔 ID       : {user.id}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━",
            )
    except Exception:
        logger.exception("/%s finalization failed for %s", command, user.id)


async def game_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user = query.from_user
    game = query.data.replace("game:", "", 1)
    if game not in GAMES:
        await query.message.reply_text("⚠️ Invalid game selection.")
        return

    logger.info("GAME_SELECTED | user_id=%s | game=%s", user.id, game)

    await query.message.reply_text(
        f"✅ {game} selected!\n\n"
        f"💬 Send your message — team will reply shortly!"
    )
    fire_and_forget(finalize_game(user, context, game))


async def finalize_game(user, context, game: str) -> None:
    try:
        row = await db_user(user.id)
        campaign_token = row.get("last_touch_campaign") if row else None
        topic_id = await get_topic(user, context, campaign_token=campaign_token)
        if topic_id:
            await db_upsert(
                user.id, user.full_name or user.first_name or "Unknown", user.username or "",
                topic_id=topic_id, game=game,
            )
            await db_topic(topic_id, user.id)
            await db_log_event(
                user.id, "game_selected",
                campaign_token=campaign_token,
                event_data={"game": game},
            )
            await group_message(user, context, topic_id, f"🎮 GAME SELECTED: {game}")
    except Exception:
        logger.exception("Game finalization failed for %s", user.id)


async def customer_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Customer sent a message (text/photo/video/etc.) in private chat."""
    if not update.message or not update.effective_user:
        return
    user = update.effective_user

    # Track first message for nudge suppression
    is_first_message = user.id not in _users_with_first_message
    if is_first_message:
        _users_with_first_message.add(user.id)

    try:
        row = await db_user(user.id)
        campaign_token = row.get("last_touch_campaign") if row else None
        topic_id = await get_topic(user, context, campaign_token=campaign_token)
    except Exception:
        logger.exception("Could not prepare support topic for customer %s", user.id)
        topic_id = None

    if not topic_id:
        await update.message.reply_text(
            "⚠️ Support system temporarily unavailable. Please try again."
        )
        return

    # Log first_message event (for conversion funnel)
    if is_first_message:
        was_nudged = user.id in _nudges_sent
        await db_log_event(
            user.id, "first_message",
            campaign_token=campaign_token,
            event_data={"was_nudged": was_nudged, "source": row.get("source") if row else None},
        )
        if was_nudged:
            await db_log_event(
                user.id, "nudge_acted",
                campaign_token=campaign_token,
            )
        logger.info(
            "FIRST_MESSAGE | user_id=%s | campaign=%s | nudged=%s",
            user.id, campaign_token or "none", was_nudged,
        )

    await enqueue(user, update, context, topic_id)


# ============================================================
# SUPPORT GROUP / STAFF MESSAGE HANDLER
# ============================================================

async def support_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    message = update.message
    if message.chat.id != SUPPORT_GROUP_ID or (message.from_user and message.from_user.is_bot):
        return
    # SECURITY: only staff can reply to customers via the group
    if not is_staff(message):
        logger.warning(
            "Ignoring non-staff support-group message from %s (id=%s)",
            message.from_user.username or "unknown", message.from_user.id,
        )
        return
    topic_id = message.message_thread_id
    if not topic_id or topic_id == 1:
        return
    row = await db_user_by_topic(topic_id)
    if not row:
        return
    try:
        await context.bot.copy_message(
            chat_id=int(row["user_id"]), from_chat_id=SUPPORT_GROUP_ID,
            message_id=message.message_id,
        )
        logger.info(
            "AGENT_REPLY | customer=%s | topic=%s | agent=%s",
            row["user_id"], topic_id, message.from_user.id,
        )
    except TelegramError as error:
        logger.error("Staff reply delivery failed: %s", error)
        error_text = str(error).lower()
        if any(v in error_text for v in (
            "blocked", "deactivated", "chat not found", "forbidden",
        )):
            warning = (
                "⚠️ DELIVERY FAILED\n\n"
                "Customer ne bot block kar diya hai ya account deactivate hai."
            )
        else:
            warning = f"⚠️ Reply delivery error: {error}"
        try:
            await context.bot.send_message(
                chat_id=SUPPORT_GROUP_ID, message_thread_id=topic_id, text=warning
            )
        except TelegramError:
            pass


# ============================================================
# STAFF COMMAND — Authorization helper
# ============================================================

async def require_staff(update: Update) -> bool:
    if not update.message or update.message.chat.id != SUPPORT_GROUP_ID:
        return False
    if not is_staff(update.message):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return False
    return True


# ============================================================
# STAFF COMMAND — /groupid (setup helper)
# ============================================================

async def group_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Staff-only setup helper; works even before SUPPORT_GROUP_ID is set correctly."""
    if not update.message:
        return
    message = update.message
    if message.chat.id > 0:
        await message.reply_text(f"⚠️ Use /groupid inside the {BRAND} support group.")
        return
    if not is_staff(message):
        await message.reply_text("⛔ You are not authorized to use this command.")
        return
    await message.reply_text(f"✅ This group's exact Bot API ID:\n{message.chat.id}")


# ============================================================
# STAFF COMMAND — /id (customer info in topic)
# ============================================================

async def customer_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_staff(update):
        return
    topic_id = update.message.message_thread_id
    if not topic_id or topic_id == 1:
        await update.message.reply_text("⚠️ Use /id inside a customer topic.")
        return
    row = await db_user_by_topic(topic_id)
    if not row:
        await update.message.reply_text("❌ No customer connected to this topic.")
        return
    await update.message.reply_text(
        f"📋 CUSTOMER INFORMATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name     : {row['name']}\n"
        f"🔗 Username : @{row['username'] or 'No username'}\n"
        f"🆔 ID       : {row['user_id']}\n"
        f"🎮 Game     : {row['game'] or 'Not selected'}\n"
        f"🎁 Bonus    : {row['bonus'] or 'Not selected'}\n"
        f"📊 Source   : {row['source'] or 'Direct/Unknown'}\n"
        f"🎯 1st Touch: {row.get('first_touch_source') or '—'} "
        f"({row.get('first_touch_campaign') or '—'})\n"
        f"🎯 Last Touch: {row.get('last_touch_source') or '—'} "
        f"({row.get('last_touch_campaign') or '—'})\n"
        f"⚠️ Suspicious: {'YES' if row.get('is_suspicious') else 'no'}\n"
        f"📌 Topic ID : {topic_id}\n"
        f"📅 Joined   : {row['created_at']}\n"
        f"👀 Last seen: {row['last_seen']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
# STAFF COMMAND — /close (close topic)
# ============================================================

async def close_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_staff(update):
        return
    topic_id = update.message.message_thread_id
    if not topic_id or topic_id == 1:
        await update.message.reply_text("⚠️ Use /close inside a customer topic.")
        return
    row = await db_user_by_topic(topic_id)
    if not row:
        await update.message.reply_text("❌ No customer connected to this topic.")
        return
    try:
        try:
            await context.bot.send_message(
                chat_id=int(row["user_id"]),
                text=f"✅ Your {BRAND} support conversation is closed.\n\nNeed help again? Just send a message!",
            )
        except TelegramError:
            pass
        await context.bot.close_forum_topic(chat_id=SUPPORT_GROUP_ID, message_thread_id=topic_id)
        await db_status(int(row["user_id"]), "closed")
        await db_log_event(int(row["user_id"]), "topic_closed")
        await update.message.reply_text(
            "✅ Topic closed. Customer dobara message kare to yeh topic reopen hoga."
        )
    except TelegramError as error:
        logger.error("Topic close failed: %s", error)
        await update.message.reply_text(f"⚠️ Error closing topic: {error}")


# ============================================================
# SHARED TEXT HELPERS — used by both commands and dashboard buttons
# ============================================================

async def _stats_text() -> str:
    """Generate stats text. Shared by /stats command and dashboard button."""
    def q(cur):
        cur.execute(f"SELECT COUNT(*) AS c FROM {USERS_TABLE}")
        total = cur.fetchone()["c"]
        cur.execute(
            f"SELECT COUNT(*) AS c FROM {USERS_TABLE} "
            "WHERE created_at >= NOW() - INTERVAL '1 day'"
        )
        today = cur.fetchone()["c"]
        cur.execute(
            f"SELECT COUNT(*) AS c FROM {USERS_TABLE} "
            "WHERE topic_id IS NOT NULL AND topic_status = 'open'"
        )
        active = cur.fetchone()["c"]
        cur.execute(
            f"SELECT COUNT(*) AS c FROM {USERS_TABLE} WHERE is_suspicious = TRUE"
        )
        suspicious = cur.fetchone()["c"]

        result = {}
        for column in ("game", "source", "bonus"):
            cur.execute(
                f"SELECT {column}, COUNT(*) AS c FROM {USERS_TABLE} "
                f"WHERE {column} IS NOT NULL GROUP BY {column} ORDER BY c DESC"
            )
            result[column] = cur.fetchall()

        cur.execute(f"""
            SELECT campaign_token, COUNT(*) AS c
            FROM {EVENTS_TABLE}
            WHERE event_name = 'bot_start' AND campaign_token IS NOT NULL
            GROUP BY campaign_token
            ORDER BY c DESC
            LIMIT 10
        """)
        result["top_campaigns"] = cur.fetchall()

        cur.execute(f"""
            SELECT event_name, COUNT(DISTINCT user_id) AS c
            FROM {EVENTS_TABLE}
            WHERE created_at >= NOW() - INTERVAL '30 days'
            GROUP BY event_name
        """)
        result["funnel"] = {r["event_name"]: r["c"] for r in cur.fetchall()}

        return total, today, active, suspicious, result

    total, today, active, suspicious, data = await asyncio.to_thread(_run, q)

    def rows(data_list, column):
        return "\n".join(
            f"  {row[column]}: {row['c']} players" for row in data_list
        ) or "  No data yet."

    funnel = data["funnel"]
    starts = funnel.get("bot_start", 0)
    first_msgs = funnel.get("first_message", 0)
    conversion_pct = f"{(first_msgs / starts * 100):.1f}%" if starts else "—"

    campaigns_text = "\n".join(
        f"  {row['campaign_token']:<25} {row['c']} starts"
        for row in data["top_campaigns"]
    ) or "  No campaign data yet."

    return (
        f"📊 {BRAND.upper()} STATISTICS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Customers   : {total}\n"
        f"🆕 New Today         : {today}\n"
        f"💬 Open Topics       : {active}\n"
        f"⚠️ Suspicious Users  : {suspicious}\n\n"
        f"🎯 AD CAMPAIGN PERFORMANCE (top 10)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{campaigns_text}\n\n"
        f"📈 CONVERSION FUNNEL (last 30 days)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  bot_start       : {starts}\n"
        f"  bonus_selected  : {funnel.get('bonus_selected', 0)}\n"
        f"  game_selected   : {funnel.get('game_selected', 0)}\n"
        f"  first_message   : {first_msgs}\n"
        f"  nudge_sent      : {funnel.get('nudge_sent', 0)}\n"
        f"  nudge_acted     : {funnel.get('nudge_acted', 0)}\n"
        f"  ➡️ Start→Msg conversion: {conversion_pct}\n\n"
        f"🎮 GAME BREAKDOWN\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{rows(data['game'], 'game')}\n\n"
        f"📊 SOURCE BREAKDOWN\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{rows(data['source'], 'source')}\n\n"
        f"🎁 BONUS BREAKDOWN\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{rows(data['bonus'], 'bonus')}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )


async def _adstats_overview_text() -> str:
    """Generate adstats overview text. Shared by /adstats command and dashboard button."""
    def q(cur):
        cur.execute(f"""
            SELECT
                c.token, c.platform, c.campaign_name,
                c.expected_clicks, c.actual_clicks,
                COUNT(DISTINCT CASE WHEN e.event_name = 'bot_start' THEN e.user_id END) AS starts,
                COUNT(DISTINCT CASE WHEN e.event_name = 'bonus_selected' THEN e.user_id END) AS bonus,
                COUNT(DISTINCT CASE WHEN e.event_name = 'game_selected' THEN e.user_id END) AS game,
                COUNT(DISTINCT CASE WHEN e.event_name = 'first_message' THEN e.user_id END) AS msgs,
                COUNT(DISTINCT CASE WHEN e.event_name = 'nudge_sent' THEN e.user_id END) AS nudges
            FROM {CAMPAIGNS_TABLE} c
            LEFT JOIN {EVENTS_TABLE} e ON e.campaign_token = c.token
            GROUP BY c.token, c.platform, c.campaign_name,
                     c.expected_clicks, c.actual_clicks
            ORDER BY starts DESC
        """)
        return cur.fetchall()

    rows_data = await asyncio.to_thread(_run, q)
    if not rows_data:
        return "📭 No campaigns registered yet.\n\nUse /menu → 📣 Campaigns → ➕ Add New Campaign"

    lines = []
    for r in rows_data:
        actual = r["actual_clicks"] or 0
        expected = r["expected_clicks"] or 0
        starts = r["starts"]
        ctr = f"{(starts / actual * 100):.1f}%" if actual else "—"
        conv = f"{(r['msgs'] / starts * 100):.1f}%" if starts else "—"
        lines.append(
            f"🎯 `{r['token']}` ({r['platform']})\n"
            f"   {r['campaign_name']}\n"
            f"   Clicks: {actual}/{expected} exp | Starts: {starts} | CTR: {ctr}\n"
            f"   Bonus: {r['bonus']} | Game: {r['game']} | Msgs: {r['msgs']}\n"
            f"   ➡️ Start→Msg: {conv} | Nudges: {r['nudges']}"
        )
    return (
        f"📈 {BRAND.upper()} AD PERFORMANCE OVERVIEW\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n\n".join(lines) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 Use /adstats <token> for per-campaign detail."
    )


async def _campaigns_list_text() -> str:
    """Generate campaigns list text. Shared by /campaigns command and dashboard button."""
    all_campaigns = await db_list_campaigns(active_only=False)
    if not all_campaigns:
        return (
            "📭 No campaigns registered yet.\n\n"
            "Use /menu → 📣 Campaigns → ➕ Add New Campaign"
        )
    lines = []
    for c in all_campaigns:
        status = "🟢" if c["is_active"] else "🔴"
        lines.append(
            f"{status} `{c['token']}`\n"
            f"   Platform : {c['platform']}\n"
            f"   Name     : {c['campaign_name']}\n"
            f"   Creative : {c['creative'] or '—'}\n"
            f"   Target   : {c['target_channel'] or '—'}\n"
            f"   Clicks   : {c['actual_clicks'] or '—'} / {c['expected_clicks'] or '—'} expected\n"
            f"   Started  : {c['started_at'].strftime('%Y-%m-%d')}"
            + (f"\n   Ended    : {c['ended_at'].strftime('%Y-%m-%d')}" if c["ended_at"] else "")
        )
    return (
        f"📣 {BRAND.upper()} CAMPAIGN REGISTRY\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )


async def _staff_text() -> str:
    """Generate staff list text."""
    ids = "\n".join(f"  {item}" for item in sorted(AUTHORIZED_STAFF)) or "  (none)"
    return (
        f"👥 AUTHORIZED STAFF\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{ids}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
# STAFF COMMAND — /stats (with ad performance section)
# ============================================================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_staff(update):
        return
    text = await _stats_text()
    await update.message.reply_text(text)


# ============================================================
# STAFF COMMAND — /broadcast
# ============================================================

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_staff(update):
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text(
            "⚠️ Usage: /broadcast <message>\n"
            "Example: /broadcast Hi {name}, naya game added!"
        )
        return
    users = await db_all_users()
    if not users:
        await update.message.reply_text("❌ No customers in database yet.")
        return
    progress = await update.message.reply_text(
        f"📡 Broadcasting to {len(users)} customers..."
    )
    delivered = failed = 0
    for row in users:
        try:
            name = (row["name"] or "there").split(" ")[0]
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=f"📢 {BRAND} UPDATE\n\n{text.replace('{name}', name)}",
            )
            delivered += 1
            await db_log_event(
                row["user_id"], "broadcast_received",
                event_data={"text_preview": text[:80]},
            )
            await asyncio.sleep(0.05)
        except TelegramError:
            failed += 1
    try:
        await progress.edit_text(
            "📡 BROADCAST COMPLETE\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Delivered : {delivered}\n"
            f"❌ Failed    : {failed}\n"
            f"📨 Total     : {len(users)}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    except TelegramError:
        pass


# ============================================================
# STAFF COMMAND — /addstaff, /removestaff, /staff
# ============================================================

async def add_staff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_staff(update):
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("⚠️ Usage: /addstaff <telegram_id> [name]")
        return
    user_id, name = int(args[0]), " ".join(args[1:]) or "Staff"
    await db_add_staff(user_id, name)
    AUTHORIZED_STAFF.add(user_id)
    await update.message.reply_text(f"✅ {name} (ID: {user_id}) ab authorized staff hai.")


async def remove_staff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_staff(update):
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("⚠️ Usage: /removestaff <telegram_id>")
        return
    user_id = int(args[0])
    await db_remove_staff(user_id)
    AUTHORIZED_STAFF.discard(user_id)
    await update.message.reply_text(f"✅ ID {user_id} ko staff se remove kar diya.")


async def staff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_staff(update):
        return
    text = await _staff_text()
    await update.message.reply_text(text)


# ============================================================
# NEW STAFF COMMAND — /campaigns (campaign registry)
# ============================================================

async def campaigns_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manage ad campaigns.

    Usage:
      /campaigns                          — list all campaigns
      /campaigns add <token> <platform> <name> [creative] [target] [expected_clicks]
      /campaigns end <token>              — mark campaign ended
      /campaigns clicks <token> <count>   — update actual_clicks from Telegram Ads dashboard
    """
    if not await require_staff(update):
        return

    args = context.args or []
    if not args:
        # List all campaigns (use shared helper)
        text = await _campaigns_list_text()
        await update.message.reply_text(text)
        return

    action = args[0].lower()

    if action == "add":
        if len(args) < 4:
            await update.message.reply_text(
                "⚠️ Usage:\n"
                "/campaigns add <token> <platform> <name> [creative] [target_channel] [expected_clicks]\n\n"
                "Example:\n"
                "/campaigns add tgads_v1 telegram_ads 'September Test' "
                "'$5 free play' 'fish-game-channels' 500"
            )
            return
        token = args[1].lower().strip()
        platform = args[2]
        name = args[3]
        creative = args[4] if len(args) > 4 else None
        target = args[5] if len(args) > 5 else None
        expected = int(args[6]) if len(args) > 6 and args[6].isdigit() else None
        await db_add_campaign(token, platform, name, creative, target, expected)
        await update.message.reply_text(
            f"✅ Campaign registered!\n\n"
            f"🎯 Token: `{token}`\n"
            f"📣 Platform: {platform}\n"
            f"📛 Name: {name}\n\n"
            f"Ad destination URL:\n"
            f"https://t.me/{(await context.bot.get_me()).username}?start=src_{token}"
        )
        return

    if action == "end":
        if len(args) < 2:
            await update.message.reply_text("⚠️ Usage: /campaigns end <token>")
            return
        token = args[1].lower().strip()
        ok = await db_end_campaign(token)
        if ok:
            await update.message.reply_text(f"✅ Campaign `{token}` marked as ended.")
        else:
            await update.message.reply_text(f"❌ Campaign `{token}` not found.")

    if action == "clicks":
        if len(args) < 3 or not args[2].isdigit():
            await update.message.reply_text(
                "⚠️ Usage: /campaigns clicks <token> <count>\n"
                "Update actual_clicks from your Telegram Ads dashboard."
            )
            return
        token = args[1].lower().strip()
        clicks = int(args[2])
        ok = await db_update_campaign_clicks(token, clicks)
        if ok:
            await update.message.reply_text(
                f"✅ Updated `{token}` actual_clicks = {clicks}"
            )
        else:
            await update.message.reply_text(f"❌ Campaign `{token}` not found.")


# ============================================================
# NEW STAFF COMMAND — /adstats (detailed ad performance)
# ============================================================

async def adstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detailed per-campaign conversion funnel.

    Usage:
      /adstats                  — overview of all campaigns
      /adstats <token>          — detailed breakdown of one campaign
    """
    if not await require_staff(update):
        return

    args = context.args or []

    if not args:
        # Overview: all campaigns with funnel (use shared helper)
        text = await _adstats_overview_text()
        await update.message.reply_text(text)
        return

    # Detail view for one campaign
    token = args[0].lower().strip()
    campaign = await db_get_campaign(token)
    if not campaign:
        await update.message.reply_text(f"❌ Campaign `{token}` not found.")
        return

    def q(cur):
        # Funnel for this campaign
        cur.execute(f"""
            SELECT event_name, COUNT(DISTINCT user_id) AS c
            FROM {EVENTS_TABLE}
            WHERE campaign_token = %s
            GROUP BY event_name
        """, (token,))
        funnel = {r["event_name"]: r["c"] for r in cur.fetchall()}

        # Suspicious users from this campaign
        cur.execute(f"""
            SELECT COUNT(DISTINCT e.user_id) AS c
            FROM {EVENTS_TABLE} e
            JOIN {USERS_TABLE} u ON u.user_id = e.user_id
            WHERE e.campaign_token = %s AND u.is_suspicious = TRUE
        """, (token,))
        suspicious_count = cur.fetchone()["c"]

        # Last 5 users from this campaign
        cur.execute(f"""
            SELECT u.name, u.username, u.user_id, u.created_at, u.is_suspicious
            FROM {EVENTS_TABLE} e
            JOIN {USERS_TABLE} u ON u.user_id = e.user_id
            WHERE e.campaign_token = %s AND e.event_name = 'bot_start'
            ORDER BY e.created_at DESC
            LIMIT 5
        """, (token,))
        recent = cur.fetchall()

        return funnel, suspicious_count, recent

    funnel, suspicious_count, recent = await asyncio.to_thread(_run, q)

    actual = campaign["actual_clicks"] or 0
    starts = funnel.get("bot_start", 0)
    ctr = f"{(starts / actual * 100):.1f}%" if actual else "—"
    conv = f"{(funnel.get('first_message', 0) / starts * 100):.1f}%" if starts else "—"

    recent_lines = "\n".join(
        f"  {r['name']} (@{r['username'] or '—'}) "
        f"{'⚠️' if r['is_suspicious'] else '✅'} {r['created_at'].strftime('%m-%d %H:%M')}"
        for r in recent
    ) or "  (no users yet)"

    await update.message.reply_text(
        f"📈 CAMPAIGN DETAIL: `{token}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📛 Name     : {campaign['campaign_name']}\n"
        f"📣 Platform : {campaign['platform']}\n"
        f"🎨 Creative: {campaign['creative'] or '—'}\n"
        f"🎯 Target   : {campaign['target_channel'] or '—'}\n"
        f"📅 Started  : {campaign['started_at'].strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"📊 Clicks   : {actual} (expected {campaign['expected_clicks'] or '—'})\n\n"
        f"📈 FUNNEL\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  bot_start       : {starts}\n"
        f"  bonus_selected  : {funnel.get('bonus_selected', 0)}\n"
        f"  game_selected   : {funnel.get('game_selected', 0)}\n"
        f"  first_message   : {funnel.get('first_message', 0)}\n"
        f"  nudge_sent      : {funnel.get('nudge_sent', 0)}\n"
        f"  nudge_acted     : {funnel.get('nudge_acted', 0)}\n\n"
        f"📊 RATES\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  Click→Start    : {ctr}\n"
        f"  Start→Message  : {conv}\n"
        f"  ⚠️ Suspicious   : {suspicious_count} users\n\n"
        f"👥 LAST 5 USERS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{recent_lines}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
# STAFF DASHBOARD — Button-based interface (no commands needed)
# ============================================================

def dashboard_text() -> str:
    """Main dashboard header text."""
    return (
        f"🎛️ {BRAND.upper()} STAFF DASHBOARD\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Tap a button below 👇"
    )


def dashboard_keyboard() -> InlineKeyboardMarkup:
    """Main dashboard keyboard — 6 primary actions."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Bot Statistics", callback_data="menu:stats")],
        [InlineKeyboardButton("📈 Ad Performance", callback_data="menu:adstats")],
        [InlineKeyboardButton("📣 Campaigns", callback_data="menu:campaigns")],
        [InlineKeyboardButton("👥 Staff List", callback_data="menu:staff")],
        [InlineKeyboardButton("📢 Broadcast Hint", callback_data="menu:broadcast")],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="menu:refresh"),
            InlineKeyboardButton("❌ Close", callback_data="menu:close"),
        ],
    ])


def campaigns_menu_keyboard() -> InlineKeyboardMarkup:
    """Sub-menu for campaign actions."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 List All Campaigns", callback_data="menu:campaigns_list")],
        [InlineKeyboardButton("➕ Add New Campaign", callback_data="menu:campaigns_add")],
        [InlineKeyboardButton("🔴 End Campaign", callback_data="menu:campaigns_end")],
        [InlineKeyboardButton("🔢 Update Clicks", callback_data="menu:campaigns_clicks")],
        [InlineKeyboardButton("🔙 Back to Dashboard", callback_data="menu:back")],
    ])


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the staff dashboard with buttons."""
    if not await require_staff(update):
        return
    await update.message.reply_text(dashboard_text(), reply_markup=dashboard_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle dashboard button clicks. Edits the dashboard message in-place."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    # Security: only staff can use dashboard, only in support group
    if not query.from_user or query.from_user.id not in AUTHORIZED_STAFF:
        return
    if query.message.chat.id != SUPPORT_GROUP_ID:
        return

    action = query.data.replace("menu:", "", 1)

    try:
        if action == "stats":
            text = await _stats_text()
            await query.message.edit_text(
                text + "\n\n" + dashboard_text(),
                reply_markup=dashboard_keyboard()
            )

        elif action == "adstats":
            text = await _adstats_overview_text()
            await query.message.edit_text(
                text + "\n\n" + dashboard_text(),
                reply_markup=dashboard_keyboard()
            )

        elif action == "campaigns":
            await query.message.edit_text(
                "📣 CAMPAIGNS MENU\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Select an action:",
                reply_markup=campaigns_menu_keyboard()
            )

        elif action == "campaigns_list":
            text = await _campaigns_list_text()
            await query.message.edit_text(
                text + "\n\n" + dashboard_text(),
                reply_markup=dashboard_keyboard()
            )

        elif action == "campaigns_add":
            await query.message.edit_text(
                "➕ ADD NEW CAMPAIGN\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Copy this command, fill in your details, and send it in this group:\n\n"
                "<code>/campaigns add &lt;token&gt; &lt;platform&gt; \"&lt;name&gt;\" "
                "\"&lt;creative&gt;\" \"&lt;target&gt;\" &lt;expected_clicks&gt;</code>\n\n"
                "Example:\n"
                "<code>/campaigns add tgads_v1 telegram_ads \"September Test\" "
                "\"$5 free play\" \"fish-game-channels\" 500</code>\n\n"
                "After registering, the bot will reply with your ad destination URL.",
                reply_markup=dashboard_keyboard(),
                parse_mode="HTML"
            )

        elif action == "campaigns_end":
            await query.message.edit_text(
                "🔴 END CAMPAIGN\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Copy this command and send:\n\n"
                "<code>/campaigns end &lt;token&gt;</code>\n\n"
                "Example:\n"
                "<code>/campaigns end tgads_v1</code>",
                reply_markup=dashboard_keyboard(),
                parse_mode="HTML"
            )

        elif action == "campaigns_clicks":
            await query.message.edit_text(
                "🔢 UPDATE CAMPAIGN CLICKS\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Get the clicks count from your Telegram Ads dashboard, then send:\n\n"
                "<code>/campaigns clicks &lt;token&gt; &lt;count&gt;</code>\n\n"
                "Example:\n"
                "<code>/campaigns clicks tgads_v1 269</code>",
                reply_markup=dashboard_keyboard(),
                parse_mode="HTML"
            )

        elif action == "staff":
            text = await _staff_text()
            await query.message.edit_text(
                text + "\n\n" + dashboard_text(),
                reply_markup=dashboard_keyboard()
            )

        elif action == "broadcast":
            await query.message.edit_text(
                "📢 BROADCAST\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Copy this command and send:\n\n"
                "<code>/broadcast &lt;message&gt;</code>\n\n"
                "Use <code>{name}</code> to personalize:\n"
                "<code>/broadcast Hi {name}, new game added! 🎮</code>",
                reply_markup=dashboard_keyboard(),
                parse_mode="HTML"
            )

        elif action == "refresh":
            await query.message.edit_text(dashboard_text(), reply_markup=dashboard_keyboard())

        elif action == "back":
            await query.message.edit_text(dashboard_text(), reply_markup=dashboard_keyboard())

        elif action == "close":
            try:
                await query.message.delete()
            except TelegramError:
                # If delete fails (e.g., no permission), edit to a minimal message
                try:
                    await query.message.edit_text(" Dashboard closed. Type /menu to reopen.")
                except TelegramError:
                    pass

    except TelegramError as e:
        # Edit can fail if message content is identical (e.g., clicking Refresh twice)
        logger.warning("Menu edit failed: %s", e)


# ============================================================
# ERROR HANDLER + POST-INIT + MAIN
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error: %s", context.error, exc_info=True)


async def post_init(application: Application) -> None:
    # Public command menu (visible to all users in private chat)
    public = [
        BotCommand("start", "Start or restart your support chat"),
        BotCommand("help", "Show help and available commands"),
        BotCommand("games", "Choose or change your game"),
        BotCommand("support", "Reach the support team"),
    ]
    await application.bot.set_my_commands(public)

    # Staff-only command menu (visible only inside support group)
    group_commands = public + [
        BotCommand("menu", "Open staff dashboard (buttons)"),
        BotCommand("id", "Show customer info for this topic"),
        BotCommand("close", "Close this customer topic"),
        BotCommand("stats", "Show bot statistics"),
        BotCommand("adstats", "Show ad campaign performance"),
        BotCommand("campaigns", "Manage ad campaigns"),
        BotCommand("broadcast", "Send a customer broadcast"),
        BotCommand("addstaff", "Authorize a staff member"),
        BotCommand("removestaff", "Remove a staff member"),
        BotCommand("staff", "List authorized staff"),
    ]
    try:
        await application.bot.set_my_commands(
            group_commands, scope=BotCommandScopeChat(chat_id=SUPPORT_GROUP_ID)
        )
    except TelegramError as error:
        logger.warning("Could not set group command menu: %s", error)

    # Optional health endpoint (for Railway / external uptime monitoring)
    if HEALTH_PORT:
        try:
            from aiohttp import web

            async def health_handler(request):
                return web.json_response({
                    "status": "ok",
                    "brand": BRAND,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

            health_app = web.Application()
            health_app.router.add_get("/health", health_handler)
            health_app.router.add_get("/", health_handler)
            runner = web.AppRunner(health_app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
            await site.start()
            logger.info("Health endpoint listening on port %s", HEALTH_PORT)
        except Exception as e:
            logger.warning("Could not start health endpoint: %s", e)

    logger.info("Bot commands registered. %s ready.", BRAND)


def main() -> None:
    print("=" * 50)
    print(f"   {BRAND.upper()} SUPPORT BOT — Ads-Optimized")
    print("=" * 50)

    if not TOKEN:
        print("❌ BOT_TOKEN not set!")
        return
    if not SUPPORT_GROUP_ID:
        print("❌ SUPPORT_GROUP_ID not set!")
        return

    init_db()
    load_staff()

    print(f"Support Group   : {SUPPORT_GROUP_ID}")
    print(f"Authorized Staff: {sorted(AUTHORIZED_STAFF)}")
    print(f"Games           : {GAMES}")
    print(f"Nudge Enabled   : {NUDGE_ENABLED} (delay: {NUDGE_DELAY_SECONDS}s)")
    print(f"Health Port     : {HEALTH_PORT or 'disabled'}")
    print("=" * 50)

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # Customer commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("games", games))
    application.add_handler(CommandHandler("support", support))

    # Callback handlers (inline button clicks)
    application.add_handler(CallbackQueryHandler(bonus_selected, pattern=r"^bonus:"))
    application.add_handler(CallbackQueryHandler(game_selected, pattern=r"^game:"))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))

    # Setup helper
    application.add_handler(CommandHandler("groupid", group_id_command))

    # Staff commands (group-only)
    group_filter = filters.Chat(chat_id=SUPPORT_GROUP_ID)
    application.add_handler(CommandHandler("menu", menu_command, filters=group_filter))
    application.add_handler(CommandHandler("id", customer_info, filters=group_filter))
    application.add_handler(CommandHandler("close", close_topic, filters=group_filter))
    application.add_handler(CommandHandler("stats", stats, filters=group_filter))
    application.add_handler(CommandHandler("adstats", adstats_command, filters=group_filter))
    application.add_handler(CommandHandler("campaigns", campaigns_command, filters=group_filter))
    application.add_handler(CommandHandler("broadcast", broadcast, filters=group_filter))
    application.add_handler(CommandHandler("addstaff", add_staff, filters=group_filter))
    application.add_handler(CommandHandler("removestaff", remove_staff, filters=group_filter))
    application.add_handler(CommandHandler("staff", staff, filters=group_filter))

    # Message handlers
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, customer_message)
    )
    application.add_handler(
        MessageHandler(group_filter & ~filters.COMMAND, support_group_message)
    )

    application.add_error_handler(error_handler)

    print("Bot is running...")
    print("=" * 50)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
