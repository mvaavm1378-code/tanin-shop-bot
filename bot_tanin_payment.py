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
from html import escape
from aiohttp import web
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
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
def main_menu_keyboard(user_id=None):
    rows = [
        [KeyboardButton("🧾 کاتالوگ محصولات")],
        [KeyboardButton("🛒 سبد خرید"), KeyboardButton("📦 سفارش‌های من")],
        [KeyboardButton("💬 پشتیبانی")],
    ]
    if user_id in ADMIN_IDS:
        rows.append([KeyboardButton("⚙️ پنل مدیریت")])
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
            [InlineKeyboardButton("🔙 انصراف", callback_data=f"addqty:cancel:{product_id}:{qty}")],
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
            new_qty = 1
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
        "سلام 👋 به فروشگاه پوشاک بچگانه و زنانه خوش اومدی!\n"
        "از منوی زیر می‌تونی محصولات رو ببینی و سفارش بدی.",
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
            f"رنگ‌بندی طبق ژورنال موجود\n"
            + (f"{escape(p['pack_info'])}\n" if p['pack_info'] else "")
            + f"قیمت: {p['price']:,} تومان"
        )
        if p["photo_url"]:
            await query.message.reply_photo(
                p["photo_url"], caption=text, parse_mode="HTML",
                reply_markup=product_keyboard(p["id"]),
            )
        else:
            await query.message.reply_text(
                text, parse_mode="HTML", reply_markup=product_keyboard(p["id"])
            )


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

    await query.message.edit_text(
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
        await query.message.edit_text("این محصول دیگر موجود نیست.")
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
        await query.message.edit_text(
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

    await query.message.edit_text(
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
    if update.callback_query:
        await update.callback_query.answer()
        target_message = update.callback_query.message
    else:
        target_message = update.message

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
    user_id = update.effective_user.id
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM cart_items WHERE user_id=%s", (user_id,)
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("سبد خریدت خالیه، اول چیزی اضافه کن.")
        return ConversationHandler.END

    await show_checkout_quantities(target_message, user_id)
    return ASK_QTY


def get_saved_data(user_id, data_type):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, value FROM customer_saved_data WHERE user_id=%s AND data_type=%s ORDER BY id DESC",
        (user_id, data_type),
    ).fetchall()
    conn.close()
    return rows


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
    if not text or any((not ch.isalpha()) or not ("\u0600" <= ch <= "\u06ff") for ch in text if ch != " "):
        return None
    parts = text.split(" ")
    if len(parts) < 2 or any(len(part) < 2 for part in parts):
        return None
    return text


def name_confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ بله، درست است", callback_data="nameconfirm:yes")],
        [InlineKeyboardButton("✏️ ویرایش", callback_data="nameconfirm:edit")],
    ])


async def handle_name_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]
    if action == "edit":
        context.user_data.pop("pending_full_name", None)
        await q.message.reply_text("✏️ لطفاً نام و نام خانوادگی را دوباره وارد کن:\nمثال: محمد احمدی")
        return ASK_NAME

    full_name = context.user_data.pop("pending_full_name", None)
    if not full_name:
        await q.message.reply_text("❌ اطلاعات نام پیدا نشد. لطفاً نام و نام خانوادگی را دوباره وارد کن.")
        return ASK_NAME

    user_id = q.from_user.id
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
        [[KeyboardButton("📱 ارسال شماره موبایل", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def gender_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("👨 مرد"), KeyboardButton("👩 زن")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_name = update.message.text or ""
    full_name = normalize_persian_full_name(raw_name)
    if not full_name:
        await update.message.reply_text(
            "❌ نام و نام خانوادگی را درست وارد کن.\n\n"
            "نام باید حداقل دو بخش داشته باشد و فقط شامل حروف فارسی باشد.\n"
            "مثال: محمد احمدی"
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
    if action == "use":
        row = next((r for r in get_saved_data(user_id, "name") if r["id"] == int(parts[3])), None)
        if row:
            context.user_data["full_name"] = row["value"]
            if await show_saved_step(q.message, context, "phone"):
                return ASK_PHONE
            await q.message.reply_text("📱 لطفاً شماره موبایل خودت رو با دکمه زیر از تلگرام ارسال کن:", reply_markup=phone_keyboard())
            return ASK_PHONE
    elif action == "new":
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

    edit_id = context.user_data.pop("editing_saved_phone_id", None)
    if edit_id:
        update_saved_data(update.effective_user.id, int(edit_id), "phone", phone)
    else:
        save_saved_data(update.effective_user.id, "phone", phone)
    context.user_data["phone"] = phone
    await update.message.reply_text("جنسیت خودت رو انتخاب کن:", reply_markup=gender_keyboard())
    return ASK_GENDER


async def handle_saved_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    action = parts[2]
    user_id = q.from_user.id
    if action == "use":
        row = next((r for r in get_saved_data(user_id, "phone") if r["id"] == int(parts[3])), None)
        if row:
            context.user_data["phone"] = row["value"]
            await q.message.reply_text("جنسیت خودت رو انتخاب کن:", reply_markup=gender_keyboard())
            return ASK_GENDER
    elif action == "new":
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
    gender_map = {"👨 مرد": "مرد", "👩 زن": "زن"}
    gender = gender_map.get(gender_text)
    if not gender:
        await update.message.reply_text(
            "لطفاً یکی از گزینه‌های «👨 مرد» یا «👩 زن» را انتخاب کن.",
            reply_markup=gender_keyboard(),
        )
        return ASK_GENDER

    context.user_data["gender"] = gender
    if await show_saved_step(update.message, context, "address"):
        return ASK_ADDRESS
    await update.message.reply_text("آدرس کامل برای ارسال رو بفرست:", reply_markup=None)
    return ASK_ADDRESS


async def handle_saved_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    action = parts[2]
    user_id = q.from_user.id
    if action == "use":
        row = next((r for r in get_saved_data(user_id, "address") if r["id"] == int(parts[3])), None)
        if row:
            context.user_data["address"] = row["value"]
            return await finalize_order_from_callback(q.message, context, q.from_user)
    elif action == "new":
        await q.message.reply_text("آدرس جدید را بفرست:")
        return ASK_ADDRESS
    elif action == "edit":
        context.user_data["editing_saved_address_id"] = parts[3]
        await q.message.reply_text("آدرس جدید را بفرست:")
        return ASK_ADDRESS
    elif action == "delete":
        delete_saved_data(user_id, int(parts[3]), "address")
        rows = get_saved_data(user_id, "address")
        if rows:
            await q.message.reply_text("آدرس حذف شد. یک آدرس را انتخاب کن یا آدرس جدید اضافه کن:", reply_markup=saved_data_keyboard("address", rows))
        else:
            await q.message.reply_text("آدرس حذف شد. آدرس کامل برای ارسال را بفرست:")
        return ASK_ADDRESS
    return ASK_ADDRESS


async def finalize_order_from_callback(message, context, user=None):
    return await create_pending_payment(message, context, user)


async def finalize_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    address = update.message.text.strip()
    edit_id = context.user_data.pop("editing_saved_address_id", None)
    if edit_id:
        update_saved_data(user_id, int(edit_id), "address", address)
    else:
        save_saved_data(user_id, "address", address)
    context.user_data["address"] = address
    return await create_pending_payment(update.message, context)


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
        conn.close()
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


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💬 پشتیبانی تنین ایران\n\n"
        "📞 شماره تماس: 09384853486\n"
        "📱 آیدی تلگرام: @tanin_modir"
    )


# ----------------------------------------------------------------------------
# پنل مدیریت کامل: محصولات + سفارش‌ها + فروش + مشتری‌ها + پرداخت
# ----------------------------------------------------------------------------
def is_admin(user_id):
    return user_id in ADMIN_IDS


def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 سفارش‌ها", callback_data="adm:orders")],
        [InlineKeyboardButton("🛍 محصولات", callback_data="adm:products")],
        [InlineKeyboardButton("📊 فروش و آمار", callback_data="adm:sales")],
        [InlineKeyboardButton("👥 مشتری‌ها", callback_data="adm:customers")],
        [InlineKeyboardButton("💳 تنظیمات پرداخت", callback_data="adm:payment")],
        [InlineKeyboardButton("🔄 بروزرسانی", callback_data="adm:home")],
        [InlineKeyboardButton("🔙 بستن پنل", callback_data="adm:close")],
    ])


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
        f"👤 مشتری: {o['full_name'] or '-'}\n"
        f"📞 تلفن: {o['phone'] or '-'}\n"
        f"📍 آدرس: {o['address'] or '-'}\n"
        f"🕐 زمان: {o['created_at'] or '-'}\n\n"
        f"🛍 <b>محصولات:</b>\n{o['items_summary'] or '-'}\n\n"
        f"💰 مبلغ: <b>{o['total_price']:,} تومان</b>\n"
        f"💳 وضعیت پرداخت: <b>{o['payment_status'] or '-'}</b>\n"
        f"📌 وضعیت: <b>{o['status']}</b>"
    )


def product_text(p):
    state = "فعال ✅" if p["active"] else "غیرفعال ⛔"
    pack = f"پک: {p['pack_info']}\n" if p['pack_info'] else ""
    return (
        f"🛍 <b>{escape(p['name'])}</b>\n"
        f"شناسه: #{p['id']}\n"
        f"دسته: {escape(p['category'])}\n"
        f"سایز: {escape(p['size'] or '-')}\n"
        "رنگ‌بندی طبق ژورنال موجود\n"
        f"{pack}"
        f"قیمت: {p['price']:,} تومان\n"
        f"وضعیت: {state}"
    )


def admin_product_keyboard(product_id, active):
    toggle = "غیرفعال کردن" if active else "فعال کردن"
    toggle_action = "deactivate" if active else "activate"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ ویرایش", callback_data=f"adm:edit_product:{product_id}")],
        [InlineKeyboardButton(f"⛔ {toggle}", callback_data=f"adm:toggle_product:{product_id}:{toggle_action}")],
        [InlineKeyboardButton("🗑 حذف", callback_data=f"adm:delete_product:{product_id}")],
        [InlineKeyboardButton("🔙 محصولات", callback_data="adm:products")],
    ])


def products_keyboard(rows):
    buttons = [[InlineKeyboardButton("➕ افزودن محصول", callback_data="adm:add_product")]]
    for p in rows:
        buttons.append([InlineKeyboardButton(f"#{p['id']} | {p['name']}", callback_data=f"adm:product:{p['id']}")])
    buttons.append([InlineKeyboardButton("🔙 پنل اصلی", callback_data="adm:home")])
    return InlineKeyboardMarkup(buttons)


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("دسترسی نداری.")
        return
    await update.message.reply_text(
        "⚙️ <b>پنل مدیریت تنین ایران</b>\n\n"
        "از اینجا می‌تونی کل فروشگاه رو مدیریت کنی:",
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard(),
    )


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

    total = sum((o["total_price"] or 0) for o in orders if o["status"] != "رد شد")
    text = (
        "👤 <b>اطلاعات مشتری</b>\n\n"
        f"نام: {c['full_name'] or '-'}\n"
        f"آیدی: <code>{c['user_id']}</code>\n"
        f"تلگرام: @{c['username'] or '-'}\n"
        f"تلفن: {c['phone'] or '-'}\n"
        f"جنسیت: {c['gender'] or '-'}\n"
        f"اولین ورود: {c['first_seen'] or '-'}\n"
        f"آخرین فعالیت: {c['last_seen'] or '-'}\n\n"
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
    buttons.append([InlineKeyboardButton("🔙 مشتری‌ها", callback_data="adm:customers")])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def admin_product_detail(q, product_id):
    conn = get_conn()
    p = conn.execute("SELECT * FROM products WHERE id=%s", (product_id,)).fetchone()
    conn.close()
    if not p:
        await q.answer("محصول پیدا نشد.", show_alert=True)
        return
    await q.edit_message_text(
        product_text(p),
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product_id, p["active"]),
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
        f"آخرین تغییر: {escape(str(a['updated_at']) if a['updated_at'] else '-')}"
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
    if not update.message or not update.message.photo:
        return
    user_id = update.effective_user.id
    conn = get_conn()
    pending = conn.execute(
        """SELECT * FROM pending_payments WHERE user_id=%s AND payment_status='در انتظار رسید'
           ORDER BY id DESC LIMIT 1""", (user_id,)
    ).fetchone()
    if not pending:
        conn.close(); return
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
                       f"💰 مبلغ: <b>{pending['total_price']:,} تومان</b>\n🕐 {now}")
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
        await q.edit_message_caption(caption=(q.message.caption or "") + "\n\n❌ <b>پرداخت رد شد</b>", parse_mode="HTML", reply_markup=None)
        try: await context.bot.send_message(pending['user_id'], "❌ رسید پرداخت شما تأیید نشد. سفارش ثبت نشد. لطفاً با پشتیبانی تماس بگیرید.")
        except Exception: pass
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
    await q.edit_message_caption(caption=(q.message.caption or "") + f"\n\n✅ <b>پرداخت تأیید شد — سفارش #{order_id} ثبت شد</b>", parse_mode="HTML", reply_markup=None)
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
            "⚙️ <b>پنل مدیریت تنین ایران</b>\n\n"
            "مدیریت محصولات، سفارش‌ها، فروش، مشتری‌ها و پرداخت:",
            parse_mode="HTML",
            reply_markup=admin_panel_keyboard(),
        )
        return
    if action == "orders":
        mode = parts[2] if len(parts) > 2 else "all"
        await admin_orders_view(q, mode)
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
    if not is_admin(update.effective_user.id):
        return
    flow = context.user_data.get("admin_flow")
    if not flow:
        return
    text = (update.message.text or "").strip()
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
                f"برای اطلاعات بیشتر می‌تونید از بخش «💬 پشتیبانی» با ما در ارتباط باشید."
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
    app.add_handler(MessageHandler(filters.Regex("^💬 پشتیبانی$"), support))
    app.add_handler(CommandHandler("orders_admin", admin_orders))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ پنل مدیریت$"), admin_panel))

    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))
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
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone),
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
    )
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_input))

    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if not hostname:
        raise RuntimeError("RENDER_EXTERNAL_HOSTNAME is not available. Deploy this bot as a Render Web Service.")

    port = int(os.getenv("PORT", "10000"))
    webhook_path = os.getenv("WEBHOOK_PATH", "telegram/webhook").strip("/")
    webhook_url = f"https://{hostname}/{webhook_path}"
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()

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
