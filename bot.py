import os
import logging
import asyncio

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

# This template is copied into each branded project before deployment.
BRAND = "Winning Wave"
OFFICIAL_CHANNEL = "@winningwaveofficial"
USERS_TABLE = "winning_wave_users"
TOPICS_TABLE = "winning_wave_topics"
STAFF_TABLE = "winning_wave_staff"

TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPPORT_GROUP_ID = int(os.getenv("SUPPORT_GROUP_ID", "0") or 0)
_staff = os.getenv("AUTHORIZED_STAFF_IDS", "")
AUTHORIZED_STAFF = {
    int(value.strip()) for value in _staff.split(",")
    if value.strip().lstrip("-").isdigit()
}

GAMES = [
    "Orionstars", "Firekirin", "Ultrapanda", "Juwa", "GameVault",
    "Riversweeps", "Milkyway", "Vblink", "Gameroom",
]
TRAFFIC_SOURCES = {"website": "🌐 Website", "facebook": "📘 Facebook"}
BONUS_OPTIONS = {
    "signup120": "💰 120% Signup Bonus",
    "freeplay": "🎁 Redeemable Freeplay",
}

MIN_GAP_SECONDS = 0.4
MAX_QUEUE_SIZE = 30
WORKER_IDLE_TIMEOUT = 5
_pool: pg_pool.ThreadedConnectionPool | None = None
_user_queues: dict[int, asyncio.Queue] = {}
_user_workers: dict[int, asyncio.Task] = {}
_background_tasks: set[asyncio.Task] = set()
_topic_locks: dict[int, asyncio.Lock] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Prevent request URLs (which contain the Telegram bot token) from appearing
# in normal Railway logs. Keep the application logger at INFO for diagnostics.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ---------------------- database ----------------------
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
    connection_pool = _pool_connection()
    connection = connection_pool.getconn()
    try:
        with connection.cursor() as cursor:
            result = query_function(cursor, *args)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection_pool.putconn(connection)


def init_db() -> None:
    def query(cursor):
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {USERS_TABLE} (
                user_id BIGINT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                topic_id BIGINT,
                topic_status TEXT NOT NULL DEFAULT 'open',
                game TEXT,
                source TEXT,
                bonus TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {TOPICS_TABLE} (
                topic_id BIGINT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES {USERS_TABLE}(user_id)
                    ON DELETE CASCADE
            )
        """)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {STAFF_TABLE} (
                user_id BIGINT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cursor.execute(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS source TEXT")
        cursor.execute(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS bonus TEXT")
        cursor.execute(
            f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS "
            "topic_status TEXT NOT NULL DEFAULT 'open'"
        )
    _run(query)


def load_staff() -> None:
    def query(cursor):
        cursor.execute(f"SELECT user_id FROM {STAFF_TABLE}")
        return {row["user_id"] for row in cursor.fetchall()}
    AUTHORIZED_STAFF.update(_run(query))


async def db_user(user_id: int):
    def query(cursor):
        cursor.execute(f"SELECT * FROM {USERS_TABLE} WHERE user_id = %s", (user_id,))
        return cursor.fetchone()
    return await asyncio.to_thread(_run, query)


async def db_user_by_topic(topic_id: int):
    def query(cursor):
        cursor.execute(f"SELECT * FROM {USERS_TABLE} WHERE topic_id = %s", (topic_id,))
        return cursor.fetchone()
    return await asyncio.to_thread(_run, query)


async def db_upsert(
    user_id: int, name: str, username: str, topic_id: int | None = None,
    game: str | None = None, status: str | None = None,
    source: str | None = None, bonus: str | None = None,
) -> None:
    def query(cursor):
        cursor.execute(f"""
            INSERT INTO {USERS_TABLE}
                (user_id, name, username, topic_id, game, topic_status, source, bonus)
            VALUES (%s, %s, %s, %s, %s, COALESCE(%s, 'open'), %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                name = EXCLUDED.name,
                username = EXCLUDED.username,
                topic_id = COALESCE(EXCLUDED.topic_id, {USERS_TABLE}.topic_id),
                game = COALESCE(EXCLUDED.game, {USERS_TABLE}.game),
                topic_status = COALESCE(%s, {USERS_TABLE}.topic_status),
                source = COALESCE(EXCLUDED.source, {USERS_TABLE}.source),
                bonus = COALESCE(EXCLUDED.bonus, {USERS_TABLE}.bonus),
                last_seen = NOW()
        """, (user_id, name, username, topic_id, game, status, source, bonus, status))
    await asyncio.to_thread(_run, query)


async def db_topic(topic_id: int, user_id: int) -> None:
    def query(cursor):
        cursor.execute(f"""
            INSERT INTO {TOPICS_TABLE} (topic_id, user_id) VALUES (%s, %s)
            ON CONFLICT(topic_id) DO UPDATE SET user_id = EXCLUDED.user_id
        """, (topic_id, user_id))
    await asyncio.to_thread(_run, query)


async def db_status(user_id: int, status: str) -> None:
    def query(cursor):
        cursor.execute(
            f"UPDATE {USERS_TABLE} SET topic_status = %s WHERE user_id = %s",
            (status, user_id),
        )
    await asyncio.to_thread(_run, query)


async def db_clear_topic(user_id: int, topic_id: int) -> None:
    def query(cursor):
        cursor.execute(
            f"UPDATE {USERS_TABLE} SET topic_id = NULL, topic_status = 'open' "
            "WHERE user_id = %s", (user_id,)
        )
        cursor.execute(f"DELETE FROM {TOPICS_TABLE} WHERE topic_id = %s", (topic_id,))
    await asyncio.to_thread(_run, query)


async def db_all_users():
    def query(cursor):
        cursor.execute(f"SELECT * FROM {USERS_TABLE}")
        return cursor.fetchall()
    return await asyncio.to_thread(_run, query)


async def db_add_staff(user_id: int, name: str) -> None:
    def query(cursor):
        cursor.execute(f"""
            INSERT INTO {STAFF_TABLE} (user_id, name) VALUES (%s, %s)
            ON CONFLICT(user_id) DO UPDATE SET name = EXCLUDED.name
        """, (user_id, name))
    await asyncio.to_thread(_run, query)


async def db_remove_staff(user_id: int) -> None:
    def query(cursor):
        cursor.execute(f"DELETE FROM {STAFF_TABLE} WHERE user_id = %s", (user_id,))
    await asyncio.to_thread(_run, query)


# ---------------------- shared helpers ----------------------
def fire_and_forget(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def is_staff(message) -> bool:
    return bool(message.from_user and message.from_user.id in AUTHORIZED_STAFF)


def user_label(user) -> str:
    name = (user.full_name or user.first_name or "Unknown Player").replace("\n", " ").strip()
    return f"{name} (@{user.username})" if user.username else f"{name} [ID: {user.id}]"


def parse_game(args: list[str]) -> str | None:
    if not args:
        return None
    value = args[0].strip().lower()
    return next((game for game in GAMES if game.lower() == value), None)


def parse_source(args: list[str]) -> str | None:
    if not args:
        return None
    value = args[0].strip().lower()
    if value.startswith("src_"):
        value = value[4:]
    return TRAFFIC_SOURCES.get(value)


def closed_topic(error: TelegramError) -> bool:
    text = str(error).lower()
    return any(item in text for item in (
        "topic_closed", "thread_not_found", "message thread not found",
    ))


def game_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for index in range(0, len(GAMES), 2):
        row = [InlineKeyboardButton(
            f"🎮 {GAMES[index]}", callback_data=f"game:{GAMES[index]}"
        )]
        if index + 1 < len(GAMES):
            row.append(InlineKeyboardButton(
                f"🎮 {GAMES[index + 1]}", callback_data=f"game:{GAMES[index + 1]}"
            ))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def bonus_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BONUS_OPTIONS["signup120"], callback_data="bonus:signup120")],
        [InlineKeyboardButton(BONUS_OPTIONS["freeplay"], callback_data="bonus:freeplay")],
    ])


# ---------------------- forum topics ----------------------
def topic_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _topic_locks:
        _topic_locks[user_id] = asyncio.Lock()
    return _topic_locks[user_id]


async def create_topic(user, context, game=None, source=None) -> int | None:
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
        )
        await db_topic(topic_id, user.id)
        game_line = f"🎮 Game     : {selected_game}" if selected_game else "🎮 Game     : Not selected yet"
        source_line = f"📊 Source   : {saved_source}" if saved_source else "📊 Source   : Direct/Unknown"
        entry_line = "🔗 Entry    : Deep-link" if game or source else "🔗 Entry    : Direct"
        await context.bot.send_message(
            chat_id=SUPPORT_GROUP_ID, message_thread_id=topic_id,
            text=(
                f"👤 NEW {BRAND.upper()} PLAYER CONNECTED\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Name     : {user.full_name or user.first_name or 'Unknown'}\n"
                f"Username : @{user.username or 'No username'}\n"
                f"ID       : {user.id}\n{game_line}\n{source_line}\n{entry_line}\n\n"
                "💬 Customer messages below.\n━━━━━━━━━━━━━━━━━━━━━━━━"
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


async def get_topic(user, context, game=None, source=None) -> int | None:
    lock = topic_lock(user.id)
    try:
        async with lock:
            row = await db_user(user.id)
            if row and row["topic_id"]:
                topic_id = (
                    await reopen_or_recreate(user, row["topic_id"], context)
                    if row.get("topic_status") == "closed" else row["topic_id"]
                )
                if topic_id and (game or source):
                    await db_upsert(
                        user.id, user.full_name or user.first_name or "Unknown",
                        user.username or "", game=game, source=source,
                    )
                return topic_id
            return await create_topic(user, context, game, source)
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


# Queue cleanup contains no await between the final empty check and removal,
# preventing an idle-boundary message from becoming stranded.
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
                    await update.message.reply_text("⚠️ Could not send your message. Please try again.")
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


# ---------------------- customer actions ----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    user = update.effective_user
    game, source = parse_game(context.args or []), parse_source(context.args or [])
    if game:
        await update.message.reply_text(
            f"👋 Welcome to {BRAND} Support!\n\n🎮 Game selected: {game}\n\n"
            "💬 Send your message — our team will assist you right away!\n\n"
            f"📢 Official updates: {OFFICIAL_CHANNEL}"
        )
    else:
        await update.message.reply_text(
            f"👋 Welcome to {BRAND} Support!\n\n🎁 Choose your bonus:\n\n"
            f"📢 Official updates: {OFFICIAL_CHANNEL}",
            reply_markup=bonus_keyboard(),
        )
    fire_and_forget(finalize_start(user, context, game, source))


async def finalize_start(user, context, game, source) -> None:
    try:
        old = await db_user(user.id)
        new_user = not (old and old.get("topic_id"))
        topic_id = await get_topic(user, context, game, source)
        if topic_id and not new_user:
            details = [
                f"👤 Name     : {user.full_name or user.first_name or 'Unknown'}",
                f"🔗 Username : @{user.username or 'No username'}", f"🆔 ID       : {user.id}",
            ]
            if game:
                details.append(f"🎮 Game     : {game}")
            if source:
                details.append(f"📊 Source   : {source}")
            await group_message(
                user, context, topic_id,
                "🔁 CUSTOMER STARTED BOT AGAIN\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
                + "\n".join(details) + "\n━━━━━━━━━━━━━━━━━━━━━━━━",
            )
    except Exception:
        logger.exception("/start finalization failed for %s", user.id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            f"ℹ️ {BRAND.upper()} SUPPORT — HELP\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "/start       — Begin or restart your support chat\n"
            "/games       — Choose or change your game\n"
            "/support — Reach our support team\n"
            "/help    — Show this message\n\n"
            f"💬 You can also send a message anytime — our team will reply here.\n\n"
            f"📢 Official updates: {OFFICIAL_CHANNEL}"
        )


async def bonus_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    bonus = BONUS_OPTIONS.get(query.data.replace("bonus:", "", 1))
    if not bonus:
        await query.message.reply_text("⚠️ Invalid bonus selection.")
        return
    await query.message.reply_text(
        f"✅ {bonus} selected!\n\n🎮 Now choose your game:", reply_markup=game_keyboard()
    )
    fire_and_forget(finalize_bonus(query.from_user, context, bonus))


async def finalize_bonus(user, context, bonus: str) -> None:
    try:
        topic_id = await get_topic(user, context)
        if topic_id:
            await db_upsert(
                user.id, user.full_name or user.first_name or "Unknown", user.username or "",
                topic_id=topic_id, bonus=bonus,
            )
            await group_message(user, context, topic_id, f"🎁 BONUS SELECTED: {bonus}")
    except Exception:
        logger.exception("Bonus finalization failed for %s", user.id)


async def games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_user:
        await update.message.reply_text("🎮 Choose your game:", reply_markup=game_keyboard())
        fire_and_forget(command_touch(update.effective_user, context, "games"))


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_user:
        await update.message.reply_text("💬 Select your game (optional):", reply_markup=game_keyboard())
        fire_and_forget(command_touch(update.effective_user, context, "support"))


async def command_touch(user, context, command: str) -> None:
    try:
        old = await db_user(user.id)
        new_user = not (old and old.get("topic_id"))
        topic_id = await get_topic(user, context)
        if topic_id and not new_user:
            await group_message(
                user, context, topic_id,
                f"🔁 CUSTOMER USED /{command}\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Name     : {user.full_name or user.first_name or 'Unknown'}\n"
                f"🔗 Username : @{user.username or 'No username'}\n🆔 ID       : {user.id}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━",
            )
    except Exception:
        logger.exception("/%s finalization failed for %s", command, user.id)


async def game_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    game = query.data.replace("game:", "", 1)
    if game not in GAMES:
        await query.message.reply_text("⚠️ Invalid game selection.")
        return
    await query.message.reply_text(
        f"✅ {game} selected!\n\n💬 Send your message — team will reply shortly!"
    )
    fire_and_forget(finalize_game(query.from_user, context, game))


async def finalize_game(user, context, game: str) -> None:
    try:
        topic_id = await get_topic(user, context)
        if topic_id:
            await db_upsert(
                user.id, user.full_name or user.first_name or "Unknown", user.username or "",
                topic_id=topic_id, game=game,
            )
            await db_topic(topic_id, user.id)
            await group_message(user, context, topic_id, f"🎮 GAME SELECTED: {game}")
    except Exception:
        logger.exception("Game finalization failed for %s", user.id)


async def customer_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    user = update.effective_user
    try:
        topic_id = await get_topic(user, context)
    except Exception:
        logger.exception("Could not prepare support topic for customer %s", user.id)
        topic_id = None
    if not topic_id:
        await update.message.reply_text("⚠️ Support system temporarily unavailable. Please try again.")
        return
    await enqueue(user, update, context, topic_id)


# ---------------------- support group / staff ----------------------
async def support_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    message = update.message
    if message.chat.id != SUPPORT_GROUP_ID or (message.from_user and message.from_user.is_bot):
        return
    # Security: group members who are not staff cannot send messages to a customer.
    if not is_staff(message):
        logger.warning("Ignoring non-staff support-group message from %s", message.from_user)
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
    except TelegramError as error:
        logger.error("Staff reply delivery failed: %s", error)
        error_text = str(error).lower()
        if any(value in error_text for value in ("blocked", "deactivated", "chat not found", "forbidden")):
            warning = "⚠️ DELIVERY FAILED\n\nCustomer ne bot block kar diya hai ya account deactivate hai."
        else:
            warning = f"⚠️ Reply delivery error: {error}"
        try:
            await context.bot.send_message(
                chat_id=SUPPORT_GROUP_ID, message_thread_id=topic_id, text=warning
            )
        except TelegramError:
            pass


async def require_staff(update: Update) -> bool:
    if not update.message or update.message.chat.id != SUPPORT_GROUP_ID:
        return False
    if not is_staff(update.message):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return False
    return True


async def group_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Staff-only setup helper; works even before SUPPORT_GROUP_ID is correct."""
    if not update.message:
        return
    message = update.message
    if message.chat.id > 0:
        await message.reply_text("⚠️ Use /groupid inside the Winning Wave support group.")
        return
    if not is_staff(message):
        await message.reply_text("⛔ You are not authorized to use this command.")
        return
    await message.reply_text(f"✅ This group's exact Bot API ID:\n{message.chat.id}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_staff(update):
        return
    def query(cursor):
        cursor.execute(f"SELECT COUNT(*) AS c FROM {USERS_TABLE}")
        total = cursor.fetchone()["c"]
        cursor.execute(f"SELECT COUNT(*) AS c FROM {USERS_TABLE} WHERE created_at >= NOW() - INTERVAL '1 day'")
        today = cursor.fetchone()["c"]
        cursor.execute(f"SELECT COUNT(*) AS c FROM {USERS_TABLE} WHERE topic_id IS NOT NULL AND topic_status = 'open'")
        active = cursor.fetchone()["c"]
        result = []
        for column in ("game", "source", "bonus"):
            cursor.execute(f"SELECT {column}, COUNT(*) AS c FROM {USERS_TABLE} WHERE {column} IS NOT NULL GROUP BY {column} ORDER BY c DESC")
            result.append(cursor.fetchall())
        return total, today, active, *result
    total, today, active, games_data, sources_data, bonuses_data = await asyncio.to_thread(_run, query)
    def rows(data, column):
        return "\n".join(f"  {row[column]}: {row['c']} players" for row in data) or "  No data yet."
    await update.message.reply_text(
        f"📊 {BRAND.upper()} STATISTICS\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Customers : {total}\n🆕 New Today       : {today}\n💬 Open Topics     : {active}\n\n"
        f"🎮 GAME BREAKDOWN\n━━━━━━━━━━━━━━━━━━━━━━━━\n{rows(games_data, 'game')}\n\n"
        f"📊 SOURCE BREAKDOWN\n━━━━━━━━━━━━━━━━━━━━━━━━\n{rows(sources_data, 'source')}\n\n"
        f"🎁 BONUS BREAKDOWN\n━━━━━━━━━━━━━━━━━━━━━━━━\n{rows(bonuses_data, 'bonus')}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_staff(update):
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text("⚠️ Usage: /broadcast <message>\nExample: /broadcast Hi {name}, naya game added!")
        return
    users = await db_all_users()
    if not users:
        await update.message.reply_text("❌ No customers in database yet.")
        return
    progress = await update.message.reply_text(f"📡 Broadcasting to {len(users)} customers...")
    delivered = failed = 0
    for row in users:
        try:
            name = (row["name"] or "there").split(" ")[0]
            await context.bot.send_message(
                chat_id=row["user_id"], text=f"📢 {BRAND} UPDATE\n\n{text.replace('{name}', name)}"
            )
            delivered += 1
            await asyncio.sleep(0.05)
        except TelegramError:
            failed += 1
    try:
        await progress.edit_text(
            "📡 BROADCAST COMPLETE\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Delivered : {delivered}\n❌ Failed    : {failed}\n📨 Total     : {len(users)}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    except TelegramError:
        pass


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
        "📋 CUSTOMER INFORMATION\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name     : {row['name']}\n🔗 Username : @{row['username'] or 'No username'}\n"
        f"🆔 ID       : {row['user_id']}\n🎮 Game     : {row['game'] or 'Not selected'}\n"
        f"🎁 Bonus    : {row['bonus'] or 'Not selected'}\n📊 Source   : {row['source'] or 'Direct/Unknown'}\n"
        f"📌 Topic ID : {topic_id}\n📅 Joined   : {row['created_at']}\n━━━━━━━━━━━━━━━━━━━━━━━━"
    )


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
        await update.message.reply_text("✅ Topic closed. Customer dobara message kare to yeh topic reopen hoga.")
    except TelegramError as error:
        logger.error("Topic close failed: %s", error)
        await update.message.reply_text(f"⚠️ Error closing topic: {error}")


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
    if await require_staff(update):
        ids = "\n".join(f"  {item}" for item in sorted(AUTHORIZED_STAFF)) or "  (none)"
        await update.message.reply_text(f"👥 AUTHORIZED STAFF\n━━━━━━━━━━━━━━━━━━━━━━━━\n{ids}\n━━━━━━━━━━━━━━━━━━━━━━━━")


# ---------------------- setup ----------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error: %s", context.error, exc_info=True)


async def post_init(application: Application) -> None:
    public = [
        BotCommand("start", "Start or restart your support chat"),
        BotCommand("help", "Show help and available commands"),
        BotCommand("games", "Choose or change your game"),
        BotCommand("support", "Reach the support team"),
    ]
    await application.bot.set_my_commands(public)
    group_commands = public + [
        BotCommand("id", "Show the customer in this topic"),
        BotCommand("close", "Close this customer topic"),
        BotCommand("stats", "Show bot statistics"),
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


def main() -> None:
    print("=" * 46)
    print(f"       {BRAND.upper()} SUPPORT BOT")
    print("=" * 46)
    if not TOKEN:
        print("❌ BOT_TOKEN not set!")
        return
    if not SUPPORT_GROUP_ID:
        print("❌ SUPPORT_GROUP_ID not set!")
        return
    init_db()
    load_staff()
    application = Application.builder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("games", games))
    application.add_handler(CommandHandler("support", support))
    application.add_handler(CallbackQueryHandler(bonus_selected, pattern=r"^bonus:"))
    application.add_handler(CallbackQueryHandler(game_selected, pattern=r"^game:"))
    # Setup helper: this is intentionally not in the public command menu.
    application.add_handler(CommandHandler("groupid", group_id_command))
    group = filters.Chat(chat_id=SUPPORT_GROUP_ID)
    application.add_handler(CommandHandler("id", customer_info, filters=group))
    application.add_handler(CommandHandler("close", close_topic, filters=group))
    application.add_handler(CommandHandler("stats", stats, filters=group))
    application.add_handler(CommandHandler("broadcast", broadcast, filters=group))
    application.add_handler(CommandHandler("addstaff", add_staff, filters=group))
    application.add_handler(CommandHandler("removestaff", remove_staff, filters=group))
    application.add_handler(CommandHandler("staff", staff, filters=group))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, customer_message))
    application.add_handler(MessageHandler(group & ~filters.COMMAND, support_group_message))
    application.add_error_handler(error_handler)
    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
