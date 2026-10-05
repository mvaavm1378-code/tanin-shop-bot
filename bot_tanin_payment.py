# -*- coding: utf-8 -*-
"""
ربات فروشگاهی تلگرام - پوشاک بچگانه و زنانه
ساخته‌شده با python-telegram-bot (نسخه ۲۰+) و Supabase PostgreSQL

راه‌اندازی سریع:
1) pip install -r requirements.txt
2) توکن ربات رو از BotFather بگیر و در متغیر محیطی BOT_TOKEN یا فایل .env بذار
3) python bot.py
"""

import os
import asyncio
import psycopg
from psycopg.rows import dict_row
import logging
import json
import re
import random
from html import escape
import aiohttp
from aiohttp import web
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ----------------------------------------------------------------------------
# تنظیمات
# ----------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Supabase PostgreSQL connection string.
# Put the value from Supabase > Connect > PostgreSQL in SUPABASE_DB_URL.
# DATABASE_URL is supported as a fallback for hosts such as Render.
DB_URL = (
    os.getenv("SUPABASE_DB_URL", "").strip()
    or os.getenv("DATABASE_URL", "").strip()
    or os.getenv("POSTGRES_URL", "").strip()
)

# ----------------------------------------------------------------------------
# تنظیمات پیامک ملی پیامک
# اطلاعات محرمانه فقط از Environment Variables خوانده می‌شوند.
# ----------------------------------------------------------------------------
MELIPAYAMAK_USERNAME = os.getenv("MELIPAYAMAK_USERNAME", "").strip()
MELIPAYAMAK_API_KEY = (
    os.getenv("MELIPAYAMAK_API_KEY", "").strip()
    or os.getenv("MELIPAYAMAK_PASSWORD", "").strip()
)
MELIPAYAMAK_SENDER = os.getenv("MELIPAYAMAK_SENDER", "50004001853486").strip()
SMS_ADMIN_PHONE = os.getenv("SMS_ADMIN_PHONE", "09384853486").strip()
MELIPAYAMAK_URL = "https://rest.payamak-panel.com/api/SendSMS/SendSMS"


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# منطقه زمانی رسمی ربات برای ثبت و نمایش زمان‌ها
IRAN_TZ = ZoneInfo("Asia/Tehran")


def iran_now_naive():
    """Current Iran local time as a naive datetime for existing DB string columns."""
    return datetime.now(IRAN_TZ).replace(tzinfo=None)


def _to_iran_datetime(value):
    """Convert a DB datetime/string to Iran time. Naive values are assumed Iran-local."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text or text == "-":
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    dt = None
            if dt is None:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IRAN_TZ)
    return dt.astimezone(IRAN_TZ)


def gregorian_to_jalali(gy, gm, gd):
    """Convert Gregorian date to Jalali date without external dependencies."""
    g_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    j_days_in_month = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1
    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    for i in range(gm2):
        g_day_no += g_days_in_month[i]
    if gm2 > 1 and ((gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)):
        g_day_no += 1
    g_day_no += gd2
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    for i in range(11):
        if j_day_no < j_days_in_month[i]:
            jm = i + 1
            jd = j_day_no + 1
            break
        j_day_no -= j_days_in_month[i]
    else:
        jm = 12
        jd = j_day_no + 1
    return jy, jm, jd


def format_iran_jalali(value, include_time=True):
    dt = _to_iran_datetime(value)
    if dt is None:
        return "-"
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    result = f"{jy:04d}/{jm:02d}/{jd:02d}"
    if include_time:
        result += f" - {dt.hour:02d}:{dt.minute:02d}"
    return result


def format_iran_jalali_fa(value, include_time=True):
    text = format_iran_jalali(value, include_time=include_time)
    return str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹") and text.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))

# مراحل مکالمه برای ثبت سفارش
ASK_QTY, ASK_NAME, ASK_PHONE, ASK_GENDER, ASK_ADDRESS = range(5)

SAVED_TYPES = ("name", "phone", "address")

# ----------------------------------------------------------------------------
# دیتابیس
# ----------------------------------------------------------------------------
def get_conn():
    """Open a PostgreSQL connection to Supabase."""
    if not DB_URL:
        raise RuntimeError(
            "SUPABASE_DB_URL (or DATABASE_URL) is not set. "
            "Add the PostgreSQL connection string from Supabase > Connect."
        )

    # prepare_threshold=None keeps the bot compatible with Supabase's
    # transaction pooler as well as a direct PostgreSQL connection.
    return psycopg.connect(
        DB_URL,
        row_factory=dict_row,
        prepare_threshold=None,
        connect_timeout=15,
    )


def init_db():
    """Verify that the Supabase schema already exists.

    The schema is intentionally managed in Supabase SQL Editor, not by the bot.
    This prevents a bot deployment from silently creating a second/local database
    or changing the production schema.
    """
    required = {
        "products",
        "customers",
        "customer_saved_data",
        "support_tickets",
        "support_ticket_messages",
        "cart_items",
        "bank_accounts",
        "pending_payments",
        "orders",
        "app_meta",
    }
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT table_name
               FROM information_schema.tables
               WHERE table_schema='public' AND table_name = ANY(%s)""",
            (list(required),),
        ).fetchall()
        found = {r["table_name"] for r in rows}
        missing = sorted(required - found)
        if missing:
            raise RuntimeError(
                "Supabase schema is incomplete. Missing tables: " + ", ".join(missing)
            )

        # Check the columns that this version of the bot relies on.
        expected_columns = {
            "products": {"id", "name", "category", "size", "color", "price", "photo_url", "active", "pack_info"},
            "customers": {"user_id", "username", "full_name", "phone", "gender", "first_seen", "last_seen"},
            "customer_saved_data": {"id", "user_id", "data_type", "value", "created_at", "updated_at"},
            "support_tickets": {"id", "user_id", "order_id", "topic", "status", "created_at", "updated_at", "closed_at"},
            "support_ticket_messages": {"id", "ticket_id", "sender_id", "sender_type", "message", "created_at"},
            "cart_items": {"id", "user_id", "product_id", "qty"},
            "bank_accounts": {"id", "bank_name", "owner_name", "card_number", "account_number", "iban", "active", "created_at", "updated_at"},
            "pending_payments": {"id", "user_id", "full_name", "phone", "address", "items_summary", "total_price", "payment_status", "receipt_file_id", "created_at", "reviewed_at", "order_id", "admin_id"},
            "orders": {"id", "user_id", "full_name", "phone", "address", "items_summary", "total_price", "status", "created_at", "finalized_at", "payment_status", "transaction_ref"},
            "app_meta": {"key", "value"},
        }
        for table, cols in expected_columns.items():
            actual = {
                r["column_name"]
                for r in conn.execute(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_schema='public' AND table_name=%s""",
                    (table,),
                ).fetchall()
            }
            missing_cols = sorted(cols - actual)
            if missing_cols:
                raise RuntimeError(
                    f"Supabase table '{table}' is missing columns: {', '.join(missing_cols)}"
                )

        # Keep the original one-time catalog seed behavior, but never delete
        # existing Supabase products.
        marker = conn.execute(
            "SELECT value FROM app_meta WHERE key=%s",
            ("children_catalog_reset_v1",),
        ).fetchone()
        if not marker:
            exists = conn.execute(
                "SELECT id FROM products WHERE name=%s AND category=%s LIMIT 1",
                ("سلین مازراتی", "بچگانه"),
            ).fetchone()
            if not exists:
                conn.execute(
                    """INSERT INTO products
                       (name, category, size, color, price, photo_url, active, pack_info)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        "سلین مازراتی",
                        "بچگانه",
                        "۱ تا ۳ سال",
                        "رنگ‌بندی طبق ژورنال موجود",
                        4380000,
                        "",
                        True,
                        "پک ۱۲ عددی",
                    ),
                )
            conn.execute(
                "INSERT INTO app_meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING",
                ("children_catalog_reset_v1", "done"),
            )
            conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# کیبوردها
# ----------------------------------------------------------------------------
def admin_menu_counts():
    """تعداد موارد قابل نمایش کنار میان‌برهای منوی ادمین."""
    conn = get_conn()
    try:
        orders = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE status='در انتظار بررسی'"
        ).fetchone()["n"]
        payments = conn.execute(
            "SELECT COUNT(*) AS n FROM pending_payments WHERE payment_status='در انتظار بررسی'"
        ).fetchone()["n"]
        products = conn.execute(
            "SELECT COUNT(*) AS n FROM products WHERE active=TRUE"
        ).fetchone()["n"]
        tickets = conn.execute(
            "SELECT COUNT(*) AS n FROM support_tickets WHERE status IN ('new','admin_waiting')"
        ).fetchone()["n"]
        return {"orders": orders or 0, "payments": payments or 0,
                "products": products or 0, "tickets": tickets or 0}
    finally:
        conn.close()


def _admin_badge(label, count):
    """عدد صفر را پنهان می‌کند و اعداد مثبت را کنار عنوان می‌آورد."""
    return f"{label} ({count})" if count else label


def main_menu_keyboard(user_id=None):
    # منوی ادمین کاملاً جدا از منوی مشتری است.
    if is_admin(user_id):
        try:
            counts = admin_menu_counts()
        except Exception:
            logger.exception("Could not load admin menu counters")
            counts = {"orders": 0, "payments": 0, "products": 0, "tickets": 0}
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("⚙️ پنل مدیریت")],
                [KeyboardButton(_admin_badge("📦 سفارش‌ها", counts["orders"])),
                 KeyboardButton(_admin_badge("💳 پرداخت‌های در انتظار", counts["payments"]))],
                [KeyboardButton(_admin_badge("🛠 مدیریت محصولات", counts["products"])),
                 KeyboardButton(_admin_badge("🎫 پشتیبانی", counts["tickets"]))],
            ],
            resize_keyboard=True,
        )

    rows = [
        [KeyboardButton("🧾 کاتالوگ محصولات")],
        [KeyboardButton("🛒 سبد خرید"), KeyboardButton("📦 سفارش‌های من")],
        [KeyboardButton("💬 مرکز پشتیبانی")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def category_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👶 بچگانه", callback_data="cat:بچگانه")],
            [InlineKeyboardButton("👗 زنانه", callback_data="cat:زنانه")],
        ]
    )


def product_keyboard(product_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ افزودن به سبد خرید", callback_data=f"add:{product_id}")
            ]
        ]
    )


def add_quantity_keyboard(product_id, qty=1):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➖", callback_data=f"addqty:minus:{product_id}:{qty}"),
                InlineKeyboardButton(f"📦 {qty} بسته", callback_data=f"addqty:noop:{product_id}:{qty}"),
                InlineKeyboardButton("➕", callback_data=f"addqty:plus:{product_id}:{qty}"),
            ],
            [InlineKeyboardButton("✅ افزودن به سبد خرید", callback_data=f"addqty:confirm:{product_id}:{qty}")],
            [InlineKeyboardButton("🔙 مرحله قبلی", callback_data=f"addqty:cancel:{product_id}:{qty}")],
        ]
    )


def cart_item_keyboard(item_id):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗑 حذف از سبد", callback_data=f"remove:{item_id}")]]
    )


def checkout_quantity_keyboard(rows):
    buttons = []
    for row in rows:
        label = str(row["name"])[:24]
        buttons.append([
            InlineKeyboardButton("➖", callback_data=f"qty:minus:{row['cid']}"),
            InlineKeyboardButton(f"{label} × {row['qty']}", callback_data=f"qty:noop:{row['cid']}"),
            InlineKeyboardButton("➕", callback_data=f"qty:plus:{row['cid']}"),
        ])
    buttons.append([InlineKeyboardButton("🗑 حذف یک محصول", callback_data="qty:delete_menu")])
    buttons.append([InlineKeyboardButton("✅ ادامه ثبت سفارش", callback_data="qty:continue")])
    buttons.append([InlineKeyboardButton("🔙 مرحله قبلی", callback_data="qty:back_cart")])
    return InlineKeyboardMarkup(buttons)


def checkout_quantity_text(rows):
    total = sum(r["price"] * r["qty"] for r in rows)
    lines = ["🛒 <b>تعداد محصولات را بررسی کن</b>", "", "با دکمه‌های ➖ و ➕ تعداد هر محصول را کم یا زیاد کن:", ""]
    for r in rows:
        line_total = r["price"] * r["qty"]
        lines.append(f"👕 {escape(r['name'])} — {r['qty']} × {r['price']:,} = {line_total:,} تومان")
    lines.extend(["", f"💰 <b>جمع کل: {total:,} تومان</b>", "", "بعد از تنظیم تعداد، روی «ادامه ثبت سفارش» بزن."])
    return "\n".join(lines)


async def show_checkout_quantities(message, user_id):
    conn = get_conn()
    rows = conn.execute(
        """SELECT cart_items.id AS cid, products.name, products.price, cart_items.qty
           FROM cart_items JOIN products ON cart_items.product_id = products.id
           WHERE cart_items.user_id=%s AND products.active=TRUE
           ORDER BY cart_items.id""",
        (user_id,),
    ).fetchall()
    conn.close()
    if not rows:
        await message.reply_text("🛒 سبد خریدت خالیه، اول چیزی اضافه کن.")
        return False
    await message.reply_text(
        checkout_quantity_text(rows),
        parse_mode="HTML",
        reply_markup=checkout_quantity_keyboard(rows),
    )
    return True


async def checkout_quantity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    action = parts[1]
    user_id = q.from_user.id

    if action == "back_cart":
        await q.message.reply_text("🔙 به سبد خرید برگشتی.", reply_markup=main_menu_keyboard(user_id))
        return ConversationHandler.END

    if action == "continue":
        if await show_saved_step(q.message, context, "name"):
            return ASK_NAME
        await q.message.reply_text("لطفاً نام و نام خانوادگی‌ت رو بفرست:")
        return ASK_NAME

    if action == "noop":
        return ASK_QTY

    if action == "delete_menu":
        conn = get_conn()
        rows = conn.execute(
            """SELECT cart_items.id AS cid, products.name, cart_items.qty
               FROM cart_items JOIN products ON cart_items.product_id = products.id
               WHERE cart_items.user_id=%s AND products.active=TRUE
               ORDER BY cart_items.id""",
            (user_id,),
        ).fetchall()
        conn.close()
        if not rows:
            await q.message.edit_text("🛒 سبد خریدت خالیه.")
            return ConversationHandler.END
        delete_buttons = [
            [InlineKeyboardButton(f"🗑 {str(r['name'])[:32]}", callback_data=f"qty:delete:{r['cid']}")]
            for r in rows
        ]
        delete_buttons.append([InlineKeyboardButton("🔙 برگشت", callback_data="qty:back")])
        await q.message.edit_text(
            "کدوم محصول رو می‌خوای از سبد حذف کنی؟",
            reply_markup=InlineKeyboardMarkup(delete_buttons),
        )
        return ASK_QTY

    if action == "back":
        conn = get_conn()
        rows = conn.execute(
            """SELECT cart_items.id AS cid, products.name, products.price, cart_items.qty
               FROM cart_items JOIN products ON cart_items.product_id = products.id
               WHERE cart_items.user_id=%s AND products.active=TRUE
               ORDER BY cart_items.id""",
            (user_id,),
        ).fetchall()
        conn.close()
        if not rows:
            await q.message.edit_text("🛒 سبد خریدت خالیه.")
            return ConversationHandler.END
        await q.message.edit_text(
            checkout_quantity_text(rows), parse_mode="HTML", reply_markup=checkout_quantity_keyboard(rows)
        )
        return ASK_QTY

    if action == "delete":
        cart_id = int(parts[2])
        conn = get_conn()
        conn.execute("DELETE FROM cart_items WHERE id=%s AND user_id=%s", (cart_id, user_id))
        conn.commit()
        rows = conn.execute(
            """SELECT cart_items.id AS cid, products.name, products.price, cart_items.qty
               FROM cart_items JOIN products ON cart_items.product_id = products.id
               WHERE cart_items.user_id=%s AND products.active=TRUE
               ORDER BY cart_items.id""",
            (user_id,),
        ).fetchall()
        conn.close()
        if not rows:
            await q.message.edit_text("🛒 سبد خریدت خالی شد.")
            return ConversationHandler.END
        await q.message.edit_text(
            checkout_quantity_text(rows), parse_mode="HTML", reply_markup=checkout_quantity_keyboard(rows)
        )
        return ASK_QTY

    if action in ("plus", "minus"):
        cart_id = int(parts[2])
        conn = get_conn()
        item = conn.execute(
            """SELECT cart_items.id, cart_items.qty
               FROM cart_items JOIN products ON cart_items.product_id = products.id
               WHERE cart_items.id=%s AND cart_items.user_id=%s AND products.active=TRUE""",
            (cart_id, user_id),
        ).fetchone()
        if not item:
            conn.close()
            await q.answer("این محصول دیگر در سبد نیست.", show_alert=True)
            return ASK_QTY
        new_qty = item["qty"] + (1 if action == "plus" else -1)
        if new_qty < 1:
            conn.close()
            return ASK_QTY
        conn.execute("UPDATE cart_items SET qty=%s WHERE id=%s AND user_id=%s", (new_qty, cart_id, user_id))
        conn.commit()
        rows = conn.execute(
            """SELECT cart_items.id AS cid, products.name, products.price, cart_items.qty
               FROM cart_items JOIN products ON cart_items.product_id = products.id
               WHERE cart_items.user_id=%s AND products.active=TRUE
               ORDER BY cart_items.id""",
            (user_id,),
        ).fetchall()
        conn.close()
        await q.message.edit_text(
            checkout_quantity_text(rows), parse_mode="HTML", reply_markup=checkout_quantity_keyboard(rows)
        )
        return ASK_QTY

    return ASK_QTY


# ----------------------------------------------------------------------------
# دستورات پایه
# ----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    conn = get_conn()
    now = iran_now_naive().strftime("%Y-%m-%d %H:%M")
    conn.execute(
        """INSERT INTO customers(user_id, username, full_name, first_seen, last_seen)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT(user_id) DO UPDATE SET
             username=excluded.username,
             full_name=excluded.full_name,
             last_seen=excluded.last_seen""",
        (u.id, u.username or "", u.full_name or "", now, now),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "🌸 به «تولیدی تنین ایران» خوش آمدید.\n\n"
        "🧵 از نخ تا ویترین، کنار شماییم.\n\n"
        "💗 از اینکه ما را برای خرید خود انتخاب و به ما اعتماد کرده‌اید، سپاسگزاریم.🫰🏻",
        reply_markup=main_menu_keyboard(update.effective_user.id),
    )


async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("یکی از دسته‌ها رو انتخاب کن:", reply_markup=category_keyboard())


async def category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.split(":", 1)[1]

    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM products WHERE category=%s AND active=TRUE", (category,)
    ).fetchall()
    conn.close()

    if not rows:
        await query.message.reply_text("فعلاً محصولی در این دسته موجود نیست.")
        return

    for p in rows:
        text = (
            f"👕 <b>{escape(p['name'])}</b>\n"
            f"سایز: {escape(p['size'] or '-') }\n"
            f"رنگ‌بندی: {escape(p.get('color') or '-')}\n"
            + (f"{escape(p['pack_info'])}\n" if p['pack_info'] else "")
            + f"قیمت: {p['price']:,} تومان"
        )
        sent = False
        if p["photo_url"]:
            try:
                await query.message.reply_photo(
                    p["photo_url"], caption=text, parse_mode="HTML",
                    reply_markup=product_keyboard(p["id"]),
                )
                sent = True
            except Exception as e:
                logger.warning(f"Could not send photo for product {p['id']}: {e}")
        if not sent:
            await query.message.reply_text(
                text, parse_mode="HTML", reply_markup=product_keyboard(p["id"])
            )


async def safe_edit(message, text, **kwargs):
    """Edit a message whether it is a photo (caption) or plain text; ignore 'not modified'."""
    try:
        if message.photo:
            return await message.edit_caption(caption=text, **kwargs)
        return await message.edit_text(text, **kwargs)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return None
        raise


async def add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """وقتی مشتری روی «افزودن به سبد خرید» می‌زند، ابتدا تعداد بسته را انتخاب می‌کند."""
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split(":", 1)[1])
    conn = get_conn()
    product = conn.execute(
        "SELECT * FROM products WHERE id=%s AND active=TRUE", (product_id,)
    ).fetchone()
    conn.close()

    if not product:
        await query.answer("این محصول دیگر موجود نیست.", show_alert=True)
        return

    pack_info = product["pack_info"] or ""
    text = (
        f"👕 <b>{escape(product['name'])}</b>\n\n"
        f"{escape(pack_info)}\n" if pack_info else
        f"👕 <b>{escape(product['name'])}</b>\n\n"
    )
    text += "📦 تعداد بسته‌ای که می‌خواهی به سبد اضافه کنی را انتخاب کن:"

    await safe_edit(
        query.message,
        text,
        parse_mode="HTML",
        reply_markup=add_quantity_keyboard(product_id, 1),
    )


async def add_quantity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]
    product_id = int(parts[2])
    qty = max(1, int(parts[3]))

    conn = get_conn()
    product = conn.execute(
        "SELECT * FROM products WHERE id=%s AND active=TRUE", (product_id,)
    ).fetchone()
    conn.close()

    if not product:
        await safe_edit(query.message, "این محصول دیگر موجود نیست.")
        return

    if action == "plus":
        qty += 1
    elif action == "minus":
        qty = max(1, qty - 1)
    elif action == "cancel":
        await query.message.delete()
        return
    elif action == "confirm":
        user_id = query.from_user.id
        conn = get_conn()
        existing = conn.execute(
            "SELECT id, qty FROM cart_items WHERE user_id=%s AND product_id=%s",
            (user_id, product_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE cart_items SET qty=%s WHERE id=%s AND user_id=%s",
                (existing["qty"] + qty, existing["id"], user_id),
            )
        else:
            conn.execute(
                "INSERT INTO cart_items (user_id, product_id, qty) VALUES (%s, %s, %s)",
                (user_id, product_id, qty),
            )
        conn.commit()
        conn.close()
        await safe_edit(
            query.message,
            f"✅ {qty} بسته از «{escape(product['name'])}» به سبد خرید اضافه شد.",
            parse_mode="HTML",
        )
        return
    elif action == "noop":
        return

    pack_info = product["pack_info"] or ""
    text = (
        f"👕 <b>{escape(product['name'])}</b>\n\n"
        f"{escape(pack_info)}\n" if pack_info else
        f"👕 <b>{escape(product['name'])}</b>\n\n"
    )
    text += f"📦 تعداد بسته‌ای که می‌خواهی به سبد اضافه کنی: <b>{qty} بسته</b>"

    await safe_edit(
        query.message,
        text,
        parse_mode="HTML",
        reply_markup=add_quantity_keyboard(product_id, qty),
    )


async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_conn()
    rows = conn.execute(
        """SELECT cart_items.id as cid, products.*, cart_items.qty
           FROM cart_items JOIN products ON cart_items.product_id = products.id
           WHERE cart_items.user_id=%s""",
        (user_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("سبد خریدت خالیه 🛒")
        return

    total = 0
    for r in rows:
        line_total = r["price"] * r["qty"]
        total += line_total
        text = (
            f"👕 {r['name']}\n"
            f"تعداد: {r['qty']} × {r['price']:,} = {line_total:,} تومان"
        )
        await update.message.reply_text(text, reply_markup=cart_item_keyboard(r["cid"]))

    await update.message.reply_text(
        f"💰 جمع کل: {total:,} تومان",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ ثبت سفارش", callback_data="checkout:start")]
        ]),
    )


async def remove_from_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("حذف شد 🗑")
    cart_id = int(query.data.split(":", 1)[1])
    conn = get_conn()
    conn.execute("DELETE FROM cart_items WHERE id=%s AND user_id=%s", (cart_id, update.effective_user.id))
    conn.commit()
    conn.close()
    await query.message.delete()


# ----------------------------------------------------------------------------
# فرآیند ثبت سفارش (Conversation)
# ----------------------------------------------------------------------------
async def checkout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع ثبت سفارش از سبد خرید؛ خطای ثبت مشتری نباید دکمه را بی‌واکنش کند."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            logger.exception("Could not answer checkout callback for user %s", update.effective_user.id if update.effective_user else "-")
        target_message = query.message
    else:
        target_message = update.message

    user_id = update.effective_user.id
    u = update.effective_user

    # ابتدا سبد را می‌خوانیم؛ اگر دیتابیس/سبد مشکل داشته باشد، کاربر پیام واضح می‌گیرد.
    try:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM cart_items WHERE user_id=%s", (user_id,)
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.exception("CHECKOUT START: failed to read cart for user %s", user_id)
        await target_message.reply_text(
            "⚠️ فعلاً نتونستم سبد خریدت رو بررسی کنم.\n"
            "لطفاً چند لحظه بعد دوباره روی «ثبت سفارش» بزن."
        )
        return ConversationHandler.END

    if not rows:
        await target_message.reply_text("سبد خریدت خالیه، اول چیزی اضافه کن.")
        return ConversationHandler.END

    # ثبت/به‌روزرسانی مشتری مرحله‌ی جانبی است؛ شکست آن نباید شروع سفارش را متوقف کند.
    try:
        now = iran_now_naive().strftime("%Y-%m-%d %H:%M")
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO customers(user_id, username, full_name, first_seen, last_seen)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT(user_id) DO UPDATE SET
                     username=excluded.username,
                     full_name=excluded.full_name,
                     last_seen=excluded.last_seen""",
                (u.id, u.username or "", u.full_name or "", now, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("CHECKOUT START: customer upsert failed for user %s; continuing checkout", user_id)

    try:
        shown = await show_checkout_quantities(target_message, user_id)
        if not shown:
            return ConversationHandler.END
    except Exception:
        logger.exception("CHECKOUT START: failed to show quantity screen for user %s", user_id)
        await target_message.reply_text(
            "⚠️ نتونستم مرحله ثبت سفارش رو باز کنم.\n"
            "لطفاً دوباره از سبد خرید روی «ثبت سفارش» بزن."
        )
        return ConversationHandler.END

    return ASK_QTY


def get_saved_data(user_id, data_type):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, value FROM customer_saved_data WHERE user_id=%s AND data_type=%s ORDER BY id DESC",
        (user_id, data_type),
    ).fetchall()
    conn.close()
    return rows


def saved_value_exists(user_id, data_type, value, exclude_id=None):
    """Check for a previously saved equivalent value for this customer."""
    value = (value or "").strip()
    if not value:
        return False

    conn = get_conn()
    rows = conn.execute(
        "SELECT id, value FROM customer_saved_data WHERE user_id=%s AND data_type=%s",
        (user_id, data_type),
    ).fetchall()
    conn.close()

    def canonical(v):
        v = str(v or "").strip()
        if data_type == "name":
            return normalize_persian_full_name(v) or re.sub(r"\s+", " ", v).replace("ي", "ی").replace("ك", "ک")
        if data_type == "phone":
            return normalize_iran_phone(v) or re.sub(r"\s+", "", v)
        return re.sub(r"\s+", " ", v)

    target = canonical(value)
    return any(
        row["id"] != exclude_id and canonical(row["value"]) == target
        for row in rows
    )

def save_saved_data(user_id, data_type, value):
    value = (value or "").strip()
    if not value or data_type not in SAVED_TYPES:
        return
    now = iran_now_naive().strftime("%Y-%m-%d %H:%M")
    conn = get_conn()
    conn.execute(
        """INSERT INTO customer_saved_data(user_id, data_type, value, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT(user_id, data_type, value) DO UPDATE SET updated_at=excluded.updated_at""",
        (user_id, data_type, value, now, now),
    )
    conn.commit()
    conn.close()


def update_saved_data(user_id, data_id, data_type, value):
    value = (value or "").strip()
    now = iran_now_naive().strftime("%Y-%m-%d %H:%M")
    conn = get_conn()
    conn.execute(
        "UPDATE customer_saved_data SET value=%s, updated_at=%s WHERE id=%s AND user_id=%s AND data_type=%s",
        (value, now, data_id, user_id, data_type),
    )
    conn.commit()
    conn.close()


def delete_saved_data(user_id, data_id, data_type):
    conn = get_conn()
    conn.execute(
        "DELETE FROM customer_saved_data WHERE id=%s AND user_id=%s AND data_type=%s",
        (data_id, user_id, data_type),
    )
    conn.commit()
    conn.close()


def saved_data_keyboard(data_type, rows):
    labels = {"name": "نام", "phone": "شماره تماس", "address": "آدرس"}
    buttons = []
    for row in rows:
        value = str(row["value"])
        display = value if data_type != "address" else value.replace("\n", " ")
        buttons.append([
            InlineKeyboardButton(
                f"✅ {display[:38]}",
                callback_data=f"saved:{data_type}:use:{row['id']}"
            ),
            InlineKeyboardButton("✏️", callback_data=f"saved:{data_type}:edit:{row['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"saved:{data_type}:delete:{row['id']}"),
        ])
    buttons.append([InlineKeyboardButton(f"➕ {labels[data_type]} جدید", callback_data=f"saved:{data_type}:new")])
    buttons.append([InlineKeyboardButton("🔙 مرحله قبلی", callback_data=f"saved:{data_type}:back")])
    return InlineKeyboardMarkup(buttons)


async def show_saved_step(message, context, data_type):
    rows = get_saved_data(message.chat_id, data_type)
    labels = {"name": "نام و نام خانوادگی", "phone": "شماره موبایل", "address": "آدرس"}
    if rows:
        await message.reply_text(
            f"📌 {labels[data_type]} قبلی را انتخاب کن یا مورد جدید اضافه کن.\n\n"
            "برای ویرایش ✏️ و برای حذف 🗑 را بزن:",
            reply_markup=saved_data_keyboard(data_type, rows),
        )
        return True
    return False


def normalize_persian_full_name(value):
    """Validate and normalize a Persian full name for checkout."""
    if not value:
        return None
    text = str(value).strip()
    text = text.replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
    text = re.sub(r"\s+", " ", text)
    # Only Arabic/Persian letters and spaces are allowed; no digits/symbols/English letters.
    if not text or any((not ch.isalpha()) or not ("\u0600" <= ch <= "\u06ff") for ch in text if ch not in (" ", "\u200c")):
        return None
    parts = text.split(" ")
    if len(parts) < 2 or any(len(part.replace("\u200c", "")) < 2 for part in parts):
        return None
    return text


def name_input_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🔙 مرحله قبلی")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def name_confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ بله، درست است", callback_data="nameconfirm:yes")],
        [InlineKeyboardButton("✏️ ویرایش", callback_data="nameconfirm:edit")],
        [InlineKeyboardButton("🔙 مرحله قبلی", callback_data="nameconfirm:back")],
    ])


async def handle_name_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]
    if action == "back":
        context.user_data.pop("pending_full_name", None)
        context.user_data.pop("editing_saved_name_id", None)
        await show_checkout_quantities(q.message, q.from_user.id)
        return ASK_QTY
    if action == "edit":
        context.user_data.pop("pending_full_name", None)
        await q.message.reply_text("✏️ لطفاً نام و نام خانوادگی را دوباره وارد کن:\nمثال: محمد احمدی")
        return ASK_NAME

    full_name = context.user_data.pop("pending_full_name", None)
    if not full_name:
        await q.message.reply_text("❌ اطلاعات نام پیدا نشد. لطفاً نام و نام خانوادگی را دوباره وارد کن.")
        return ASK_NAME

    user_id = q.from_user.id
    edit_id = context.user_data.get("editing_saved_name_id")
    if saved_value_exists(user_id, "name", full_name, int(edit_id) if edit_id else None):
        await q.message.reply_text(
            "⚠️ این نام و نام خانوادگی را قبلاً ثبت کرده‌ای.\n"
            "لطفاً نام و نام خانوادگی دیگری وارد کن.",
            reply_markup=name_input_keyboard(),
        )
        return ASK_NAME

    edit_id = context.user_data.pop("editing_saved_name_id", None)
    if edit_id:
        update_saved_data(user_id, int(edit_id), "name", full_name)
    else:
        save_saved_data(user_id, "name", full_name)
    context.user_data["full_name"] = full_name
    if await show_saved_step(q.message, context, "phone"):
        return ASK_PHONE
    await q.message.reply_text(
        "📱 لطفاً شماره موبایل خودت رو با دکمه زیر از تلگرام ارسال کن:\n\n"
        "شماره باید متعلق به همین حساب تلگرام باشد.",
        reply_markup=phone_keyboard(),
    )
    return ASK_PHONE


def normalize_iran_phone(value):
    """Normalize Iranian mobile numbers to 11-digit 09xxxxxxxxx format."""
    if not value:
        return None

    translation = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )
    phone = str(value).translate(translation)

    # Remove all common separators/whitespace that Telegram may include.
    for ch in (" ", "-", "(", ")", "\u200b", "\u200c", "\u200d", "\ufeff"):
        phone = phone.replace(ch, "")

    # Accept the common Iranian formats returned/entered by Telegram: 
    # 09xxxxxxxxx, +989xxxxxxxxx, 00989xxxxxxxxx, 989xxxxxxxxx, 9xxxxxxxxx
    if phone.startswith("+98"):
        phone = phone[3:]
    elif phone.startswith("0098"):
        phone = phone[4:]
    elif phone.startswith("98"):
        phone = phone[2:]

    # After removing the country code, an Iranian mobile should have
    # 10 digits beginning with 9. Convert it to the local 09xxxxxxxxx form.
    if len(phone) == 10 and phone.startswith("9") and phone.isdigit():
        phone = "0" + phone

    if len(phone) == 11 and phone.startswith("09") and phone.isdigit():
        return phone
    return None


def phone_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📱 ارسال شماره موبایل", request_contact=True)],
            [KeyboardButton("🔙 مرحله قبلی")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def gender_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("👨 مرد"), KeyboardButton("👩 زن")],
            [KeyboardButton("🔙 مرحله قبلی")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_name = update.message.text or ""
    if raw_name.strip() == "🔙 مرحله قبلی":
        context.user_data.pop("pending_full_name", None)
        context.user_data.pop("editing_saved_name_id", None)
        await show_checkout_quantities(update.message, update.effective_user.id)
        return ASK_QTY
    full_name = normalize_persian_full_name(raw_name)
    if not full_name:
        await update.message.reply_text(
            "❌ نام و نام خانوادگی را درست وارد کن.\n\n"
            "نام باید حداقل دو بخش داشته باشد و فقط شامل حروف فارسی باشد.\n"
            "مثال: محمد احمدی",
            reply_markup=name_input_keyboard(),
        )
        return ASK_NAME

    context.user_data["pending_full_name"] = full_name
    await update.message.reply_text(
        f"👤 نام و نام خانوادگی شما:\n<b>{escape(full_name)}</b>\n\n"
        "آیا اطلاعات درست است؟",
        parse_mode="HTML",
        reply_markup=name_confirm_keyboard(),
    )
    return ASK_NAME


async def handle_saved_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    action = parts[2]
    user_id = q.from_user.id
    if action == "back":
        await show_checkout_quantities(q.message, user_id)
        return ASK_QTY
    if action == "use":
        row = next((r for r in get_saved_data(user_id, "name") if r["id"] == int(parts[3])), None)
        if row:
            context.user_data["full_name"] = row["value"]
            if await show_saved_step(q.message, context, "phone"):
                return ASK_PHONE
            await q.message.reply_text("📱 لطفاً شماره موبایل خودت رو با دکمه زیر از تلگرام ارسال کن:", reply_markup=phone_keyboard())
            return ASK_PHONE
    elif action == "new":
        context.user_data.pop("editing_saved_name_id", None)
        await q.message.reply_text("لطفاً نام و نام خانوادگی جدید را بفرست:\nمثال: محمد احمدی")
        return ASK_NAME
    elif action == "edit":
        context.user_data["editing_saved_name_id"] = parts[3]
        await q.message.reply_text("نام و نام خانوادگی جدید را بفرست:\nمثال: محمد احمدی")
        return ASK_NAME
    elif action == "delete":
        delete_saved_data(user_id, int(parts[3]), "name")
        rows = get_saved_data(user_id, "name")
        if rows:
            await q.message.reply_text("نام حذف شد. یک نام را انتخاب کن یا نام جدید اضافه کن:", reply_markup=saved_data_keyboard("name", rows))
            return ASK_NAME
        await q.message.reply_text("نام حذف شد. نام و نام خانوادگی را بفرست:\nمثال: محمد احمدی")
        return ASK_NAME
    return ASK_NAME


async def ask_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.contact and (update.message.text or "").strip() == "🔙 مرحله قبلی":
        await show_saved_step(update.message, context, "name")
        return ASK_NAME
    if not update.message.contact:
        await update.message.reply_text(
            "❌ لطفاً شماره را فقط با دکمه «📱 ارسال شماره موبایل» بفرست.",
            reply_markup=phone_keyboard(),
        )
        return ASK_PHONE

    contact = update.message.contact
    if contact.user_id is not None and contact.user_id != update.effective_user.id:
        await update.message.reply_text(
            "❌ این شماره به حساب تلگرام شما مربوط نیست. لطفاً شماره خودت را ارسال کن.",
            reply_markup=phone_keyboard(),
        )
        return ASK_PHONE

    phone = normalize_iran_phone(contact.phone_number)
    if not phone:
        await update.message.reply_text(
            "❌ شماره باید یک موبایل ایرانی ۱۱ رقمی و با 09 شروع شود. دوباره ارسال کن.",
            reply_markup=phone_keyboard(),
        )
        return ASK_PHONE

    user_id = update.effective_user.id
    edit_id = context.user_data.get("editing_saved_phone_id")
    if saved_value_exists(user_id, "phone", phone, int(edit_id) if edit_id else None):
        await update.message.reply_text(
            "⚠️ این شماره تماس را قبلاً ثبت کرده‌ای.\n"
            "لطفاً شماره دیگری وارد کن.",
            reply_markup=phone_keyboard(),
        )
        return ASK_PHONE

    edit_id = context.user_data.pop("editing_saved_phone_id", None)
    if edit_id:
        update_saved_data(user_id, int(edit_id), "phone", phone)
    else:
        save_saved_data(user_id, "phone", phone)
    context.user_data["phone"] = phone
    await update.message.reply_text("جنسیت خودت رو انتخاب کن:", reply_markup=gender_keyboard())
    return ASK_GENDER


async def handle_saved_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    action = parts[2]
    user_id = q.from_user.id
    if action == "back":
        await show_saved_step(q.message, context, "name")
        return ASK_NAME
    if action == "use":
        row = next((r for r in get_saved_data(user_id, "phone") if r["id"] == int(parts[3])), None)
        if row:
            context.user_data["phone"] = row["value"]
            await q.message.reply_text("جنسیت خودت رو انتخاب کن:", reply_markup=gender_keyboard())
            return ASK_GENDER
    elif action == "new":
        context.user_data.pop("editing_saved_phone_id", None)
        await q.message.reply_text("📱 شماره موبایل جدیدت را با دکمه زیر ارسال کن:", reply_markup=phone_keyboard())
        return ASK_PHONE
    elif action == "edit":
        context.user_data["editing_saved_phone_id"] = parts[3]
        await q.message.reply_text("📱 شماره جدیدت را با دکمه زیر ارسال کن:", reply_markup=phone_keyboard())
        return ASK_PHONE
    elif action == "delete":
        delete_saved_data(user_id, int(parts[3]), "phone")
        rows = get_saved_data(user_id, "phone")
        if rows:
            await q.message.reply_text("شماره حذف شد. یک شماره را انتخاب کن یا شماره جدید اضافه کن:", reply_markup=saved_data_keyboard("phone", rows))
        else:
            await q.message.reply_text("شماره حذف شد. شماره موبایل خودت را با دکمه زیر ارسال کن:", reply_markup=phone_keyboard())
        return ASK_PHONE
    return ASK_PHONE


async def ask_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gender_text = (update.message.text or "").strip()
    if gender_text == "🔙 مرحله قبلی":
        await show_saved_step(update.message, context, "phone")
        return ASK_PHONE
    gender_map = {"👨 مرد": "مرد", "👩 زن": "زن"}
    gender = gender_map.get(gender_text)
    if not gender:
        await update.message.reply_text(
            "لطفاً یکی از گزینه‌های «👨 مرد» یا «👩 زن» را انتخاب کن.",
            reply_markup=gender_keyboard(),
        )
        return ASK_GENDER

    context.user_data["gender"] = gender
    # اگر آدرس ذخیره‌شده داریم، پیام بعدی فقط دکمه شیشه‌ای دارد؛ پس کیبورد مرد/زن
    # را اینجا جمع می‌کنیم تا روی صفحه نماند.
    if get_saved_data(update.effective_user.id, "address"):
        await update.message.reply_text("✅ جنسیت ثبت شد.", reply_markup=ReplyKeyboardRemove())
        await show_saved_step(update.message, context, "address")
        return ASK_ADDRESS
    await update.message.reply_text("آدرس کامل برای ارسال رو بفرست:", reply_markup=address_input_keyboard())
    return ASK_ADDRESS


def address_input_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🔙 مرحله قبلی")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def handle_saved_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    action = parts[2]
    user_id = q.from_user.id
    if action == "back":
        await show_saved_step(q.message, context, "phone")
        return ASK_PHONE
    if action == "use":
        row = next((r for r in get_saved_data(user_id, "address") if r["id"] == int(parts[3])), None)
        if row:
            context.user_data["address"] = row["value"]
            return await finalize_order_from_callback(q.message, context, q.from_user)
    elif action == "new":
        context.user_data.pop("editing_saved_address_id", None)
        await q.message.reply_text("آدرس جدید را بفرست:", reply_markup=address_input_keyboard())
        return ASK_ADDRESS
    elif action == "edit":
        context.user_data["editing_saved_address_id"] = parts[3]
        await q.message.reply_text("آدرس جدید را بفرست:", reply_markup=address_input_keyboard())
        return ASK_ADDRESS
    elif action == "delete":
        delete_saved_data(user_id, int(parts[3]), "address")
        rows = get_saved_data(user_id, "address")
        if rows:
            await q.message.reply_text("آدرس حذف شد. یک آدرس را انتخاب کن یا آدرس جدید اضافه کن:", reply_markup=saved_data_keyboard("address", rows))
        else:
            await q.message.reply_text("آدرس حذف شد. آدرس کامل برای ارسال را بفرست:", reply_markup=address_input_keyboard())
        return ASK_ADDRESS
    return ASK_ADDRESS


async def finalize_order_from_callback(message, context, user=None):
    return await create_pending_payment(message, context, user)


async def finalize_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (update.message.text or "").strip() == "🔙 مرحله قبلی":
        await show_saved_step(update.message, context, "phone")
        return ASK_PHONE
    user_id = update.effective_user.id
    address = re.sub(r"\s+", " ", update.message.text.strip())
    edit_id = context.user_data.get("editing_saved_address_id")
    if saved_value_exists(user_id, "address", address, int(edit_id) if edit_id else None):
        await update.message.reply_text(
            "⚠️ این آدرس را قبلاً ثبت کرده‌ای.\n"
            "لطفاً آدرس دیگری وارد کن.",
            reply_markup=address_input_keyboard(),
        )
        return ASK_ADDRESS

    edit_id = context.user_data.pop("editing_saved_address_id", None)
    if edit_id:
        update_saved_data(user_id, int(edit_id), "address", address)
    else:
        save_saved_data(user_id, "address", address)
    context.user_data["address"] = address
    return await create_pending_payment(update.message, context)


async def send_order_sms_to_admin(pending_id, full_name, phone, total_price, items_summary):
    """ارسال پیامک سفارش جدید به مدیر و بررسی واقعی نتیجه API."""
    missing = []
    if not MELIPAYAMAK_USERNAME:
        missing.append("MELIPAYAMAK_USERNAME")
    if not MELIPAYAMAK_API_KEY:
        missing.append("MELIPAYAMAK_API_KEY/MELIPAYAMAK_PASSWORD")
    if not MELIPAYAMAK_SENDER:
        missing.append("MELIPAYAMAK_SENDER")
    if not SMS_ADMIN_PHONE:
        missing.append("SMS_ADMIN_PHONE")

    if missing:
        logger.error(
            "ORDER SMS SKIPPED for #%s: missing %s",
            pending_id, ", ".join(missing)
        )
        return False

    clean_items = re.sub(r"\s+", " ", str(items_summary or "-")).strip()
    if len(clean_items) > 180:
        clean_items = clean_items[:177] + "..."

    sms_text = (
        f"تنین ایران | سفارش جدید #{pending_id}\n"
        f"مشتری: {full_name or '-'}\n"
        f"موبایل: {phone or '-'}\n"
        f"مبلغ: {int(total_price):,} تومان\n"
        f"محصولات: {clean_items}"
    )

    payload = {
        "username": MELIPAYAMAK_USERNAME,
        "password": MELIPAYAMAK_API_KEY,
        "to": SMS_ADMIN_PHONE,
        "from": MELIPAYAMAK_SENDER,
        "text": sms_text,
        "isFlash": False,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                MELIPAYAMAK_URL,
                data=payload,
                headers={"Accept": "application/json, text/plain, */*"},
            ) as response:
                body = (await response.text()).strip()

                logger.info(
                    "Melipayamak response for order #%s: HTTP=%s BODY=%s",
                    pending_id, response.status, body[:1000]
                )

                if response.status >= 400:
                    logger.error(
                        "ORDER SMS FAILED for #%s: HTTP %s",
                        pending_id, response.status
                    )
                    return False

                try:
                    result = json.loads(body)
                except (json.JSONDecodeError, TypeError):
                    logger.error(
                        "ORDER SMS FAILED for #%s: invalid API response: %s",
                        pending_id, body[:1000]
                    )
                    return False

                if not isinstance(result, dict):
                    logger.error(
                        "ORDER SMS FAILED for #%s: unexpected API response: %s",
                        pending_id, body[:1000]
                    )
                    return False

                ret_status = result.get("RetStatus", result.get("retStatus"))
                str_status = result.get("StrRetStatus", result.get("strRetStatus", ""))
                rec_id = result.get("Value", result.get("value", ""))

                try:
                    ret_status = int(ret_status)
                except (TypeError, ValueError):
                    ret_status = -1

                if ret_status != 1:
                    logger.error(
                        "ORDER SMS REJECTED for #%s: RetStatus=%s StrRetStatus=%s Value=%s",
                        pending_id, ret_status, str_status, rec_id
                    )
                    return False

                logger.info(
                    "ORDER SMS ACCEPTED for #%s: RecID=%s Status=%s",
                    pending_id, rec_id, str_status
                )
                return True

    except asyncio.TimeoutError:
        logger.error("ORDER SMS TIMEOUT for #%s", pending_id)
        return False
    except aiohttp.ClientError as e:
        logger.error("ORDER SMS NETWORK ERROR for #%s: %s", pending_id, e)
        return False
    except Exception:
        logger.exception("ORDER SMS UNEXPECTED ERROR for #%s", pending_id)
        return False


async def create_pending_payment(message, context, user=None):
    user_id = message.chat_id
    username = (user.username if user is not None else message.from_user.username) or ""
    conn = get_conn()
    now = iran_now_naive().strftime("%Y-%m-%d %H:%M")
    full_name = context.user_data["full_name"].strip()
    phone = context.user_data["phone"].strip()
    address = context.user_data["address"].strip()
    gender = context.user_data["gender"]
    conn.execute(
        "UPDATE customers SET phone=%s, gender=%s, full_name=%s, username=%s, last_seen=%s WHERE user_id=%s",
        (phone, gender, full_name, username, now, user_id),
    )
    rows = conn.execute(
        """SELECT products.id AS product_id, products.name, products.price,
                  cart_items.qty, products.pack_info
           FROM cart_items JOIN products ON cart_items.product_id = products.id
           WHERE cart_items.user_id=%s AND products.active=TRUE""", (user_id,)
    ).fetchall()
    if not rows:
        conn.close()
        await message.reply_text("🛒 سبد خریدت خالی شده. دوباره محصولاتت رو انتخاب کن.", reply_markup=main_menu_keyboard(user_id))
        return ConversationHandler.END

    total = sum(r["price"] * r["qty"] for r in rows)
    summary = "\n".join(
        f"{r['name']}" + (f" ({r['pack_info']})" if r['pack_info'] else "") + f" × {r['qty']}"
        for r in rows
    )
    # فقط یک پرداخت باز برای هر مشتری؛ اگر قبلی هنوز منتظر رسید/بررسی است همان را نشان می‌دهیم.
    pending = conn.execute(
        """SELECT * FROM pending_payments WHERE user_id=%s
           AND payment_status IN ('در انتظار رسید','در انتظار بررسی')
           ORDER BY id DESC LIMIT 1""", (user_id,)
    ).fetchone()
    if pending:
        if pending["payment_status"] == "در انتظار رسید":
            # هنوز رسیدی ارسال نشده؛ اطلاعات و مبلغ را با سبد فعلی هماهنگ می‌کنیم.
            conn.execute(
                """UPDATE pending_payments
                   SET full_name=%s, phone=%s, address=%s, items_summary=%s, total_price=%s
                   WHERE id=%s AND payment_status='در انتظار رسید'""",
                (full_name, phone, address, summary, total, pending["id"]),
            )
            conn.commit()
            pending = dict(pending)
            pending["total_price"] = total
        conn.close()
        for key in ("full_name", "phone", "address", "gender"):
            context.user_data.pop(key, None)
        await show_payment_instructions(message, pending)
        return ConversationHandler.END

    cur = conn.execute(
        """INSERT INTO pending_payments
           (user_id, full_name, phone, address, items_summary, total_price, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (user_id, full_name, phone, address, summary, total, now),
    )
    pending_id = cur.fetchone()["id"]
    conn.commit()
    conn.close()

    # اطلاع‌رسانی پیامکی به مدیر؛ در صورت خطا، ثبت سفارش ادامه پیدا می‌کند.
    await send_order_sms_to_admin(
        pending_id=pending_id,
        full_name=full_name,
        phone=phone,
        total_price=total,
        items_summary=summary,
    )

    context.user_data.pop("full_name", None)
    context.user_data.pop("phone", None)
    context.user_data.pop("address", None)
    context.user_data.pop("gender", None)

    await show_payment_instructions(message, {
        "id": pending_id, "total_price": total, "payment_status": "در انتظار رسید"
    })
    return ConversationHandler.END


async def show_payment_instructions(message, pending):
    conn = get_conn()
    settings = conn.execute("SELECT * FROM bank_accounts WHERE active=TRUE ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not settings or not any(settings[k] for k in ("bank_name", "owner_name", "card_number", "account_number", "iban")):
        await message.reply_text("⚠️ اطلاعات حساب پرداخت هنوز توسط مدیریت تنظیم نشده است. لطفاً کمی بعد دوباره تلاش کنید.")
        return
    def v(key): return escape(settings[key] or "-")
    total = int(pending["total_price"])
    text = (
        "💳 <b>پرداخت کارت‌به‌کارت</b>\n\n"
        f"💰 مبلغ قابل پرداخت: <b>{total:,} تومان</b>\n\n"
        "لطفاً مبلغ دقیق را به حساب زیر واریز کنید:\n\n"
        f"🏦 بانک: <b>{v('bank_name')}</b>\n"
        f"👤 به نام: <b>{v('owner_name')}</b>\n"
        f"💳 شماره کارت: <code>{v('card_number')}</code>\n"
        f"🏦 شماره حساب: <code>{v('account_number')}</code>\n"
        f"🔹 شماره شبا: <code>{v('iban')}</code>\n\n"
        "بعد از واریز، عکس رسید را همینجا ارسال کنید.\n"
        "🟡 سفارش تا تأیید پرداخت توسط مدیریت نهایی نمی‌شود."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 شماره کارت", callback_data=f"pay:copycard:{pending['id']}")],
                               [InlineKeyboardButton("📤 راهنمای ارسال رسید", callback_data=f"pay:receipt:{pending['id']}")]])
    await message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def cancel_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ثبت سفارش لغو شد.", reply_markup=main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# ----------------------------------------------------------------------------
# سفارش‌های من / پشتیبانی
# ----------------------------------------------------------------------------
def customer_orders_keyboard(rows):
    buttons = []
    for o in rows:
        order_date = format_iran_jalali_fa(o["created_at"], include_time=False)
        buttons.append([
            InlineKeyboardButton(
                f"📦 سفارش #{o['id']} | {order_date}",
                callback_data=f"customer_order:view:{o['id']}"
            )
        ])
    buttons.append([InlineKeyboardButton("🔙 بستن", callback_data="customer_order:close")])
    return InlineKeyboardMarkup(buttons)


def customer_order_detail_text(o):
    finalized = o.get("finalized_at") or o.get("created_at")
    return (
        f"📦 <b>جزئیات سفارش #{o['id']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📅 تاریخ سفارش: <b>{escape(format_iran_jalali_fa(o.get('created_at'), include_time=True))}</b>\n"
        f"✅ نهایی شده در: <b>{escape(format_iran_jalali_fa(finalized, include_time=True))}</b>\n"
        f"📌 وضعیت: <b>{escape(str(o['status'] or '-'))}</b>\n"
        f"💳 وضعیت پرداخت: <b>{escape(str(o['payment_status'] or '-'))}</b>\n\n"
        f"👤 نام: {escape(str(o['full_name'] or '-'))}\n"
        f"📞 شماره تماس: {escape(str(o['phone'] or '-'))}\n"
        f"📍 آدرس: {escape(str(o['address'] or '-'))}\n\n"
        f"🛍 <b>محصولات:</b>\n{escape(str(o['items_summary'] or '-'))}\n\n"
        f"💰 مبلغ: <b>{int(o['total_price'] or 0):,} تومان</b>"
    )


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, created_at FROM orders WHERE user_id=%s ORDER BY id DESC", (user_id,)
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("هنوز سفارشی ثبت نکردی.")
        return

    await update.message.reply_text(
        "📦 <b>سفارش‌های من</b>\n\nسفارش موردنظرت رو انتخاب کن:",
        parse_mode="HTML",
        reply_markup=customer_orders_keyboard(rows),
    )


async def customer_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "close":
        await q.answer()
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text("منوی سفارش‌ها بسته شد.")
        return

    if action == "list":
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, created_at FROM orders WHERE user_id=%s ORDER BY id DESC", (q.from_user.id,)
        ).fetchall()
        conn.close()
        if not rows:
            await q.answer()
            await q.edit_message_text("هنوز سفارشی ثبت نکردی.")
            return
        await q.answer()
        await q.edit_message_text(
            "📦 <b>سفارش‌های من</b>\n\nسفارش موردنظرت رو انتخاب کن:",
            parse_mode="HTML",
            reply_markup=customer_orders_keyboard(rows),
        )
        return

    if action != "view" or len(parts) != 3:
        await q.answer("درخواست نامعتبر است.", show_alert=True)
        return

    try:
        order_id = int(parts[2])
    except ValueError:
        await q.answer("شماره سفارش نامعتبر است.", show_alert=True)
        return

    conn = get_conn()
    order = conn.execute(
        "SELECT * FROM orders WHERE id=%s AND user_id=%s", (order_id, q.from_user.id)
    ).fetchone()
    conn.close()

    if not order:
        await q.answer("این سفارش برای حساب شما پیدا نشد.", show_alert=True)
        return

    await q.answer()
    await q.edit_message_text(
        customer_order_detail_text(order),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 بازگشت به سفارش‌ها", callback_data="customer_order:list")]
        ]),
    )

    return


def support_center_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 پیگیری سفارش", callback_data="support:orders")],
        [InlineKeyboardButton("💳 مشکل در پرداخت", callback_data="support:topic:پرداخت")],
        [InlineKeyboardButton("🛍 سؤال درباره محصول", callback_data="support:topic:محصول")],
        [InlineKeyboardButton("🚚 سؤال درباره ارسال", callback_data="support:topic:ارسال")],
        [InlineKeyboardButton("🔄 تعویض / مرجوعی", callback_data="support:topic:تعویض")],
        [InlineKeyboardButton("📝 ارسال درخواست پشتیبانی", callback_data="support:new")],
        [InlineKeyboardButton("🎫 درخواست‌های من", callback_data="support:mine")],
        [InlineKeyboardButton("❓ سؤالات متداول", callback_data="support:faq")],
        [InlineKeyboardButton("💬 چت مستقیم با پشتیبانی", url="https://t.me/tanin_modir")],
        [InlineKeyboardButton("📞 شماره تماس پشتیبانی", callback_data="support:phone")],
    ])


def support_topic_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 پرداخت", callback_data="support:topic:پرداخت")],
        [InlineKeyboardButton("🛍 محصول", callback_data="support:topic:محصول")],
        [InlineKeyboardButton("🚚 ارسال", callback_data="support:topic:ارسال")],
        [InlineKeyboardButton("🔄 تعویض / مرجوعی", callback_data="support:topic:تعویض")],
        [InlineKeyboardButton("❓ سایر", callback_data="support:topic:سایر")],
        [InlineKeyboardButton("🔙 مرکز پشتیبانی", callback_data="support:home")],
    ])


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💬 <b>مرکز پشتیبانی تنین ایران</b>\n\n"
        "از این بخش می‌تونی سفارش‌هات رو پیگیری کنی، درخواست پشتیبانی ثبت کنی یا مستقیم با ما در ارتباط باشی.",
        parse_mode="HTML", reply_markup=support_center_keyboard()
    )


def support_ticket_status_fa(status):
    return {
        "new": "🔴 جدید",
        "admin_waiting": "🟡 در انتظار پاسخ ادمین",
        "customer_waiting": "🔵 در انتظار پاسخ مشتری",
        "closed": "🟢 بسته‌شده",
    }.get(status, status or "-")


def support_ticket_list_keyboard(rows, prefix="support:view"):
    buttons = []
    for t in rows:
        buttons.append([InlineKeyboardButton(
            f"🎫 #{t['id']} | {t['topic']} | {support_ticket_status_fa(t['status'])}",
            callback_data=f"{prefix}:{t['id']}"
        )])
    return InlineKeyboardMarkup(buttons)


async def support_orders_view(q):
    conn=get_conn()
    rows=conn.execute("SELECT id, created_at, status FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT 20", (q.from_user.id,)).fetchall()
    conn.close()
    if not rows:
        await q.edit_message_text("📦 هنوز سفارشی برای این حساب ثبت نشده.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 مرکز پشتیبانی", callback_data="support:home")]]))
        return
    buttons=[[InlineKeyboardButton(f"📦 سفارش #{o['id']} | {format_iran_jalali_fa(o.get('created_at'), False)} | {o['status']}", callback_data=f"customer_order:view:{o['id']}")] for o in rows]
    buttons.append([InlineKeyboardButton("🔙 مرکز پشتیبانی", callback_data="support:home")])
    await q.edit_message_text("📦 <b>سفارش‌های شما</b>\n\nبرای مشاهده جزئیات، یک سفارش را انتخاب کن:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def support_faq_view(q):
    text=(
        "❓ <b>سؤالات متداول</b>\n\n"
        "🛒 <b>ثبت سفارش:</b> محصول را از کاتالوگ انتخاب کن و مراحل ثبت سفارش را کامل کن.\n\n"
        "💳 <b>پرداخت:</b> پرداخت به‌صورت کارت‌به‌کارت انجام می‌شود و بعد از بررسی رسید، سفارش ثبت نهایی می‌شود.\n\n"
        "🚚 <b>ارسال:</b> زمان و روش ارسال پس از ثبت سفارش با مشتری هماهنگ می‌شود.\n\n"
        "🔄 <b>تعویض / مرجوعی:</b> برای بررسی شرایط، از طریق تیکت یا ارتباط مستقیم با پشتیبانی درخواستت را ثبت کن.\n\n"
        "📦 <b>پیگیری سفارش:</b> از گزینه «📦 پیگیری سفارش» در همین بخش استفاده کن."
    )
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 مرکز پشتیبانی", callback_data="support:home")]]))


async def support_mine_view(q):
    conn=get_conn()
    rows=conn.execute("SELECT * FROM support_tickets WHERE user_id=%s ORDER BY id DESC LIMIT 30", (q.from_user.id,)).fetchall()
    conn.close()
    if not rows:
        await q.edit_message_text("🎫 هنوز تیکتی ثبت نکرده‌ای.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 ثبت درخواست جدید", callback_data="support:new")],[InlineKeyboardButton("🔙 مرکز پشتیبانی", callback_data="support:home")]]))
        return
    kb=support_ticket_list_keyboard(rows)
    buttons=list(kb.inline_keyboard)+[[InlineKeyboardButton("🔙 مرکز پشتیبانی", callback_data="support:home")]]
    await q.edit_message_text("🎫 <b>درخواست‌های شما</b>\n\nبرای مشاهده و ادامه هر تیکت، آن را انتخاب کن:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def support_ticket_detail(q, ticket_id, is_admin_view=False):
    conn=get_conn()
    if is_admin_view:
        t=conn.execute("""SELECT t.*, c.full_name, c.username
                           FROM support_tickets t
                           LEFT JOIN customers c ON c.user_id=t.user_id
                           WHERE t.id=%s""", (ticket_id,)).fetchone()
    else:
        t=conn.execute("SELECT * FROM support_tickets WHERE id=%s AND user_id=%s", (ticket_id,q.from_user.id)).fetchone()
    msgs=conn.execute("SELECT * FROM support_ticket_messages WHERE ticket_id=%s ORDER BY id ASC", (ticket_id,)).fetchall() if t else []
    conn.close()
    if not t:
        await q.answer("تیکت پیدا نشد.", show_alert=True); return
    lines=[f"🎫 <b>تیکت #{t['id']}</b>",f"🏷 موضوع: {escape(t['topic'])}",f"📌 وضعیت: {support_ticket_status_fa(t['status'])}",f"🕐 ثبت: {format_iran_jalali_fa(t.get('created_at'), True)}"]
    if is_admin_view:
        lines += [
            f"👤 مشتری: {escape(t.get('full_name') or '-')}",
            f"🆔 آیدی مشتری: <code>{t['user_id']}</code>",
            f"📱 تلگرام: @{escape(t.get('username') or '-')}",
        ]
        if t.get('order_id'): lines.append(f"📦 سفارش: #{t['order_id']}")
    lines.append("\n<b>💬 گفتگو:</b>")
    for m in msgs[-15:]:
        who="مشتری" if m['sender_type']=='customer' else "ادمین"
        lines.append(f"<b>{who}:</b> {escape(m['message'])}")
    if is_admin_view:
        buttons=[]
        if t['status']!="closed": buttons.append([InlineKeyboardButton("💬 پاسخ به مشتری", callback_data=f"adm:ticket_reply:{ticket_id}")])
        buttons.append([InlineKeyboardButton("🟢 بستن تیکت", callback_data=f"adm:ticket_close:{ticket_id}")])
        buttons.append([InlineKeyboardButton("👤 مشاهده مشتری", callback_data=f"adm:customer:{t['user_id']}")])
        buttons.append([InlineKeyboardButton("🔙 تیکت‌ها", callback_data="adm:tickets")])
    else:
        buttons=[]
        if t['status']!="closed": buttons.append([InlineKeyboardButton("💬 ارسال پاسخ", callback_data=f"support:reply:{ticket_id}")])
        buttons.append([InlineKeyboardButton("🔙 درخواست‌های من", callback_data="support:mine")])
    await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    parts=q.data.split(":",2)
    action=parts[1] if len(parts)>1 else "home"
    await q.answer()
    if action=="home":
        await q.edit_message_text("💬 <b>مرکز پشتیبانی تنین ایران</b>\n\nیک گزینه را انتخاب کن:",parse_mode="HTML",reply_markup=support_center_keyboard()); return
    if action=="orders": await support_orders_view(q); return
    if action=="faq": await support_faq_view(q); return
    if action=="phone":
        await q.message.reply_text("📞 شماره تماس پشتیبانی:\n09384853486")
        return
    if action=="mine": await support_mine_view(q); return
    if action=="new":
        context.user_data["support_flow"]={"type":"new","step":"topic"}
        await q.message.reply_text("📝 <b>ثبت درخواست پشتیبانی</b>\n\nموضوع درخواستت را انتخاب کن:",parse_mode="HTML",reply_markup=support_topic_keyboard()); return
    if action=="topic":
        topic=parts[2] if len(parts)>2 else "سایر"
        flow=context.user_data.get("support_flow",{})
        flow.update({"type":"new","step":"message","topic":topic})
        context.user_data["support_flow"]=flow
        await q.message.reply_text(f"🏷 موضوع: <b>{escape(topic)}</b>\n\nحالا متن درخواستت را بفرست:",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data="support:home")]])); return
    if action=="reply":
        ticket_id=int(parts[2]); context.user_data["support_flow"]={"type":"reply","ticket_id":ticket_id}
        await q.message.reply_text("💬 پیام جدیدت را برای این تیکت بفرست:"); return
    if action=="view": await support_ticket_detail(q,int(parts[2]),False); return




# ----------------------------------------------------------------------------
# پنل مدیریت کامل: محصولات + سفارش‌ها + فروش + مشتری‌ها + پرداخت
# ----------------------------------------------------------------------------
def is_admin(user_id):
    return user_id in ADMIN_IDS


async def admin_dashboard_stats():
    """Return small, fast counters used only by the admin dashboard."""
    conn = get_conn()
    try:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM pending_payments WHERE payment_status='در انتظار بررسی'"
        ).fetchone()["n"]
        orders = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE status='در انتظار بررسی'"
        ).fetchone()["n"]
        tickets = conn.execute(
            "SELECT COUNT(*) AS n FROM support_tickets WHERE status IN ('new','admin_waiting')"
        ).fetchone()["n"]
        products = conn.execute(
            "SELECT COUNT(*) AS n FROM products WHERE active=TRUE"
        ).fetchone()["n"]
        customers = conn.execute(
            "SELECT COUNT(*) AS n FROM customers"
        ).fetchone()["n"]
        return {
            "pending": pending or 0,
            "orders": orders or 0,
            "tickets": tickets or 0,
            "products": products or 0,
            "customers": customers or 0,
        }
    finally:
        conn.close()


def admin_panel_keyboard():
    """Admin-only navigation. Customer shopping actions never appear here."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💳 پرداخت‌های در انتظار", callback_data="adm:pending_payments"),
            InlineKeyboardButton("📦 سفارش‌ها", callback_data="adm:orders"),
        ],
        [
            InlineKeyboardButton("🛠 مدیریت محصولات", callback_data="adm:products"),
            InlineKeyboardButton("👥 مشتری‌ها", callback_data="adm:customers"),
        ],
        [
            InlineKeyboardButton("📊 فروش و آمار", callback_data="adm:sales"),
            InlineKeyboardButton("🎫 پشتیبانی", callback_data="adm:tickets"),
        ],
        [
            InlineKeyboardButton("📢 مدیریت کانال", callback_data="adm:channel"),
            InlineKeyboardButton("💳 تنظیمات پرداخت", callback_data="adm:payment"),
        ],
        [
            InlineKeyboardButton("🔄 بروزرسانی داشبورد", callback_data="adm:home"),
            InlineKeyboardButton("✕ بستن پنل", callback_data="adm:close"),
        ],
    ])


async def admin_dashboard_text():
    stats = await admin_dashboard_stats()
    return (
        "⚙️ <b>داشبورد مدیریت تنین ایران</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🔔 <b>وضعیت نیازمند اقدام</b>\n"
        f"💳 پرداخت در انتظار بررسی: <b>{stats['pending']}</b>\n"
        f"📦 سفارش در انتظار بررسی: <b>{stats['orders']}</b>\n"
        f"🎫 تیکت جدید/نیازمند پاسخ: <b>{stats['tickets']}</b>\n\n"
        "📊 <b>وضعیت فروشگاه</b>\n"
        f"🛠 محصولات فعال: {stats['products']}  |  👥 مشتری‌ها: {stats['customers']}\n\n"
        "از منوی زیر بخش موردنظر را انتخاب کن:"
    )


def back_keyboard(target="home"):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"adm:{target}")]]
    )


def order_status_keyboard(order_id, status):
    rows = []
    if status == "در انتظار بررسی":
        rows.append([InlineKeyboardButton("✅ تأیید سفارش", callback_data=f"order:status:{order_id}:confirmed")])
        rows.append([InlineKeyboardButton("❌ رد سفارش", callback_data=f"order:status:{order_id}:cancelled")])
    rows.append([InlineKeyboardButton("🔙 لیست سفارش‌ها", callback_data="adm:orders")])
    return InlineKeyboardMarkup(rows)


def order_text(o):
    return (
        f"📦 <b>سفارش #{o['id']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"👤 مشتری: {escape(str(o['full_name'] or '-'))}\n"
        f"📞 تلفن: {escape(str(o['phone'] or '-'))}\n"
        f"📍 آدرس: {escape(str(o['address'] or '-'))}\n"
        f"🕐 زمان: {escape(format_iran_jalali_fa(o.get('created_at'), include_time=True))}\n\n"
        f"🛍 <b>محصولات:</b>\n{escape(str(o['items_summary'] or '-'))}\n\n"
        f"💰 مبلغ: <b>{o['total_price']:,} تومان</b>\n"
        f"💳 وضعیت پرداخت: <b>{escape(str(o['payment_status'] or '-'))}</b>\n"
        f"📌 وضعیت: <b>{escape(str(o['status']))}</b>"
    )


def product_text(p):
    state = "فعال ✅" if p["active"] else "غیرفعال ⛔"
    pack = f"پک: {escape(p['pack_info'])}\n" if p['pack_info'] else ""
    return (
        f"🛍 <b>{escape(p['name'])}</b>\n"
        f"شناسه: #{p['id']}\n"
        f"دسته: {escape(p['category'])}\n"
        f"سایز: {escape(p['size'] or '-')}\n"
        f"رنگ‌بندی: {escape(p.get('color') or '-')}\n"
        f"{pack}"
        f"قیمت: {p['price']:,} تومان\n"
        f"وضعیت: {state}"
    )


def admin_product_keyboard(product_id, active, show_quick_edit=False, show_price_adjust=False):
    toggle = "غیرفعال کردن" if active else "فعال کردن"
    toggle_action = "deactivate" if active else "activate"
    rows = [[InlineKeyboardButton(
        "⚡ بستن ویرایش سریع" if show_quick_edit else "⚡ ویرایش سریع",
        callback_data=f"adm:quick_toggle:{product_id}:{0 if show_quick_edit else 1}"
    )]]
    if show_quick_edit:
        rows.extend([
            [InlineKeyboardButton("📝 ویرایش سریع نام", callback_data=f"adm:quick_field:{product_id}:name")],
            [InlineKeyboardButton("📏 ویرایش سریع سایز", callback_data=f"adm:quick_field:{product_id}:size")],
            [InlineKeyboardButton("🎨 ویرایش سریع رنگ‌بندی", callback_data=f"adm:quick_field:{product_id}:color")],
            [InlineKeyboardButton("📦 ویرایش سریع پک", callback_data=f"adm:quick_field:{product_id}:pack_info")],
            [InlineKeyboardButton(
                "💰 بستن گزینه‌های قیمت" if show_price_adjust else "💰 ویرایش سریع قیمت",
                callback_data=f"adm:price_quick_toggle:{product_id}:{0 if show_price_adjust else 1}"
            )],
        ])
        if show_price_adjust:
            rows.extend([
                [InlineKeyboardButton("➖۵۰", callback_data=f"adm:price_adjust:{product_id}:-50000"), InlineKeyboardButton("➕۵۰", callback_data=f"adm:price_adjust:{product_id}:50000"), InlineKeyboardButton("➖۱۰۰", callback_data=f"adm:price_adjust:{product_id}:-100000"), InlineKeyboardButton("➕۱۰۰", callback_data=f"adm:price_adjust:{product_id}:100000")],
                [InlineKeyboardButton("➖۲۰۰", callback_data=f"adm:price_adjust:{product_id}:-200000"), InlineKeyboardButton("➕۲۰۰", callback_data=f"adm:price_adjust:{product_id}:200000"), InlineKeyboardButton("➖۳۰۰", callback_data=f"adm:price_adjust:{product_id}:-300000"), InlineKeyboardButton("➕۳۰۰", callback_data=f"adm:price_adjust:{product_id}:300000")],
                [InlineKeyboardButton("➖۴۰۰", callback_data=f"adm:price_adjust:{product_id}:-400000"), InlineKeyboardButton("➕۴۰۰", callback_data=f"adm:price_adjust:{product_id}:400000"), InlineKeyboardButton("➖۵۰۰", callback_data=f"adm:price_adjust:{product_id}:-500000"), InlineKeyboardButton("➕۵۰۰", callback_data=f"adm:price_adjust:{product_id}:500000")],
            ])
        rows.append([InlineKeyboardButton("💵 ثبت قیمت دلخواه", callback_data=f"adm:price_set:{product_id}")])
    rows.extend([
        [InlineKeyboardButton(f"⛔ {toggle}", callback_data=f"adm:toggle_product:{product_id}:{toggle_action}")],
        [InlineKeyboardButton("🗑 حذف", callback_data=f"adm:delete_product:{product_id}")],
        [InlineKeyboardButton("🔙 محصولات", callback_data="adm:products")],
    ])
    return InlineKeyboardMarkup(rows)


def products_keyboard(rows):
    buttons = [[InlineKeyboardButton("➕ افزودن محصول", callback_data="adm:add_product")]]
    for p in rows:
        buttons.append([InlineKeyboardButton(f"#{p['id']} | {p['name']}", callback_data=f"adm:product:{p['id']}")])
    buttons.append([InlineKeyboardButton("🔙 پنل اصلی", callback_data="adm:home")])
    return InlineKeyboardMarkup(buttons)


async def admin_tickets_view(q, status_filter="all"):
    conn=get_conn()
    if status_filter=="all":
        rows=conn.execute("SELECT t.*, c.username, c.full_name FROM support_tickets t LEFT JOIN customers c ON c.user_id=t.user_id ORDER BY t.id DESC LIMIT 50").fetchall()
    else:
        rows=conn.execute("SELECT t.*, c.username, c.full_name FROM support_tickets t LEFT JOIN customers c ON c.user_id=t.user_id WHERE t.status=%s ORDER BY t.id DESC LIMIT 50",(status_filter,)).fetchall()
    counts=conn.execute("SELECT status, COUNT(*) AS n FROM support_tickets GROUP BY status").fetchall()
    conn.close()
    count={r['status']:r['n'] for r in counts}
    buttons=[]
    for t in rows:
        name=(t.get('full_name') or t.get('username') or str(t['user_id']))[:18]
        buttons.append([InlineKeyboardButton(f"🎫 #{t['id']} | {name} | {t['topic']} | {support_ticket_status_fa(t['status'])}",callback_data=f"adm:ticket:{t['id']}")])
    buttons += [
        [InlineKeyboardButton(f"🔴 جدید ({count.get('new',0)})",callback_data="adm:tickets:new"),InlineKeyboardButton(f"🟡 پاسخ ادمین ({count.get('admin_waiting',0)})",callback_data="adm:tickets:admin_waiting")],
        [InlineKeyboardButton(f"🔵 پاسخ مشتری ({count.get('customer_waiting',0)})",callback_data="adm:tickets:customer_waiting"),InlineKeyboardButton(f"🟢 بسته‌شده ({count.get('closed',0)})",callback_data="adm:tickets:closed")],
        [InlineKeyboardButton("📊 همه تیکت‌ها",callback_data="adm:tickets:all")],
        [InlineKeyboardButton("🔙 پنل اصلی",callback_data="adm:home")]
    ]
    title="🎫 <b>تیکت‌های پشتیبانی</b>"
    await q.edit_message_text(title,parse_mode="HTML",reply_markup=InlineKeyboardMarkup(buttons))


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("دسترسی نداری.")
        return
    await update.message.reply_text(
        await admin_dashboard_text(),
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard(),
    )


class AdminMessageViewAdapter:
    """سازگارکننده برای نمایش صفحه‌های ادمین با دکمه‌های ReplyKeyboard."""
    def __init__(self, update):
        self.message = update.effective_message
        self.from_user = update.effective_user

    async def edit_message_text(self, text, **kwargs):
        # پیام جدید می‌فرستیم؛ پیام دریافتی از کیبورد قابل ویرایش نیست.
        await self.message.reply_text(text, **kwargs)

    async def answer(self, *args, **kwargs):
        return None


async def admin_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("دسترسی نداری.")
        return

    text = (update.effective_message.text or "").strip()
    # بخش عددیِ نشانگر را حذف می‌کنیم تا دکمه‌های دارای شمارنده هم شناسایی شوند.
    text = re.sub(r"\s+\(\d+\)$", "", text)
    q = AdminMessageViewAdapter(update)
    if text == "📦 سفارش‌ها":
        await admin_orders_view(q, "new")
    elif text == "💳 پرداخت‌های در انتظار":
        await admin_pending_payments_view(q)
    elif text == "🛠 مدیریت محصولات":
        await admin_products_view(q)
    elif text == "🎫 پشتیبانی":
        await admin_tickets_view(q, "all")


async def admin_pending_payments_view(q):
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM pending_payments
           WHERE payment_status='در انتظار بررسی'
           ORDER BY id DESC LIMIT 50"""
    ).fetchall()
    conn.close()

    if not rows:
        await q.edit_message_text(
            "🟡 <b>پرداخت‌های در انتظار تأیید</b>\n\nموردی برای بررسی وجود ندارد.",
            parse_mode="HTML",
            reply_markup=back_keyboard("home"),
        )
        return

    buttons = []
    for p in rows:
        date_text = format_iran_jalali_fa(p.get("created_at"), include_time=False)
        name = (p.get("full_name") or "-")[:24]
        amount = f"{p.get('total_price') or 0:,}"
        buttons.append([
            InlineKeyboardButton(
                f"💳 #{p['id']} | {name} | {amount} تومان | {date_text}",
                callback_data=f"adm:pending:{p['id']}",
            )
        ])

    buttons.append([InlineKeyboardButton("🔄 بروزرسانی", callback_data="adm:pending_payments")])
    buttons.append([InlineKeyboardButton("🔙 پنل اصلی", callback_data="adm:home")])
    text = (
        "🟡 <b>پرداخت‌های در انتظار تأیید</b>\n\n"
        "روی هر مورد بزنید تا جزئیات و رسید پرداخت نمایش داده شود."
    )
    markup = InlineKeyboardMarkup(buttons)
    if q.message and (q.message.photo or q.message.document):
        await q.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


async def admin_pending_payment_detail(q, pending_id):
    conn = get_conn()
    pending = conn.execute(
        "SELECT * FROM pending_payments WHERE id=%s", (pending_id,)
    ).fetchone()
    conn.close()

    if not pending or pending.get("payment_status") != "در انتظار بررسی":
        await q.answer("این پرداخت دیگر در انتظار بررسی نیست.", show_alert=True)
        await admin_pending_payments_view(q)
        return

    text = (
        f"🟡 <b>پرداخت در انتظار تأیید #{pending['id']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"👤 مشتری: {escape(pending.get('full_name') or '-')}\n"
        f"📞 تلفن: {escape(pending.get('phone') or '-')}\n"
        f"📍 آدرس: {escape(pending.get('address') or '-')}\n"
        f"🕐 زمان ثبت: {escape(format_iran_jalali_fa(pending.get('created_at'), include_time=True))}\n\n"
        f"🛍 <b>محصولات:</b>\n{escape(pending.get('items_summary') or '-')}\n\n"
        f"💰 مبلغ: <b>{pending.get('total_price') or 0:,} تومان</b>\n"
        f"💳 وضعیت: <b>{escape(pending.get('payment_status') or '-')}</b>"
    )

    buttons = [
        [InlineKeyboardButton("📎 مشاهده رسید پرداخت", callback_data=f"adm:pending_receipt:{pending_id}")],
        [
            InlineKeyboardButton("✅ تأیید پرداخت و ثبت سفارش", callback_data=f"payadmin:approve:{pending_id}"),
            InlineKeyboardButton("❌ رد پرداخت", callback_data=f"payadmin:reject:{pending_id}"),
        ],
        [InlineKeyboardButton("🔙 پرداخت‌های در انتظار", callback_data="adm:pending_payments")],
    ]
    await q.edit_message_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def admin_pending_payment_receipt(q, context, pending_id):
    conn = get_conn()
    pending = conn.execute(
        "SELECT * FROM pending_payments WHERE id=%s", (pending_id,)
    ).fetchone()
    conn.close()

    if not pending or pending.get("payment_status") != "در انتظار بررسی":
        await q.answer("این پرداخت دیگر در انتظار بررسی نیست.", show_alert=True)
        return

    file_id = pending.get("receipt_file_id")
    if not file_id:
        await q.answer("برای این پرداخت رسیدی ثبت نشده است.", show_alert=True)
        return

    caption = (
        f"🧾 <b>رسید پرداخت #{pending_id}</b>\n"
        f"👤 {escape(pending.get('full_name') or '-')}\n"
        f"💰 {pending.get('total_price') or 0:,} تومان\n"
        f"🕐 {escape(format_iran_jalali_fa(pending.get('created_at'), include_time=True))}"
    )
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ تأیید پرداخت و ثبت سفارش", callback_data=f"payadmin:approve:{pending_id}"),
            InlineKeyboardButton("❌ رد پرداخت", callback_data=f"payadmin:reject:{pending_id}"),
        ],
        [InlineKeyboardButton("🔙 جزئیات پرداخت", callback_data=f"adm:pending:{pending_id}")],
    ])

    try:
        await context.bot.send_photo(q.from_user.id, file_id, caption=caption, parse_mode="HTML", reply_markup=buttons)
    except Exception:
        try:
            await context.bot.send_document(q.from_user.id, file_id, caption=caption, parse_mode="HTML", reply_markup=buttons)
        except Exception as e:
            logger.warning(f"Could not send pending receipt to admin {q.from_user.id}: {e}")
            await q.answer("نمایش رسید ممکن نیست.", show_alert=True)


async def admin_products_view(q):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM products ORDER BY id DESC LIMIT 50"
    ).fetchall()
    conn.close()
    text = f"🛍 <b>مدیریت محصولات</b>\n\nتعداد محصولات: {len(rows)}"
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=products_keyboard(rows))


async def admin_orders_view(q, mode="all"):
    conn = get_conn()
    if mode == "new":
        rows = conn.execute(
            "SELECT * FROM orders WHERE status='در انتظار بررسی' ORDER BY id DESC LIMIT 30"
        ).fetchall()
    elif mode == "active":
        rows = conn.execute(
            "SELECT * FROM orders WHERE status!='رد شد' ORDER BY id DESC LIMIT 30"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT 30"
        ).fetchall()
    conn.close()

    if not rows:
        await q.edit_message_text("سفارشی برای نمایش وجود ندارد.", reply_markup=back_keyboard())
        return

    title = {
        "new": "🆕 سفارش‌های جدید",
        "active": "📦 سفارش‌های فعال",
        "all": "📋 همه سفارش‌ها",
    }.get(mode, "📋 سفارش‌ها")

    buttons = []
    for o in rows:
        buttons.append([
            InlineKeyboardButton(
                f"#{o['id']} | {o['full_name'] or '-'} | {o['status']}",
                callback_data=f"order:view:{o['id']}"
            )
        ])
    buttons.append([
        InlineKeyboardButton("🟡 پرداخت‌های در انتظار", callback_data="adm:pending_payments"),
    ])
    buttons.append([
        InlineKeyboardButton("🆕 جدید", callback_data="adm:orders:new"),
        InlineKeyboardButton("📦 فعال", callback_data="adm:orders:active"),
        InlineKeyboardButton("📋 همه", callback_data="adm:orders:all"),
    ])
    buttons.append([InlineKeyboardButton("🔙 پنل اصلی", callback_data="adm:home")])
    await q.edit_message_text(title, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_sales_view(q):
    conn = get_conn()
    total_orders = conn.execute("SELECT COUNT(*) AS value FROM orders").fetchone()["value"]
    valid_revenue = conn.execute(
        "SELECT COALESCE(SUM(total_price),0) AS value FROM orders WHERE status!='رد شد'"
    ).fetchone()["value"]
    confirmed_revenue = conn.execute(
        "SELECT COALESCE(SUM(total_price),0) AS value FROM orders WHERE status='تأیید شد'"
    ).fetchone()["value"]
    pending = conn.execute("SELECT COUNT(*) AS value FROM orders WHERE status='در انتظار بررسی'").fetchone()["value"]
    confirmed = conn.execute("SELECT COUNT(*) AS value FROM orders WHERE status='تأیید شد'").fetchone()["value"]
    cancelled = conn.execute("SELECT COUNT(*) AS value FROM orders WHERE status='رد شد'").fetchone()["value"]
    customers = conn.execute("SELECT COUNT(*) AS value FROM customers").fetchone()["value"]
    products = conn.execute("SELECT COUNT(*) AS value FROM products WHERE active=TRUE").fetchone()["value"]
    conn.close()

    avg = (valid_revenue / total_orders) if total_orders else 0
    text = (
        "📊 <b>فروش و آمار فروشگاه</b>\n\n"
        f"💰 فروش سفارش‌های غیررد‌شده: <b>{valid_revenue:,}</b> تومان\n"
        f"💵 فروش سفارش‌های تأییدشده: <b>{confirmed_revenue:,}</b> تومان\n"
        f"🧾 میانگین مبلغ هر سفارش: <b>{avg:,.0f}</b> تومان\n\n"
        f"📦 کل سفارش‌ها: {total_orders}\n"
        f"⏳ در انتظار بررسی: {pending}\n"
        f"✅ تأیید شده: {confirmed}\n"
        f"❌ رد شده: {cancelled}\n\n"
        f"👥 مشتری‌ها: {customers}\n"
        f"🛍 محصولات فعال: {products}"
    )
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=back_keyboard())




async def admin_customers_view(q):
    conn = get_conn()
    rows = conn.execute(
        """SELECT c.user_id, c.username, c.full_name, c.phone,
                  COUNT(o.id) AS orders_count,
                  COALESCE(SUM(CASE WHEN o.status!='رد شد' THEN o.total_price ELSE 0 END),0) AS spent
           FROM customers c
           LEFT JOIN orders o ON o.user_id=c.user_id
           GROUP BY c.user_id
           ORDER BY orders_count DESC, c.last_seen DESC
           LIMIT 30"""
    ).fetchall()
    conn.close()

    if not rows:
        await q.edit_message_text("هنوز مشتری ثبت نشده.", reply_markup=back_keyboard())
        return

    buttons = []
    for c in rows:
        name = c["full_name"] or c["username"] or str(c["user_id"])
        buttons.append([
            InlineKeyboardButton(
                f"👤 {name[:24]} | {c['orders_count']} سفارش",
                callback_data=f"adm:customer:{c['user_id']}"
            )
        ])
    buttons.append([InlineKeyboardButton("🔙 پنل اصلی", callback_data="adm:home")])
    await q.edit_message_text(
        "👥 <b>مشتری‌ها</b>\n\n"
        f"تعداد نمایش‌داده‌شده: {len(rows)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def admin_customer_detail(q, user_id):
    conn = get_conn()
    c = conn.execute("SELECT * FROM customers WHERE user_id=%s", (user_id,)).fetchone()
    orders = conn.execute(
        "SELECT * FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT 10", (user_id,)
    ).fetchall()
    conn.close()
    if not c:
        await q.edit_message_text("مشتری پیدا نشد.", reply_markup=back_keyboard("customers"))
        return

    # اطلاعات فعلی مشتری را از customer_saved_data می‌خوانیم تا آخرین
    # نام/شماره/آدرس ذخیره‌شده توسط خود مشتری در پنل ادمین نمایش داده شود.
    # اطلاعات داخل orders و pending_payments به‌عنوان سابقه سفارش دست‌نخورده می‌ماند.
    saved_names = get_saved_data(user_id, "name")
    saved_phones = get_saved_data(user_id, "phone")
    saved_addresses = get_saved_data(user_id, "address")
    current_name = saved_names[0]["value"] if saved_names else (c["full_name"] or "-")
    current_phone = saved_phones[0]["value"] if saved_phones else (c["phone"] or "-")
    current_address = saved_addresses[0]["value"] if saved_addresses else "-"

    total = sum((o["total_price"] or 0) for o in orders if o["status"] != "رد شد")
    text = (
        "👤 <b>اطلاعات فعلی مشتری</b>\n\n"
        f"نام: {escape(current_name)}\n"
        f"آیدی: <code>{c['user_id']}</code>\n"
        f"تلگرام: @{escape(c['username'] or '-')}\n"
        f"تلفن: {escape(current_phone)}\n"
        f"آدرس: {escape(current_address)}\n"
        f"جنسیت: {escape(c['gender'] or '-')}\n"
        f"اولین ورود: {escape(format_iran_jalali_fa(c.get('first_seen'), include_time=True))}\n"
        f"آخرین فعالیت: {escape(format_iran_jalali_fa(c.get('last_seen'), include_time=True))}\n\n"
        f"📦 تعداد سفارش‌ها: {len(orders)}\n"
        f"💰 مجموع خرید غیرلغوشده: {total:,} تومان"
    )
    buttons = []
    for o in orders:
        buttons.append([
            InlineKeyboardButton(
                f"#{o['id']} | {o['status']}",
                callback_data=f"order:view:{o['id']}"
            )
        ])
    buttons.append([InlineKeyboardButton(str(c["user_id"]), url=f"tg://user?id={c['user_id']}")])
    buttons.append([InlineKeyboardButton("🔙 مشتری‌ها", callback_data="adm:customers")])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def admin_product_detail(q, product_id, show_price_adjust=False, show_quick_edit=False):
    conn = get_conn()
    p = conn.execute("SELECT * FROM products WHERE id=%s", (product_id,)).fetchone()
    conn.close()
    if not p:
        await q.answer("محصول پیدا نشد.", show_alert=True)
        return
    await q.edit_message_text(
        product_text(p),
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product_id, p["active"], show_quick_edit, show_price_adjust),
    )


async def admin_payment_view(q):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM bank_accounts ORDER BY active DESC, id DESC").fetchall()
    conn.close()
    if not rows:
        text = "💳 <b>مدیریت حساب‌های بانکی</b>\n\nهنوز هیچ حسابی ثبت نشده است."
    else:
        lines = ["💳 <b>مدیریت حساب‌های بانکی</b>", ""]
        for a in rows:
            state = "🟢 فعال" if a["active"] else "⚪ غیرفعال"
            lines.append(f"#{a['id']} | 🏦 {escape(a['bank_name'])} | {state}")
        text = "\n".join(lines)
    buttons = [[InlineKeyboardButton("➕ افزودن حساب جدید", callback_data="adm:bank_add")]]
    for a in rows:
        buttons.append([InlineKeyboardButton(f"#{a['id']} | {a['bank_name']} {'🟢' if a['active'] else '⚪'}", callback_data=f"adm:bank:{a['id']}")])
    buttons.append([InlineKeyboardButton("🔙 پنل اصلی", callback_data="adm:home")])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def admin_bank_detail(q, account_id):
    conn = get_conn()
    a = conn.execute("SELECT * FROM bank_accounts WHERE id=%s", (account_id,)).fetchone()
    conn.close()
    if not a:
        await q.answer("حساب پیدا نشد.", show_alert=True); return
    state = "🟢 فعال" if a["active"] else "⚪ غیرفعال"
    text = (
        f"🏦 <b>حساب #{a['id']}</b>\n\n"
        f"بانک: {escape(a['bank_name'] or '-')}\n"
        f"صاحب حساب: {escape(a['owner_name'] or '-')}\n"
        f"کارت: <code>{escape(a['card_number'] or '-')}</code>\n"
        f"حساب: <code>{escape(a['account_number'] or '-')}</code>\n"
        f"شبا: <code>{escape(a['iban'] or '-')}</code>\n"
        f"وضعیت: <b>{state}</b>\n"
        f"آخرین تغییر: {escape(format_iran_jalali_fa(a.get('updated_at'), include_time=True))}"
    )
    buttons = []
    if a["active"]:
        buttons.append([InlineKeyboardButton("🔴 غیرفعال کردن", callback_data=f"adm:bank_deactivate:{account_id}")])
    else:
        buttons.append([InlineKeyboardButton("🟢 فعال کردن", callback_data=f"adm:bank_activate:{account_id}")])
    buttons.append([InlineKeyboardButton("✏️ ویرایش", callback_data=f"adm:bank_edit:{account_id}")])
    buttons.append([InlineKeyboardButton("🗑 حذف حساب", callback_data=f"adm:bank_delete:{account_id}")])
    buttons.append([InlineKeyboardButton("🔙 حساب‌ها", callback_data="adm:payment")])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def bank_accounts_action(q, context, action, account_id):
    conn = get_conn()
    a = conn.execute("SELECT * FROM bank_accounts WHERE id=%s", (account_id,)).fetchone()
    if not a:
        conn.close(); await q.answer("حساب پیدا نشد.", show_alert=True); return
    now = iran_now_naive().strftime("%Y-%m-%d %H:%M")
    if action == "activate":
        conn.execute("UPDATE bank_accounts SET active=FALSE, updated_at=%s", (now,))
        conn.execute("UPDATE bank_accounts SET active=TRUE, updated_at=%s WHERE id=%s", (now, account_id))
        conn.commit(); conn.close(); await q.answer("این حساب فعال شد و حساب قبلی غیرفعال شد.")
        await admin_bank_detail(q, account_id); return
    if action == "deactivate":
        # برای جلوگیری از صفحه پرداخت بدون حساب، غیرفعال‌کردن حساب فعال ممنوع است.
        other = conn.execute("SELECT id FROM bank_accounts WHERE id!=%s AND active=FALSE LIMIT 1", (account_id,)).fetchone()
        conn.close()
        await q.answer("برای غیرفعال‌کردن حساب فعال، اول یک حساب دیگر را فعال کن.", show_alert=True); return
    if action == "delete":
        if a["active"]:
            conn.close(); await q.answer("حساب فعال را نمی‌توان حذف کرد؛ اول یک حساب دیگر را فعال کن.", show_alert=True); return
        conn.execute("DELETE FROM bank_accounts WHERE id=%s", (account_id,)); conn.commit(); conn.close()
        await q.answer("حساب حذف شد."); await admin_payment_view(q); return


async def admin_bank_delete_confirm(q, account_id):
    conn=get_conn(); a=conn.execute("SELECT * FROM bank_accounts WHERE id=%s",(account_id,)).fetchone(); conn.close()
    if not a:
        await q.answer("حساب پیدا نشد.", show_alert=True); return
    if a["active"]:
        await q.answer("حساب فعال را نمی‌توان حذف کرد؛ اول یک حساب دیگر را فعال کن.", show_alert=True); return
    await q.edit_message_text(
        f"⚠️ <b>حذف حساب #{account_id}</b>\n\nآیا مطمئنی که می‌خواهی حساب {escape(a['bank_name'])} را برای همیشه حذف کنی؟",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 بله، حذف کن", callback_data=f"adm:bank_delete_confirm:{account_id}")], [InlineKeyboardButton("🔙 انصراف", callback_data=f"adm:bank:{account_id}")]])
    )

async def payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    parts = q.data.split(":")
    if parts[1] == "copycard":
        await q.answer("شماره کارت در پیام بالا قابل کپی است.", show_alert=True)
        return
    if parts[1] == "receipt":
        await q.answer("عکس رسید پرداخت را همینجا ارسال کن.", show_alert=True)
        return
    await q.answer()

async def receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not (update.message.photo or update.message.document):
        return
    user_id = update.effective_user.id
    conn = get_conn()
    pending = conn.execute(
        """SELECT * FROM pending_payments WHERE user_id=%s AND payment_status='در انتظار رسید'
           ORDER BY id DESC LIMIT 1""", (user_id,)
    ).fetchone()
    if not pending:
        under_review = conn.execute(
            """SELECT id FROM pending_payments WHERE user_id=%s AND payment_status='در انتظار بررسی'
               LIMIT 1""", (user_id,)
        ).fetchone()
        conn.close()
        if under_review:
            await update.message.reply_text("🟡 رسید قبلی شما در حال بررسی است. لطفاً منتظر نتیجه بمانید.")
        return
    file_id = update.message.photo[-1].file_id if update.message.photo else update.message.document.file_id
    now = iran_now_naive().strftime("%Y-%m-%d %H:%M")
    conn.execute("UPDATE pending_payments SET receipt_file_id=%s, payment_status='در انتظار بررسی' WHERE id=%s", (file_id, pending['id']))
    conn.commit(); conn.close()
    await update.message.reply_text("✅ رسید دریافت شد. پرداختت برای بررسی ارسال شد.\n🟡 تا تأیید پرداخت، سفارش نهایی ثبت نمی‌شود.", reply_markup=main_menu_keyboard(user_id))
    for admin_id in ADMIN_IDS:
        try:
            caption = (f"💳 <b>رسید پرداخت جدید</b>\n\n🆔 پرداخت موقت: #{pending['id']}\n"
                       f"👤 مشتری: {escape(pending['full_name'] or '-')}\n📞 تلفن: {escape(pending['phone'] or '-')}\n"
                       f"📍 آدرس: {escape(pending['address'] or '-')}\n\n🛍 محصولات:\n{escape(pending['items_summary'] or '-')}\n\n"
                       f"💰 مبلغ: <b>{pending['total_price']:,} تومان</b>\n🕐 {escape(format_iran_jalali_fa(now, include_time=True))}")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ تأیید پرداخت و ثبت سفارش", callback_data=f"payadmin:approve:{pending['id']}")],
                [InlineKeyboardButton("❌ رد پرداخت", callback_data=f"payadmin:reject:{pending['id']}")],
            ])
            if update.message.photo:
                await context.bot.send_photo(admin_id, file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
            else:
                await context.bot.send_document(admin_id, file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logger.warning(f"Could not send payment receipt to admin {admin_id}: {e}")

async def mark_admin_message(q, suffix_html):
    """Append a status line to the admin's message (photo/document caption or plain text)."""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 پرداخت‌های در انتظار", callback_data="adm:pending_payments")]])
    try:
        msg = q.message
        if msg.photo or msg.document:
            await q.edit_message_caption(
                caption=(msg.caption_html or "") + suffix_html, parse_mode="HTML", reply_markup=kb
            )
        else:
            await q.edit_message_text(
                text=(msg.text_html or "") + suffix_html, parse_mode="HTML", reply_markup=kb
            )
    except Exception as e:
        logger.warning(f"Could not update admin payment message: {e}")


async def payment_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("دسترسی نداری.", show_alert=True); return
    parts = q.data.split(":")
    pid = int(parts[2]); action = parts[1]
    conn = get_conn()
    pending = conn.execute("SELECT * FROM pending_payments WHERE id=%s", (pid,)).fetchone()
    if not pending:
        conn.close(); await q.answer("پرداخت پیدا نشد.", show_alert=True); return
    if pending['payment_status'] not in ('در انتظار بررسی',):
        conn.close(); await q.answer("این پرداخت قبلاً بررسی شده است.", show_alert=True); return
    now = iran_now_naive().strftime("%Y-%m-%d %H:%M")
    if action == 'reject':
        conn.execute("UPDATE pending_payments SET payment_status='رد شد', reviewed_at=%s, admin_id=%s WHERE id=%s", (now, q.from_user.id, pid))
        conn.commit(); conn.close()
        await q.answer("پرداخت رد شد.")
        try: await context.bot.send_message(pending['user_id'], "❌ رسید پرداخت شما تأیید نشد. سفارش ثبت نشد. لطفاً با پشتیبانی تماس بگیرید.")
        except Exception: pass
        await mark_admin_message(q, "\n\n❌ <b>پرداخت رد شد</b>")
        return
    # approve: only here is the real order inserted and cart cleared
    cur = conn.execute("""INSERT INTO orders
        (user_id, full_name, phone, address, items_summary, total_price, status, created_at, finalized_at, payment_status, transaction_ref)
        VALUES (%s, %s, %s, %s, %s, %s, 'در انتظار بررسی', %s, %s, 'تأیید شده', %s)
        RETURNING id""",
        (pending['user_id'], pending['full_name'], pending['phone'], pending['address'], pending['items_summary'], pending['total_price'], now, now, f"CARD-TRANSFER-{pid}"))
    order_id = cur.fetchone()["id"]
    conn.execute("DELETE FROM cart_items WHERE user_id=%s", (pending['user_id'],))
    conn.execute("UPDATE pending_payments SET payment_status='تأیید شده', reviewed_at=%s, admin_id=%s, order_id=%s WHERE id=%s", (now, q.from_user.id, order_id, pid))
    conn.commit(); conn.close()
    await q.answer(f"پرداخت تأیید شد؛ سفارش #{order_id} ثبت شد.")
    await mark_admin_message(q, f"\n\n✅ <b>پرداخت تأیید شد — سفارش #{order_id} ثبت شد</b>")
    try:
        await context.bot.send_message(pending['user_id'], f"✅ پرداخت شما تأیید شد.\n🆔 شماره سفارش: #{order_id}\n💰 مبلغ: {pending['total_price']:,} تومان\nسفارش شما ثبت نهایی شد.")
    except Exception: pass
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, f"🔔 <b>سفارش جدید #{order_id}</b>\n👤 {escape(pending['full_name'] or '-')}\n📞 {escape(pending['phone'] or '-')}\n📍 {escape(pending['address'] or '-')}\n\n🛍 {escape(pending['items_summary'] or '-')}\n\n💰 <b>{pending['total_price']:,} تومان</b>", parse_mode="HTML", reply_markup=order_status_keyboard(order_id, 'در انتظار بررسی'))
        except Exception as e: logger.warning(f"Could not notify admin after payment {admin_id}: {e}")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("دسترسی نداری.", show_alert=True)
        return
    await q.answer()
    parts = q.data.split(":")
    action = parts[1]

    if action == "close":
        await q.edit_message_text("پنل مدیریت بسته شد.")
        return
    if action == "home":
        await q.edit_message_text(
            await admin_dashboard_text(),
            parse_mode="HTML",
            reply_markup=admin_panel_keyboard(),
        )
        return
    if action == "orders":
        mode = parts[2] if len(parts) > 2 else "all"
        await admin_orders_view(q, mode)
        return
    if action == "pending_payments":
        await admin_pending_payments_view(q)
        return
    if action == "pending":
        await admin_pending_payment_detail(q, int(parts[2]))
        return
    if action == "pending_receipt":
        await admin_pending_payment_receipt(q, context, int(parts[2]))
        return
    if action == "products":
        await admin_products_view(q)
        return
    if action == "sales":
        await admin_sales_view(q)
        return
    if action == "customers":
        await admin_customers_view(q)
        return
    if action == "tickets":
        mode=parts[2] if len(parts)>2 else "all"
        await admin_tickets_view(q, mode)
        return
    if action == "ticket":
        await support_ticket_detail(q,int(parts[2]),True)
        return
    if action == "ticket_reply":
        ticket_id=int(parts[2])
        context.user_data["support_flow"]={"type":"admin_reply","ticket_id":ticket_id}
        await q.message.reply_text("💬 پاسخ خود را برای مشتری بنویس:")
        return
    if action == "ticket_close":
        ticket_id=int(parts[2]); now=iran_now_naive().strftime("%Y-%m-%d %H:%M")
        conn=get_conn(); t=conn.execute("SELECT user_id FROM support_tickets WHERE id=%s",(ticket_id,)).fetchone(); conn.execute("UPDATE support_tickets SET status='closed', closed_at=%s, updated_at=%s WHERE id=%s",(now,now,ticket_id)); conn.commit(); conn.close()
        if t:
            try: await context.bot.send_message(t['user_id'],f"🟢 تیکت پشتیبانی #{ticket_id} بسته شد. اگر دوباره نیاز به کمک داشتی، می‌تونی درخواست جدید ثبت کنی.")
            except Exception: pass
        await support_ticket_detail(q,ticket_id,True); return
    if action == "channel":
        await admin_channel_view(q)
        return
    if action == "chpause":
        set_channel_posting_enabled(False)
        await admin_channel_view(q, "⏸ پست‌گذاری خودکار کانال متوقف شد.")
        return
    if action == "chresume":
        set_channel_posting_enabled(True)
        await admin_channel_view(q, "▶️ پست‌گذاری خودکار کانال دوباره فعال شد.")
        return
    if action == "chtime":
        context.user_data["admin_flow"] = {"type": "channel_settings", "step": "morning"}
        await q.message.reply_text(
            "⏰ <b>تغییر ساعت پست‌گذاری</b>\n\n"
            "ساعت پست صبح را به وقت تهران وارد کن.\n"
            "مثال: <code>08:30</code>",
            parse_mode="HTML",
        )
        return
    if action == "chadd":
        kind = parts[2] if len(parts) > 2 else "morning"
        if kind not in CHANNEL_KINDS:
            return
        context.user_data["admin_flow"] = {"type": "channel_msg", "kind": kind}
        await q.message.reply_text(
            f"✍️ متن پیام {CHANNEL_KINDS[kind]} را بفرست.\n\n"
            "برای افزودن چند پیام با هم، آن‌ها را با یک خط جداکننده بفرست:\n"
            "---\n\n(حداکثر ۱۲۰۰ کاراکتر برای هر پیام)"
        )
        return
    if action == "chlist":
        kind = parts[2] if len(parts) > 2 else "morning"
        page = int(parts[3]) if len(parts) > 3 else 0
        await admin_channel_list(q, kind, page)
        return
    if action == "chdel":
        kind, msg_id = parts[2], int(parts[3])
        page = int(parts[4]) if len(parts) > 4 else 0
        if kind in CHANNEL_KINDS:
            conn = get_conn()
            try:
                conn.execute("DELETE FROM channel_messages WHERE id=%s AND kind=%s", (msg_id, kind))
                conn.commit()
            finally:
                conn.close()
        await admin_channel_list(q, kind, page)
        return
    if action == "chpost":
        kind = parts[2] if len(parts) > 2 else "morning"
        if kind not in CHANNEL_KINDS:
            return
        ok, info = await channel_post(context.bot, kind, force=True)
        note = f"✅ پست {CHANNEL_KINDS[kind]} در کانال ارسال شد." if ok else f"❌ ارسال نشد: {info}"
        await admin_channel_view(q, note)
        return
    if action == "chtest":
        await admin_channel_selftest(q, context)
        return
    if action == "chclear":
        removed = 0
        for k in CHANNEL_KINDS:
            if await channel_delete_stored(context.bot, f"channel_{k}_msg"):
                removed += 1
        await admin_channel_view(q, f"🧹 {removed} پست از کانال پاک شد.")
        return
    if action == "payment":
        await admin_payment_view(q)
        return
    if action == "bank":
        await admin_bank_detail(q, int(parts[2]))
        return
    if action in ("bank_activate", "bank_deactivate"):
        await bank_accounts_action(q, context, action.replace("bank_", ""), int(parts[2]))
        return
    if action == "bank_delete":
        await admin_bank_delete_confirm(q, int(parts[2]))
        return
    if action == "bank_delete_confirm":
        await bank_accounts_action(q, context, "delete", int(parts[2]))
        return
    if action == "bank_add":
        context.user_data["admin_flow"] = {"type": "bank_account", "mode": "add", "step": "bank_name"}
        await q.message.reply_text("🏦 نام بانک را بفرست (مثلاً ملت):")
        return
    if action == "bank_edit":
        account_id=int(parts[2])
        context.user_data["admin_flow"] = {"type": "bank_account", "mode": "edit", "account_id": account_id, "step": "bank_name"}
        await q.message.reply_text("🏦 نام بانک جدید را بفرست:")
        return
    if action == "customer":
        await admin_customer_detail(q, int(parts[2]))
        return
    if action == "product":
        await admin_product_detail(q, int(parts[2]))
        return
    if action == "add_product":
        context.user_data["admin_flow"] = {"type": "add_product", "step": "name"}
        await q.message.reply_text("➕ نام محصول را بفرست:")
        return
    if action == "edit_product":
        pid = int(parts[2])
        context.user_data["admin_flow"] = {"type": "edit_product", "product_id": pid, "step": "name"}
        await q.message.reply_text("✏️ نام جدید محصول را بفرست:")
        return
    if action == "quick_toggle":
        pid, show = int(parts[2]), parts[3] == "1"
        await q.answer()
        await admin_product_detail(q, pid, show_quick_edit=show)
        return
    if action == "quick_field":
        pid, field = int(parts[2]), parts[3]
        prompts = {"name": "نام جدید محصول را بفرست:", "size": "سایز جدید را بفرست:", "color": "رنگ‌بندی جدید را بفرست:", "pack_info": "اطلاعات پک جدید را بفرست؛ برای حذف پک «ندارد» را بفرست:"}
        if field not in prompts:
            await q.answer("گزینه نامعتبر است.", show_alert=True); return
        context.user_data["admin_flow"] = {"type": "quick_product_field", "product_id": pid, "field": field}
        await q.answer()
        await q.message.reply_text("✏️ " + prompts[field] + " برای لغو /cancel را بزن:")
        return
    if action == "price_quick_toggle":
        pid, show = int(parts[2]), parts[3] == "1"
        await q.answer()
        await admin_product_detail(q, pid, show_price_adjust=show, show_quick_edit=True)
        return
    if action == "price_adjust":
        pid, delta = int(parts[2]), int(parts[3])
        conn = get_conn()
        product = conn.execute("SELECT price FROM products WHERE id=%s", (pid,)).fetchone()
        if not product:
            conn.close()
            await q.answer("محصول پیدا نشد.", show_alert=True)
            return
        new_price = max(0, int(product["price"] or 0) + delta)
        conn.execute("UPDATE products SET price=%s WHERE id=%s", (new_price, pid))
        conn.commit()
        conn.close()
        await q.answer(f"قیمت جدید: {new_price:,} تومان")
        await admin_product_detail(q, pid)
        return
    if action == "price_set":
        pid = int(parts[2])
        context.user_data["admin_flow"] = {"type": "quick_price", "product_id": pid, "step": "price"}
        await q.message.reply_text("💰 قیمت جدید را فقط به تومان و با عدد بفرست. برای لغو /cancel را بزن.")
        return
    if action == "toggle_product":
        pid = int(parts[2])
        active = parts[3] == "activate"
        conn = get_conn()
        conn.execute("UPDATE products SET active=%s WHERE id=%s", (active, pid))
        conn.commit()
        conn.close()
        await q.answer("وضعیت محصول تغییر کرد.")
        await admin_product_detail(q, pid)
        return
    if action == "delete_product":
        pid = int(parts[2])
        context.user_data["admin_delete_product"] = pid
        await q.edit_message_text(
            "⚠️ <b>حذف محصول</b>\n\nمطمئنی می‌خواهی این محصول حذف شود؟",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 بله، حذف شود", callback_data=f"adm:delete_confirm:{pid}")],
                [InlineKeyboardButton("🔙 لغو", callback_data=f"adm:product:{pid}")],
            ])
        )
        return
    if action == "delete_confirm":
        pid = int(parts[2])
        conn = get_conn()
        conn.execute("DELETE FROM products WHERE id=%s", (pid,))
        conn.commit()
        conn.close()
        await q.answer("محصول حذف شد.")
        await admin_products_view(q)
        return


async def admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    flow = context.user_data.get("admin_flow")
    support_flow = context.user_data.get("support_flow")

    # مشتری‌ها هم باید بتوانند متن تیکت و پاسخ تیکت را ارسال کنند.
    # فقط وقتی جریان پشتیبانی وجود ندارد، پیام مشتری را نادیده می‌گیریم.
    if not is_admin(update.effective_user.id) and not support_flow:
        return

    text = (update.message.text or "").strip()

    if support_flow:
        user_id = update.effective_user.id
        now = iran_now_naive().strftime("%Y-%m-%d %H:%M")
        if support_flow.get("type") == "new" and support_flow.get("step") == "message":
            topic = support_flow.get("topic", "سایر")
            conn = get_conn()
            cur = conn.execute(
                "INSERT INTO support_tickets(user_id,topic,status,created_at,updated_at) "
                "VALUES(%s,%s,'new',%s,%s) RETURNING id",
                (user_id, topic, now, now),
            )
            ticket_id = cur.fetchone()["id"]
            conn.execute(
                "INSERT INTO support_ticket_messages(ticket_id,sender_id,sender_type,message,created_at) "
                "VALUES(%s,%s,'customer',%s,%s)",
                (ticket_id, user_id, text, now),
            )
            conn.commit()
            conn.close()
            context.user_data.pop("support_flow", None)
            await update.message.reply_text(
                f"✅ درخواستت ثبت شد.\n🎫 شماره تیکت: #{ticket_id}\n\nپشتیبانی در اولین فرصت پاسخ می‌دهد.",
                reply_markup=support_center_keyboard(),
            )
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"🆕 <b>درخواست پشتیبانی #{ticket_id}</b>\n"
                        f"👤 آیدی مشتری: <code>{user_id}</code>\n"
                        f"🏷 موضوع: {escape(topic)}\n"
                        f"🕐 {format_iran_jalali_fa(now, True)}\n\n"
                        f"💬 {escape(text)}",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🎫 مشاهده تیکت", callback_data=f"adm:ticket:{ticket_id}")
                        ]]),
                    )
                except Exception:
                    pass
            return

        if support_flow.get("type") in ("reply", "admin_reply"):
            ticket_id = int(support_flow["ticket_id"])
            sender_type = "admin" if support_flow["type"] == "admin_reply" else "customer"
            conn = get_conn()
            t = conn.execute("SELECT * FROM support_tickets WHERE id=%s", (ticket_id,)).fetchone()
            if not t or (sender_type == "customer" and t["user_id"] != user_id) or t["status"] == "closed":
                conn.close()
                context.user_data.pop("support_flow", None)
                await update.message.reply_text("این تیکت قابل ادامه نیست.")
                return
            conn.execute(
                "INSERT INTO support_ticket_messages(ticket_id,sender_id,sender_type,message,created_at) "
                "VALUES(%s,%s,%s,%s,%s)",
                (ticket_id, user_id, sender_type, text, now),
            )
            new_status = "customer_waiting" if sender_type == "admin" else "admin_waiting"
            conn.execute(
                "UPDATE support_tickets SET status=%s,updated_at=%s WHERE id=%s",
                (new_status, now, ticket_id),
            )
            conn.commit()
            conn.close()
            context.user_data.pop("support_flow", None)
            if sender_type == "admin":
                try:
                    await context.bot.send_message(
                        t["user_id"],
                        f"💬 پاسخ پشتیبانی برای تیکت #{ticket_id}:\n\n{text}",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🎫 مشاهده تیکت", callback_data=f"support:view:{ticket_id}")
                        ]]),
                    )
                except Exception:
                    pass
                await update.message.reply_text("✅ پاسخ برای مشتری ارسال شد.", reply_markup=admin_panel_keyboard())
            else:
                for admin_id in ADMIN_IDS:
                    try:
                        await context.bot.send_message(
                            admin_id,
                            f"💬 <b>پاسخ جدید مشتری در تیکت #{ticket_id}</b>\n"
                            f"👤 آیدی: <code>{user_id}</code>\n\n{escape(text)}",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton("🎫 مشاهده تیکت", callback_data=f"adm:ticket:{ticket_id}")
                            ]]),
                        )
                    except Exception:
                        pass
                await update.message.reply_text("✅ پیام شما به پشتیبانی ارسال شد.", reply_markup=support_center_keyboard())
            return

    # از اینجا به بعد فقط ورودی‌های پنل ادمین پردازش می‌شوند.
    if not is_admin(update.effective_user.id) or not flow:
        return

    if flow["type"] == "channel_settings":
        step = flow.get("step")
        value = _valid_hhmm(text)
        if not value:
            await update.message.reply_text("❌ ساعت نامعتبر است. فرمت صحیح مثل 08:30 یا 22:15 است.")
            return
        if step == "morning":
            set_channel_schedule("morning", value)
            flow["step"] = "night"
            await update.message.reply_text(
                "🌙 حالا ساعت پست شب را به وقت تهران وارد کن.\nمثال: <code>22:00</code>",
                parse_mode="HTML",
            )
            return
        set_channel_schedule("night", value)
        context.user_data.pop("admin_flow", None)
        m = channel_schedule_min("morning")
        n = channel_schedule_min("night")
        await update.message.reply_text(
            f"✅ ساعت‌ها ذخیره شد.\n☀️ صبح: {m//60:02d}:{m%60:02d}\n🌙 شب: {n//60:02d}:{n%60:02d}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 مدیریت کانال", callback_data="adm:channel")]]),
        )
        return

    if flow["type"] == "channel_msg":
        kind = flow.get("kind")
        chunks = [c.strip() for c in re.split(r"\n\s*---+\s*\n", text) if c.strip()]
        if kind not in CHANNEL_KINDS or not chunks or any(len(c) > 1200 for c in chunks):
            await update.message.reply_text("❌ هر پیام باید حداکثر ۱۲۰۰ کاراکتر باشد. دوباره بفرست:")
            return
        now_txt = iran_now_naive().strftime("%Y-%m-%d %H:%M")
        try:
            conn = get_conn()
            try:
                for c in chunks:
                    conn.execute(
                        "INSERT INTO channel_messages(kind, text, active, created_at) VALUES(%s,%s,TRUE,%s)",
                        (kind, c, now_txt),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Could not save channel messages: {e}")
            context.user_data.pop("admin_flow", None)
            await update.message.reply_text(
                "❌ ذخیره نشد. احتمالاً جدول channel_messages هنوز در Supabase ساخته نشده است."
            )
            return
        context.user_data.pop("admin_flow", None)
        await update.message.reply_text(
            f"✅ {len(chunks)} پیام {CHANNEL_KINDS[kind]} اضافه شد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 مدیریت کانال", callback_data="adm:channel")]]),
        )
        return

    if flow["type"] == "bank_account":
        step = flow["step"]
        prompts = {
            "bank_name": ("bank_name", "👤 نام صاحب حساب را بفرست:"),
            "owner_name": ("owner_name", "💳 شماره کارت را بفرست:"),
            "card_number": ("card_number", "🏦 شماره حساب را بفرست:"),
            "account_number": ("account_number", "🔹 شماره شبا را بفرست (با IR یا بدون فاصله):"),
        }
        if step in prompts:
            field, prompt = prompts[step]; flow[field] = text
            flow["step"] = {"bank_name":"owner_name","owner_name":"card_number","card_number":"account_number","account_number":"iban"}[step]
            await update.message.reply_text(prompt); return
        if step == "iban":
            iban=text.replace(" ", "").upper()
            if iban.startswith("IR") and len(iban)!=26:
                await update.message.reply_text("❌ شماره شبا باید ۲۶ کاراکتر باشد (IR + 24 رقم). دوباره بفرست:"); return
            if not iban.startswith("IR") and len(iban)!=24:
                await update.message.reply_text("❌ شماره شبا باید ۲۴ رقم یا با IR مجموعاً ۲۶ کاراکتر باشد. دوباره بفرست:"); return
            if not iban.startswith("IR"): iban="IR"+iban
            flow["iban"]=iban; now=iran_now_naive().strftime("%Y-%m-%d %H:%M")
            conn=get_conn()
            if flow["mode"]=="edit":
                conn.execute("""UPDATE bank_accounts SET bank_name=%s, owner_name=%s, card_number=%s, account_number=%s, iban=%s, updated_at=%s WHERE id=%s""",
                             (flow["bank_name"],flow["owner_name"],flow["card_number"],flow["account_number"],flow["iban"],now,flow["account_id"]))
                msg="✅ اطلاعات حساب با موفقیت ویرایش شد."
            else:
                # حساب جدید ابتدا غیرفعال ساخته می‌شود؛ سپس ادمین می‌تواند آن را فعال کند.
                cur=conn.execute("""INSERT INTO bank_accounts(bank_name,owner_name,card_number,account_number,iban,active,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,FALSE,%s,%s) RETURNING id""",
                                 (flow["bank_name"],flow["owner_name"],flow["card_number"],flow["account_number"],flow["iban"],now,now))
                msg=f"✅ حساب جدید #{cur.fetchone()['id']} اضافه شد و فعلاً غیرفعال است. برای استفاده، آن را فعال کن."
            conn.commit(); conn.close(); context.user_data.pop("admin_flow",None)
            await update.message.reply_text(msg, reply_markup=admin_panel_keyboard()); return

    if flow["type"] == "quick_price":
        pid = flow["product_id"]
        try:
            raw = text.replace(",", "").replace("٬", "").replace(" ", "")
            price = int(raw)
            if price < 0:
                raise ValueError
        except (TypeError, ValueError):
            await update.message.reply_text("❌ قیمت نامعتبر است. یک عدد صفر یا بزرگ‌تر به تومان بفرست.")
            return
        conn = get_conn()
        exists = conn.execute("SELECT 1 FROM products WHERE id=%s", (pid,)).fetchone()
        if not exists:
            conn.close()
            context.user_data.pop("admin_flow", None)
            await update.message.reply_text("محصول پیدا نشد.")
            return
        conn.execute("UPDATE products SET price=%s WHERE id=%s", (price, pid))
        conn.commit()
        conn.close()
        context.user_data.pop("admin_flow", None)
        await update.message.reply_text(f"✅ قیمت محصول با موفقیت به {price:,} تومان تغییر کرد.", reply_markup=admin_panel_keyboard())
        return

    if flow["type"] == "add_product":
        step = flow["step"]
        if step == "name":
            flow["name"] = text; flow["step"] = "category"; await update.message.reply_text("دسته محصول را بنویس: بچگانه یا زنانه")
        elif step == "category":
            if text not in ("بچگانه", "زنانه"): await update.message.reply_text("فقط «بچگانه» یا «زنانه» وارد کن."); return
            flow["category"] = text; flow["step"] = "size"; await update.message.reply_text("سایز را وارد کن:")
        elif step == "size":
            flow["size"] = text; flow["step"] = "pack_info"; await update.message.reply_text("اطلاعات پک را وارد کن؛ مثلاً «پک ۱۲ عددی» یا «ندارد»:")
        elif step == "pack_info":
            flow["pack_info"] = "" if text.lower() in ("ندارد","ندارم","-","no") else text; flow["step"] = "price"; await update.message.reply_text("قیمت را فقط به تومان و به عدد وارد کن:")
        elif step == "price":
            try: flow["price"] = int(text.replace(",", "")); assert flow["price"] >= 0
            except Exception: await update.message.reply_text("❌ قیمت نامعتبر است."); return
            flow["step"] = "photo"; await update.message.reply_text("لینک عکس محصول را بفرست؛ اگر عکس نداری بنویس «ندارد».")
        elif step == "photo":
            photo_url = "" if text.lower() in ("ندارد","ندارم","-","no") else text
            conn=get_conn(); cur=conn.execute("""INSERT INTO products(name,category,size,color,price,photo_url,active,pack_info) VALUES(%s,%s,%s,%s,%s,%s,TRUE,%s) RETURNING id""",
                (flow["name"],flow["category"],flow["size"],"رنگ‌بندی طبق ژورنال موجود",flow["price"],photo_url,flow["pack_info"]))
            pid=cur.fetchone()["id"]; conn.commit(); conn.close(); context.user_data.pop("admin_flow",None)
            await update.message.reply_text(f"✅ محصول «{flow['name']}» اضافه شد.\nشناسه: #{pid}", reply_markup=admin_panel_keyboard())
        return

    if flow["type"] == "quick_product_field":
        pid, field = flow["product_id"], flow["field"]
        value = text.strip()
        if field == "name" and not value:
            await update.message.reply_text("❌ نام نمی‌تواند خالی باشد. دوباره بفرست:"); return
        if field == "pack_info" and value.lower() in ("ندارد", "ندارم", "-", "no"):
            value = ""
        column = {"name": "name", "size": "size", "color": "color", "pack_info": "pack_info"}.get(field)
        if not column:
            context.user_data.pop("admin_flow", None); return
        conn = get_conn()
        exists = conn.execute("SELECT 1 FROM products WHERE id=%s", (pid,)).fetchone()
        if not exists:
            conn.close(); context.user_data.pop("admin_flow", None)
            await update.message.reply_text("محصول پیدا نشد."); return
        conn.execute(f"UPDATE products SET {column}=%s WHERE id=%s", (value, pid))
        conn.commit(); conn.close(); context.user_data.pop("admin_flow", None)
        await update.message.reply_text("✅ اطلاعات محصول با موفقیت تغییر کرد.")
        return

    if flow["type"] == "quick_name":
        pid = flow["product_id"]
        name = text.strip()
        if not name:
            await update.message.reply_text("❌ نام نمی‌تواند خالی باشد. نام جدید را بفرست:")
            return
        conn = get_conn()
        product = conn.execute("SELECT id FROM products WHERE id=%s", (pid,)).fetchone()
        if not product:
            conn.close()
            context.user_data.pop("admin_flow", None)
            await update.message.reply_text("محصول پیدا نشد.")
            return
        conn.execute("UPDATE products SET name=%s WHERE id=%s", (name, pid))
        conn.commit()
        conn.close()
        context.user_data.pop("admin_flow", None)
        await update.message.reply_text(f"✅ نام محصول با موفقیت به «{name}» تغییر کرد.")
        return

    if flow["type"] == "edit_product":
        step=flow["step"]; pid=flow["product_id"]; conn=get_conn()
        if not conn.execute("SELECT 1 FROM products WHERE id=%s",(pid,)).fetchone(): conn.close(); context.user_data.pop("admin_flow",None); await update.message.reply_text("محصول پیدا نشد."); return
        if step == "name": conn.execute("UPDATE products SET name=%s WHERE id=%s",(text,pid)); flow["step"]="category"; await update.message.reply_text("دسته جدید: بچگانه یا زنانه")
        elif step == "category":
            if text not in ("بچگانه","زنانه"): conn.close(); await update.message.reply_text("فقط «بچگانه» یا «زنانه» وارد کن."); return
            conn.execute("UPDATE products SET category=%s WHERE id=%s",(text,pid)); flow["step"]="size"; await update.message.reply_text("سایز جدید:")
        elif step == "size": conn.execute("UPDATE products SET size=%s WHERE id=%s",(text,pid)); flow["step"]="pack_info"; await update.message.reply_text("اطلاعات پک جدید؛ مثلاً «پک ۱۲ عددی» یا «ندارد»:")
        elif step == "pack_info": conn.execute("UPDATE products SET pack_info=%s WHERE id=%s",("" if text.lower() in ("ندارد","ندارم","-","no") else text,pid)); flow["step"]="price"; await update.message.reply_text("قیمت جدید به تومان:")
        elif step == "price":
            try: price=int(text.replace(",","")); assert price>=0
            except Exception: conn.close(); await update.message.reply_text("❌ قیمت نامعتبر است."); return
            conn.execute("UPDATE products SET price=%s WHERE id=%s",(price,pid)); flow["step"]="photo"; await update.message.reply_text("لینک عکس جدید را بفرست؛ برای بدون عکس «ندارد».")
        elif step == "photo":
            photo_url="" if text.lower() in ("ندارد","ندارم","-","no") else text; conn.execute("UPDATE products SET photo_url=%s WHERE id=%s",(photo_url,pid)); conn.commit(); p=conn.execute("SELECT * FROM products WHERE id=%s",(pid,)).fetchone(); conn.close(); context.user_data.pop("admin_flow",None); await update.message.reply_text(f"✅ محصول «{p['name']}» ویرایش شد.",reply_markup=admin_panel_keyboard()); return
        conn.commit(); conn.close()


async def order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("دسترسی نداری.", show_alert=True)
        return
    parts = q.data.split(":", 3)
    action = parts[1]
    order_id = int(parts[2])

    conn = get_conn()
    order = conn.execute("SELECT * FROM orders WHERE id=%s", (order_id,)).fetchone()
    if not order:
        conn.close()
        await q.answer("سفارش پیدا نشد.", show_alert=True)
        return

    if action == "view":
        conn.close()
        await q.answer()
        await q.edit_message_text(
            order_text(order), parse_mode="HTML",
            reply_markup=order_status_keyboard(order_id, order["status"])
        )
        return

    status_map = {
        "confirmed": "تأیید شد",
        "cancelled": "رد شد",
    }
    new_status = status_map.get(parts[3])
    if not new_status:
        conn.close()
        await q.answer("وضعیت نامعتبر است.", show_alert=True)
        return

    old_status = order["status"]
    conn.execute("UPDATE orders SET status=%s WHERE id=%s", (new_status, order_id))
    conn.commit()
    order = conn.execute("SELECT * FROM orders WHERE id=%s", (order_id,)).fetchone()
    conn.close()

    await q.answer(f"وضعیت سفارش #{order_id}: {new_status}")
    await q.edit_message_text(
        order_text(order), parse_mode="HTML",
        reply_markup=order_status_keyboard(order_id, new_status)
    )

    if old_status != new_status:
        if parts[3] == "confirmed":
            customer_text = (
                f"✅ سفارش شما (#{order_id}) <b>تأیید شد</b> و در حال پردازش هست.\n"
                f"به‌زودی برای هماهنگی ارسال باهاتون تماس می‌گیریم."
            )
        else:  # cancelled
            customer_text = (
                f"❌ متأسفانه سفارش شما (#{order_id}) <b>رد شد</b>.\n"
                f"برای اطلاعات بیشتر می‌تونید از بخش «💬 مرکز پشتیبانی» با ما در ارتباط باشید."
            )
        try:
            await context.bot.send_message(
                order["user_id"],
                customer_text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Could not notify customer {order['user_id']}: {e}")


async def admin_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("دسترسی نداری.")
        return
    await update.message.reply_text(
        "⚙️ پنل مدیریت تنین ایران",
        reply_markup=admin_panel_keyboard()
    )

# ----------------------------------------------------------------------------
# پست‌های زمان‌بندی‌شدهٔ کانال (صبح: صبح‌بخیر + آب‌وهوا | شب: شب‌بخیر)
# ----------------------------------------------------------------------------
# تنظیمات از طریق متغیرهای محیطی (Render > Environment):
#   CHANNEL_ID              مثل @my_channel یا -1001234567890  (خالی = غیرفعال)
#   CHANNEL_MORNING_TIME    پیش‌فرض 08:00 (به وقت تهران)
#   CHANNEL_NIGHT_TIME      پیش‌فرض 22:00 (به وقت تهران)
#   CHANNEL_GRACE_MINUTES   اگر ربات سر وقت بیدار نبود تا چند دقیقه بعد هنوز پست بگذارد (پیش‌فرض 180)
#   زمان‌های ذخیره‌شده در app_meta بر مقادیر ENV اولویت دارند و از پنل قابل تغییرند.
_channel_raw = os.getenv("CHANNEL_ID", "").strip()
CHANNEL_ID = int(_channel_raw) if _channel_raw.lstrip("-").isdigit() else _channel_raw
CHANNEL_KINDS = {"morning": "صبح", "night": "شب"}


def _parse_hhmm(name, default):
    raw = os.getenv(name, default).strip()
    try:
        h, m = raw.split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except Exception:
        pass
    logger.warning("%s=%r is invalid; using %s", name, raw, default)
    h, m = default.split(":")
    return int(h) * 60 + int(m)


CHANNEL_MORNING_MIN = _parse_hhmm("CHANNEL_MORNING_TIME", "08:00")
CHANNEL_NIGHT_MIN = _parse_hhmm("CHANNEL_NIGHT_TIME", "22:00")
try:
    CHANNEL_GRACE_MIN = max(0, int(os.getenv("CHANNEL_GRACE_MINUTES", "180")))
except ValueError:
    CHANNEL_GRACE_MIN = 180

WEATHER_CITY = os.getenv("WEATHER_CITY_NAME", "مشهد").strip() or "مشهد"
WEATHER_LAT = os.getenv("WEATHER_LAT", "36.2605").strip()
WEATHER_LON = os.getenv("WEATHER_LON", "59.6168").strip()

# اگر جدول channel_messages خالی باشد از این پیام‌های آماده استفاده می‌شود.
CHANNEL_DEFAULTS = {
    "morning": ['امروز را با آرامش شروع کن؛ لازم نیست همه\u200cچیز یک\u200cجا حل شود. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز را با آرامش شروع کن؛ لازم نیست همه\u200cچیز یک\u200cجا حل شود. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز را با آرامش شروع کن؛ لازم نیست همه\u200cچیز یک\u200cجا حل شود. 💪 به خودت و تلاشت اعتماد کن.', 'امروز را با آرامش شروع کن؛ لازم نیست همه\u200cچیز یک\u200cجا حل شود. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز را با آرامش شروع کن؛ لازم نیست همه\u200cچیز یک\u200cجا حل شود. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'هر صبح یک فرصت تازه برای ساختن یک روز بهتر است. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'هر صبح یک فرصت تازه برای ساختن یک روز بهتر است. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'هر صبح یک فرصت تازه برای ساختن یک روز بهتر است. 💪 به خودت و تلاشت اعتماد کن.', 'هر صبح یک فرصت تازه برای ساختن یک روز بهتر است. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'هر صبح یک فرصت تازه برای ساختن یک روز بهتر است. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز فقط روی قدم بعدی تمرکز کن؛ همین کافی است. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز فقط روی قدم بعدی تمرکز کن؛ همین کافی است. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز فقط روی قدم بعدی تمرکز کن؛ همین کافی است. 💪 به خودت و تلاشت اعتماد کن.', 'امروز فقط روی قدم بعدی تمرکز کن؛ همین کافی است. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز فقط روی قدم بعدی تمرکز کن؛ همین کافی است. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'با یک فکر خوب شروع کن و اجازه بده بقیه روز خودش شکل بگیرد. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'با یک فکر خوب شروع کن و اجازه بده بقیه روز خودش شکل بگیرد. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'با یک فکر خوب شروع کن و اجازه بده بقیه روز خودش شکل بگیرد. 💪 به خودت و تلاشت اعتماد کن.', 'با یک فکر خوب شروع کن و اجازه بده بقیه روز خودش شکل بگیرد. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'با یک فکر خوب شروع کن و اجازه بده بقیه روز خودش شکل بگیرد. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'صبح یعنی دوباره فرصت داری از نو شروع کنی. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'صبح یعنی دوباره فرصت داری از نو شروع کنی. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'صبح یعنی دوباره فرصت داری از نو شروع کنی. 💪 به خودت و تلاشت اعتماد کن.', 'صبح یعنی دوباره فرصت داری از نو شروع کنی. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'صبح یعنی دوباره فرصت داری از نو شروع کنی. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'اگر دیروز سخت بود، امروز می\u200cتواند نقطه شروع تازه\u200cای باشد. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'اگر دیروز سخت بود، امروز می\u200cتواند نقطه شروع تازه\u200cای باشد. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'اگر دیروز سخت بود، امروز می\u200cتواند نقطه شروع تازه\u200cای باشد. 💪 به خودت و تلاشت اعتماد کن.', 'اگر دیروز سخت بود، امروز می\u200cتواند نقطه شروع تازه\u200cای باشد. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'اگر دیروز سخت بود، امروز می\u200cتواند نقطه شروع تازه\u200cای باشد. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'برای یک روز خوب، نیت خوب و یک قدم کوچک کافی است. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'برای یک روز خوب، نیت خوب و یک قدم کوچک کافی است. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'برای یک روز خوب، نیت خوب و یک قدم کوچک کافی است. 💪 به خودت و تلاشت اعتماد کن.', 'برای یک روز خوب، نیت خوب و یک قدم کوچک کافی است. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'برای یک روز خوب، نیت خوب و یک قدم کوچک کافی است. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز به خودت یادآوری کن که تلاش\u200cهای کوچک بی\u200cاثر نیستند. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز به خودت یادآوری کن که تلاش\u200cهای کوچک بی\u200cاثر نیستند. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز به خودت یادآوری کن که تلاش\u200cهای کوچک بی\u200cاثر نیستند. 💪 به خودت و تلاشت اعتماد کن.', 'امروز به خودت یادآوری کن که تلاش\u200cهای کوچک بی\u200cاثر نیستند. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز به خودت یادآوری کن که تلاش\u200cهای کوچک بی\u200cاثر نیستند. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'آرام شروع کن، منظم ادامه بده و از مسیر لذت ببر. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'آرام شروع کن، منظم ادامه بده و از مسیر لذت ببر. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'آرام شروع کن، منظم ادامه بده و از مسیر لذت ببر. 💪 به خودت و تلاشت اعتماد کن.', 'آرام شروع کن، منظم ادامه بده و از مسیر لذت ببر. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'آرام شروع کن، منظم ادامه بده و از مسیر لذت ببر. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'هر موفقیت بزرگی از قدم\u200cهای کوچک ساخته شده است. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'هر موفقیت بزرگی از قدم\u200cهای کوچک ساخته شده است. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'هر موفقیت بزرگی از قدم\u200cهای کوچک ساخته شده است. 💪 به خودت و تلاشت اعتماد کن.', 'هر موفقیت بزرگی از قدم\u200cهای کوچک ساخته شده است. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'هر موفقیت بزرگی از قدم\u200cهای کوچک ساخته شده است. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز قرار نیست کامل باشی؛ فقط ادامه بده. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز قرار نیست کامل باشی؛ فقط ادامه بده. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز قرار نیست کامل باشی؛ فقط ادامه بده. 💪 به خودت و تلاشت اعتماد کن.', 'امروز قرار نیست کامل باشی؛ فقط ادامه بده. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز قرار نیست کامل باشی؛ فقط ادامه بده. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'یک صبح تازه، یک فرصت تازه برای بهتر شدن. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'یک صبح تازه، یک فرصت تازه برای بهتر شدن. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'یک صبح تازه، یک فرصت تازه برای بهتر شدن. 💪 به خودت و تلاشت اعتماد کن.', 'یک صبح تازه، یک فرصت تازه برای بهتر شدن. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'یک صبح تازه، یک فرصت تازه برای بهتر شدن. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'کارهای بزرگ با شروع\u200cهای ساده شکل می\u200cگیرند. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'کارهای بزرگ با شروع\u200cهای ساده شکل می\u200cگیرند. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'کارهای بزرگ با شروع\u200cهای ساده شکل می\u200cگیرند. 💪 به خودت و تلاشت اعتماد کن.', 'کارهای بزرگ با شروع\u200cهای ساده شکل می\u200cگیرند. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'کارهای بزرگ با شروع\u200cهای ساده شکل می\u200cگیرند. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز چیزی را شروع کن که فردای تو بابتش خوشحال باشد. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز چیزی را شروع کن که فردای تو بابتش خوشحال باشد. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز چیزی را شروع کن که فردای تو بابتش خوشحال باشد. 💪 به خودت و تلاشت اعتماد کن.', 'امروز چیزی را شروع کن که فردای تو بابتش خوشحال باشد. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز چیزی را شروع کن که فردای تو بابتش خوشحال باشد. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'به جای فکر کردن به همه مسیر، قدم اول را بردار. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'به جای فکر کردن به همه مسیر، قدم اول را بردار. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'به جای فکر کردن به همه مسیر، قدم اول را بردار. 💪 به خودت و تلاشت اعتماد کن.', 'به جای فکر کردن به همه مسیر، قدم اول را بردار. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'به جای فکر کردن به همه مسیر، قدم اول را بردار. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'صبح بخیر؛ امروز می\u200cتواند یکی از همان روزهای خوب باشد. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'صبح بخیر؛ امروز می\u200cتواند یکی از همان روزهای خوب باشد. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'صبح بخیر؛ امروز می\u200cتواند یکی از همان روزهای خوب باشد. 💪 به خودت و تلاشت اعتماد کن.', 'صبح بخیر؛ امروز می\u200cتواند یکی از همان روزهای خوب باشد. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'صبح بخیر؛ امروز می\u200cتواند یکی از همان روزهای خوب باشد. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز را با امید شروع کن و با تلاش ادامه بده. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز را با امید شروع کن و با تلاش ادامه بده. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز را با امید شروع کن و با تلاش ادامه بده. 💪 به خودت و تلاشت اعتماد کن.', 'امروز را با امید شروع کن و با تلاش ادامه بده. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز را با امید شروع کن و با تلاش ادامه بده. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'گاهی یک تصمیم کوچک، مسیر یک روز را عوض می\u200cکند. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'گاهی یک تصمیم کوچک، مسیر یک روز را عوض می\u200cکند. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'گاهی یک تصمیم کوچک، مسیر یک روز را عوض می\u200cکند. 💪 به خودت و تلاشت اعتماد کن.', 'گاهی یک تصمیم کوچک، مسیر یک روز را عوض می\u200cکند. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'گاهی یک تصمیم کوچک، مسیر یک روز را عوض می\u200cکند. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'هر روز فرصتی برای یاد گرفتن، ساختن و بهتر شدن است. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'هر روز فرصتی برای یاد گرفتن، ساختن و بهتر شدن است. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'هر روز فرصتی برای یاد گرفتن، ساختن و بهتر شدن است. 💪 به خودت و تلاشت اعتماد کن.', 'هر روز فرصتی برای یاد گرفتن، ساختن و بهتر شدن است. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'هر روز فرصتی برای یاد گرفتن، ساختن و بهتر شدن است. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز برای خودت یک دلیل کوچک برای لبخند پیدا کن. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز برای خودت یک دلیل کوچک برای لبخند پیدا کن. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز برای خودت یک دلیل کوچک برای لبخند پیدا کن. 💪 به خودت و تلاشت اعتماد کن.', 'امروز برای خودت یک دلیل کوچک برای لبخند پیدا کن. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز برای خودت یک دلیل کوچک برای لبخند پیدا کن. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'با حوصله جلو برو؛ نتیجه از استمرار می\u200cآید. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'با حوصله جلو برو؛ نتیجه از استمرار می\u200cآید. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'با حوصله جلو برو؛ نتیجه از استمرار می\u200cآید. 💪 به خودت و تلاشت اعتماد کن.', 'با حوصله جلو برو؛ نتیجه از استمرار می\u200cآید. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'با حوصله جلو برو؛ نتیجه از استمرار می\u200cآید. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز را به جای نگرانی، با برنامه شروع کن. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز را به جای نگرانی، با برنامه شروع کن. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز را به جای نگرانی، با برنامه شروع کن. 💪 به خودت و تلاشت اعتماد کن.', 'امروز را به جای نگرانی، با برنامه شروع کن. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز را به جای نگرانی، با برنامه شروع کن. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'هیچ قدم صادقی کوچک نیست؛ همه قدم\u200cها جمع می\u200cشوند. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'هیچ قدم صادقی کوچک نیست؛ همه قدم\u200cها جمع می\u200cشوند. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'هیچ قدم صادقی کوچک نیست؛ همه قدم\u200cها جمع می\u200cشوند. 💪 به خودت و تلاشت اعتماد کن.', 'هیچ قدم صادقی کوچک نیست؛ همه قدم\u200cها جمع می\u200cشوند. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'هیچ قدم صادقی کوچک نیست؛ همه قدم\u200cها جمع می\u200cشوند. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'صبح تازه یعنی هنوز فرصت داری بهتر انتخاب کنی. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'صبح تازه یعنی هنوز فرصت داری بهتر انتخاب کنی. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'صبح تازه یعنی هنوز فرصت داری بهتر انتخاب کنی. 💪 به خودت و تلاشت اعتماد کن.', 'صبح تازه یعنی هنوز فرصت داری بهتر انتخاب کنی. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'صبح تازه یعنی هنوز فرصت داری بهتر انتخاب کنی. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز به جای عجله، با تمرکز پیش برو. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز به جای عجله، با تمرکز پیش برو. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز به جای عجله، با تمرکز پیش برو. 💪 به خودت و تلاشت اعتماد کن.', 'امروز به جای عجله، با تمرکز پیش برو. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز به جای عجله، با تمرکز پیش برو. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'یک شروع آرام می\u200cتواند یک روز پربرکت بسازد. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'یک شروع آرام می\u200cتواند یک روز پربرکت بسازد. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'یک شروع آرام می\u200cتواند یک روز پربرکت بسازد. 💪 به خودت و تلاشت اعتماد کن.', 'یک شروع آرام می\u200cتواند یک روز پربرکت بسازد. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'یک شروع آرام می\u200cتواند یک روز پربرکت بسازد. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز را ساده بگیر؛ یک کار مهم را درست انجام بده. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز را ساده بگیر؛ یک کار مهم را درست انجام بده. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز را ساده بگیر؛ یک کار مهم را درست انجام بده. 💪 به خودت و تلاشت اعتماد کن.', 'امروز را ساده بگیر؛ یک کار مهم را درست انجام بده. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز را ساده بگیر؛ یک کار مهم را درست انجام بده. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امید را نگه دار و کار امروزت را انجام بده. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امید را نگه دار و کار امروزت را انجام بده. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امید را نگه دار و کار امروزت را انجام بده. 💪 به خودت و تلاشت اعتماد کن.', 'امید را نگه دار و کار امروزت را انجام بده. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امید را نگه دار و کار امروزت را انجام بده. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'هر صبح یادآور این است که هنوز فرصت ادامه دادن هست. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'هر صبح یادآور این است که هنوز فرصت ادامه دادن هست. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'هر صبح یادآور این است که هنوز فرصت ادامه دادن هست. 💪 به خودت و تلاشت اعتماد کن.', 'هر صبح یادآور این است که هنوز فرصت ادامه دادن هست. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'هر صبح یادآور این است که هنوز فرصت ادامه دادن هست. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز با خودت مهربان باش و در عین حال دست از تلاش نکش. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز با خودت مهربان باش و در عین حال دست از تلاش نکش. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز با خودت مهربان باش و در عین حال دست از تلاش نکش. 💪 به خودت و تلاشت اعتماد کن.', 'امروز با خودت مهربان باش و در عین حال دست از تلاش نکش. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز با خودت مهربان باش و در عین حال دست از تلاش نکش. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'گاهی بهترین کار این است که فقط شروع کنی. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'گاهی بهترین کار این است که فقط شروع کنی. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'گاهی بهترین کار این است که فقط شروع کنی. 💪 به خودت و تلاشت اعتماد کن.', 'گاهی بهترین کار این است که فقط شروع کنی. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'گاهی بهترین کار این است که فقط شروع کنی. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'یک قدم امروز، تو را از دیروز جلوتر می\u200cبرد. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'یک قدم امروز، تو را از دیروز جلوتر می\u200cبرد. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'یک قدم امروز، تو را از دیروز جلوتر می\u200cبرد. 💪 به خودت و تلاشت اعتماد کن.', 'یک قدم امروز، تو را از دیروز جلوتر می\u200cبرد. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'یک قدم امروز، تو را از دیروز جلوتر می\u200cبرد. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'صبح بخیر؛ مسیر با حرکت کردن روشن\u200cتر می\u200cشود. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'صبح بخیر؛ مسیر با حرکت کردن روشن\u200cتر می\u200cشود. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'صبح بخیر؛ مسیر با حرکت کردن روشن\u200cتر می\u200cشود. 💪 به خودت و تلاشت اعتماد کن.', 'صبح بخیر؛ مسیر با حرکت کردن روشن\u200cتر می\u200cشود. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'صبح بخیر؛ مسیر با حرکت کردن روشن\u200cتر می\u200cشود. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز را با انرژی خوب و فکر روشن آغاز کن. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز را با انرژی خوب و فکر روشن آغاز کن. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز را با انرژی خوب و فکر روشن آغاز کن. 💪 به خودت و تلاشت اعتماد کن.', 'امروز را با انرژی خوب و فکر روشن آغاز کن. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز را با انرژی خوب و فکر روشن آغاز کن. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'به خودت فرصت بده و در مسیرت ثابت\u200cقدم بمان. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'به خودت فرصت بده و در مسیرت ثابت\u200cقدم بمان. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'به خودت فرصت بده و در مسیرت ثابت\u200cقدم بمان. 💪 به خودت و تلاشت اعتماد کن.', 'به خودت فرصت بده و در مسیرت ثابت\u200cقدم بمان. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'به خودت فرصت بده و در مسیرت ثابت\u200cقدم بمان. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'قرار نیست همه جواب\u200cها را امروز داشته باشی؛ فقط ادامه بده. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'قرار نیست همه جواب\u200cها را امروز داشته باشی؛ فقط ادامه بده. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'قرار نیست همه جواب\u200cها را امروز داشته باشی؛ فقط ادامه بده. 💪 به خودت و تلاشت اعتماد کن.', 'قرار نیست همه جواب\u200cها را امروز داشته باشی؛ فقط ادامه بده. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'قرار نیست همه جواب\u200cها را امروز داشته باشی؛ فقط ادامه بده. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'هر روز جدید، صفحه\u200cای تازه برای نوشتن است. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'هر روز جدید، صفحه\u200cای تازه برای نوشتن است. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'هر روز جدید، صفحه\u200cای تازه برای نوشتن است. 💪 به خودت و تلاشت اعتماد کن.', 'هر روز جدید، صفحه\u200cای تازه برای نوشتن است. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'هر روز جدید، صفحه\u200cای تازه برای نوشتن است. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز را با یک هدف روشن شروع کن. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز را با یک هدف روشن شروع کن. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز را با یک هدف روشن شروع کن. 💪 به خودت و تلاشت اعتماد کن.', 'امروز را با یک هدف روشن شروع کن. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز را با یک هدف روشن شروع کن. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'تلاش آرام و پیوسته، نتیجه\u200cهای ماندگار می\u200cسازد. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'تلاش آرام و پیوسته، نتیجه\u200cهای ماندگار می\u200cسازد. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'تلاش آرام و پیوسته، نتیجه\u200cهای ماندگار می\u200cسازد. 💪 به خودت و تلاشت اعتماد کن.', 'تلاش آرام و پیوسته، نتیجه\u200cهای ماندگار می\u200cسازد. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'تلاش آرام و پیوسته، نتیجه\u200cهای ماندگار می\u200cسازد. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'صبح بخیر؛ اتفاق\u200cهای خوب گاهی از ساده\u200cترین قدم\u200cها شروع می\u200cشوند. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'صبح بخیر؛ اتفاق\u200cهای خوب گاهی از ساده\u200cترین قدم\u200cها شروع می\u200cشوند. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'صبح بخیر؛ اتفاق\u200cهای خوب گاهی از ساده\u200cترین قدم\u200cها شروع می\u200cشوند. 💪 به خودت و تلاشت اعتماد کن.', 'صبح بخیر؛ اتفاق\u200cهای خوب گاهی از ساده\u200cترین قدم\u200cها شروع می\u200cشوند. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'صبح بخیر؛ اتفاق\u200cهای خوب گاهی از ساده\u200cترین قدم\u200cها شروع می\u200cشوند. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز به چیزی که می\u200cسازی افتخار کن، حتی اگر هنوز کامل نیست. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز به چیزی که می\u200cسازی افتخار کن، حتی اگر هنوز کامل نیست. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز به چیزی که می\u200cسازی افتخار کن، حتی اگر هنوز کامل نیست. 💪 به خودت و تلاشت اعتماد کن.', 'امروز به چیزی که می\u200cسازی افتخار کن، حتی اگر هنوز کامل نیست. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز به چیزی که می\u200cسازی افتخار کن، حتی اگر هنوز کامل نیست. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'به جای منتظر ماندن برای شرایط عالی، از همین امروز شروع کن. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'به جای منتظر ماندن برای شرایط عالی، از همین امروز شروع کن. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'به جای منتظر ماندن برای شرایط عالی، از همین امروز شروع کن. 💪 به خودت و تلاشت اعتماد کن.', 'به جای منتظر ماندن برای شرایط عالی، از همین امروز شروع کن. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'به جای منتظر ماندن برای شرایط عالی، از همین امروز شروع کن. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز می\u200cتواند شروع یک عادت خوب باشد. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز می\u200cتواند شروع یک عادت خوب باشد. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز می\u200cتواند شروع یک عادت خوب باشد. 💪 به خودت و تلاشت اعتماد کن.', 'امروز می\u200cتواند شروع یک عادت خوب باشد. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز می\u200cتواند شروع یک عادت خوب باشد. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'با دل آرام و ذهن روشن، روزت را بساز. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'با دل آرام و ذهن روشن، روزت را بساز. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'با دل آرام و ذهن روشن، روزت را بساز. 💪 به خودت و تلاشت اعتماد کن.', 'با دل آرام و ذهن روشن، روزت را بساز. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'با دل آرام و ذهن روشن، روزت را بساز. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'صبح بخیر؛ هرچه بیشتر ادامه بدهی، مسیر آشناتر می\u200cشود. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'صبح بخیر؛ هرچه بیشتر ادامه بدهی، مسیر آشناتر می\u200cشود. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'صبح بخیر؛ هرچه بیشتر ادامه بدهی، مسیر آشناتر می\u200cشود. 💪 به خودت و تلاشت اعتماد کن.', 'صبح بخیر؛ هرچه بیشتر ادامه بدهی، مسیر آشناتر می\u200cشود. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'صبح بخیر؛ هرچه بیشتر ادامه بدهی، مسیر آشناتر می\u200cشود. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز فرصت خوبی است برای یک انتخاب بهتر. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز فرصت خوبی است برای یک انتخاب بهتر. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز فرصت خوبی است برای یک انتخاب بهتر. 💪 به خودت و تلاشت اعتماد کن.', 'امروز فرصت خوبی است برای یک انتخاب بهتر. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز فرصت خوبی است برای یک انتخاب بهتر. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'موفقیت همیشه پر سر و صدا نیست؛ گاهی فقط ادامه دادن است. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'موفقیت همیشه پر سر و صدا نیست؛ گاهی فقط ادامه دادن است. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'موفقیت همیشه پر سر و صدا نیست؛ گاهی فقط ادامه دادن است. 💪 به خودت و تلاشت اعتماد کن.', 'موفقیت همیشه پر سر و صدا نیست؛ گاهی فقط ادامه دادن است. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'موفقیت همیشه پر سر و صدا نیست؛ گاهی فقط ادامه دادن است. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'صبح را با شکرگزاری برای فرصت امروز شروع کن. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'صبح را با شکرگزاری برای فرصت امروز شروع کن. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'صبح را با شکرگزاری برای فرصت امروز شروع کن. 💪 به خودت و تلاشت اعتماد کن.', 'صبح را با شکرگزاری برای فرصت امروز شروع کن. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'صبح را با شکرگزاری برای فرصت امروز شروع کن. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'امروز یک قدم کوچک بردار و به خودت ثابت کن که می\u200cتوانی. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'امروز یک قدم کوچک بردار و به خودت ثابت کن که می\u200cتوانی. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'امروز یک قدم کوچک بردار و به خودت ثابت کن که می\u200cتوانی. 💪 به خودت و تلاشت اعتماد کن.', 'امروز یک قدم کوچک بردار و به خودت ثابت کن که می\u200cتوانی. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'امروز یک قدم کوچک بردار و به خودت ثابت کن که می\u200cتوانی. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.', 'روز خوب را خودت با انتخاب\u200cهای خوبت می\u200cسازی. 🌱 قدم\u200cبه\u200cقدم جلو برو.', 'روز خوب را خودت با انتخاب\u200cهای خوبت می\u200cسازی. ✨ روزت روشن و پر از اتفاق\u200cهای خوب.', 'روز خوب را خودت با انتخاب\u200cهای خوبت می\u200cسازی. 💪 به خودت و تلاشت اعتماد کن.', 'روز خوب را خودت با انتخاب\u200cهای خوبت می\u200cسازی. 🌷 امیدوارم امروز دلت آرام\u200cتر و لبت خندون\u200cتر باشه.', 'روز خوب را خودت با انتخاب\u200cهای خوبت می\u200cسازی. ☀️ صبح قشنگی برات آرزو می\u200cکنیم.'],
    "night": ['امروز هرچقدر هم شلوغ بود، حالا وقت آرام شدن است. 🌙 شبت آروم و خوابت شیرین.', 'امروز هرچقدر هم شلوغ بود، حالا وقت آرام شدن است. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امروز هرچقدر هم شلوغ بود، حالا وقت آرام شدن است. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امروز هرچقدر هم شلوغ بود، حالا وقت آرام شدن است. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امروز هرچقدر هم شلوغ بود، حالا وقت آرام شدن است. 🌌 شب بخیر از طرف تنین ایران.', 'شب بخیر؛ کارهای امروز تمام شد و فردا فرصت تازه\u200cای است. 🌙 شبت آروم و خوابت شیرین.', 'شب بخیر؛ کارهای امروز تمام شد و فردا فرصت تازه\u200cای است. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب بخیر؛ کارهای امروز تمام شد و فردا فرصت تازه\u200cای است. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب بخیر؛ کارهای امروز تمام شد و فردا فرصت تازه\u200cای است. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب بخیر؛ کارهای امروز تمام شد و فردا فرصت تازه\u200cای است. 🌌 شب بخیر از طرف تنین ایران.', 'قبل از خواب، چند لحظه برای خودت و آرامشت وقت بگذار. 🌙 شبت آروم و خوابت شیرین.', 'قبل از خواب، چند لحظه برای خودت و آرامشت وقت بگذار. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'قبل از خواب، چند لحظه برای خودت و آرامشت وقت بگذار. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'قبل از خواب، چند لحظه برای خودت و آرامشت وقت بگذار. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'قبل از خواب، چند لحظه برای خودت و آرامشت وقت بگذار. 🌌 شب بخیر از طرف تنین ایران.', 'امشب خستگی\u200cها را کنار بگذار و به فردا با امید نگاه کن. 🌙 شبت آروم و خوابت شیرین.', 'امشب خستگی\u200cها را کنار بگذار و به فردا با امید نگاه کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امشب خستگی\u200cها را کنار بگذار و به فردا با امید نگاه کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امشب خستگی\u200cها را کنار بگذار و به فردا با امید نگاه کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امشب خستگی\u200cها را کنار بگذار و به فردا با امید نگاه کن. 🌌 شب بخیر از طرف تنین ایران.', 'هر روزی پایان دارد؛ امشب نوبت استراحت و آرامش است. 🌙 شبت آروم و خوابت شیرین.', 'هر روزی پایان دارد؛ امشب نوبت استراحت و آرامش است. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'هر روزی پایان دارد؛ امشب نوبت استراحت و آرامش است. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'هر روزی پایان دارد؛ امشب نوبت استراحت و آرامش است. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'هر روزی پایان دارد؛ امشب نوبت استراحت و آرامش است. 🌌 شب بخیر از طرف تنین ایران.', 'اگر امروز سخت گذشت، اجازه بده شب کمی سبک\u200cترت کند. 🌙 شبت آروم و خوابت شیرین.', 'اگر امروز سخت گذشت، اجازه بده شب کمی سبک\u200cترت کند. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'اگر امروز سخت گذشت، اجازه بده شب کمی سبک\u200cترت کند. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'اگر امروز سخت گذشت، اجازه بده شب کمی سبک\u200cترت کند. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'اگر امروز سخت گذشت، اجازه بده شب کمی سبک\u200cترت کند. 🌌 شب بخیر از طرف تنین ایران.', 'شب فرصتی است برای جمع کردن فکرها و آرام کردن دل. 🌙 شبت آروم و خوابت شیرین.', 'شب فرصتی است برای جمع کردن فکرها و آرام کردن دل. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب فرصتی است برای جمع کردن فکرها و آرام کردن دل. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب فرصتی است برای جمع کردن فکرها و آرام کردن دل. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب فرصتی است برای جمع کردن فکرها و آرام کردن دل. 🌌 شب بخیر از طرف تنین ایران.', 'امشب بابت قدم\u200cهایی که امروز برداشتی از خودت تشکر کن. 🌙 شبت آروم و خوابت شیرین.', 'امشب بابت قدم\u200cهایی که امروز برداشتی از خودت تشکر کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امشب بابت قدم\u200cهایی که امروز برداشتی از خودت تشکر کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امشب بابت قدم\u200cهایی که امروز برداشتی از خودت تشکر کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امشب بابت قدم\u200cهایی که امروز برداشتی از خودت تشکر کن. 🌌 شب بخیر از طرف تنین ایران.', 'فردا هنوز نیامده؛ امشب فقط آرام باش. 🌙 شبت آروم و خوابت شیرین.', 'فردا هنوز نیامده؛ امشب فقط آرام باش. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'فردا هنوز نیامده؛ امشب فقط آرام باش. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'فردا هنوز نیامده؛ امشب فقط آرام باش. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'فردا هنوز نیامده؛ امشب فقط آرام باش. 🌌 شب بخیر از طرف تنین ایران.', 'گاهی بهترین پایان یک روز، خواب آرام است. 🌙 شبت آروم و خوابت شیرین.', 'گاهی بهترین پایان یک روز، خواب آرام است. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'گاهی بهترین پایان یک روز، خواب آرام است. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'گاهی بهترین پایان یک روز، خواب آرام است. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'گاهی بهترین پایان یک روز، خواب آرام است. 🌌 شب بخیر از طرف تنین ایران.', 'شب بخیر؛ لازم نیست همه چیز را همین امشب حل کنی. 🌙 شبت آروم و خوابت شیرین.', 'شب بخیر؛ لازم نیست همه چیز را همین امشب حل کنی. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب بخیر؛ لازم نیست همه چیز را همین امشب حل کنی. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب بخیر؛ لازم نیست همه چیز را همین امشب حل کنی. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب بخیر؛ لازم نیست همه چیز را همین امشب حل کنی. 🌌 شب بخیر از طرف تنین ایران.', 'امروز گذشت؛ فردا را با ذهنی تازه شروع خواهی کرد. 🌙 شبت آروم و خوابت شیرین.', 'امروز گذشت؛ فردا را با ذهنی تازه شروع خواهی کرد. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امروز گذشت؛ فردا را با ذهنی تازه شروع خواهی کرد. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امروز گذشت؛ فردا را با ذهنی تازه شروع خواهی کرد. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امروز گذشت؛ فردا را با ذهنی تازه شروع خواهی کرد. 🌌 شب بخیر از طرف تنین ایران.', 'چشم\u200cهایت را ببند و اجازه بده خستگی روز تمام شود. 🌙 شبت آروم و خوابت شیرین.', 'چشم\u200cهایت را ببند و اجازه بده خستگی روز تمام شود. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'چشم\u200cهایت را ببند و اجازه بده خستگی روز تمام شود. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'چشم\u200cهایت را ببند و اجازه بده خستگی روز تمام شود. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'چشم\u200cهایت را ببند و اجازه بده خستگی روز تمام شود. 🌌 شب بخیر از طرف تنین ایران.', 'شب آرام، دل آرام و فردایی روشن برایت آرزو می\u200cکنیم. 🌙 شبت آروم و خوابت شیرین.', 'شب آرام، دل آرام و فردایی روشن برایت آرزو می\u200cکنیم. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب آرام، دل آرام و فردایی روشن برایت آرزو می\u200cکنیم. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب آرام، دل آرام و فردایی روشن برایت آرزو می\u200cکنیم. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب آرام، دل آرام و فردایی روشن برایت آرزو می\u200cکنیم. 🌌 شب بخیر از طرف تنین ایران.', 'هر پایان می\u200cتواند مقدمه یک شروع تازه باشد. 🌙 شبت آروم و خوابت شیرین.', 'هر پایان می\u200cتواند مقدمه یک شروع تازه باشد. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'هر پایان می\u200cتواند مقدمه یک شروع تازه باشد. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'هر پایان می\u200cتواند مقدمه یک شروع تازه باشد. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'هر پایان می\u200cتواند مقدمه یک شروع تازه باشد. 🌌 شب بخیر از طرف تنین ایران.', 'امشب به جای نگرانی، به چیزهای خوب امروز فکر کن. 🌙 شبت آروم و خوابت شیرین.', 'امشب به جای نگرانی، به چیزهای خوب امروز فکر کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امشب به جای نگرانی، به چیزهای خوب امروز فکر کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امشب به جای نگرانی، به چیزهای خوب امروز فکر کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امشب به جای نگرانی، به چیزهای خوب امروز فکر کن. 🌌 شب بخیر از طرف تنین ایران.', 'خواب خوب، شروع خوب فرداست. 🌙 شبت آروم و خوابت شیرین.', 'خواب خوب، شروع خوب فرداست. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'خواب خوب، شروع خوب فرداست. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'خواب خوب، شروع خوب فرداست. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'خواب خوب، شروع خوب فرداست. 🌌 شب بخیر از طرف تنین ایران.', 'شب بخیر؛ تلاش امروزت ارزشمند بود، حتی اگر همه\u200cچیز طبق برنامه پیش نرفت. 🌙 شبت آروم و خوابت شیرین.', 'شب بخیر؛ تلاش امروزت ارزشمند بود، حتی اگر همه\u200cچیز طبق برنامه پیش نرفت. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب بخیر؛ تلاش امروزت ارزشمند بود، حتی اگر همه\u200cچیز طبق برنامه پیش نرفت. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب بخیر؛ تلاش امروزت ارزشمند بود، حتی اگر همه\u200cچیز طبق برنامه پیش نرفت. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب بخیر؛ تلاش امروزت ارزشمند بود، حتی اگر همه\u200cچیز طبق برنامه پیش نرفت. 🌌 شب بخیر از طرف تنین ایران.', 'امشب خودت را بابت ادامه دادن تحسین کن. 🌙 شبت آروم و خوابت شیرین.', 'امشب خودت را بابت ادامه دادن تحسین کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امشب خودت را بابت ادامه دادن تحسین کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امشب خودت را بابت ادامه دادن تحسین کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امشب خودت را بابت ادامه دادن تحسین کن. 🌌 شب بخیر از طرف تنین ایران.', 'روز را با آرامش تمام کن و فردا دوباره ادامه بده. 🌙 شبت آروم و خوابت شیرین.', 'روز را با آرامش تمام کن و فردا دوباره ادامه بده. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'روز را با آرامش تمام کن و فردا دوباره ادامه بده. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'روز را با آرامش تمام کن و فردا دوباره ادامه بده. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'روز را با آرامش تمام کن و فردا دوباره ادامه بده. 🌌 شب بخیر از طرف تنین ایران.', 'شب یعنی چند ساعت فاصله از شلوغی\u200cهای روز. 🌙 شبت آروم و خوابت شیرین.', 'شب یعنی چند ساعت فاصله از شلوغی\u200cهای روز. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب یعنی چند ساعت فاصله از شلوغی\u200cهای روز. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب یعنی چند ساعت فاصله از شلوغی\u200cهای روز. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب یعنی چند ساعت فاصله از شلوغی\u200cهای روز. 🌌 شب بخیر از طرف تنین ایران.', 'امشب وقت آن است که ذهن را کمی از کار و فکر خالی کنی. 🌙 شبت آروم و خوابت شیرین.', 'امشب وقت آن است که ذهن را کمی از کار و فکر خالی کنی. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امشب وقت آن است که ذهن را کمی از کار و فکر خالی کنی. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امشب وقت آن است که ذهن را کمی از کار و فکر خالی کنی. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امشب وقت آن است که ذهن را کمی از کار و فکر خالی کنی. 🌌 شب بخیر از طرف تنین ایران.', 'هر روز تجربه\u200cای است؛ امشب از آن عبور کن و استراحت کن. 🌙 شبت آروم و خوابت شیرین.', 'هر روز تجربه\u200cای است؛ امشب از آن عبور کن و استراحت کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'هر روز تجربه\u200cای است؛ امشب از آن عبور کن و استراحت کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'هر روز تجربه\u200cای است؛ امشب از آن عبور کن و استراحت کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'هر روز تجربه\u200cای است؛ امشب از آن عبور کن و استراحت کن. 🌌 شب بخیر از طرف تنین ایران.', 'آرام بخواب؛ فردا فرصت تازه\u200cای برای ساختن داری. 🌙 شبت آروم و خوابت شیرین.', 'آرام بخواب؛ فردا فرصت تازه\u200cای برای ساختن داری. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'آرام بخواب؛ فردا فرصت تازه\u200cای برای ساختن داری. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'آرام بخواب؛ فردا فرصت تازه\u200cای برای ساختن داری. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'آرام بخواب؛ فردا فرصت تازه\u200cای برای ساختن داری. 🌌 شب بخیر از طرف تنین ایران.', 'شب بخیر؛ اتفاق\u200cهای امروز را به فردا منتقل نکن، کمی استراحت کن. 🌙 شبت آروم و خوابت شیرین.', 'شب بخیر؛ اتفاق\u200cهای امروز را به فردا منتقل نکن، کمی استراحت کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب بخیر؛ اتفاق\u200cهای امروز را به فردا منتقل نکن، کمی استراحت کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب بخیر؛ اتفاق\u200cهای امروز را به فردا منتقل نکن، کمی استراحت کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب بخیر؛ اتفاق\u200cهای امروز را به فردا منتقل نکن، کمی استراحت کن. 🌌 شب بخیر از طرف تنین ایران.', 'گاهی یک خواب خوب، بهترین تصمیم برای ادامه مسیر است. 🌙 شبت آروم و خوابت شیرین.', 'گاهی یک خواب خوب، بهترین تصمیم برای ادامه مسیر است. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'گاهی یک خواب خوب، بهترین تصمیم برای ادامه مسیر است. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'گاهی یک خواب خوب، بهترین تصمیم برای ادامه مسیر است. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'گاهی یک خواب خوب، بهترین تصمیم برای ادامه مسیر است. 🌌 شب بخیر از طرف تنین ایران.', 'امشب به خودت اجازه بده خسته باشی و استراحت کنی. 🌙 شبت آروم و خوابت شیرین.', 'امشب به خودت اجازه بده خسته باشی و استراحت کنی. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امشب به خودت اجازه بده خسته باشی و استراحت کنی. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امشب به خودت اجازه بده خسته باشی و استراحت کنی. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امشب به خودت اجازه بده خسته باشی و استراحت کنی. 🌌 شب بخیر از طرف تنین ایران.', 'فردا یک روز تازه است؛ امشب آرامش را انتخاب کن. 🌙 شبت آروم و خوابت شیرین.', 'فردا یک روز تازه است؛ امشب آرامش را انتخاب کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'فردا یک روز تازه است؛ امشب آرامش را انتخاب کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'فردا یک روز تازه است؛ امشب آرامش را انتخاب کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'فردا یک روز تازه است؛ امشب آرامش را انتخاب کن. 🌌 شب بخیر از طرف تنین ایران.', 'شب بخیر؛ هر قدمی که امروز برداشتی بخشی از مسیر تو بود. 🌙 شبت آروم و خوابت شیرین.', 'شب بخیر؛ هر قدمی که امروز برداشتی بخشی از مسیر تو بود. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب بخیر؛ هر قدمی که امروز برداشتی بخشی از مسیر تو بود. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب بخیر؛ هر قدمی که امروز برداشتی بخشی از مسیر تو بود. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب بخیر؛ هر قدمی که امروز برداشتی بخشی از مسیر تو بود. 🌌 شب بخیر از طرف تنین ایران.', 'امشب را با امید به فردا به پایان برسان. 🌙 شبت آروم و خوابت شیرین.', 'امشب را با امید به فردا به پایان برسان. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امشب را با امید به فردا به پایان برسان. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امشب را با امید به فردا به پایان برسان. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امشب را با امید به فردا به پایان برسان. 🌌 شب بخیر از طرف تنین ایران.', 'روز تمام شد؛ حالا نوبت آرامش توست. 🌙 شبت آروم و خوابت شیرین.', 'روز تمام شد؛ حالا نوبت آرامش توست. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'روز تمام شد؛ حالا نوبت آرامش توست. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'روز تمام شد؛ حالا نوبت آرامش توست. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'روز تمام شد؛ حالا نوبت آرامش توست. 🌌 شب بخیر از طرف تنین ایران.', 'شب آرامی داشته باشی و صبح با انرژی بیدار شوی. 🌙 شبت آروم و خوابت شیرین.', 'شب آرامی داشته باشی و صبح با انرژی بیدار شوی. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب آرامی داشته باشی و صبح با انرژی بیدار شوی. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب آرامی داشته باشی و صبح با انرژی بیدار شوی. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب آرامی داشته باشی و صبح با انرژی بیدار شوی. 🌌 شب بخیر از طرف تنین ایران.', 'امشب همه چیز را کمی ساده\u200cتر ببین و استراحت کن. 🌙 شبت آروم و خوابت شیرین.', 'امشب همه چیز را کمی ساده\u200cتر ببین و استراحت کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امشب همه چیز را کمی ساده\u200cتر ببین و استراحت کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امشب همه چیز را کمی ساده\u200cتر ببین و استراحت کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امشب همه چیز را کمی ساده\u200cتر ببین و استراحت کن. 🌌 شب بخیر از طرف تنین ایران.', 'شب بخیر؛ فردا دوباره فرصت داری بهتر ادامه بدهی. 🌙 شبت آروم و خوابت شیرین.', 'شب بخیر؛ فردا دوباره فرصت داری بهتر ادامه بدهی. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب بخیر؛ فردا دوباره فرصت داری بهتر ادامه بدهی. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب بخیر؛ فردا دوباره فرصت داری بهتر ادامه بدهی. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب بخیر؛ فردا دوباره فرصت داری بهتر ادامه بدهی. 🌌 شب بخیر از طرف تنین ایران.', 'خستگی امروز را به خواب بسپار و برای فردا انرژی ذخیره کن. 🌙 شبت آروم و خوابت شیرین.', 'خستگی امروز را به خواب بسپار و برای فردا انرژی ذخیره کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'خستگی امروز را به خواب بسپار و برای فردا انرژی ذخیره کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'خستگی امروز را به خواب بسپار و برای فردا انرژی ذخیره کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'خستگی امروز را به خواب بسپار و برای فردا انرژی ذخیره کن. 🌌 شب بخیر از طرف تنین ایران.', 'امشب برای دل خودت یک جای آرام بساز. 🌙 شبت آروم و خوابت شیرین.', 'امشب برای دل خودت یک جای آرام بساز. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امشب برای دل خودت یک جای آرام بساز. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امشب برای دل خودت یک جای آرام بساز. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امشب برای دل خودت یک جای آرام بساز. 🌌 شب بخیر از طرف تنین ایران.', 'شب بخیر؛ لازم نیست همیشه قوی باشی، گاهی فقط استراحت کن. 🌙 شبت آروم و خوابت شیرین.', 'شب بخیر؛ لازم نیست همیشه قوی باشی، گاهی فقط استراحت کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب بخیر؛ لازم نیست همیشه قوی باشی، گاهی فقط استراحت کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب بخیر؛ لازم نیست همیشه قوی باشی، گاهی فقط استراحت کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب بخیر؛ لازم نیست همیشه قوی باشی، گاهی فقط استراحت کن. 🌌 شب بخیر از طرف تنین ایران.', 'امروز تمام شد؛ برای فردا امیدت را نگه دار. 🌙 شبت آروم و خوابت شیرین.', 'امروز تمام شد؛ برای فردا امیدت را نگه دار. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امروز تمام شد؛ برای فردا امیدت را نگه دار. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امروز تمام شد؛ برای فردا امیدت را نگه دار. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امروز تمام شد؛ برای فردا امیدت را نگه دار. 🌌 شب بخیر از طرف تنین ایران.', 'شب فرصتی است برای نفس کشیدن و دوباره پیدا کردن انرژی. 🌙 شبت آروم و خوابت شیرین.', 'شب فرصتی است برای نفس کشیدن و دوباره پیدا کردن انرژی. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب فرصتی است برای نفس کشیدن و دوباره پیدا کردن انرژی. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب فرصتی است برای نفس کشیدن و دوباره پیدا کردن انرژی. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب فرصتی است برای نفس کشیدن و دوباره پیدا کردن انرژی. 🌌 شب بخیر از طرف تنین ایران.', 'آرامش امشب می\u200cتواند انرژی فردای تو باشد. 🌙 شبت آروم و خوابت شیرین.', 'آرامش امشب می\u200cتواند انرژی فردای تو باشد. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'آرامش امشب می\u200cتواند انرژی فردای تو باشد. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'آرامش امشب می\u200cتواند انرژی فردای تو باشد. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'آرامش امشب می\u200cتواند انرژی فردای تو باشد. 🌌 شب بخیر از طرف تنین ایران.', 'شب بخیر؛ به خودت فرصت بده از شلوغی روز فاصله بگیری. 🌙 شبت آروم و خوابت شیرین.', 'شب بخیر؛ به خودت فرصت بده از شلوغی روز فاصله بگیری. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب بخیر؛ به خودت فرصت بده از شلوغی روز فاصله بگیری. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب بخیر؛ به خودت فرصت بده از شلوغی روز فاصله بگیری. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب بخیر؛ به خودت فرصت بده از شلوغی روز فاصله بگیری. 🌌 شب بخیر از طرف تنین ایران.', 'امشب به جای مرور اشتباه\u200cها، برای فردا یک قدم بهتر در نظر بگیر. 🌙 شبت آروم و خوابت شیرین.', 'امشب به جای مرور اشتباه\u200cها، برای فردا یک قدم بهتر در نظر بگیر. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امشب به جای مرور اشتباه\u200cها، برای فردا یک قدم بهتر در نظر بگیر. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امشب به جای مرور اشتباه\u200cها، برای فردا یک قدم بهتر در نظر بگیر. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امشب به جای مرور اشتباه\u200cها، برای فردا یک قدم بهتر در نظر بگیر. 🌌 شب بخیر از طرف تنین ایران.', 'هر روز با همه خوبی و سختی\u200cاش می\u200cگذرد؛ امشب استراحت کن. 🌙 شبت آروم و خوابت شیرین.', 'هر روز با همه خوبی و سختی\u200cاش می\u200cگذرد؛ امشب استراحت کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'هر روز با همه خوبی و سختی\u200cاش می\u200cگذرد؛ امشب استراحت کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'هر روز با همه خوبی و سختی\u200cاش می\u200cگذرد؛ امشب استراحت کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'هر روز با همه خوبی و سختی\u200cاش می\u200cگذرد؛ امشب استراحت کن. 🌌 شب بخیر از طرف تنین ایران.', 'شب بخیر؛ فردا ادامه داستان توست. 🌙 شبت آروم و خوابت شیرین.', 'شب بخیر؛ فردا ادامه داستان توست. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب بخیر؛ فردا ادامه داستان توست. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب بخیر؛ فردا ادامه داستان توست. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب بخیر؛ فردا ادامه داستان توست. 🌌 شب بخیر از طرف تنین ایران.', 'امشب را با یک لبخند کوچک تمام کن. 🌙 شبت آروم و خوابت شیرین.', 'امشب را با یک لبخند کوچک تمام کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امشب را با یک لبخند کوچک تمام کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امشب را با یک لبخند کوچک تمام کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امشب را با یک لبخند کوچک تمام کن. 🌌 شب بخیر از طرف تنین ایران.', 'آرام بخواب و اجازه بده صبح، فکرهای تازه بیاورد. 🌙 شبت آروم و خوابت شیرین.', 'آرام بخواب و اجازه بده صبح، فکرهای تازه بیاورد. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'آرام بخواب و اجازه بده صبح، فکرهای تازه بیاورد. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'آرام بخواب و اجازه بده صبح، فکرهای تازه بیاورد. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'آرام بخواب و اجازه بده صبح، فکرهای تازه بیاورد. 🌌 شب بخیر از طرف تنین ایران.', 'شب بخیر؛ تو برای امروز به اندازه کافی تلاش کردی. 🌙 شبت آروم و خوابت شیرین.', 'شب بخیر؛ تو برای امروز به اندازه کافی تلاش کردی. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب بخیر؛ تو برای امروز به اندازه کافی تلاش کردی. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب بخیر؛ تو برای امروز به اندازه کافی تلاش کردی. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب بخیر؛ تو برای امروز به اندازه کافی تلاش کردی. 🌌 شب بخیر از طرف تنین ایران.', 'امشب وقت آرامش است، نه حل کردن همه مشکلات. 🌙 شبت آروم و خوابت شیرین.', 'امشب وقت آرامش است، نه حل کردن همه مشکلات. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'امشب وقت آرامش است، نه حل کردن همه مشکلات. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'امشب وقت آرامش است، نه حل کردن همه مشکلات. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'امشب وقت آرامش است، نه حل کردن همه مشکلات. 🌌 شب بخیر از طرف تنین ایران.', 'فردا فرصت دوباره\u200cای است؛ امشب خوب استراحت کن. 🌙 شبت آروم و خوابت شیرین.', 'فردا فرصت دوباره\u200cای است؛ امشب خوب استراحت کن. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'فردا فرصت دوباره\u200cای است؛ امشب خوب استراحت کن. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'فردا فرصت دوباره\u200cای است؛ امشب خوب استراحت کن. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'فردا فرصت دوباره\u200cای است؛ امشب خوب استراحت کن. 🌌 شب بخیر از طرف تنین ایران.', 'شب بخیر؛ امشب را با آرامش تمام کن و برای فردا انرژی نگه دار. 🌙 شبت آروم و خوابت شیرین.', 'شب بخیر؛ امشب را با آرامش تمام کن و برای فردا انرژی نگه دار. ✨ امیدواریم فردات از امروز روشن\u200cتر باشه.', 'شب بخیر؛ امشب را با آرامش تمام کن و برای فردا انرژی نگه دار. 💤 استراحت کن؛ فردا دوباره ادامه می\u200cدیم.', 'شب بخیر؛ امشب را با آرامش تمام کن و برای فردا انرژی نگه دار. 🤍 شبی آرام و دلی سبک برات آرزو داریم.', 'شب بخیر؛ امشب را با آرامش تمام کن و برای فردا انرژی نگه دار. 🌌 شب بخیر از طرف تنین ایران.'],
}
_WEEKDAYS_FA = {5: "شنبه", 6: "یکشنبه", 0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه", 3: "پنج‌شنبه", 4: "جمعه"}
_JALALI_MONTHS_FA = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

# وضعیت داخل حافظه (برای اینکه هر ۳۰ ثانیه به دیتابیس کوئری نزنیم)
_channel_done = {}
_channel_fail = {}
_channel_notified = {}
_channel_lock = None


def _fa(value):
    return str(value).translate(_FA_DIGITS)


def _get_channel_lock():
    global _channel_lock
    if _channel_lock is None:
        _channel_lock = asyncio.Lock()
    return _channel_lock


def meta_get(key):
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def meta_set(key, value):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO app_meta(key,value) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()
    finally:
        conn.close()


def meta_delete(key):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM app_meta WHERE key=%s", (key,))
        conn.commit()
    finally:
        conn.close()



def _channel_time_key(kind):
    return f"channel_{kind}_time"


def channel_schedule_min(kind):
    """Read current schedule from Supabase; fall back to environment defaults."""
    try:
        raw = meta_get(_channel_time_key(kind))
    except Exception:
        raw = None
    if not raw:
        return CHANNEL_MORNING_MIN if kind == "morning" else CHANNEL_NIGHT_MIN
    try:
        h, m = str(raw).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except Exception:
        pass
    return CHANNEL_MORNING_MIN if kind == "morning" else CHANNEL_NIGHT_MIN


def channel_posting_enabled():
    try:
        value = meta_get("channel_posting_enabled")
        if value is None:
            return True
        return str(value).strip().lower() not in ("0", "false", "off", "no", "disabled")
    except Exception:
        return True


def set_channel_posting_enabled(enabled):
    meta_set("channel_posting_enabled", "1" if enabled else "0")


def _valid_hhmm(value):
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except Exception:
        return None
    return None


def set_channel_schedule(kind, hhmm):
    value = _valid_hhmm(hhmm)
    if kind not in CHANNEL_KINDS or not value:
        return False
    meta_set(_channel_time_key(kind), value)
    return True


def seed_channel_defaults():
    """Seed 250 prepared messages per category once, without duplicating on restart."""
    try:
        conn = get_conn()
        try:
            for kind in CHANNEL_KINDS:
                existing_rows = conn.execute(
                    "SELECT text FROM channel_messages WHERE kind=%s",
                    (kind,),
                ).fetchall()
                existing = {r["text"] for r in existing_rows}
                target = len(CHANNEL_DEFAULTS[kind])
                if len(existing) >= target:
                    continue
                now_txt = iran_now_naive().strftime("%Y-%m-%d %H:%M")
                for msg in CHANNEL_DEFAULTS[kind]:
                    if msg not in existing:
                        conn.execute(
                            "INSERT INTO channel_messages(kind,text,active,created_at) VALUES(%s,%s,TRUE,%s)",
                            (kind, msg, now_txt),
                        )
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # If channel_messages does not exist, built-in defaults remain available.
        logger.warning(f"Could not seed prepared channel messages: {e}")


def channel_load_messages(kind):
    """Active messages from DB as [(key, text)]; empty list if table is missing/empty."""
    try:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT id, text FROM channel_messages WHERE kind=%s AND active=TRUE ORDER BY id",
                (kind,),
            ).fetchall()
        finally:
            conn.close()
        return [(f"db:{r['id']}", r["text"]) for r in rows]
    except Exception as e:
        logger.warning(f"Could not load channel_messages ({kind}): {e}")
        return []


def channel_pick_message(kind):
    """Random message that was not used recently."""
    items = channel_load_messages(kind)
    if not items:
        items = [(f"d:{i}", t) for i, t in enumerate(CHANNEL_DEFAULTS[kind])]
    recent_key = f"channel_recent_{kind}"
    try:
        recent = json.loads(meta_get(recent_key) or "[]")
    except Exception:
        recent = []
    valid = {k for k, _ in items}
    recent = [k for k in recent if k in valid]
    keep = min(len(items) - 1, 30)
    blocked = set(recent[-keep:]) if keep > 0 else set()
    fresh = [it for it in items if it[0] not in blocked] or items
    key, text = random.choice(fresh)
    recent.append(key)
    try:
        meta_set(recent_key, json.dumps(recent[-30:]))
    except Exception as e:
        logger.warning(f"Could not store recent channel messages: {e}")
    return text


def _weather_desc(code):
    if code == 0:
        return "☀️", "آسمان صاف"
    if code in (1, 2):
        return "🌤", "کمی ابری"
    if code == 3:
        return "☁️", "ابری"
    if code in (45, 48):
        return "🌫", "مه‌آلود"
    if code in (51, 53, 55, 56, 57):
        return "🌦", "نم‌نم باران"
    if code in (61, 63, 65, 66, 67):
        return "🌧", "بارانی"
    if code in (71, 73, 75, 77, 85, 86):
        return "❄️", "برفی"
    if code in (80, 81, 82):
        return "🌧", "رگبار"
    if code in (95, 96, 99):
        return "⛈", "رعد و برق"
    return "🌡", "نامشخص"


def _parse_weather(data):
    cur, daily = data["current"], data["daily"]
    return {
        "temp": cur["temperature_2m"],
        "humidity": cur.get("relative_humidity_2m"),
        "wind": cur.get("wind_speed_10m"),
        "code": int(cur.get("weather_code", -1)),
        "tmax": daily["temperature_2m_max"][0],
        "tmin": daily["temperature_2m_min"][0],
        "rain": (daily.get("precipitation_probability_max") or [None])[0],
    }


async def fetch_weather():
    """Open-Meteo (free, no API key). Returns None on any failure."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Asia%2FTehran&forecast_days=1"
    )
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Weather API returned HTTP %s", resp.status)
                    return None
                return _parse_weather(await resp.json())
    except Exception as e:
        logger.warning(f"Weather fetch failed: {e}")
        return None


def _weather_block(w):
    emoji, desc = _weather_desc(w["code"])
    lines = [
        f"{emoji} <b>آب‌وهوای {escape(WEATHER_CITY)}</b>",
        f"{desc}",
        f"🌡 دمای فعلی: {_fa(round(w['temp']))}°",
        f"🔼 بیشینه {_fa(round(w['tmax']))}°  |  🔽 کمینه {_fa(round(w['tmin']))}°",
    ]
    if w.get("rain") is not None:
        lines.append(f"☔ احتمال بارش: {_fa(round(w['rain']))}٪")
    if w.get("humidity") is not None:
        lines.append(f"💧 رطوبت: {_fa(round(w['humidity']))}٪")
    if w.get("wind") is not None:
        lines.append(f"🌬 باد: {_fa(round(w['wind']))} کیلومتر بر ساعت")
    return "\n".join(lines)


def channel_date_line(now):
    jy, jm, jd = gregorian_to_jalali(now.year, now.month, now.day)
    return _fa(f"{_WEEKDAYS_FA[now.weekday()]} {jd} {_JALALI_MONTHS_FA[jm - 1]} {jy}")


async def channel_build_text(kind, now):
    message = escape(channel_pick_message(kind))
    if kind == "morning":
        parts = ["☀️ <b>صبح بخیر!</b>", f"📅 {channel_date_line(now)}", "", message]
        weather = await fetch_weather()
        if weather:
            parts += ["", _weather_block(weather)]
        return "\n".join(parts)
    return "\n".join(["🌙 <b>شب بخیر</b>", "", message])


async def channel_delete_stored(bot, meta_key):
    """Delete the stored channel post (if any). A post removed by hand is not an error."""
    try:
        mid = meta_get(meta_key)
    except Exception as e:
        logger.warning(f"Could not read {meta_key}: {e}")
        return False
    if not mid:
        return False
    deleted = True
    try:
        await bot.delete_message(CHANNEL_ID, int(mid))
    except Exception as e:
        deleted = False
        logger.info(f"Channel post {mid} was not deleted (already removed?): {e}")
    try:
        meta_delete(meta_key)
    except Exception as e:
        logger.warning(f"Could not clear {meta_key}: {e}")
    return deleted


async def channel_post(bot, kind, force=False):
    """Send the morning/night post. Returns (ok, info). Never raises."""
    if not CHANNEL_ID:
        return False, "CHANNEL_ID تنظیم نشده است."
    async with _get_channel_lock():
        now = datetime.now(IRAN_TZ)
        today = now.strftime("%Y-%m-%d")
        date_key = f"channel_{kind}_date"
        try:
            if not force:
                if _channel_done.get(kind) == today:
                    return True, "قبلاً ارسال شده"
                if meta_get(date_key) == today:
                    _channel_done[kind] = today
                    return True, "قبلاً ارسال شده"

            text = await channel_build_text(kind, now)
            old_ids = {}
            for k in CHANNEL_KINDS:
                try:
                    old_ids[k] = meta_get(f"channel_{k}_msg")
                except Exception:
                    old_ids[k] = None

            msg = await bot.send_message(CHANNEL_ID, text, parse_mode="HTML")

            # پست جدید رفت؛ حالا پست‌های قبلی (همان نوع و نوع دیگر) پاک می‌شوند.
            for k, mid in old_ids.items():
                if not mid:
                    continue
                try:
                    await bot.delete_message(CHANNEL_ID, int(mid))
                except Exception as e:
                    logger.info(f"Old channel post {mid} was not deleted: {e}")
                if k != kind:
                    try:
                        meta_delete(f"channel_{k}_msg")
                    except Exception:
                        pass
            meta_set(f"channel_{kind}_msg", msg.message_id)
            meta_set(date_key, today)
            _channel_done[kind] = today
            return True, "ارسال شد"
        except Exception as e:
            logger.exception("Channel post (%s) failed: %s", kind, e)
            return False, str(e)


async def channel_tick(context: ContextTypes.DEFAULT_TYPE):
    """Runs every 30s. Posts at the scheduled time, or shortly after if the bot was asleep."""
    if not CHANNEL_ID or not channel_posting_enabled():
        return
    now = datetime.now(IRAN_TZ)
    today = now.strftime("%Y-%m-%d")
    now_min = now.hour * 60 + now.minute
    for kind in CHANNEL_KINDS:
        sched = channel_schedule_min(kind)
        if _channel_done.get(kind) == today:
            continue
        if not (sched <= now_min <= min(sched + CHANNEL_GRACE_MIN, 1439)):
            continue
        last_fail = _channel_fail.get(kind)
        if last_fail and (now - last_fail).total_seconds() < 300:
            continue
        ok, info = await channel_post(context.bot, kind)
        if ok:
            _channel_fail.pop(kind, None)
            continue
        _channel_fail[kind] = now
        if _channel_notified.get(kind) != today:
            _channel_notified[kind] = today
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"⚠️ ارسال پست {CHANNEL_KINDS[kind]} کانال ناموفق بود.\n{info}\n\n"
                        "بررسی کن ربات در کانال ادمین باشد و دسترسی «ارسال پیام» و «حذف پیام» داشته باشد. "
                        "هر ۵ دقیقه دوباره تلاش می‌شود.",
                    )
                except Exception:
                    pass


def setup_channel_jobs(app):
    if not CHANNEL_ID:
        logger.info("CHANNEL_ID is not set; channel auto-posting is disabled.")
        return
    if app.job_queue is None:
        logger.warning(
            "JobQueue is unavailable; channel auto-posting is disabled. "
            "Use python-telegram-bot[job-queue] in requirements.txt."
        )
        return
    seed_channel_defaults()
    app.job_queue.run_repeating(channel_tick, interval=30, first=10, name="channel_tick")
    logger.info(
        "Channel auto-posting enabled: morning %02d:%02d, night %02d:%02d (Asia/Tehran)",
        channel_schedule_min("morning") // 60, channel_schedule_min("morning") % 60,
        channel_schedule_min("night") // 60, channel_schedule_min("night") % 60,
    )


# ---- پنل ادمین: مدیریت پست‌های کانال ----
SELFTEST_DELAY_SECONDS = 20


async def channel_selftest_job(context: ContextTypes.DEFAULT_TYPE):
    """Fired by JobQueue after the self-test button: proves the scheduler works and the bot can post + delete."""
    data = context.job.data or {}
    admin_id = data.get("admin_id")
    started = data.get("started")
    elapsed = None
    if started is not None:
        elapsed = round((datetime.now(IRAN_TZ) - started).total_seconds())
    lines = ["🧪 <b>نتیجهٔ تست زندهٔ زمان‌بندی</b>", ""]
    if elapsed is not None:
        lines.append(f"✅ زمان‌بند job را اجرا کرد (حدود {_fa(elapsed)} ثانیه بعد از زدن دکمه).")
    msg = None
    try:
        msg = await context.bot.send_message(
            CHANNEL_ID, "🧪 پیام تست ربات؛ چند ثانیه دیگر پاک می‌شود.", disable_notification=True
        )
        lines.append("✅ ارسال پیام در کانال موفق بود.")
    except Exception as e:
        lines.append(f"❌ ارسال در کانال ناموفق بود: {escape(str(e))}")
    if msg is not None:
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(CHANNEL_ID, msg.message_id)
            lines.append("✅ حذف پیام از کانال موفق بود.")
        except Exception as e:
            lines.append(f"❌ حذف پیام ناموفق بود (دسترسی «حذف پیام‌ها» را بررسی کن): {escape(str(e))}")
    all_ok = all(l.startswith("✅") for l in lines[2:])
    lines += ["", "🎉 همه‌چیز سالمه." if all_ok else "⚠️ مورد ❌ بالا را برطرف کن و دوباره تست بزن."]
    if admin_id:
        try:
            await context.bot.send_message(admin_id, "\n".join(lines), parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Could not send self-test result to admin: {e}")


async def admin_channel_selftest(q, context):
    """Instant checks now + a live scheduled job a few seconds later."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 تست دوباره", callback_data="adm:chtest")],
        [InlineKeyboardButton("🔙 مدیریت کانال", callback_data="adm:channel")],
    ])
    if not CHANNEL_ID:
        await _safe_edit_text(
            q, "🧪 <b>تست مکانیزم</b>\n\n❌ متغیر محیطی <code>CHANNEL_ID</code> تنظیم نشده است.",
            parse_mode="HTML", reply_markup=keyboard,
        )
        return
    await _safe_edit_text(q, "⏳ در حال تست ...")

    lines = ["🧪 <b>نتیجهٔ تست مکانیزم</b>", ""]
    good = True

    def add(ok, text):
        nonlocal good
        good = good and ok
        lines.append(("✅ " if ok else "❌ ") + text)

    add(True, f"کانال: <code>{escape(str(CHANNEL_ID))}</code>")

    # 1) JobQueue
    jq = context.job_queue
    jobs = jq.get_jobs_by_name("channel_tick") if jq else []
    if jobs:
        add(True, "زمان‌بند (JobQueue) فعال است و کار اصلی کانال در حال اجراست.")
    elif jq is None:
        add(False, "زمان‌بند در دسترس نیست؛ در requirements بنویس <code>python-telegram-bot[job-queue]</code>.")
    else:
        add(False, "کار اصلی کانال ثبت نشده است (ری‌دیپلوی کن).")

    # 2) database
    try:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM channel_messages WHERE active=TRUE GROUP BY kind"
            ).fetchall()
        finally:
            conn.close()
        counts = {r["kind"]: r["n"] for r in rows}
        add(True, f"دیتابیس: پیام صبح {_fa(counts.get('morning', 0))} | پیام شب {_fa(counts.get('night', 0))} "
                  "(اگر صفر باشد از پیام‌های آماده استفاده می‌شود).")
    except Exception:
        add(False, "جدول <code>channel_messages</code> در Supabase ساخته نشده (فعلاً از پیام‌های آماده استفاده می‌شود).")

    # 3) bot permissions in the channel
    try:
        me = await context.bot.get_chat_member(CHANNEL_ID, context.bot.id)
        if me.status == "creator":
            add(True, "ربات مالک کانال است.")
        elif me.status == "administrator":
            add(bool(getattr(me, "can_post_messages", False)), "دسترسی «ارسال پیام» ربات در کانال.")
            add(bool(getattr(me, "can_delete_messages", False)), "دسترسی «حذف پیام‌ها» ربات در کانال.")
        else:
            add(False, f"ربات ادمین کانال نیست (وضعیت: {escape(str(me.status))}).")
    except Exception as e:
        add(False, f"دسترسی به کانال ممکن نیست: {escape(str(e))}")

    # 4) weather
    weather = await fetch_weather()
    if weather:
        add(True, f"آب‌وهوای {escape(WEATHER_CITY)}: {_fa(round(weather['temp']))}° — {_weather_desc(weather['code'])[1]}")
    else:
        add(False, "دریافت آب‌وهوا ناموفق بود (پست صبح بدون آب‌وهوا ارسال می‌شود).")

    # 5) what the scheduler would do right now
    now = datetime.now(IRAN_TZ)
    today = now.strftime("%Y-%m-%d")
    now_min = now.hour * 60 + now.minute
    lines += ["", f"🕐 ساعت تهران: {_fa(now.strftime('%H:%M'))}"]
    for kind, sched in (("morning", CHANNEL_MORNING_MIN), ("night", CHANNEL_NIGHT_MIN)):
        try:
            done = _channel_done.get(kind) == today or meta_get(f"channel_{kind}_date") == today
        except Exception:
            done = False
        hhmm = _fa(f"{sched // 60:02d}:{sched % 60:02d}")
        if done:
            state = "امروز ارسال شده ✔️"
        elif now_min < sched:
            state = f"در انتظار ساعت {hhmm} ({_fa(sched - now_min)} دقیقه دیگر)"
        elif now_min <= min(sched + CHANNEL_GRACE_MIN, 1439):
            state = f"ساعتش رسیده؛ در تیک بعدی (حداکثر ۳۰ ثانیه) ارسال می‌شود"
        else:
            state = "امروز از بازهٔ مجاز گذشته؛ فردا ارسال می‌شود"
        lines.append(f"{'☀️' if kind == 'morning' else '🌙'} پست {CHANNEL_KINDS[kind]}: {state}")

    # 6) live scheduler test
    if jq is not None:
        jq.run_once(
            channel_selftest_job, when=SELFTEST_DELAY_SECONDS,
            data={"admin_id": q.from_user.id, "started": now}, name="channel_selftest",
        )
        lines += ["", f"⏱ تست زندهٔ زمان‌بند: تا {_fa(SELFTEST_DELAY_SECONDS)} ثانیه دیگر یک پیام تست در کانال "
                      "ارسال و چند ثانیه بعد پاک می‌شود و نتیجه همین‌جا برایت می‌آید."]
    lines += ["", "🎉 تست‌های فوری سالم‌اند." if good else "⚠️ مورد ❌ بالا را برطرف کن و دوباره تست بزن."]
    await _safe_edit_text(q, "\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


def channel_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸ توقف پست‌گذاری", callback_data="adm:chpause"),
         InlineKeyboardButton("▶️ شروع مجدد", callback_data="adm:chresume")],
        [InlineKeyboardButton("⏰ تغییر ساعت‌ها", callback_data="adm:chtime")],
        [InlineKeyboardButton("🧪 تست مکانیزم", callback_data="adm:chtest")],
        [InlineKeyboardButton("➕ پیام صبح", callback_data="adm:chadd:morning"),
         InlineKeyboardButton("➕ پیام شب", callback_data="adm:chadd:night")],
        [InlineKeyboardButton("📋 لیست صبح", callback_data="adm:chlist:morning:0"),
         InlineKeyboardButton("📋 لیست شب", callback_data="adm:chlist:night:0")],
        [InlineKeyboardButton("📤 ارسال پست صبح همین الان", callback_data="adm:chpost:morning")],
        [InlineKeyboardButton("📤 ارسال پست شب همین الان", callback_data="adm:chpost:night")],
        [InlineKeyboardButton("🧹 پاک‌کردن پست‌های فعلی کانال", callback_data="adm:chclear")],
        [InlineKeyboardButton("🔄 بروزرسانی", callback_data="adm:channel")],
        [InlineKeyboardButton("🔙 پنل اصلی", callback_data="adm:home")],
    ])


async def _safe_edit_text(q, text, **kwargs):
    try:
        await q.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def admin_channel_view(q, note=""):
    if not CHANNEL_ID:
        await _safe_edit_text(
            q,
            "📢 <b>پست‌های کانال</b>\n\nمتغیر محیطی <code>CHANNEL_ID</code> تنظیم نشده است، "
            "پس ارسال خودکار غیرفعال است.",
            parse_mode="HTML", reply_markup=back_keyboard("home"),
        )
        return
    counts = {}
    table_ok = True
    try:
        conn = get_conn()
        try:
            for r in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM channel_messages WHERE active=TRUE GROUP BY kind"
            ).fetchall():
                counts[r["kind"]] = r["n"]
        finally:
            conn.close()
    except Exception:
        table_ok = False

    def last_post(kind):
        try:
            d = meta_get(f"channel_{kind}_date")
        except Exception:
            d = None
        return format_iran_jalali_fa(d, include_time=False) if d else "—"

    def t(minutes):
        return _fa(f"{minutes // 60:02d}:{minutes % 60:02d}")

    lines = []
    if note:
        lines += [escape(note), ""]
    morning_min = channel_schedule_min("morning")
    night_min = channel_schedule_min("night")
    enabled = channel_posting_enabled()
    lines += [
        "📢 <b>پست‌های کانال</b>",
        "",
        f"🎯 کانال: <code>{escape(str(CHANNEL_ID))}</code>",
        f"📡 وضعیت خودکار: <b>{'فعال ✅' if enabled else 'متوقف ⏸'}</b>",
        f"☀️ پست صبح: ساعت {t(morning_min)} — آخرین ارسال: {last_post('morning')}",
        f"🌙 پست شب: ساعت {t(night_min)} — آخرین ارسال: {last_post('night')}",
        "",
    ]
    if table_ok:
        lines.append(
            f"📝 پیام‌های ثبت‌شده: صبح {_fa(counts.get('morning', 0))} | شب {_fa(counts.get('night', 0))}"
        )
        if not counts.get("morning") or not counts.get("night"):
            lines.append("ℹ️ برای هر دسته‌ای که پیامی ثبت نکنی، از پیام‌های آمادهٔ داخل ربات استفاده می‌شود.")
    else:
        lines.append("⚠️ جدول <code>channel_messages</code> در Supabase ساخته نشده؛ فعلاً از پیام‌های آماده استفاده می‌شود.")
    await _safe_edit_text(q, "\n".join(lines), parse_mode="HTML", reply_markup=channel_admin_keyboard())


async def admin_channel_list(q, kind, page=0):
    if kind not in CHANNEL_KINDS:
        return
    per_page = 8
    page = max(0, page)
    try:
        conn = get_conn()
        try:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM channel_messages WHERE kind=%s", (kind,)
            ).fetchone()["n"]
            rows = conn.execute(
                "SELECT id, text FROM channel_messages WHERE kind=%s ORDER BY id DESC LIMIT %s OFFSET %s",
                (kind, per_page, page * per_page),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        await _safe_edit_text(q, "❌ جدول channel_messages در دسترس نیست.", reply_markup=back_keyboard("channel"))
        return
    if not rows and page > 0:
        await admin_channel_list(q, kind, page - 1)
        return
    if not rows:
        await _safe_edit_text(
            q, f"📋 هنوز پیام {CHANNEL_KINDS[kind]} ثبت نشده است.\n(از پیام‌های آماده استفاده می‌شود.)",
            reply_markup=back_keyboard("channel"),
        )
        return
    lines = [f"📋 <b>پیام‌های {CHANNEL_KINDS[kind]}</b> ({_fa(total)} مورد)", ""]
    buttons = []
    for r in rows:
        preview = " ".join(str(r["text"]).split())
        lines.append(f"#{r['id']}: {escape(preview[:90])}")
        buttons.append([InlineKeyboardButton(
            f"🗑 #{r['id']} | {preview[:24]}", callback_data=f"adm:chdel:{kind}:{r['id']}:{page}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"adm:chlist:{kind}:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"adm:chlist:{kind}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 مدیریت کانال", callback_data="adm:channel")])
    await _safe_edit_text(q, "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


# ----------------------------------------------------------------------------
# راه‌اندازی ربات
# ----------------------------------------------------------------------------
async def async_main():
    # Web Service رایگان Render باید یک HTTP port باز کند.
    # این نسخه به‌جای run_webhook داخلی، یک HTTP server کوچک aiohttp اجرا می‌کند
    # تا هم Telegram webhook و هم /health روی همان PORT در دسترس باشند.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Add BOT_TOKEN to the Render environment variables.")

    # اتصال به Supabase و بررسی اسکیمای production.
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^🧾 کاتالوگ محصولات$"), show_catalog))
    app.add_handler(MessageHandler(filters.Regex("^🛒 سبد خرید$"), show_cart))
    app.add_handler(MessageHandler(filters.Regex("^📦 سفارش‌های من$"), my_orders))
    app.add_handler(MessageHandler(filters.Regex(r"^💬\s*(?:مرکز پشتیبانی|پشتیبانی)$"), support))
    app.add_handler(CommandHandler("orders_admin", admin_orders))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ پنل مدیریت$"), admin_panel))
    app.add_handler(MessageHandler(filters.Regex(r"^(?:📦 سفارش‌ها|💳 پرداخت‌های در انتظار|🛠 مدیریت محصولات|🎫 پشتیبانی)(?: \(\d+\))?$"), admin_shortcut))

    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(support_callback, pattern=r"^support:"))
    app.add_handler(CallbackQueryHandler(order_callback, pattern=r"^order:"))
    app.add_handler(CallbackQueryHandler(customer_order_callback, pattern=r"^customer_order:"))
    app.add_handler(CallbackQueryHandler(payment_admin_callback, pattern=r"^payadmin:"))
    app.add_handler(CallbackQueryHandler(payment_callback, pattern=r"^pay:"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, receipt_photo))
    app.add_handler(CallbackQueryHandler(category_selected, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(add_quantity_callback, pattern=r"^addqty:"))
    app.add_handler(CallbackQueryHandler(add_to_cart, pattern=r"^add:"))
    app.add_handler(CallbackQueryHandler(remove_from_cart, pattern=r"^remove:"))

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("checkout", checkout_start),
            CallbackQueryHandler(checkout_start, pattern=r"^checkout:start$")
        ],
        states={
            ASK_QTY: [
                CallbackQueryHandler(checkout_quantity_callback, pattern=r"^qty:")
            ],
            ASK_NAME: [
                CallbackQueryHandler(handle_saved_name, pattern=r"^saved:name:"),
                CallbackQueryHandler(handle_name_confirmation, pattern=r"^nameconfirm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name),
            ],
            ASK_PHONE: [
                CallbackQueryHandler(handle_saved_phone, pattern=r"^saved:phone:"),
                MessageHandler(filters.CONTACT | (filters.TEXT & ~filters.COMMAND), ask_gender),
            ],
            ASK_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_address)],
            ASK_ADDRESS: [
                CallbackQueryHandler(handle_saved_address, pattern=r"^saved:address:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, finalize_order),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_checkout)],
        allow_reentry=True,  # زدن دوباره «ثبت سفارش» مکالمه را ری‌استارت می‌کند
        conversation_timeout=1800 if app.job_queue is not None else None,
    )
    app.add_handler(conv)

    # دکمه‌های مراحل ثبت سفارش که مکالمه‌شان منقضی شده (مثلاً بعد از ری‌استارت سرور)
    async def stale_checkout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.callback_query.answer(
            "این مرحله منقضی شده؛ لطفاً دوباره از سبد خرید «ثبت سفارش» را بزن.",
            show_alert=True,
        )
    app.add_handler(CallbackQueryHandler(stale_checkout_callback, pattern=r"^(qty|saved|nameconfirm):"))

    # هر خطای مدیریت‌نشده هم در لاگ ثبت می‌شود و هم به کاربر اطلاع داده می‌شود
    async def on_error(update, context):
        logger.error("Unhandled error", exc_info=context.error)
        if isinstance(update, Update):
            try:
                if update.callback_query:
                    await update.callback_query.answer("⚠️ مشکلی پیش آمد، دوباره تلاش کن.", show_alert=True)
                elif update.effective_message:
                    await update.effective_message.reply_text("⚠️ مشکلی پیش آمد. لطفاً دوباره تلاش کن.")
            except Exception:
                logger.exception("Could not notify user about error")
    app.add_error_handler(on_error)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_input))
    setup_channel_jobs(app)

    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if not hostname:
        raise RuntimeError("RENDER_EXTERNAL_HOSTNAME is not available. Deploy this bot as a Render Web Service.")

    port = int(os.getenv("PORT", "10000"))
    webhook_path = os.getenv("WEBHOOK_PATH", "telegram/webhook").strip("/")
    webhook_url = f"https://{hostname}/{webhook_path}"
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        logger.warning("WEBHOOK_SECRET is not set; anyone who knows the webhook URL can send fake updates.")

    async def health_handler(request):
        return web.Response(text="Bot is running", status=200, content_type="text/plain")

    async def webhook_handler(request):
        # اگر WEBHOOK_SECRET تنظیم شده باشد، Telegram باید همین secret را بفرستد.
        if webhook_secret:
            received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if received != webhook_secret:
                return web.Response(status=403, text="Forbidden")

        try:
            data = await request.json()
            update = Update.de_json(data, app.bot)
            await app.update_queue.put(update)
            return web.Response(text="OK", status=200)
        except Exception as e:
            logger.exception("Webhook request failed: %s", e)
            return web.Response(text="Bad Request", status=400)

    web_app = web.Application()
    # aiohttp automatically handles HEAD for GET routes; adding a separate
    # HEAD route would cause a duplicate-route RuntimeError.
    web_app.router.add_get("/health", health_handler)
    web_app.router.add_post(f"/{webhook_path}", webhook_handler)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)

    await app.initialize()
    await app.start()
    await app.bot.set_webhook(
        url=webhook_url,
        secret_token=webhook_secret or None,
        drop_pending_updates=False,
    )
    await site.start()

    logger.info("Bot is running with Telegram webhook: %s", webhook_url)
    logger.info("HTTP server is listening on 0.0.0.0:%s", port)
    logger.info("Health endpoint: https://%s/health", hostname)

    # Render باید پروسه را زنده نگه دارد.
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await app.stop()
        await app.shutdown()


def main():
    asyncio.run(async_main())

if __name__ == "__main__":
    main()
