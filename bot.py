import asyncio
import base64
import io
import json
import logging
import os
import sqlite3
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib import error, parse, request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("vpn_bot")


@dataclass
class Config:
    bot_token: str
    admin_ids: set[int]
    trial_days: int
    month_seconds: int
    price_stars_month: int
    vpn_backend_url: str
    vpn_backend_token: str
    support_contact: str
    terms_url: str


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is required")

    raw_admins = os.getenv("ADMIN_IDS", "").strip()
    admin_ids: set[int] = set()
    if raw_admins:
        for item in raw_admins.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                admin_ids.add(int(item))
            except ValueError:
                logger.warning("Invalid ADMIN_IDS value ignored: %s", item)

    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        trial_days=int(os.getenv("TRIAL_DAYS", "7")),
        month_seconds=int(os.getenv("MONTH_SECONDS", "2592000")),
        price_stars_month=int(os.getenv("PRICE_STARS_MONTH", "99")),
        vpn_backend_url=os.getenv("VPN_BACKEND_URL", "").strip().rstrip("/"),
        vpn_backend_token=os.getenv("VPN_BACKEND_TOKEN", "").strip(),
        support_contact=os.getenv("SUPPORT_CONTACT", "@support"),
        terms_url=os.getenv("TERMS_URL", "https://example.com/terms"),
    )


CFG = load_config()
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")
BUY_RATE_LIMIT_SECONDS = 10


MAIN_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("Получить VPN", callback_data="vpn")],
        [InlineKeyboardButton("Статус", callback_data="status")],
        [InlineKeyboardButton("Продлить подписку", callback_data="buy")],
        [InlineKeyboardButton("Поддержка", callback_data="support")],
        [InlineKeyboardButton("Условия", callback_data="terms")],
    ]
)

ADMIN_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("Список пользователей", callback_data="admin:stats")],
        [InlineKeyboardButton("Выдать доступ вручную", callback_data="admin:grant_help")],
        [InlineKeyboardButton("Заблокировать/разблокировать", callback_data="admin:block_help")],
        [InlineKeyboardButton("Рассылка", callback_data="admin:broadcast_help")],
    ]
)


# --------------------------- DB helpers ---------------------------
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                created_at INTEGER NOT NULL,
                trial_start INTEGER,
                trial_end INTEGER,
                access_until INTEGER,
                is_blocked INTEGER DEFAULT 0,
                last_seen INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                charge_id TEXT UNIQUE,
                amount_stars INTEGER,
                created_at INTEGER,
                raw_json TEXT
            )
            """
        )
        conn.commit()


def now_ts() -> int:
    return int(time.time())


def fmt_dt(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def format_remaining(seconds: int) -> str:
    if seconds <= 0:
        return "0ч"
    days, rem = divmod(seconds, 86400)
    hours, _ = divmod(rem, 3600)
    if days > 0:
        return f"{days}д {hours}ч"
    return f"{hours}ч"


def ensure_user(user_id: int) -> sqlite3.Row:
    ts = now_ts()
    with db_connect() as conn:
        cur = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row:
            conn.execute("UPDATE users SET last_seen = ? WHERE user_id = ?", (ts, user_id))
            conn.commit()
            return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

        trial_end = ts + CFG.trial_days * 86400
        conn.execute(
            """
            INSERT INTO users (user_id, created_at, trial_start, trial_end, access_until, is_blocked, last_seen)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (user_id, ts, ts, trial_end, trial_end, ts),
        )
        conn.commit()
        logger.info("Created new user with trial: user_id=%s until=%s", user_id, trial_end)
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def is_access_active(user_row: sqlite3.Row) -> bool:
    return bool(user_row) and user_row["is_blocked"] == 0 and now_ts() < int(user_row["access_until"] or 0)


def access_status_text(user_row: sqlite3.Row) -> str:
    if user_row["is_blocked"]:
        return "🚫 Доступ заблокирован администратором."

    access_until = int(user_row["access_until"] or 0)
    left = access_until - now_ts()
    if left > 0:
        return (
            "✅ Доступ активен\n"
            f"До: <b>{fmt_dt(access_until)}</b>\n"
            f"Осталось: <b>{format_remaining(left)}</b>"
        )
    return "⛔️ Доступ закончился. Нажмите «Продлить подписку», чтобы продолжить пользоваться VPN."


def record_payment_if_new(
    user_id: int, charge_id: str, amount_stars: int, raw_payload: dict[str, Any]
) -> bool:
    ts = now_ts()
    with db_connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO payments (user_id, charge_id, amount_stars, created_at, raw_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, charge_id, amount_stars, ts, json.dumps(raw_payload, ensure_ascii=False)),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            logger.warning("Duplicate charge ignored: %s", charge_id)
            return False


def extend_access(user_id: int, add_seconds: int) -> int:
    ts = now_ts()
    with db_connect() as conn:
        row = conn.execute("SELECT access_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
        current_until = int(row["access_until"] or 0) if row else 0
        new_until = max(ts, current_until) + add_seconds
        conn.execute("UPDATE users SET access_until = ?, last_seen = ? WHERE user_id = ?", (new_until, ts, user_id))
        conn.commit()
    return new_until


def set_blocked(user_id: int, blocked: bool) -> bool:
    with db_connect() as conn:
        cur = conn.execute(
            "UPDATE users SET is_blocked = ? WHERE user_id = ?",
            (1 if blocked else 0, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_stats() -> dict[str, int]:
    ts = now_ts()
    with db_connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_blocked = 0 AND access_until > ?", (ts,)
        ).fetchone()[0]
        expired = total - active
    return {"total": total, "active": active, "expired": expired}


def list_user_ids() -> list[int]:
    with db_connect() as conn:
        rows = conn.execute("SELECT user_id FROM users WHERE is_blocked = 0").fetchall()
    return [int(r[0]) for r in rows]


# --------------------------- VPN adapter ---------------------------

def _http_get_json(url: str, headers: dict[str, str], timeout: float = 10.0) -> dict[str, Any]:
    req = request.Request(url=url, headers=headers, method="GET")
    with request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        content_type = resp.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise ValueError(f"Unexpected content-type: {content_type}")
        return json.loads(body)


async def fetch_vpn_profile(user_id: int) -> dict[str, Any]:
    if not CFG.vpn_backend_url:
        return {
            "type": "stub",
            "config_text": "VPN backend не настроен. Обратитесь в поддержку.",
            "hint": "Укажите VPN_BACKEND_URL и VPN_BACKEND_TOKEN в ENV.",
        }

    url = f"{CFG.vpn_backend_url}/profile?{parse.urlencode({'user_id': user_id})}"
    headers = {"Authorization": f"Bearer {CFG.vpn_backend_token}"} if CFG.vpn_backend_token else {}

    try:
        data = await asyncio.to_thread(_http_get_json, url, headers)
        if not isinstance(data, dict):
            raise ValueError("Backend response is not an object")
        return data
    except (error.URLError, error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logger.error("VPN backend request failed for user=%s: %s", user_id, exc)
        return {
            "type": "stub",
            "config_text": "VPN backend временно недоступен. Попробуйте позже.",
            "hint": "Если проблема повторяется — обратитесь в поддержку.",
        }


# --------------------------- Bot text/UI ---------------------------

def is_admin(user_id: int) -> bool:
    return user_id in CFG.admin_ids


def invoice_payload(user_id: int) -> str:
    return f"vpn_month:{user_id}:{now_ts()}"


async def send_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=MAIN_KB)


async def status_reply(user_id: int) -> str:
    user = ensure_user(user_id)
    return access_status_text(user)


# --------------------------- Handlers ---------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    row_before = get_user(user.id)
    row_after = ensure_user(user.id)
    is_new = row_before is None

    greeting = [f"Привет, {user.first_name or 'друг'}! 👋"]
    if is_new:
        greeting.append(
            f"🎁 Вам активирован бесплатный триал на <b>{CFG.trial_days}</b> дней до <b>{fmt_dt(int(row_after['trial_end']))}</b>."
        )
    greeting.append(await status_reply(user.id))

    await send_main_menu(update.effective_chat.id, context, "\n\n".join(greeting))


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    text = await status_reply(user.id)
    await send_main_menu(update.effective_chat.id, context, text)


async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_main_menu(update.effective_chat.id, context, f"Поддержка: {CFG.support_contact}")


async def terms_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_main_menu(update.effective_chat.id, context, f"Условия использования: {CFG.terms_url}")


async def vpn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    row = ensure_user(user.id)
    if not is_access_active(row):
        text = "⛔️ Доступ неактивен. Чтобы получить VPN, продлите подписку."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Оплатить ⭐", callback_data="buy")]])
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=kb)
        return

    profile = await fetch_vpn_profile(user.id)
    hint = profile.get("hint", "")
    config_text = profile.get("config_text")
    qr_base64 = profile.get("qr_base64")

    if config_text:
        bio = io.BytesIO(config_text.encode("utf-8"))
        bio.name = f"wg_{user.id}.conf"
        bio.seek(0)
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=bio,
            filename=bio.name,
            caption=hint or "Ваш VPN-профиль готов.",
        )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=hint or "Профиль пока недоступен. Обратитесь в поддержку.",
        )

    if qr_base64:
        try:
            qr_bytes = base64.b64decode(qr_base64, validate=True)
            qr_bio = io.BytesIO(qr_bytes)
            qr_bio.name = f"vpn_qr_{user.id}.png"
            qr_bio.seek(0)
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=qr_bio, caption="QR для быстрого импорта")
        except Exception:
            logger.warning("Invalid QR base64 for user=%s", user.id)


async def buy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    ensure_user(user.id)
    key = f"buy_last_ts:{user.id}"
    ts = now_ts()
    last = int(context.application.bot_data.get(key, 0))
    if ts - last < BUY_RATE_LIMIT_SECONDS:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Подождите пару секунд перед повторной попыткой ({BUY_RATE_LIMIT_SECONDS}с лимит).",
        )
        return
    context.application.bot_data[key] = ts

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="VPN подписка",
        description="Продление доступа к VPN на 30 дней",
        payload=invoice_payload(user.id),
        currency="XTR",
        prices=[LabeledPrice(label="VPN 30 дней", amount=CFG.price_stars_month)],
        provider_token="",
    )


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    if not q:
        return
    if q.currency != "XTR":
        await q.answer(ok=False, error_message="Поддерживается только оплата в Telegram Stars.")
        return
    if q.total_amount != CFG.price_stars_month:
        await q.answer(ok=False, error_message="Некорректная сумма платежа.")
        return
    await q.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.successful_payment:
        return

    payment = msg.successful_payment
    if payment.currency != "XTR":
        await msg.reply_text("Платёж отклонён: неподдерживаемая валюта.")
        return

    if payment.total_amount != CFG.price_stars_month:
        await msg.reply_text("Платёж отклонён: сумма не совпадает с тарифом.")
        return

    ensure_user(user.id)

    raw = payment.to_dict()
    charge_id = payment.telegram_payment_charge_id
    inserted = record_payment_if_new(user.id, charge_id, payment.total_amount, raw)
    if not inserted:
        await msg.reply_text("Платёж уже был учтён ранее. Доступ повторно не продлевается.")
        return

    new_until = extend_access(user.id, CFG.month_seconds)
    await msg.reply_text(
        "✅ Оплата получена!\n"
        f"Доступ продлён до: <b>{fmt_dt(new_until)}</b>",
        parse_mode=ParseMode.HTML,
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    simple = {
        "vpn": vpn_handler,
        "status": status_handler,
        "buy": buy_handler,
        "support": support_handler,
        "terms": terms_handler,
        "admin:stats": None,
        "admin:grant_help": None,
        "admin:block_help": None,
        "admin:broadcast_help": None,
    }

    if data in {"vpn", "status", "buy", "support", "terms"}:
        fake_update = update
        await simple[data](fake_update, context)
        return

    if not data.startswith("admin:"):
        return

    if not is_admin(user_id):
        await query.message.reply_text("Недостаточно прав.")
        return

    if data == "admin:stats":
        stats = get_stats()
        await query.message.reply_text(
            "📊 Пользователи:\n"
            f"Всего: {stats['total']}\n"
            f"Активные: {stats['active']}\n"
            f"Истёкшие/неактивные: {stats['expired']}"
        )
    elif data == "admin:grant_help":
        await query.message.reply_text(
            "Используйте: /grant <user_id> <days>\n"
            "Пример: /grant 123456789 30"
        )
    elif data == "admin:block_help":
        await query.message.reply_text(
            "Используйте:\n/block <user_id>\n/unblock <user_id>"
        )
    elif data == "admin:broadcast_help":
        await query.message.reply_text(
            "Используйте:\n/broadcast Подтверждаю | текст рассылки"
        )


def parse_args_int(value: str) -> int:
    return int(value.strip())


async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("Недостаточно прав.")
        return
    await update.effective_message.reply_text("Админ-меню:", reply_markup=ADMIN_KB)


async def grant_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("Недостаточно прав.")
        return

    try:
        target_user_id = parse_args_int(context.args[0])
        days = parse_args_int(context.args[1])
        if days <= 0:
            raise ValueError("days must be positive")
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Формат: /grant <user_id> <days>")
        return

    ensure_user(target_user_id)
    new_until = extend_access(target_user_id, days * 86400)
    await update.effective_message.reply_text(
        f"✅ Пользователю {target_user_id} выдан доступ на {days} дн.\nДо: {fmt_dt(new_until)}"
    )


async def block_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, blocked: bool) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("Недостаточно прав.")
        return

    try:
        target_user_id = parse_args_int(context.args[0])
    except (IndexError, ValueError):
        cmd = "/block" if blocked else "/unblock"
        await update.effective_message.reply_text(f"Формат: {cmd} <user_id>")
        return

    ok = set_blocked(target_user_id, blocked)
    if not ok:
        await update.effective_message.reply_text("Пользователь не найден.")
        return

    state = "заблокирован" if blocked else "разблокирован"
    await update.effective_message.reply_text(f"✅ Пользователь {target_user_id} {state}.")


async def block_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await block_handler(update, context, blocked=True)


async def unblock_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await block_handler(update, context, blocked=False)


async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not is_admin(user.id):
        if msg:
            await msg.reply_text("Недостаточно прав.")
        return

    raw_text = msg.text or ""
    payload = raw_text.replace("/broadcast", "", 1).strip()
    if "|" not in payload:
        await msg.reply_text("Формат: /broadcast Подтверждаю | текст")
        return

    confirmation, text = [x.strip() for x in payload.split("|", 1)]
    if confirmation.lower() != "подтверждаю" or not text:
        await msg.reply_text("Нужна фраза подтверждения: Подтверждаю")
        return

    user_ids = list_user_ids()
    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
            await asyncio.sleep(0.03)
        except Exception:
            failed += 1

    await msg.reply_text(f"Рассылка завершена. Отправлено: {sent}, ошибок: {failed}")


async def unknown_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_main_menu(update.effective_chat.id, context, "Неизвестная команда. Используйте кнопки ниже.")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception: %s", context.error)
    logger.error("Traceback:\n%s", "".join(traceback.format_exception(None, context.error, context.error.__traceback__)))
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Произошла ошибка. Мы уже разбираемся 🙏")
        except Exception:
            pass


def build_application() -> Application:
    app = Application.builder().token(CFG.bot_token).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("vpn", vpn_handler))
    app.add_handler(CommandHandler("buy", buy_handler))
    app.add_handler(CommandHandler("support", support_handler))
    app.add_handler(CommandHandler("terms", terms_handler))

    app.add_handler(CommandHandler("admin", admin_handler))
    app.add_handler(CommandHandler("grant", grant_handler))
    app.add_handler(CommandHandler("block", block_cmd_handler))
    app.add_handler(CommandHandler("unblock", unblock_cmd_handler))
    app.add_handler(CommandHandler("broadcast", broadcast_handler))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_handler))

    app.add_error_handler(global_error_handler)
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler), group=-1)

    return app


def main() -> None:
    init_db()
    logger.info("DB initialized at %s", DB_PATH)
    app = build_application()
    logger.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


# Мини-инструкция:
# 1) Установка:
#    pip install "python-telegram-bot==22.*"
#
# 2) Пример ENV:
#    export BOT_TOKEN="123456:ABC..."
#    export ADMIN_IDS="123456789,987654321"
#    export TRIAL_DAYS="7"
#    export MONTH_SECONDS="2592000"
#    export PRICE_STARS_MONTH="99"
#    export VPN_BACKEND_URL="https://example.com/api"
#    export VPN_BACKEND_TOKEN="secret"
#    export SUPPORT_CONTACT="@my_support"
#    export TERMS_URL="https://example.com/terms"
#
# 3) Запуск:
#    python bot.py
