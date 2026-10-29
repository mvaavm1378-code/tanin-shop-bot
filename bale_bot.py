# -*- coding: utf-8 -*-
"""Bale shop bot: shared Supabase catalog/cart + checkout, persistent customer saved data, and card-transfer receipt review.
Run on Render as a Web Service: python bale_bot.py
Required env: BALE_BOT_TOKEN, SUPABASE_DB_URL (or DATABASE_URL)
Optional env: BALE_ADMIN_IDS (comma-separated Bale user IDs for receipt review)
"""
import os
import asyncio
import logging
import re
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
import aiohttp
from aiohttp import web
import psycopg
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bale-shop")
TOKEN = os.getenv("BALE_BOT_TOKEN", "").strip()
DB_URL = (os.getenv("SUPABASE_DB_URL", "").strip()
          or os.getenv("DATABASE_URL", "").strip()
          or os.getenv("POSTGRES_URL", "").strip())
API = f"https://tapi.bale.ai/bot{TOKEN}/"
POLL_TIMEOUT = 25
ADMIN_IDS = {int(x.strip()) for x in os.getenv("BALE_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
# SMS settings: configure these on the Bale Render service as well as Telegram.
MELIPAYAMAK_USERNAME = os.getenv("MELIPAYAMAK_USERNAME", "").strip()
MELIPAYAMAK_API_KEY = (os.getenv("MELIPAYAMAK_API_KEY", "").strip()
                       or os.getenv("MELIPAYAMAK_PASSWORD", "").strip())
MELIPAYAMAK_SENDER = os.getenv("MELIPAYAMAK_SENDER", "").strip()
SMS_ADMIN_PHONE = os.getenv("SMS_ADMIN_PHONE", "").strip()
MELIPAYAMAK_URL = "https://rest.payamak-panel.com/api/SendSMS/SendSMS"
# Checkout and support/admin conversations are persisted in app_meta, so a Render restart
# does not silently discard an in-progress flow. Existing table schema is not altered.
IRAN_TZ = ZoneInfo("Asia/Tehran")


def _persist_checkout(uid, state):
    try:
        key = f"bale_checkout_state:{int(uid)}"
        with get_conn() as conn:
            if state:
                conn.execute("INSERT INTO app_meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, json.dumps(dict(state), ensure_ascii=False)))
            else:
                conn.execute("DELETE FROM app_meta WHERE key=%s", (key,))
            conn.commit()
    except Exception:
        log.exception("Could not persist checkout state for Bale user %s", uid)


class PersistentState(dict):
    def __init__(self, uid, initial=None):
        self.uid = int(uid)
        super().__init__(initial or {})

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        _persist_checkout(self.uid, self)

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        _persist_checkout(self.uid, self)


class CheckoutStore(dict):
    def __setitem__(self, uid, value):
        state = value if isinstance(value, PersistentState) else PersistentState(uid, value)
        super().__setitem__(int(uid), state)
        _persist_checkout(uid, state)

    def pop(self, uid, *args):
        value = super().pop(int(uid), *args)
        _persist_checkout(uid, None)
        return value


CHECKOUT = CheckoutStore()


def restore_checkout(uid):
    if uid in CHECKOUT:
        return CHECKOUT[uid]
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key=%s", (f"bale_checkout_state:{int(uid)}",)).fetchone()
        if row and row.get("value"):
            state = json.loads(row["value"])
            CHECKOUT[uid] = state
            return CHECKOUT[uid]
    except Exception:
        log.exception("Could not restore checkout state for Bale user %s", uid)
    return {}


def iran_now_text():
    return datetime.now(IRAN_TZ).strftime("%Y-%m-%d %H:%M")


def _to_iran_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw or raw == "-":
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    pass
            if dt is None:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IRAN_TZ)
    return dt.astimezone(IRAN_TZ)


def gregorian_to_jalali(gy, gm, gd):
    g_days = [31,28,31,30,31,30,31,31,30,31,30,31]
    j_days = [31,31,31,31,31,31,30,30,30,30,30,29]
    gy2, gm2, gd2 = gy - 1600, gm - 1, gd - 1
    gdn = 365*gy2 + (gy2+3)//4 - (gy2+99)//100 + (gy2+399)//400
    for i in range(gm2): gdn += g_days[i]
    if gm2 > 1 and ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0): gdn += 1
    gdn += gd2
    jdn = gdn - 79
    jnp = jdn // 12053
    jdn %= 12053
    jy = 979 + 33*jnp + 4*(jdn//1461)
    jdn %= 1461
    if jdn >= 366:
        jy += (jdn-1)//365
        jdn = (jdn-1)%365
    for i in range(11):
        if jdn < j_days[i]: return jy, i+1, jdn+1
        jdn -= j_days[i]
    return jy, 12, jdn+1


def format_iran_jalali(value, include_time=True):
    dt = _to_iran_datetime(value)
    if not dt: return "-"
    jy,jm,jd = gregorian_to_jalali(dt.year,dt.month,dt.day)
    out=f"{jy:04d}/{jm:02d}/{jd:02d}"
    if include_time: out += f" - {dt.hour:02d}:{dt.minute:02d}"
    return out


def format_iran_jalali_fa(value, include_time=True):
    return format_iran_jalali(value, include_time).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def normalize_phone(value):
    digits = str(value or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    digits = re.sub(r"[^0-9+]", "", digits)
    if digits.startswith("+98"): digits = "0" + digits[3:]
    elif digits.startswith("0098"): digits = "0" + digits[4:]
    elif digits.startswith("98") and len(digits) == 12: digits = "0" + digits[2:]
    elif len(digits) == 10 and digits.startswith("9"): digits = "0" + digits
    return digits


def normalize_name(value):
    return re.sub(r"\s+", " ", str(value or "").replace("ي", "ی").replace("ك", "ک").replace("‌", " ")).strip()


def gender_meta_key(uid, full_name):
    return f"bale_gender:{int(uid)}:{normalize_name(full_name)}"


def get_known_gender(uid, full_name):
    try:
        with get_conn() as conn:
            row=conn.execute("SELECT value FROM app_meta WHERE key=%s", (gender_meta_key(uid, full_name),)).fetchone()
        return row["value"] if row and row.get("value") in ("مرد", "زن") else None
    except Exception:
        log.exception("Could not load saved gender")
        return None


def set_known_gender(uid, full_name, gender):
    if gender not in ("مرد", "زن") or not normalize_name(full_name): return
    with get_conn() as conn:
        conn.execute("INSERT INTO app_meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (gender_meta_key(uid, full_name), gender))
        conn.commit()


def get_conn():
    if not DB_URL:
        raise RuntimeError("SUPABASE_DB_URL یا DATABASE_URL تنظیم نشده است.")
    return psycopg.connect(DB_URL, row_factory=dict_row, prepare_threshold=None, connect_timeout=15)


def main_keyboard(uid=None):
    if uid is not None and is_admin(uid):
        try:
            counts = db_admin_stats()
            pending, orders, products, tickets = counts["pending"], counts["orders"], counts["products"], counts["tickets"]
        except Exception:
            pending = orders = products = tickets = 0
        def badge(label, n): return f"{label} ({n})" if n else label
        return {"keyboard": [[{"text": "⚙️ پنل مدیریت"}],
                             [{"text": badge("📦 سفارش‌ها", orders)}, {"text": badge("💳 پرداخت‌های در انتظار", pending)}],
                             [{"text": badge("🛠 مدیریت محصولات", products)}, {"text": badge("🎫 پشتیبانی", tickets)}],
                             [{"text": "👥 مشتری‌ها"}, {"text": "📊 فروش و آمار"}],
                             [{"text": "📢 مدیریت کانال"}, {"text": "💳 تنظیمات پرداخت"}]], "resize_keyboard": True}
    return {"keyboard": [[{"text": "🧾 کاتالوگ محصولات"}],
                         [{"text": "🛒 سبد خرید"}, {"text": "📦 سفارش‌های من"}],
                         [{"text": "💬 مرکز پشتیبانی"}]], "resize_keyboard": True}


def category_keyboard():
    return {"inline_keyboard": [[{"text": "👶 بچگانه", "callback_data": "cat:بچگانه"}],
                                 [{"text": "👗 زنانه", "callback_data": "cat:زنانه"}]]}


def product_keyboard(pid):
    return {"inline_keyboard": [[{"text": "➕ افزودن به سبد خرید", "callback_data": f"add:{pid}"}]]}


def quantity_keyboard(pid, qty):
    return {"inline_keyboard": [
        [{"text": "➖", "callback_data": f"qty:minus:{pid}:{qty}"},
         {"text": f"📦 {qty} بسته", "callback_data": f"qty:noop:{pid}:{qty}"},
         {"text": "➕", "callback_data": f"qty:plus:{pid}:{qty}"}],
        [{"text": "✅ تأیید و افزودن", "callback_data": f"qty:confirm:{pid}:{qty}"}],
        [{"text": "لغو", "callback_data": f"qty:cancel:{pid}:{qty}"}]]}


def cart_keyboard(cid):
    return {"inline_keyboard": [[{"text": "🗑 حذف از سبد", "callback_data": f"remove:{cid}"}]]}


def checkout_keyboard():
    return {"inline_keyboard": [[{"text": "✅ ادامه ثبت سفارش", "callback_data": "checkout:start"}]]}


def payment_keyboard(pid, card_number=None):
    rows = []
    if card_number:
        rows.append([{"text": "📋 نمایش شماره کارت برای کپی", "callback_data": f"pay:card:{pid}"}])
    rows.append([{"text": "📤 راهنمای ارسال رسید", "callback_data": f"pay:receipt:{pid}"}])
    return {"inline_keyboard": rows}


def review_keyboard(pid):
    return {"inline_keyboard": [[{"text": "✅ تأیید پرداخت و ثبت سفارش", "callback_data": f"payadmin:approve:{pid}"}],
                                [{"text": "❌ رد پرداخت", "callback_data": f"payadmin:reject:{pid}"}]]}



# ---------------------------------------------------------------------------
# پنل مدیریت مخصوص مدیران؛ مدیر وارد جریان خرید و سبد مشتری نمی‌شود.
# گزینه‌ها با پنل تلگرام هم‌راستا هستند. عملیات خطرناک فقط برای ADMIN_IDS است.
# ---------------------------------------------------------------------------
def is_admin(uid):
    return uid in ADMIN_IDS


def admin_keyboard():
    return {"inline_keyboard": [
        [{"text": "💳 پرداخت‌های در انتظار", "callback_data": "adm:pending"},
         {"text": "📦 سفارش‌ها", "callback_data": "adm:orders"}],
        [{"text": "🛠 مدیریت محصولات", "callback_data": "adm:products"},
         {"text": "👥 مشتری‌ها", "callback_data": "adm:customers"}],
        [{"text": "📊 فروش و آمار", "callback_data": "adm:sales"},
         {"text": "🎫 پشتیبانی", "callback_data": "adm:tickets"}],
        [{"text": "📢 مدیریت کانال", "callback_data": "adm:channel"},
         {"text": "💳 تنظیمات پرداخت", "callback_data": "adm:payment"}],
        [{"text": "🔄 بروزرسانی داشبورد", "callback_data": "adm:home"},
         {"text": "✕ بستن پنل", "callback_data": "adm:close"}],
    ]}


def admin_back(target="home"):
    return {"inline_keyboard": [[{"text": "🔙 بازگشت", "callback_data": f"adm:{target}"}]]}


def admin_order_buttons(order_id, status):
    rows = []
    if status == "در انتظار بررسی":
        rows.extend([
            [{"text": "✅ تأیید سفارش", "callback_data": f"adm:orderstatus:{order_id}:confirmed"}],
            [{"text": "❌ رد سفارش", "callback_data": f"adm:orderstatus:{order_id}:cancelled"}],
        ])
    rows.append([{"text": "🔙 لیست سفارش‌ها", "callback_data": "adm:orders"}])
    return {"inline_keyboard": rows}


def admin_home_text(stats):
    return (
        "⚙️ <b>پنل مدیریت تنین ایران</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🔔 <b>نیازمند اقدام</b>\n"
        f"💳 پرداخت‌های در انتظار بررسی: <b>{stats['pending']}</b>\n"
        f"📦 سفارش‌های در انتظار بررسی: <b>{stats['orders']}</b>\n"
        f"🎫 تیکت‌های باز: <b>{stats['tickets']}</b>\n\n"
        "📊 <b>وضعیت فروشگاه</b>\n"
        f"🛠 محصولات فعال: {stats['products']}  |  👥 مشتریان: {stats['customers']}\n\n"
        "یک بخش مدیریتی را انتخاب کن:"
    )


def db_admin_stats():
    with get_conn() as conn:
        def count(sql):
            try:
                return int(conn.execute(sql).fetchone()["n"] or 0)
            except Exception:
                return 0
        return {
            "pending": count("SELECT COUNT(*) AS n FROM pending_payments WHERE payment_status='در انتظار بررسی'"),
            "orders": count("SELECT COUNT(*) AS n FROM orders WHERE status='در انتظار بررسی'"),
            "tickets": count("SELECT COUNT(*) AS n FROM support_tickets WHERE status IN ('new','admin_waiting')"),
            "products": count("SELECT COUNT(*) AS n FROM products WHERE active=TRUE"),
            "customers": count("SELECT COUNT(*) AS n FROM customers"),
        }


async def show_admin_home(api, chat_id):
    if not ADMIN_IDS:
        await api.send(chat_id, "⚠️ شناسه مدیر در BALE_ADMIN_IDS تنظیم نشده است.")
        return
    try:
        stats = await asyncio.to_thread(db_admin_stats)
        await api.send(chat_id, admin_home_text(stats), admin_keyboard())
    except Exception:
        log.exception("Admin dashboard failed")
        await api.send(chat_id, "❌ داشبورد مدیریت بارگذاری نشد. Logs را بررسی کن.", admin_keyboard())


async def show_admin_section(api, chat_id, section):
    if section == "pending":
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id,full_name,total_price,created_at,payment_status FROM pending_payments "
                "WHERE payment_status='در انتظار بررسی' ORDER BY id DESC LIMIT 30"
            ).fetchall()
        if not rows:
            await api.send(chat_id, "💳 پرداخت‌های در انتظار بررسی\n\nموردی برای بررسی وجود ندارد.", admin_back())
            return
        buttons = []
        for r in rows:
            label = f"#{r['id']} · {str(r['full_name'] or '-')[:18]} · {int(r['total_price'] or 0):,} تومان"
            buttons.append([{"text": label[:60], "callback_data": f"adm:pendingdetail:{r['id']}"}])
        buttons += [[{"text": "🔄 بروزرسانی", "callback_data": "adm:pending"}],
                    [{"text": "🔙 پنل اصلی", "callback_data": "adm:home"}]]
        await api.send(chat_id, "💳 <b>پرداخت‌های در انتظار بررسی</b>", {"inline_keyboard": buttons})
    elif section == "orders":
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id,full_name,total_price,status,created_at FROM orders ORDER BY id DESC LIMIT 30"
            ).fetchall()
        if not rows:
            await api.send(chat_id, "📦 سفارشی ثبت نشده است.", admin_back())
            return
        buttons = []
        for r in rows:
            label = f"#{r['id']} · {str(r['full_name'] or '-')[:16]} · {int(r['total_price'] or 0):,} · {str(r['status'] or '-')[:12]}"
            buttons.append([{"text": label[:60], "callback_data": f"adm:orderdetail:{r['id']}"}])
        buttons += [[{"text": "🔄 بروزرسانی", "callback_data": "adm:orders"}],
                    [{"text": "🔙 پنل اصلی", "callback_data": "adm:home"}]]
        await api.send(chat_id, "📦 <b>سفارش‌ها</b>", {"inline_keyboard": buttons})
    elif section == "products":
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id,name,category,price,active FROM products ORDER BY id DESC LIMIT 40"
            ).fetchall()
        buttons = []
        for r in rows:
            status = "🟢" if r["active"] else "⚪"
            label = f"{status} #{r['id']} {str(r['name'])[:24]} · {int(r['price'] or 0):,}"
            buttons.append([{"text": label[:60], "callback_data": f"adm:producttoggle:{r['id']}"}])
        buttons += [[{"text": "🔄 بروزرسانی", "callback_data": "adm:products"}],
                    [{"text": "🔙 پنل اصلی", "callback_data": "adm:home"}]]
        await api.send(chat_id, "🛠 <b>مدیریت محصولات</b>\nروی محصول بزن تا فعال/غیرفعال شود.", {"inline_keyboard": buttons})
    elif section == "customers":
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT user_id,full_name,phone,username FROM customers ORDER BY last_seen DESC NULLS LAST LIMIT 30"
            ).fetchall()
        buttons=[]
        for r in rows:
            label=f"👤 {str(r['full_name'] or r['username'] or r['user_id'])[:25]} · {str(r['phone'] or '-')[:14]}"
            buttons.append([{"text":label[:60],"callback_data":f"adm:customer:{r['user_id']}"}])
        buttons += [[{"text":"🔄 بروزرسانی","callback_data":"adm:customers"}],[{"text":"🔙 پنل اصلی","callback_data":"adm:home"}]]
        await api.send(chat_id,"👥 <b>آخرین مشتریان</b>\n\nبرای دیدن سفارش‌ها و اطلاعات هر مشتری، نام او را انتخاب کن." if rows else "👥 هنوز مشتری‌ای ثبت نشده است.", {"inline_keyboard":buttons} if rows else admin_back())
    elif section == "sales":
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(total_price),0) AS total FROM orders "
                "WHERE payment_status='تأیید شده'"
            ).fetchone()
        await api.send(chat_id, f"📊 <b>آمار فروش</b>\nسفارش‌های پرداخت‌شده: {row['n']}\nجمع مبلغ: {int(row['total'] or 0):,} تومان", admin_back())
    elif section == "tickets":
        try:
            with get_conn() as conn:
                rows = conn.execute("SELECT id,user_id,topic,status,created_at FROM support_tickets ORDER BY id DESC LIMIT 20").fetchall()
            lines = ["🎫 <b>تیکت‌های پشتیبانی</b>", ""]
            for r in rows:
                lines.append(f"#{r['id']} · کاربر {r['user_id']} · {escape(str(r['topic'] or '-'))} · {escape(str(r['status'] or '-'))}")
            await api.send(chat_id, "\n".join(lines)[:3900] if rows else "🎫 تیکتی ثبت نشده است.", admin_back())
        except Exception:
            log.exception("Tickets view failed")
            await api.send(chat_id, "🎫 بخش تیکت‌ها هنوز با ساختار فعلی دیتابیس سازگار نشده است.", admin_back())
    elif section == "payment":
        with get_conn() as conn:
            rows = conn.execute("SELECT id,bank_name,owner_name,card_number,active FROM bank_accounts ORDER BY id DESC LIMIT 20").fetchall()
        lines = ["💳 <b>تنظیمات پرداخت</b>", ""]
        for r in rows:
            lines.append(f"#{r['id']} · {escape(str(r['bank_name'] or '-'))} · {escape(str(r['owner_name'] or '-'))} · کارت {escape(str(r['card_number'] or '-'))} · {'فعال' if r['active'] else 'غیرفعال'}")
        await api.send(chat_id, "\n".join(lines)[:3900] if rows else "حساب بانکی ثبت نشده است.", admin_back())
    elif section == "channel":
        await show_admin_channel(api, chat_id)


async def show_admin_pending_detail(api, chat_id, pid):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pending_payments WHERE id=%s", (pid,)).fetchone()
    if not row:
        await api.send(chat_id, "پرداخت پیدا نشد.", admin_back("pending")); return
    caption = (f"💳 <b>پرداخت #{row['id']}</b>\n"
               f"مشتری: {escape(str(row['full_name'] or '-'))}\n"
               f"تلفن: {escape(str(row['phone'] or '-'))}\n"
               f"آدرس: {escape(str(row['address'] or '-'))}\n"
               f"محصولات: {escape(str(row['items_summary'] or '-'))}\n"
               f"مبلغ: {int(row['total_price'] or 0):,} تومان\n"
               f"وضعیت: {escape(str(row['payment_status'] or '-'))}")
    buttons = []
    if row["receipt_file_id"]:
        buttons += [[{"text": "📷 ارسال/نمایش رسید", "callback_data": f"adm:receipt:{pid}"}]]
    if row["payment_status"] == "در انتظار بررسی":
        buttons += [[{"text": "✅ تأیید پرداخت", "callback_data": f"payadmin:approve:{pid}"}],
                    [{"text": "❌ رد پرداخت", "callback_data": f"payadmin:reject:{pid}"}]]
    buttons += [[{"text": "🔙 پرداخت‌های در انتظار", "callback_data": "adm:pending"}]]
    await api.send(chat_id, caption, {"inline_keyboard": buttons})


class BaleAPI:
    def __init__(self, session):
        self.session = session

    async def call(self, method, payload=None):
        async with self.session.post(API + method, json=payload or {}) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400 or not data.get("ok", False):
                raise RuntimeError(f"Bale API {method} failed: {data}")
            return data.get("result")

    async def send(self, chat_id, text, markup=None):
        p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if markup:
            p["reply_markup"] = markup
        return await self.call("sendMessage", p)

    async def photo(self, chat_id, url, caption, markup=None):
        p = {"chat_id": chat_id, "photo": url, "caption": caption, "parse_mode": "HTML"}
        if markup:
            p["reply_markup"] = markup
        return await self.call("sendPhoto", p)

    async def answer(self, callback_id, text=None, alert=False):
        p = {"callback_query_id": callback_id}
        if text:
            p["text"] = text
        if alert:
            p["show_alert"] = True
        try:
            await self.call("answerCallbackQuery", p)
        except Exception:
            log.debug("answerCallbackQuery failed", exc_info=True)

    async def edit(self, chat_id, message_id, text, markup=None):
        p = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if markup is not None:
            p["reply_markup"] = markup
        return await self.call("editMessageText", p)


def db_products(category):
    with get_conn() as conn:
        return conn.execute("SELECT id,name,category,size,color,price,photo_url,pack_info FROM products WHERE category=%s AND active=TRUE ORDER BY id DESC", (category,)).fetchall()


def db_product(pid):
    with get_conn() as conn:
        return conn.execute("SELECT id,name,category,size,color,price,photo_url,pack_info FROM products WHERE id=%s AND active=TRUE", (pid,)).fetchone()


def db_add_cart(uid, pid, qty):
    with get_conn() as conn:
        item = conn.execute("SELECT id,qty FROM cart_items WHERE user_id=%s AND product_id=%s", (uid, pid)).fetchone()
        if item:
            conn.execute("UPDATE cart_items SET qty=%s WHERE id=%s AND user_id=%s", (int(item["qty"]) + qty, item["id"], uid))
        else:
            conn.execute("INSERT INTO cart_items(user_id,product_id,qty) VALUES(%s,%s,%s)", (uid, pid, qty))
        conn.commit()


def db_cart(uid):
    with get_conn() as conn:
        return conn.execute("""SELECT ci.id AS cart_id,p.name,p.price,p.pack_info,ci.qty,(p.price*ci.qty) AS line_total
            FROM cart_items ci JOIN products p ON p.id=ci.product_id
            WHERE ci.user_id=%s AND p.active=TRUE ORDER BY ci.id""", (uid,)).fetchall()


def db_remove_cart(uid, cid):
    with get_conn() as conn:
        conn.execute("DELETE FROM cart_items WHERE id=%s AND user_id=%s", (cid, uid))
        conn.commit()


# Prefix Bale IDs in customer_saved_data so they cannot collide with Telegram IDs.
def saved_user_key(uid):
    return -abs(int(uid))


def db_saved_data(uid, data_type):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id,value FROM customer_saved_data WHERE user_id=%s AND data_type=%s ORDER BY id DESC",
            (saved_user_key(uid), data_type),
        ).fetchall()


def db_save_data(uid, data_type, value):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value:
        return
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM customer_saved_data WHERE user_id=%s AND data_type=%s AND value=%s LIMIT 1",
            (saved_user_key(uid), data_type, value),
        ).fetchone()
        now = __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M")
        if row:
            conn.execute("UPDATE customer_saved_data SET updated_at=%s WHERE id=%s", (now, row["id"]))
        else:
            conn.execute("INSERT INTO customer_saved_data(user_id,data_type,value,created_at,updated_at) VALUES(%s,%s,%s,%s,%s)",
                         (saved_user_key(uid), data_type, value, now, now))
        conn.commit()


def db_update_saved(uid, data_id, data_type, value):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    now = __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M")
    with get_conn() as conn:
        conn.execute("UPDATE customer_saved_data SET value=%s,updated_at=%s WHERE id=%s AND user_id=%s AND data_type=%s",
                     (value, now, data_id, saved_user_key(uid), data_type))
        conn.commit()


def db_delete_saved(uid, data_id, data_type):
    with get_conn() as conn:
        conn.execute("DELETE FROM customer_saved_data WHERE id=%s AND user_id=%s AND data_type=%s",
                     (data_id, saved_user_key(uid), data_type))
        conn.commit()


def saved_keyboard(uid, data_type, step):
    rows = db_saved_data(uid, data_type)
    buttons = []
    for row in rows[:8]:
        val = str(row["value"])
        label = val if len(val) <= 34 else val[:31] + "..."
        buttons.append([{"text": "استفاده: " + label, "callback_data": f"saved:use:{step}:{row['id']}"}])
    buttons.append([{"text": "➕ وارد کردن اطلاعات جدید", "callback_data": f"saved:new:{step}"}])
    if rows:
        buttons.append([{"text": "✏️ ویرایش / 🗑 حذف اطلاعات ذخیره‌شده", "callback_data": f"saved:manage:{data_type}"}])
    return {"inline_keyboard": buttons}


def _order_sms_marker_key(pending_id):
    return f"order_sms_sent:{int(pending_id)}"


def _order_sms_was_sent(pending_id):
    """Use shared app_meta to prevent duplicate alerts across Telegram and Bale."""
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key=%s",
                               (_order_sms_marker_key(pending_id),)).fetchone()
        return row is not None
    except Exception:
        log.exception("Could not check SMS marker for Bale payment #%s", pending_id)
        return False


def _mark_order_sms_sent(pending_id, platform):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING",
            (_order_sms_marker_key(pending_id),
             f"{platform}:{__import__('datetime').datetime.now(__import__('zoneinfo').ZoneInfo('Asia/Tehran')).isoformat()}"),
        )
        conn.commit()


def _sms_norm_phone(value):
    """Normalize an Iranian mobile number to 09xxxxxxxxx (digits only)."""
    digits = str(value or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    digits = re.sub(r"\D+", "", digits)
    if digits.startswith("0098"):
        digits = "0" + digits[4:]
    elif digits.startswith("98") and len(digits) == 12:
        digits = "0" + digits[2:]
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "0" + digits
    return digits


def _sms_clean(value, limit=None):
    """Make text safe for SMS: no emoji/control/zero-width chars, single-line, optional length cap."""
    text = str(value or "")
    text = text.replace("×", "x").replace("\u200c", " ").replace("\n", "، ").replace("\r", " ")
    text = re.sub(r"[\u200b\u200d\u200e\u200f\u202a-\u202e\ufeff]", "", text)
    text = "".join(ch for ch in text if ord(ch) <= 0xFFFF and (ch.isprintable() or ch == " "))
    text = re.sub(r"[\U00010000-\U0010ffff]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(،\s*){2,}", "، ", text)
    if limit and len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


async def _melipayamak_post(pending_id, to_phone, from_line, text):
    """POST one SMS. Returns (ok, ret_status, ret_text)."""
    payload = {
        "username": MELIPAYAMAK_USERNAME,
        "password": MELIPAYAMAK_API_KEY,
        "to": to_phone,
        "from": from_line,
        "text": text,
        "isFlash": "false",
    }
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(MELIPAYAMAK_URL, data=payload,
                                headers={"Accept": "application/json, text/plain, */*"}) as response:
            body = (await response.text()).strip()
            log.info("Melipayamak Bale order #%s response: HTTP=%s BODY=%s",
                     pending_id, response.status, body[:600])
            if response.status >= 400:
                return False, None, f"HTTP {response.status}"
            try:
                result = json.loads(body)
            except (json.JSONDecodeError, TypeError):
                return False, None, "invalid API response"
            if not isinstance(result, dict):
                return False, None, "unexpected API response"
            ret_status = result.get("RetStatus", result.get("retStatus"))
            try:
                ret_status = int(ret_status)
            except (TypeError, ValueError):
                ret_status = -1
            detail = "RetStatus=%s StrRetStatus=%s Value=%s" % (
                ret_status, result.get("StrRetStatus", result.get("strRetStatus", "")),
                result.get("Value", result.get("value", "")))
            return ret_status == 1, ret_status, detail


async def send_order_sms_to_admin(pending_id, full_name, phone, total_price, items_summary):
    """Send new Bale order SMS to the configured admin number via Melipayamak."""
    missing = []
    if not MELIPAYAMAK_USERNAME: missing.append("MELIPAYAMAK_USERNAME")
    if not MELIPAYAMAK_API_KEY: missing.append("MELIPAYAMAK_API_KEY/MELIPAYAMAK_PASSWORD")
    if not MELIPAYAMAK_SENDER: missing.append("MELIPAYAMAK_SENDER")
    if not SMS_ADMIN_PHONE: missing.append("SMS_ADMIN_PHONE")
    if missing:
        log.error("BALE ORDER SMS SKIPPED for #%s: missing %s", pending_id, ", ".join(missing))
        return False

    to_phone = _sms_norm_phone(SMS_ADMIN_PHONE)
    from_line = re.sub(r"\s+", "", MELIPAYAMAK_SENDER)
    if len(to_phone) != 11 or not to_phone.startswith("09"):
        log.error("BALE ORDER SMS SKIPPED for #%s: SMS_ADMIN_PHONE is not a valid mobile number (got %r)",
                  pending_id, SMS_ADMIN_PHONE)
        return False

    items = _sms_clean(items_summary or "-", 110)
    name = _sms_clean(full_name or "-", 40)
    cust_phone = _sms_norm_phone(phone) or "-"
    full_text = (
        f"تنین ایران | سفارش جدید بله #{pending_id}\n"
        f"مشتری: {name}\n"
        f"موبایل: {cust_phone}\n"
        f"مبلغ: {int(total_price or 0):,} تومان\n"
        f"محصولات: {items}"
    )
    # Fallback: very short and plain, used only if the full text is rejected as InvalidData.
    short_text = f"سفارش جدید بله #{pending_id} - {int(total_price or 0):,} تومان - موبایل {cust_phone}"

    log.info("SMS debug #%s: to=%r from=%r len=%s text=%r", pending_id, to_phone, from_line, len(full_text), full_text)
    try:
        ok, status, detail = await _melipayamak_post(pending_id, to_phone, from_line, full_text)
        if not ok and status == 35:
            log.warning("BALE ORDER SMS #%s rejected as InvalidData; retrying with short text (%s)", pending_id, detail)
            ok, status, detail = await _melipayamak_post(pending_id, to_phone, from_line, short_text)
        if not ok:
            log.error("BALE ORDER SMS REJECTED for #%s: %s", pending_id, detail)
            return False
        log.info("BALE ORDER SMS ACCEPTED for #%s", pending_id)
        return True
    except asyncio.TimeoutError:
        log.error("BALE ORDER SMS TIMEOUT for #%s", pending_id)
        return False
    except aiohttp.ClientError:
        log.exception("BALE ORDER SMS NETWORK ERROR for #%s", pending_id)
        return False
    except Exception:
        log.exception("BALE ORDER SMS UNEXPECTED ERROR for #%s", pending_id)
        return False


async def send_order_sms_once(pending_id, full_name, phone, total_price, items_summary, platform="bale"):
    """Send at most one successful SMS per pending payment; failed sends can retry."""
    if await asyncio.to_thread(_order_sms_was_sent, pending_id):
        log.info("BALE ORDER SMS SKIPPED for #%s: already marked sent", pending_id)
        return True
    sent = await send_order_sms_to_admin(pending_id, full_name, phone, total_price, items_summary)
    if sent:
        try:
            await asyncio.to_thread(_mark_order_sms_sent, pending_id, platform)
        except Exception:
            log.exception("Bale SMS accepted but marker write failed for #%s", pending_id)
    return sent


def db_create_pending(uid, full_name, phone, gender, address, username):
    """Create/reuse an unpaid payment record; return (payment row, bank account)."""
    with get_conn() as conn:
        now = __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M")
        conn.execute("""INSERT INTO customers(user_id,username,full_name,phone,gender,first_seen,last_seen)
            VALUES(%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,full_name=excluded.full_name,
            phone=excluded.phone,gender=excluded.gender,last_seen=excluded.last_seen""",
            (uid, username or "", full_name, phone, gender, now, now))
        rows = conn.execute("""SELECT p.name,p.price,p.pack_info,ci.qty FROM cart_items ci
            JOIN products p ON p.id=ci.product_id WHERE ci.user_id=%s AND p.active=TRUE ORDER BY ci.id""", (uid,)).fetchall()
        if not rows:
            raise ValueError("سبد خرید خالی است.")
        total = sum(int(r["price"] or 0) * int(r["qty"] or 0) for r in rows)
        summary = "\n".join(f"{r['name']}" + (f" ({r['pack_info']})" if r["pack_info"] else "") + f" × {r['qty']}" for r in rows)
        pending = conn.execute("""SELECT * FROM pending_payments WHERE user_id=%s
            AND payment_status IN ('در انتظار رسید','در انتظار بررسی') ORDER BY id DESC LIMIT 1""", (uid,)).fetchone()
        if pending and pending["payment_status"] == "در انتظار رسید":
            conn.execute("""UPDATE pending_payments SET full_name=%s,phone=%s,address=%s,items_summary=%s,total_price=%s
                WHERE id=%s AND payment_status='در انتظار رسید'""", (full_name, phone, address, summary, total, pending["id"]))
            pending = dict(pending)
            pending.update(full_name=full_name, phone=phone, address=address, items_summary=summary, total_price=total)
        elif not pending:
            pending = conn.execute("""INSERT INTO pending_payments
                (user_id,full_name,phone,address,items_summary,total_price,payment_status,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,'در انتظار رسید',%s) RETURNING *""",
                (uid, full_name, phone, address, summary, total, now)).fetchone()
        bank = conn.execute("SELECT * FROM bank_accounts WHERE active=TRUE ORDER BY id DESC LIMIT 1").fetchone()
        conn.commit()
        return dict(pending), dict(bank) if bank else None


def db_record_receipt(uid, file_id):
    with get_conn() as conn:
        pending = conn.execute("""SELECT * FROM pending_payments WHERE user_id=%s AND payment_status='در انتظار رسید'
            ORDER BY id DESC LIMIT 1""", (uid,)).fetchone()
        if not pending:
            return None
        conn.execute("UPDATE pending_payments SET receipt_file_id=%s,payment_status='در انتظار بررسی' WHERE id=%s", (file_id, pending["id"]))
        conn.commit()
        result = dict(pending)
        result["receipt_file_id"] = file_id
        result["payment_status"] = "در انتظار بررسی"
        return result




def _award_loyalty_cashback(conn, platform, platform_user_id, order_id,
                            eligible_amount_toman, display_name=None, phone=None):
    """
    Create/reuse a platform-specific loyalty identity and award 3% cashback.
    Must be called inside the same DB transaction that finalizes the order.
    Phone numbers are intentionally NOT used to merge identities because the
    current checkout does not verify phone ownership.
    """
    eligible = max(0, int(eligible_amount_toman or 0))
    cashback = (eligible * 3) // 100
    if cashback <= 0:
        return 0

    platform_user_id = str(platform_user_id)
    lock_key = f"loyalty-account:{platform}:{platform_user_id}"
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))

    account = conn.execute(
        """SELECT loyalty_customer_id
           FROM customer_platform_accounts
           WHERE platform=%s AND platform_user_id=%s
           FOR UPDATE""",
        (platform, platform_user_id),
    ).fetchone()

    if account:
        loyalty_customer_id = account["loyalty_customer_id"]
        conn.execute(
            """UPDATE customer_platform_accounts
               SET last_seen_at=NOW(), username=COALESCE(%s, username)
               WHERE platform=%s AND platform_user_id=%s""",
            (None, platform, platform_user_id),
        )
        if display_name:
            conn.execute(
                """UPDATE loyalty_customers SET display_name=%s, updated_at=NOW()
                   WHERE id=%s""",
                (str(display_name)[:250], loyalty_customer_id),
            )
    else:
        customer = conn.execute(
            """INSERT INTO loyalty_customers(display_name)
               VALUES(%s) RETURNING id""",
            (str(display_name)[:250] if display_name else None,),
        ).fetchone()
        loyalty_customer_id = customer["id"]
        conn.execute(
            """INSERT INTO customer_platform_accounts
               (loyalty_customer_id, platform, platform_user_id, linked_at, last_seen_at)
               VALUES(%s,%s,%s,NOW(),NOW())""",
            (loyalty_customer_id, platform, platform_user_id),
        )

    conn.execute(
        """INSERT INTO wallet_accounts(loyalty_customer_id)
           VALUES(%s) ON CONFLICT(loyalty_customer_id) DO NOTHING""",
        (loyalty_customer_id,),
    )
    conn.execute(
        """INSERT INTO sms_customer_preferences(loyalty_customer_id)
           VALUES(%s) ON CONFLICT(loyalty_customer_id) DO NOTHING""",
        (loyalty_customer_id,),
    )

    source_order_id = str(order_id)
    link = conn.execute(
        """INSERT INTO loyalty_order_links
           (platform, source_order_id, loyalty_customer_id, order_status,
            eligible_items_amount_toman, shipping_amount_toman, paid_at,
            cashback_processed_at)
           VALUES(%s,%s,%s,'paid',%s,0,NOW(),NOW())
           ON CONFLICT(platform, source_order_id) DO NOTHING
           RETURNING id""",
        (platform, source_order_id, loyalty_customer_id, eligible),
    ).fetchone()

    # Existing link means this order was already processed; do not award twice.
    if not link:
        return 0

    wallet = conn.execute(
        """SELECT id, balance_toman FROM wallet_accounts
           WHERE loyalty_customer_id=%s FOR UPDATE""",
        (loyalty_customer_id,),
    ).fetchone()
    new_balance = int(wallet["balance_toman"]) + cashback
    source_id = f"{platform}:{source_order_id}"
    idempotency_key = f"cashback:{platform}:{source_order_id}"

    conn.execute(
        """INSERT INTO wallet_transactions
           (wallet_account_id, transaction_type, amount_toman, balance_after_toman,
            source_type, source_id, idempotency_key, note, metadata)
           VALUES(%s,'cashback',%s,%s,'order',%s,%s,%s,%s::jsonb)""",
        (
            wallet["id"], cashback, new_balance, source_id, idempotency_key,
            f"کش‌بک ۳٪ سفارش {platform}:{source_order_id}",
            json.dumps({
                "platform": platform,
                "order_id": source_order_id,
                "eligible_items_amount_toman": eligible,
                "rate_percent": 3,
                "shipping_excluded": True,
            }, ensure_ascii=False),
        ),
    )
    conn.execute(
        """UPDATE wallet_accounts
           SET balance_toman=%s, updated_at=NOW()
           WHERE id=%s""",
        (new_balance, wallet["id"]),
    )

    # Queue a cashback notice only when the customer has a verified phone
    # and transactional SMS is enabled. Sending is handled by a separate worker.
    sms_target = conn.execute(
        """SELECT lc.phone_e164, lc.phone_verified_at, scp.transactional_sms_enabled
           FROM loyalty_customers lc
           JOIN sms_customer_preferences scp ON scp.loyalty_customer_id=lc.id
           WHERE lc.id=%s""",
        (loyalty_customer_id,),
    ).fetchone()
    if (sms_target and sms_target["phone_e164"]
            and sms_target["phone_verified_at"]
            and sms_target["transactional_sms_enabled"]):
        sms_phone = str(sms_target["phone_e164"]).strip()
        sms_text = (
            f"مشتری گرامی، مبلغ {cashback:,} تومان کش‌بک بابت سفارش "
            f"{source_order_id} به کیف پول شما اضافه شد."
        )
        conn.execute(
            """INSERT INTO sms_outbox
               (loyalty_customer_id, message_category, destination_phone,
                message_text, status, idempotency_key)
               VALUES(%s,'cashback_notice',%s,%s,'queued',%s)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (
                loyalty_customer_id, sms_phone, sms_text,
                f"cashback-sms:{platform}:{source_order_id}",
            ),
        )
    return cashback




def _ensure_loyalty_profile_for_platform(conn, platform, platform_user_id, display_name=None):
    """Ensure a platform-specific loyalty profile exists; never merge by unverified phone."""
    platform_user_id = str(platform_user_id)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 (f"loyalty-account:{platform}:{platform_user_id}",))
    account = conn.execute(
        """SELECT loyalty_customer_id FROM customer_platform_accounts
           WHERE platform=%s AND platform_user_id=%s FOR UPDATE""",
        (platform, platform_user_id),
    ).fetchone()
    if account:
        loyalty_customer_id = account["loyalty_customer_id"]
        if display_name:
            conn.execute(
                "UPDATE loyalty_customers SET display_name=%s, updated_at=NOW() WHERE id=%s",
                (str(display_name)[:250], loyalty_customer_id),
            )
    else:
        customer = conn.execute(
            "INSERT INTO loyalty_customers(display_name) VALUES(%s) RETURNING id",
            (str(display_name)[:250] if display_name else None,),
        ).fetchone()
        loyalty_customer_id = customer["id"]
        conn.execute(
            """INSERT INTO customer_platform_accounts
               (loyalty_customer_id, platform, platform_user_id, linked_at, last_seen_at)
               VALUES(%s,%s,%s,NOW(),NOW())""",
            (loyalty_customer_id, platform, platform_user_id),
        )
    conn.execute(
        "INSERT INTO wallet_accounts(loyalty_customer_id) VALUES(%s) ON CONFLICT(loyalty_customer_id) DO NOTHING",
        (loyalty_customer_id,),
    )
    conn.execute(
        "INSERT INTO sms_customer_preferences(loyalty_customer_id) VALUES(%s) ON CONFLICT(loyalty_customer_id) DO NOTHING",
        (loyalty_customer_id,),
    )
    return loyalty_customer_id


def _loyalty_admin_summary(conn, platform, platform_user_id):
    row = conn.execute(
        """SELECT lc.id AS loyalty_customer_id, COALESCE(wa.balance_toman,0) AS balance_toman,
                  COALESCE(scp.transactional_sms_enabled,TRUE) AS sms_enabled
           FROM customer_platform_accounts cpa
           JOIN loyalty_customers lc ON lc.id=cpa.loyalty_customer_id
           LEFT JOIN wallet_accounts wa ON wa.loyalty_customer_id=lc.id
           LEFT JOIN sms_customer_preferences scp ON scp.loyalty_customer_id=lc.id
           WHERE cpa.platform=%s AND cpa.platform_user_id=%s""",
        (platform, str(platform_user_id)),
    ).fetchone()
    return row

def db_review_payment(pid, admin_id, approve):
    with get_conn() as conn:
        now = __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M")
        with conn.transaction():
            pending = conn.execute(
                "SELECT * FROM pending_payments WHERE id=%s FOR UPDATE", (pid,)
            ).fetchone()
            if not pending or pending["payment_status"] != "در انتظار بررسی":
                return None, "already"
            if not approve:
                conn.execute(
                    """UPDATE pending_payments
                       SET payment_status='رد شد',reviewed_at=%s,admin_id=%s
                       WHERE id=%s""",
                    (now, admin_id, pid),
                )
                return dict(pending), "rejected"

            order = conn.execute(
                """INSERT INTO orders
                   (user_id,full_name,phone,address,items_summary,total_price,status,
                    created_at,finalized_at,payment_status,transaction_ref)
                   VALUES(%s,%s,%s,%s,%s,%s,'در انتظار بررسی',%s,%s,'تأیید شده',%s)
                   RETURNING id""",
                (
                    pending["user_id"], pending["full_name"], pending["phone"],
                    pending["address"], pending["items_summary"], pending["total_price"],
                    now, now, f"BALE-CARD-TRANSFER-{pid}",
                ),
            ).fetchone()

            cashback_amount = _award_loyalty_cashback(
                conn=conn,
                platform="bale",
                platform_user_id=pending["user_id"],
                order_id=order["id"],
                eligible_amount_toman=pending["total_price"],
                display_name=pending["full_name"],
                phone=pending["phone"],
            )
            conn.execute("DELETE FROM cart_items WHERE user_id=%s", (pending["user_id"],))
            conn.execute(
                """UPDATE pending_payments
                   SET payment_status='تأیید شده',reviewed_at=%s,admin_id=%s,order_id=%s
                   WHERE id=%s""",
                (now, admin_id, order["id"], pid),
            )
            return dict(pending), f"approved:{order['id']}:{cashback_amount}"


async def show_catalog(api, chat_id):
    await api.send(chat_id, "یکی از دسته‌ها را انتخاب کن:", category_keyboard())


async def show_category(api, chat_id, category):
    try:
        products = await asyncio.to_thread(db_products, category)
    except Exception:
        log.exception("Cannot read products")
        await api.send(chat_id, "❌ دریافت محصولات از دیتابیس ناموفق بود.")
        return
    if not products:
        await api.send(chat_id, "فعلاً محصول فعالی در این دسته موجود نیست.")
        return
    for p in products:
        caption = (f"👕 <b>{escape(str(p['name'] or ''))}</b>\nدسته: {escape(str(p['category'] or '-'))}\n"
                   f"سایز: {escape(str(p['size'] or '-'))}\nرنگ‌بندی: {escape(str(p['color'] or '-'))}\n")
        if p["pack_info"]:
            caption += escape(str(p["pack_info"])) + "\n"
        caption += f"قیمت: {int(p['price'] or 0):,} تومان"
        try:
            if str(p["photo_url"] or "").strip():
                await api.photo(chat_id, str(p["photo_url"]).strip(), caption, product_keyboard(p["id"]))
            else:
                await api.send(chat_id, caption, product_keyboard(p["id"]))
        except Exception:
            log.warning("Product send failed, id=%s", p["id"], exc_info=True)
            try:
                await api.send(chat_id, caption, product_keyboard(p["id"]))
            except Exception:
                log.exception("Could not send product id=%s", p["id"])


async def show_cart(api, chat_id, uid):
    try:
        items = await asyncio.to_thread(db_cart, uid)
    except Exception:
        log.exception("Cannot read cart")
        await api.send(chat_id, "❌ دریافت سبد خرید ناموفق بود.")
        return
    if not items:
        await api.send(chat_id, "🛒 سبد خریدت خالی است.", main_keyboard())
        return
    total = 0
    for item in items:
        amount = int(item["line_total"] or 0)
        total += amount
        text = (f"👕 <b>{escape(str(item['name']))}</b>\nتعداد بسته: {item['qty']}\n"
                f"قیمت هر بسته: {int(item['price'] or 0):,} تومان\nجمع: {amount:,} تومان")
        await api.send(chat_id, text, cart_keyboard(item["cart_id"]))
    await api.send(chat_id, f"💰 <b>جمع کل سبد: {total:,} تومان</b>\n\nبرای ادامه، ثبت سفارش را بزن.", checkout_keyboard())


async def start_checkout(api, chat_id, uid):
    try:
        items = await asyncio.to_thread(db_cart, uid)
    except Exception:
        log.exception("Checkout cart lookup failed")
        await api.send(chat_id, "⚠️ فعلاً نتوانستم سبد خرید را بخوانم. چند لحظه بعد دوباره تلاش کن.")
        return
    if not items:
        await api.send(chat_id, "🛒 سبد خرید خالی است؛ اول محصولی اضافه کن.", main_keyboard())
        return
    CHECKOUT[uid] = {"step": "review_quantities"}
    await show_checkout_quantities(api, chat_id, uid)


async def send_payment_instructions(api, chat_id, pending, bank):
    if not bank or not any(bank.get(k) for k in ("bank_name", "owner_name", "card_number", "account_number", "iban")):
        await api.send(chat_id, "⚠️ اطلاعات حساب بانکی هنوز توسط مدیریت تنظیم نشده است. لطفاً بعداً دوباره تلاش کن.", main_keyboard())
        return
    def val(k): return escape(str(bank.get(k) or "-"))
    text = ("💳 <b>پرداخت کارت‌به‌کارت</b>\n\n"
            f"شماره سفارش موقت: <b>#{pending['id']}</b>\n💰 مبلغ قابل پرداخت: <b>{int(pending['total_price']):,} تومان</b>\n\n"
            f"🏦 بانک: <b>{val('bank_name')}</b>\n👤 به نام: <b>{val('owner_name')}</b>\n"
            f"💳 شماره کارت: <code>{val('card_number')}</code>\n🏦 شماره حساب: <code>{val('account_number')}</code>\n🔹 شبا: <code>{val('iban')}</code>\n\n"
            "پس از واریز، عکس رسید را همین‌جا ارسال کن. تا تأیید مدیر، سفارش نهایی نمی‌شود.")
    await api.send(chat_id, text, payment_keyboard(pending["id"], bank.get("card_number")))


async def ask_bale_address(api, chat_id, uid):
    saved = await asyncio.to_thread(db_saved_data, uid, "address")
    if saved:
        await api.send(chat_id, "📍 آدرس ارسال را انتخاب کن یا آدرس جدید وارد کن:", await asyncio.to_thread(saved_keyboard, uid, "address", "address"))
    else:
        await api.send(chat_id, "📍 آدرس کامل پستی، شهر و کدپستی را وارد کن.")


async def handle_customer_support_text(api, msg, uid, chat_id, text):
    state=restore_checkout(uid)
    step=state.get("step")
    if step=="support_new_message":
        body=text.strip()
        if len(body)<3:
            await api.send(chat_id,"متن درخواست خیلی کوتاه است؛ لطفاً جزئیات بیشتری بنویس."); return True
        tid=await create_support_ticket(uid,body,state.get("support_topic","سایر"),state.get("support_order_id"))
        CHECKOUT.pop(uid,None)
        await api.send(chat_id,f"✅ درخواست پشتیبانی ثبت شد.\nشماره تیکت: #{tid}\nاز بخش «درخواست‌های من» می‌توانی پاسخ‌ها را پیگیری کنی.",main_keyboard(uid))
        for aid in ADMIN_IDS:
            try: await api.send(aid,f"🎫 <b>تیکت جدید #{tid}</b>\nموضوع: {escape(state.get('support_topic','سایر'))}\nمشتری: <code>{uid}</code>\n\n{escape(body)}",{"inline_keyboard":[[{"text":"مشاهده و پاسخ","callback_data":f"adm:ticket:{tid}"}]]})
            except Exception: log.exception("Could not notify admin about support ticket %s",tid)
        return True
    if step=="support_reply":
        tid=int(state.get("ticket_id",0)); body=text.strip()
        if len(body)<1: await api.send(chat_id,"پیام خالی قابل ارسال نیست."); return True
        ok=await add_ticket_message(tid,uid,"customer",body)
        CHECKOUT.pop(uid,None)
        if ok:
            await api.send(chat_id,f"✅ پاسخ شما به تیکت #{tid} ثبت شد.",main_keyboard(uid))
            for aid in ADMIN_IDS:
                try: await api.send(aid,f"💬 پاسخ جدید مشتری در تیکت #{tid}\nمشتری: <code>{uid}</code>\n\n{escape(body)}",{"inline_keyboard":[[{"text":"مشاهده تیکت","callback_data":f"adm:ticket:{tid}"}]]})
                except Exception: log.exception("Could not notify admin about ticket reply")
        else: await api.send(chat_id,"این تیکت بسته شده یا دیگر قابل پاسخ نیست.",main_keyboard(uid))
        return True
    return False


async def handle_checkout_text(api, msg, uid, chat_id, text):
    state = CHECKOUT.get(uid, {})
    step = state.get("step")
    if step == "edit_saved":
        data_type, data_id = state.get("edit_type"), state.get("edit_id")
        value = re.sub(r"\s+", " ", text).strip()
        if not value:
            await api.send(chat_id, "مقدار خالی قابل ذخیره نیست؛ دوباره وارد کن."); return True
        if data_type == "phone":
            digits = value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
            value = re.sub(r"[\s\-()]+", "", digits)
            if value.startswith("+98"): value = "0" + value[3:]
            elif value.startswith("0098"): value = "0" + value[4:]
            elif value.startswith("98"): value = "0" + value[2:]
            if len(value) != 11 or not value.startswith("09") or not value.isdigit():
                await api.send(chat_id, "شماره معتبر نیست؛ نمونه: 09123456789"); return True
        await asyncio.to_thread(db_update_saved, uid, data_id, data_type, value)
        CHECKOUT.pop(uid, None)
        await api.send(chat_id, "✅ اطلاعات ذخیره‌شده ویرایش شد.", main_keyboard())
        return True
    if text == "🔙 لغو ثبت سفارش" or text == "/cancel":
        CHECKOUT.pop(uid, None)
        await api.send(chat_id, "ثبت سفارش لغو شد.", main_keyboard())
        return True
    if step == "name":
        name = normalize_name(text)
        if len(name.split()) < 2 or len(name) < 5:
            await api.send(chat_id, "❌ نام و نام خانوادگی را کامل وارد کن. مثال: محمد احمدی")
            return True
        state["full_name"] = name
        state["step"] = "confirm_name"
        await asyncio.to_thread(db_save_data, uid, "name", name)
        await api.send(chat_id, f"👤 نام ثبت سفارش: <b>{escape(name)}</b>\n\nآیا این نام درست است؟", {"inline_keyboard":[[{"text":"✅ تأیید نام","callback_data":"nameconfirm:yes"},{"text":"✏️ ویرایش نام","callback_data":"nameconfirm:edit"}]]})
        return True
    if step == "phone":
        phone = normalize_phone(text)
        if len(phone) != 11 or not phone.startswith("09") or not phone.isdigit():
            await api.send(chat_id, "❌ شماره معتبر نیست. شماره را به شکل 09123456789 وارد کن.")
            return True
        state["phone"] = phone
        await asyncio.to_thread(db_save_data, uid, "phone", phone)
        known = await asyncio.to_thread(get_known_gender, uid, state.get("full_name", ""))
        if known:
            state["gender"] = known
            state["step"] = "address"
            await ask_bale_address(api, chat_id, uid)
        else:
            state["step"] = "gender"
            await api.send(chat_id, "جنسیت را انتخاب کن:", {"inline_keyboard":[[{"text":"مرد","callback_data":"checkoutgender:مرد"},{"text":"زن","callback_data":"checkoutgender:زن"}]]})
        return True
    if step == "gender":
        if text not in ("مرد", "زن"):
            await api.send(chat_id, "لطفاً یکی از گزینه‌های «مرد» یا «زن» را انتخاب کن.")
            return True
        state["gender"] = text
        await asyncio.to_thread(set_known_gender, uid, state.get("full_name", ""), text)
        state["step"] = "address"
        saved = await asyncio.to_thread(db_saved_data, uid, "address")
        if saved:
            await api.send(chat_id, "📍 آدرس ارسال را انتخاب کن یا آدرس جدید وارد کن:", await asyncio.to_thread(saved_keyboard, uid, "address", "address"))
        else:
            await api.send(chat_id, "📍 آدرس کامل پستی، شهر و کدپستی را وارد کن.")
        return True
    if step == "address":
        address = re.sub(r"\s+", " ", text).strip()
        if len(address) < 10:
            await api.send(chat_id, "❌ آدرس خیلی کوتاه است. لطفاً آدرس کامل‌تری وارد کن.")
            return True
        state["address"] = address
        await asyncio.to_thread(db_save_data, uid, "address", address)
        try:
            pending, bank = await asyncio.to_thread(db_create_pending, uid, state["full_name"], state["phone"], state["gender"], address, (msg.get("from") or {}).get("username", ""))
        except Exception:
            log.exception("Could not create pending payment for Bale user %s", uid)
            await api.send(chat_id, "⚠️ ثبت سفارش انجام نشد. اطلاعات سبد خرید حذف نشده؛ لطفاً کمی بعد دوباره «سبد خرید» را باز کن.", main_keyboard())
            CHECKOUT.pop(uid, None)
            return True
        CHECKOUT.pop(uid, None)
        await send_order_sms_once(
            pending_id=pending["id"],
            full_name=pending.get("full_name", state["full_name"]),
            phone=pending.get("phone", state["phone"]),
            total_price=pending.get("total_price", 0),
            items_summary=pending.get("items_summary", ""),
            platform="bale",
        )
        await send_payment_instructions(api, chat_id, pending, bank)
        return True
    return False


async def handle_receipt(api, msg, uid, chat_id):
    photos = msg.get("photo") or []
    document = msg.get("document") or {}
    file_id = photos[-1].get("file_id") if photos else document.get("file_id")
    if not file_id:
        return False
    try:
        pending = await asyncio.to_thread(db_record_receipt, uid, file_id)
    except Exception:
        log.exception("Could not store Bale receipt")
        await api.send(chat_id, "⚠️ ذخیره رسید انجام نشد. لطفاً دوباره ارسال کن.")
        return True
    if not pending:
        await api.send(chat_id, "پرداختی در انتظار رسید برای این حساب پیدا نکردم. اگر قبلاً رسید فرستاده‌ای، وضعیت آن در حال بررسی است.")
        return True
    await api.send(chat_id, "✅ رسید دریافت شد و برای بررسی ارسال شد. تا تأیید مدیر منتظر بمان.", main_keyboard())
    if not ADMIN_IDS:
        log.error("BALE_ADMIN_IDS is not configured; receipt #%s is awaiting review but no Bale admin was notified", pending["id"])
        await api.send(chat_id, "⚠️ رسید ذخیره شد، اما مدیر بله برای دریافت اعلان تنظیم نشده است. لطفاً فروشگاه را از طریق پشتیبانی مطلع کن.")
        return True
    caption = (f"💳 <b>رسید پرداخت جدید</b>\nشناسه پرداخت: #{pending['id']}\n"
               f"👤 مشتری: {escape(str(pending.get('full_name') or '-'))}\n📞 موبایل: {escape(str(pending.get('phone') or '-'))}\n"
               f"📍 آدرس: {escape(str(pending.get('address') or '-'))}\n\n🛍 محصولات:\n{escape(str(pending.get('items_summary') or '-'))}\n"
               f"💰 مبلغ: <b>{int(pending.get('total_price') or 0):,} تومان</b>")
    for admin_id in ADMIN_IDS:
        try:
            if photos:
                await api.call("sendPhoto", {"chat_id": admin_id, "photo": file_id, "caption": caption, "parse_mode": "HTML", "reply_markup": review_keyboard(pending["id"])})
            else:
                await api.call("sendDocument", {"chat_id": admin_id, "document": file_id, "caption": caption, "parse_mode": "HTML", "reply_markup": review_keyboard(pending["id"])})
        except Exception:
            log.exception("Could not notify Bale admin %s for payment %s", admin_id, pending["id"])
    return True


# ---------------------------------------------------------------------------
# Telegram-parity customer flows: orders, support tickets, editable checkout
# ---------------------------------------------------------------------------
def db_register_customer(uid, username=""):
    now = iran_now_text()
    with get_conn() as conn:
        conn.execute("""INSERT INTO customers(user_id,username,first_seen,last_seen)
            VALUES(%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,last_seen=excluded.last_seen""",
            (int(uid), username or "", now, now))
        conn.commit()


def checkout_quantity_keyboard(rows):
    buttons=[]
    for row in rows:
        buttons.append([
            {"text":"➖", "callback_data":f"checkoutqty:minus:{row['cart_id']}"},
            {"text":f"{str(row['name'])[:20]} × {row['qty']}", "callback_data":f"checkoutqty:noop:{row['cart_id']}"},
            {"text":"➕", "callback_data":f"checkoutqty:plus:{row['cart_id']}"},
        ])
    buttons.append([{"text":"🗑 حذف یک محصول", "callback_data":"checkoutqty:delete_menu"}])
    buttons.append([{"text":"✅ ادامه ثبت سفارش", "callback_data":"checkoutqty:continue"}])
    buttons.append([{"text":"🔙 بازگشت به سبد خرید", "callback_data":"checkoutqty:back"}])
    return {"inline_keyboard":buttons}


def checkout_quantity_text(rows):
    total=sum(int(r["price"] or 0)*int(r["qty"] or 0) for r in rows)
    lines=["🛒 <b>بازبینی سبد خرید</b>","","تعداد بسته‌ها را پیش از ثبت سفارش بررسی کن:",""]
    for r in rows:
        lines.append(f"• {escape(str(r['name']))} — {int(r['qty'])} × {int(r['price']):,} = {int(r['qty'])*int(r['price']):,} تومان")
    lines += ["",f"💰 <b>جمع کل: {total:,} تومان</b>"]
    return "\n".join(lines)


async def show_checkout_quantities(api, chat_id, uid, mid=None):
    rows=await asyncio.to_thread(db_cart,uid)
    if not rows:
        await api.send(chat_id,"🛒 سبد خرید خالی است؛ ابتدا محصولی انتخاب کن.",main_keyboard(uid)); return
    text=checkout_quantity_text(rows)
    markup=checkout_quantity_keyboard(rows)
    if mid:
        try: await api.edit(chat_id,mid,text,markup); return
        except Exception: log.debug("Checkout quantity edit unsupported; sending a new message",exc_info=True)
    await api.send(chat_id,text,markup)


def customer_order_keyboard(rows):
    buttons=[]
    for o in rows:
        buttons.append([{"text":f"📦 سفارش #{o['id']} | {format_iran_jalali_fa(o.get('created_at'),False)}", "callback_data":f"customer_order:view:{o['id']}"}])
    buttons.append([{"text":"🔙 بستن", "callback_data":"customer_order:close"}])
    return {"inline_keyboard":buttons}


async def show_my_orders(api, chat_id, uid, mid=None):
    with get_conn() as conn:
        rows=conn.execute("SELECT id,created_at FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT 30",(uid,)).fetchall()
    if not rows:
        await api.send(chat_id,"📦 هنوز سفارشی برای این حساب ثبت نشده است.",main_keyboard(uid)); return
    text="📦 <b>سفارش‌های من</b>\n\nبرای مشاهده جزئیات، یک سفارش را انتخاب کن:"
    if mid:
        try: await api.edit(chat_id,mid,text,customer_order_keyboard(rows)); return
        except Exception: pass
    await api.send(chat_id,text,customer_order_keyboard(rows))


def support_home_keyboard():
    return {"inline_keyboard":[
        [{"text":"📦 پیگیری سفارش", "callback_data":"support:orders"}],
        [{"text":"💳 مشکل پرداخت", "callback_data":"support:topic:پرداخت"},{"text":"🛍 سؤال محصول", "callback_data":"support:topic:محصول"}],
        [{"text":"🚚 ارسال", "callback_data":"support:topic:ارسال"},{"text":"🔄 تعویض / مرجوعی", "callback_data":"support:topic:تعویض"}],
        [{"text":"📝 ثبت درخواست پشتیبانی", "callback_data":"support:new"}],
        [{"text":"🎫 درخواست‌های من", "callback_data":"support:mine"}],
        [{"text":"❓ سؤالات متداول", "callback_data":"support:faq"}],
        [{"text":"📞 تماس با پشتیبانی", "callback_data":"support:phone"}],
    ]}


def support_status_fa(status):
    return {"new":"🔴 جدید","admin_waiting":"🟡 در انتظار پاسخ مدیر","customer_waiting":"🔵 در انتظار پاسخ مشتری","closed":"🟢 بسته‌شده"}.get(status,status or "-")


async def show_support_tickets(api, chat_id, uid, mid=None, admin=False, status_filter="all"):
    with get_conn() as conn:
        if admin:
            if status_filter=="all":
                rows=conn.execute("SELECT t.*,c.full_name,c.username FROM support_tickets t LEFT JOIN customers c ON c.user_id=t.user_id ORDER BY t.id DESC LIMIT 40").fetchall()
            else:
                rows=conn.execute("SELECT t.*,c.full_name,c.username FROM support_tickets t LEFT JOIN customers c ON c.user_id=t.user_id WHERE t.status=%s ORDER BY t.id DESC LIMIT 40",(status_filter,)).fetchall()
        else:
            rows=conn.execute("SELECT * FROM support_tickets WHERE user_id=%s ORDER BY id DESC LIMIT 30",(uid,)).fetchall()
    buttons=[]
    for t in rows:
        if admin:
            label=f"🎫 #{t['id']} · {str(t.get('full_name') or t.get('username') or t['user_id'])[:14]} · {str(t['topic'])[:12]} · {support_status_fa(t['status'])}"
            buttons.append([{"text":label[:60],"callback_data":f"adm:ticket:{t['id']}"}])
        else:
            label=f"🎫 #{t['id']} · {str(t['topic'])[:16]} · {support_status_fa(t['status'])}"
            buttons.append([{"text":label[:60],"callback_data":f"support:view:{t['id']}"}])
    if admin:
        buttons += [[{"text":"🔴 جدید", "callback_data":"adm:tickets:new"},{"text":"🟡 منتظر مدیر", "callback_data":"adm:tickets:admin_waiting"}],
                    [{"text":"🔵 منتظر مشتری", "callback_data":"adm:tickets:customer_waiting"},{"text":"🟢 بسته", "callback_data":"adm:tickets:closed"}],
                    [{"text":"➕ همه", "callback_data":"adm:tickets:all"}],
                    [{"text":"🔙 پنل اصلی", "callback_data":"adm:home"}]]
        text="🎫 <b>تیکت‌های پشتیبانی</b>"
    else:
        buttons += [[{"text":"📝 ثبت درخواست جدید", "callback_data":"support:new"}], [{"text":"🔙 مرکز پشتیبانی", "callback_data":"support:home"}]]
        text="🎫 <b>درخواست‌های پشتیبانی من</b>"
    markup={"inline_keyboard":buttons}
    if mid:
        try: await api.edit(chat_id,mid,text,markup); return
        except Exception: pass
    await api.send(chat_id,text,markup)


async def show_ticket_detail(api, chat_id, uid, ticket_id, mid=None, admin=False):
    with get_conn() as conn:
        if admin:
            ticket=conn.execute("SELECT t.*,c.full_name,c.username FROM support_tickets t LEFT JOIN customers c ON c.user_id=t.user_id WHERE t.id=%s",(ticket_id,)).fetchone()
        else:
            ticket=conn.execute("SELECT * FROM support_tickets WHERE id=%s AND user_id=%s",(ticket_id,uid)).fetchone()
        messages=conn.execute("SELECT * FROM support_ticket_messages WHERE ticket_id=%s ORDER BY id ASC",(ticket_id,)).fetchall() if ticket else []
    if not ticket:
        await api.send(chat_id,"تیکت پیدا نشد یا اجازه مشاهده آن را نداری."); return
    lines=[f"🎫 <b>تیکت #{ticket_id}</b>",f"🏷 موضوع: {escape(str(ticket['topic']))}",f"📌 وضعیت: {support_status_fa(ticket['status'])}",f"📅 ثبت: {format_iran_jalali_fa(ticket.get('created_at'),True)}"]
    if admin:
        lines += [f"👤 مشتری: {escape(str(ticket.get('full_name') or '-'))}",f"🆔 شناسه مشتری: <code>{ticket['user_id']}</code>"]
        if ticket.get("order_id"): lines.append(f"📦 سفارش: #{ticket['order_id']}")
    lines += ["","<b>💬 گفتگو:</b>"]
    for m in messages[-12:]:
        who="مشتری" if m["sender_type"]=="customer" else "مدیر"
        lines.append(f"<b>{who}:</b> {escape(str(m['message']))}")
    buttons=[]
    if ticket["status"]!="closed":
        buttons.append([{"text":"💬 پاسخ دادن", "callback_data":(f"adm:ticket_reply:{ticket_id}" if admin else f"support:reply:{ticket_id}")}])
    if admin and ticket["status"]!="closed": buttons.append([{"text":"🟢 بستن تیکت", "callback_data":f"adm:ticket_close:{ticket_id}"}])
    buttons.append([{"text":"🔙 بازگشت", "callback_data":"adm:tickets" if admin else "support:mine"}])
    text="\n".join(lines)
    if mid:
        try: await api.edit(chat_id,mid,text,{"inline_keyboard":buttons}); return
        except Exception: pass
    await api.send(chat_id,text,{"inline_keyboard":buttons})


async def create_support_ticket(uid, text, topic="سایر", order_id=None):
    now=iran_now_text()
    with get_conn() as conn:
        cur=conn.execute("INSERT INTO support_tickets(user_id,order_id,topic,status,created_at,updated_at) VALUES(%s,%s,%s,'new',%s,%s) RETURNING id",(uid,order_id,topic,now,now))
        tid=cur.fetchone()["id"]
        conn.execute("INSERT INTO support_ticket_messages(ticket_id,sender_id,sender_type,message,created_at) VALUES(%s,%s,'customer',%s,%s)",(tid,uid,text,now))
        conn.commit()
    return tid


async def add_ticket_message(ticket_id, sender_id, sender_type, message):
    now=iran_now_text()
    with get_conn() as conn:
        t=conn.execute("SELECT id,status FROM support_tickets WHERE id=%s",(ticket_id,)).fetchone()
        if not t or t["status"]=="closed": return False
        conn.execute("INSERT INTO support_ticket_messages(ticket_id,sender_id,sender_type,message,created_at) VALUES(%s,%s,%s,%s,%s)",(ticket_id,sender_id,sender_type,message,now))
        status="customer_waiting" if sender_type=="admin" else "admin_waiting"
        conn.execute("UPDATE support_tickets SET status=%s,updated_at=%s WHERE id=%s",(status,now,ticket_id))
        conn.commit()
    return True


async def handle_admin_text(api, msg, uid, chat_id, text):
    state=restore_checkout(uid)
    flow=state.get("admin_flow")
    if not flow: return False
    step=flow.get("step")
    if flow.get("type")=="add_product":
        if step=="name":
            if not text.strip(): await api.send(chat_id,"نام محصول خالی است؛ دوباره وارد کن."); return True
            state.update({"step":"category","name":text.strip()}); await api.send(chat_id,"دسته محصول را وارد کن: بچگانه یا زنانه."); return True
        if step=="category":
            if text not in ("بچگانه","زنانه"): await api.send(chat_id,"فقط «بچگانه» یا «زنانه» وارد کن."); return True
            state.update({"step":"size","category":text}); await api.send(chat_id,"سایز را وارد کن:"); return True
        if step=="size": state.update({"step":"color","size":text}); await api.send(chat_id,"رنگ‌بندی را وارد کن (یا -):"); return True
        if step=="color": state.update({"step":"pack_info","color":"" if text.strip()=="-" else text.strip()}); await api.send(chat_id,"اطلاعات پک را وارد کن؛ اگر ندارد «ندارد» بنویس:"); return True
        if step=="pack_info": state.update({"step":"price","pack_info":"" if text.lower().strip() in ("ندارد","ندارم","-","no") else text.strip()}); await api.send(chat_id,"قیمت را به تومان و فقط به عدد وارد کن:"); return True
        if step=="price":
            try: price=int(text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹","0123456789")).replace(",","")); assert price>=0
            except Exception: await api.send(chat_id,"قیمت نامعتبر است؛ یک عدد صفر یا بزرگ‌تر وارد کن."); return True
            state.update({"step":"photo_url","price":price}); await api.send(chat_id,"لینک مستقیم عکس محصول را بفرست؛ در صورت نداشتن عکس «ندارد» بنویس:"); return True
        if step=="photo_url":
            url="" if text.lower().strip() in ("ندارد","ندارم","-","no") else text.strip()
            if url and not url.startswith(("https://","http://")): await api.send(chat_id,"لینک عکس باید با http:// یا https:// شروع شود."); return True
            with get_conn() as conn:
                row=conn.execute("INSERT INTO products(name,category,size,color,price,photo_url,active,pack_info) VALUES(%s,%s,%s,%s,%s,%s,TRUE,%s) RETURNING id",(state["name"],state["category"],state["size"],state.get("color",""),state["price"],url,state.get("pack_info",""))).fetchone(); conn.commit()
            CHECKOUT.pop(uid,None); await api.send(chat_id,f"✅ محصول «{escape(state['name'])}» با شناسه #{row['id']} اضافه شد.",admin_keyboard()); return True
    if flow.get("type")=="product_field":
        pid=int(flow["product_id"]); field=flow["field"]
        allowed={"name","category","size","color","pack_info","price","photo_url"}
        if field not in allowed: CHECKOUT.pop(uid,None); return True
        value=text.strip()
        if field=="category" and value not in ("بچگانه","زنانه"): await api.send(chat_id,"دسته باید بچگانه یا زنانه باشد."); return True
        if field=="price":
            try: value=int(value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹","0123456789")).replace(",","")); assert value>=0
            except Exception: await api.send(chat_id,"قیمت نامعتبر است؛ دوباره به تومان وارد کن."); return True
        if field=="pack_info" and value.lower() in ("ندارد","ندارم","-","no"): value=""
        if field=="photo_url" and value.lower() in ("ندارد","ندارم","-","no"): value=""
        with get_conn() as conn:
            conn.execute(f"UPDATE products SET {field}=%s WHERE id=%s",(value,pid)); conn.commit()
        CHECKOUT.pop(uid,None); await api.send(chat_id,f"✅ فیلد {field} برای محصول #{pid} به‌روزرسانی شد.",admin_keyboard()); return True
    if flow.get("type")=="bank_add" or flow.get("type")=="bank_edit":
        keys=["bank_name","owner_name","card_number","account_number","iban"]
        idx=int(flow.get("index",0)); mode=flow["type"]
        if idx>=len(keys): CHECKOUT.pop(uid,None); return True
        key=keys[idx]; value=text.strip()
        if key in ("bank_name","owner_name","card_number") and not value:
            await api.send(chat_id,"این فیلد نمی‌تواند خالی باشد؛ دوباره وارد کن."); return True
        data=dict(flow.get("data") or {}); data[key]=value
        idx+=1
        if idx<len(keys):
            state.update({"index":idx,"data":data})
            prompts={"owner_name":"نام صاحب حساب:","card_number":"شماره کارت ۱۶ رقمی:","account_number":"شماره حساب (در صورت نداشتن -):","iban":"شماره شبا (در صورت نداشتن -):"}
            await api.send(chat_id,prompts.get(keys[idx],"اطلاعات حساب را وارد کن:")); return True
        now=iran_now_text()
        data={k:("" if data.get(k)=="-" else data.get(k,"")) for k in keys}
        with get_conn() as conn:
            if mode=="bank_add":
                conn.execute("UPDATE bank_accounts SET active=FALSE,updated_at=%s WHERE active=TRUE",(now,))
                conn.execute("INSERT INTO bank_accounts(bank_name,owner_name,card_number,account_number,iban,active,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,TRUE,%s,%s)",(data["bank_name"],data["owner_name"],data["card_number"],data["account_number"],data["iban"],now,now))
            else:
                aid=int(flow["account_id"])
                conn.execute("UPDATE bank_accounts SET bank_name=%s,owner_name=%s,card_number=%s,account_number=%s,iban=%s,updated_at=%s WHERE id=%s",(data["bank_name"],data["owner_name"],data["card_number"],data["account_number"],data["iban"],now,aid))
            conn.commit()
        CHECKOUT.pop(uid,None); await api.send(chat_id,"✅ اطلاعات حساب بانکی ذخیره شد.",admin_keyboard()); return True
    if flow.get("type")=="channel_time":
        value=text.strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",value):
            await api.send(chat_id,"فرمت ساعت نامعتبر است؛ مانند 08:15 وارد کن."); return True
        meta_set(f"bale_channel_time_{flow['kind']}",value)
        CHECKOUT.pop(uid,None); await api.send(chat_id,f"✅ ساعت پست {'صبح' if flow['kind']=='morning' else 'شب'} روی {value} تنظیم شد.",admin_keyboard()); return True
    if flow.get("type")=="channel_message":
        if not text.strip(): await api.send(chat_id,"متن پست خالی است؛ دوباره ارسال کن."); return True
        meta_set(f"bale_channel_message_{flow['kind']}",text.strip())
        CHECKOUT.pop(uid,None); await api.send(chat_id,"✅ متن پست کانال ذخیره شد.",admin_keyboard()); return True
    if flow.get("type")=="ticket_reply":
        tid=int(flow["ticket_id"])
        with get_conn() as conn:
            ticket=conn.execute("SELECT user_id FROM support_tickets WHERE id=%s",(tid,)).fetchone()
        if not ticket: CHECKOUT.pop(uid,None); await api.send(chat_id,"تیکت پیدا نشد."); return True
        ok=await add_ticket_message(tid,uid,"admin",text)
        CHECKOUT.pop(uid,None)
        if ok:
            try: await api.send(int(ticket["user_id"]),f"💬 پاسخ جدید پشتیبانی برای تیکت #{tid} دریافت شد. برای مشاهده، «مرکز پشتیبانی» را باز کن.",main_keyboard(int(ticket["user_id"])))
            except Exception: log.exception("Could not notify ticket owner")
            await api.send(chat_id,"✅ پاسخ برای مشتری ثبت شد.",admin_keyboard())
        else: await api.send(chat_id,"این تیکت بسته شده یا دیگر قابل پاسخ نیست.",admin_keyboard())
        return True
    return False


# ---------------------------------------------------------------------------
# Admin product/bank/customer/ticket management parity
# ---------------------------------------------------------------------------
def admin_products_keyboard(rows):
    buttons=[[{"text":"➕ افزودن محصول","callback_data":"adm:add_product"}]]
    for r in rows:
        state="🟢" if r["active"] else "⚪"
        buttons.append([{"text":f"{state} #{r['id']} · {str(r['name'])[:35]}","callback_data":f"adm:product:{r['id']}"}])
    buttons += [[{"text":"🔄 بروزرسانی","callback_data":"adm:products"}], [{"text":"🔙 پنل اصلی","callback_data":"adm:home"}]]
    return {"inline_keyboard":buttons}


async def show_admin_products(api, chat_id, mid=None):
    with get_conn() as conn: rows=conn.execute("SELECT id,name,price,active FROM products ORDER BY id DESC LIMIT 45").fetchall()
    text=f"🛠 <b>مدیریت محصولات</b>\n\nتعداد محصولات: {len(rows)}\nبرای ویرایش قیمت، نام، سایز، رنگ، پک یا وضعیت، محصول را انتخاب کن."
    markup=admin_products_keyboard(rows)
    if mid:
        try: await api.edit(chat_id,mid,text,markup); return
        except Exception: pass
    await api.send(chat_id,text,markup)


async def show_admin_product(api, chat_id, pid, mid=None, quick=False, price_quick=False):
    with get_conn() as conn: p=conn.execute("SELECT * FROM products WHERE id=%s",(pid,)).fetchone()
    if not p: await api.send(chat_id,"محصول پیدا نشد.",admin_back("products")); return
    text=(f"🛍 <b>{escape(str(p['name']))}</b>\nشناسه: #{pid}\nدسته: {escape(str(p['category'] or '-'))}\n"
          f"سایز: {escape(str(p['size'] or '-'))}\nرنگ‌بندی: {escape(str(p.get('color') or '-'))}\n"
          f"پک: {escape(str(p.get('pack_info') or '-'))}\nقیمت: <b>{int(p['price'] or 0):,} تومان</b>\n"
          f"وضعیت: {'فعال 🟢' if p['active'] else 'غیرفعال ⚪'}")
    buttons=[[{"text":"✏️ ویرایش نام","callback_data":f"adm:product_field:{pid}:name"},{"text":"💰 تغییر قیمت","callback_data":f"adm:price_set:{pid}"}],
             [{"text":"📂 دسته","callback_data":f"adm:product_field:{pid}:category"},{"text":"📏 سایز","callback_data":f"adm:product_field:{pid}:size"}],
             [{"text":"🎨 رنگ","callback_data":f"adm:product_field:{pid}:color"},{"text":"📦 اطلاعات پک","callback_data":f"adm:product_field:{pid}:pack_info"}],
             [{"text":"🖼 لینک عکس","callback_data":f"adm:product_field:{pid}:photo_url"}],
             [{"text":"➖ ۵۰ هزار","callback_data":f"adm:price_adjust:{pid}:-50000"},{"text":"➕ ۵۰ هزار","callback_data":f"adm:price_adjust:{pid}:50000"}],
             [{"text":"➖ ۱۰۰ هزار","callback_data":f"adm:price_adjust:{pid}:-100000"},{"text":"➕ ۱۰۰ هزار","callback_data":f"adm:price_adjust:{pid}:100000"}],
             [{"text":("⛔ غیرفعال کردن" if p['active'] else "🟢 فعال کردن"),"callback_data":f"adm:toggle_product:{pid}"}],
             [{"text":"🗑 حذف امن (غیرفعال‌سازی)","callback_data":f"adm:delete_product:{pid}"}],
             [{"text":"🔙 فهرست محصولات","callback_data":"adm:products"}]]
    markup={"inline_keyboard":buttons}
    if mid:
        try: await api.edit(chat_id,mid,text,markup); return
        except Exception: pass
    await api.send(chat_id,text,markup)


async def show_admin_banks(api, chat_id, mid=None):
    with get_conn() as conn: rows=conn.execute("SELECT * FROM bank_accounts ORDER BY active DESC,id DESC LIMIT 30").fetchall()
    lines=["💳 <b>مدیریت حساب‌های بانکی</b>",""]
    buttons=[[{"text":"➕ افزودن حساب بانکی","callback_data":"adm:bank_add"}]]
    for r in rows:
        lines.append(f"#{r['id']} · {escape(str(r['bank_name'] or '-'))} · {'🟢 فعال' if r['active'] else '⚪ غیرفعال'}")
        buttons.append([{"text":f"#{r['id']} · {str(r['bank_name'] or '-')[:25]} {'🟢' if r['active'] else '⚪'}","callback_data":f"adm:bank:{r['id']}"}])
    if not rows: lines.append("حساب بانکی ثبت نشده است.")
    buttons.append([{"text":"🔙 پنل اصلی","callback_data":"adm:home"}])
    if mid:
        try: await api.edit(chat_id,mid,"\n".join(lines),{"inline_keyboard":buttons}); return
        except Exception: pass
    await api.send(chat_id,"\n".join(lines),{"inline_keyboard":buttons})


async def show_admin_bank(api, chat_id, aid, mid=None):
    with get_conn() as conn: a=conn.execute("SELECT * FROM bank_accounts WHERE id=%s",(aid,)).fetchone()
    if not a: await api.send(chat_id,"حساب بانکی پیدا نشد.",admin_back("payment")); return
    text=(f"🏦 <b>حساب بانکی #{aid}</b>\n\nبانک: {escape(str(a.get('bank_name') or '-'))}\n"
          f"صاحب حساب: {escape(str(a.get('owner_name') or '-'))}\nکارت: <code>{escape(str(a.get('card_number') or '-'))}</code>\n"
          f"حساب: <code>{escape(str(a.get('account_number') or '-'))}</code>\nشبا: <code>{escape(str(a.get('iban') or '-'))}</code>\n"
          f"وضعیت: {'فعال 🟢' if a['active'] else 'غیرفعال ⚪'}\nآخرین تغییر: {format_iran_jalali_fa(a.get('updated_at'),True)}")
    buttons=[[{"text":"✏️ ویرایش همه اطلاعات","callback_data":f"adm:bank_edit:{aid}"}],
             [{"text":"🟢 فعال‌کردن (حساب‌های دیگر غیرفعال می‌شوند)" if not a['active'] else "🔴 غیرفعال‌کردن","callback_data":f"adm:bank_toggle:{aid}"}],
             [{"text":"🗑 حذف حساب غیرفعال","callback_data":f"adm:bank_delete:{aid}"}],
             [{"text":"🔙 حساب‌های بانکی","callback_data":"adm:payment"}]]
    if mid:
        try: await api.edit(chat_id,mid,text,{"inline_keyboard":buttons}); return
        except Exception: pass
    await api.send(chat_id,text,{"inline_keyboard":buttons})


async def show_admin_customer(api, chat_id, customer_id, mid=None):
    with get_conn() as conn:
        c=conn.execute("SELECT * FROM customers WHERE user_id=%s",(customer_id,)).fetchone()
        orders=conn.execute("SELECT * FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT 10",(customer_id,)).fetchall()
        tickets=conn.execute("SELECT id,topic,status FROM support_tickets WHERE user_id=%s ORDER BY id DESC LIMIT 5",(customer_id,)).fetchall()
        loyalty_summary=_loyalty_admin_summary(conn,"bale",customer_id)
    if not c: await api.send(chat_id,"مشتری پیدا نشد.",admin_back("customers")); return
    saved_name=await asyncio.to_thread(db_saved_data,customer_id,"name")
    saved_phone=await asyncio.to_thread(db_saved_data,customer_id,"phone")
    saved_address=await asyncio.to_thread(db_saved_data,customer_id,"address")
    name=saved_name[0]["value"] if saved_name else c.get("full_name") or "-"
    phone=saved_phone[0]["value"] if saved_phone else c.get("phone") or "-"
    address=saved_address[0]["value"] if saved_address else "-"
    total=sum(int(o.get("total_price") or 0) for o in orders if o.get("status") not in ("رد شد","لغو شده"))
    lines=["👤 <b>جزئیات مشتری</b>",f"نام: {escape(str(name))}",f"شناسه: <code>{customer_id}</code>",f"نام کاربری: @{escape(str(c.get('username') or '-'))}",f"تلفن: {escape(str(phone))}",f"آدرس ذخیره‌شده: {escape(str(address))}",f"جنسیت: {escape(str(c.get('gender') or '-'))}",f"اولین ورود: {format_iran_jalali_fa(c.get('first_seen'),True)}",f"آخرین فعالیت: {format_iran_jalali_fa(c.get('last_seen'),True)}",f"تعداد سفارش‌های اخیر: {len(orders)}",f"مجموع سفارش‌های غیرلغوشده: {total:,} تومان",f"👛 موجودی کیف پول باشگاه: {int((loyalty_summary or {}).get('balance_toman') or 0):,} تومان",f"📩 پیامک سیستمی: {'فعال' if (loyalty_summary or {}).get('sms_enabled',True) else 'غیرفعال'}"]
    buttons=[]
    for o in orders: buttons.append([{"text":f"📦 سفارش #{o['id']} · {str(o['status'])[:15]}","callback_data":f"adm:orderdetail:{o['id']}"}])
    for t in tickets: buttons.append([{"text":f"🎫 تیکت #{t['id']} · {str(t['topic'])[:15]}","callback_data":f"adm:ticket:{t['id']}"}])
    sms_label = "🔕 غیرفعال‌سازی پیامک مشتری" if (loyalty_summary or {}).get("sms_enabled",True) else "🔔 فعال‌سازی پیامک مشتری"
    buttons.append([{"text":sms_label,"callback_data":f"adm:sms_toggle:{customer_id}"}])
    buttons.append([{"text":"🔙 مشتری‌ها","callback_data":"adm:customers"}])
    if mid:
        try: await api.edit(chat_id,mid,"\n".join(lines),{"inline_keyboard":buttons}); return
        except Exception: pass
    await api.send(chat_id,"\n".join(lines),{"inline_keyboard":buttons})


# ---------------------------------------------------------------------------
# Bale channel management. Sending is enabled only when BALE_CHANNEL_ID is set
# and the bot has permission to post in that channel.
# ---------------------------------------------------------------------------
BALE_CHANNEL_ID = os.getenv("BALE_CHANNEL_ID", "").strip()
BALE_WEATHER_CITY = os.getenv("WEATHER_CITY_NAME", "مشهد").strip() or "مشهد"
BALE_WEATHER_LAT = os.getenv("WEATHER_LAT", "36.2605").strip()
BALE_WEATHER_LON = os.getenv("WEATHER_LON", "59.6168").strip()
BALE_MARKET_URL = "https://iran-market.github.io/data/popular.json"
CHANNEL_DEFAULTS = {
    "morning": "🌸 روز خوبی را با انرژی و لبخند شروع کنیم. از همراهی شما با تنین ایران سپاسگزاریم.",
    "night": "🌙 شبتان آرام و پر از حال خوب. ممنون که همراه تنین ایران هستید.",
}


def meta_get(key):
    with get_conn() as conn:
        row=conn.execute("SELECT value FROM app_meta WHERE key=%s",(key,)).fetchone()
    return row["value"] if row else None


def meta_set(key,value):
    with get_conn() as conn:
        conn.execute("INSERT INTO app_meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))
        conn.commit()


def channel_enabled():
    return str(meta_get("bale_channel_enabled") or "1").lower() not in ("0","false","off","no")


def channel_time(kind):
    fallback="08:00" if kind=="morning" else "21:00"
    value=meta_get(f"bale_channel_time_{kind}") or fallback
    return value if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",value) else fallback


def channel_keyboard():
    return {"inline_keyboard":[
        [{"text":"▶️ فعال‌سازی خودکار","callback_data":"adm:chresume"},{"text":"⏸ توقف خودکار","callback_data":"adm:chpause"}],
        [{"text":"⏰ ساعت پست صبح","callback_data":"adm:chtime:morning"},{"text":"⏰ ساعت پست شب","callback_data":"adm:chtime:night"}],
        [{"text":"➕ ویرایش پیام صبح","callback_data":"adm:chadd:morning"},{"text":"➕ ویرایش پیام شب","callback_data":"adm:chadd:night"}],
        [{"text":"📤 ارسال پست صبح","callback_data":"adm:chpost:morning"},{"text":"📤 ارسال پست شب","callback_data":"adm:chpost:night"}],
        [{"text":"🧪 تست دسترسی کانال","callback_data":"adm:chtest"},{"text":"📋 مشاهده پیام‌ها","callback_data":"adm:chlist"}],
        [{"text":"🧹 حذف آخرین پست‌های ربات","callback_data":"adm:chclear"}],
        [{"text":"🔄 بروزرسانی","callback_data":"adm:channel"},{"text":"🔙 پنل اصلی","callback_data":"adm:home"}],
    ]}


async def show_admin_channel(api,chat_id,mid=None):
    channel=BALE_CHANNEL_ID or "تنظیم نشده (متغیر BALE_CHANNEL_ID)"
    try: enabled=channel_enabled()
    except Exception: enabled=False
    text=("📢 <b>مدیریت کانال بله</b>\n\n"
          f"شناسه کانال: <code>{escape(str(channel))}</code>\n"
          f"ارسال خودکار: {'فعال 🟢' if enabled else 'متوقف ⏸'}\n"
          f"☀️ ساعت صبح: {channel_time('morning')}\n🌙 ساعت شب: {channel_time('night')}\n\n"
          "برای ارسال، ربات باید در کانال دسترسی ارسال پیام داشته باشد. آب‌وهوا و قیمت‌های بازار در صورت دسترس بودن API به پست صبح افزوده می‌شوند.")
    if mid:
        try: await api.edit(chat_id,mid,text,channel_keyboard()); return
        except Exception: pass
    await api.send(chat_id,text,channel_keyboard())


async def channel_build_text(kind):
    now=datetime.now(IRAN_TZ)
    saved=meta_get(f"bale_channel_message_{kind}") or CHANNEL_DEFAULTS[kind]
    if kind=="night": return f"🌙 <b>شب بخیر</b>\n📅 {format_iran_jalali_fa(now,False)}\n\n{escape(saved)}"
    text=["☀️ <b>صبح بخیر!</b>",f"📅 {format_iran_jalali_fa(now,False)}","",escape(saved)]
    try:
        url=("https://api.open-meteo.com/v1/forecast?latitude="+BALE_WEATHER_LAT+"&longitude="+BALE_WEATHER_LON+
             "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Asia%2FTehran&forecast_days=1")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(url) as resp:
                if resp.status==200:
                    data=await resp.json(); cur=data.get("current",{}); daily=data.get("daily",{})
                    if cur.get("temperature_2m") is not None:
                        text += ["",f"🌤 <b>{escape(BALE_WEATHER_CITY)}</b>: {round(cur['temperature_2m'])}°؛ کمینه {round((daily.get('temperature_2m_min') or [0])[0])}° و بیشینه {round((daily.get('temperature_2m_max') or [0])[0])}°"]
    except Exception: log.warning("Bale channel weather block unavailable",exc_info=True)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(BALE_MARKET_URL) as resp:
                if resp.status==200:
                    payload=await resp.json(content_type=None); items={i.get("symbol"):i for i in payload.get("data",[])}; prices=[]
                    for symbol,label in (("USD_IRR_FREE","💵 دلار"),("GOLD_18K_IRR","🥇 طلای ۱۸ عیار")):
                        item=items.get(symbol)
                        if item and item.get("price") and not item.get("stale"):
                            prices.append(f"{label}: {int(item['price']):,} تومان")
                    if prices: text += [""]+prices
    except Exception: log.warning("Bale channel market block unavailable",exc_info=True)
    return "\n".join(text)


async def channel_post(api,kind,chat_id=None):
    if kind not in ("morning","night"): return False,"نوع پست نامعتبر است."
    if not BALE_CHANNEL_ID: return False,"متغیر BALE_CHANNEL_ID تنظیم نشده است."
    text=await channel_build_text(kind)
    try:
        result=await api.call("sendMessage",{"chat_id":BALE_CHANNEL_ID,"text":text,"parse_mode":"HTML","disable_notification":True})
        mid=(result or {}).get("message_id") if isinstance(result,dict) else None
        now=datetime.now(IRAN_TZ).strftime("%Y-%m-%d %H:%M")
        meta_set(f"bale_channel_last_{kind}",now)
        if mid: meta_set(f"bale_channel_post_id_{kind}",mid)
        return True,f"پست {'صبح' if kind=='morning' else 'شب'} با موفقیت ارسال شد."
    except Exception as e:
        log.exception("Bale channel post failed")
        return False,f"ارسال پست انجام نشد: {str(e)[:250]}"


async def channel_scheduler(api):
    last_minute=None
    while True:
        try:
            now=datetime.now(IRAN_TZ); minute=now.strftime("%Y-%m-%d %H:%M")
            if minute!=last_minute and channel_enabled() and BALE_CHANNEL_ID:
                for kind in ("morning","night"):
                    if now.strftime("%H:%M")==channel_time(kind):
                        marker=f"bale_channel_sent_date_{kind}"
                        if meta_get(marker)!=now.strftime("%Y-%m-%d"):
                            ok,note=await channel_post(api,kind)
                            if ok: meta_set(marker,now.strftime("%Y-%m-%d"))
                            else: log.error("Scheduled Bale channel post failed: %s",note)
                last_minute=minute
        except asyncio.CancelledError: raise
        except Exception: log.exception("Bale channel scheduler iteration failed")
        await asyncio.sleep(20)


async def on_message(api, msg):
    chat_id = (msg.get("chat") or {}).get("id")
    user = msg.get("from") or {}
    uid = user.get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or uid is None:
        return

    restore_checkout(uid)
    # مدیر منوی مدیریتی جدا دارد؛ جریان‌های ورودی مدیریتی قبل از پیام راهنما بررسی می‌شوند.
    if is_admin(uid):
        if text.startswith("/start") or text in ("/admin", "⚙️ پنل مدیریت", "پنل مدیریت"):
            await show_admin_home(api, chat_id)
            return
        if text in ("/cancel", "✕ بستن پنل"):
            CHECKOUT.pop(uid, None)
            await api.send(chat_id, "عملیات جاری لغو شد. برای بازکردن پنل /admin را بفرست.", admin_keyboard())
            return
        if text and await handle_admin_text(api, msg, uid, chat_id, text):
            return
        clean_admin_text = re.sub(r"\s+\(\d+\)$", "", text)
        if clean_admin_text in ("📦 سفارش‌ها", "💳 پرداخت‌های در انتظار", "🛠 مدیریت محصولات", "🎫 پشتیبانی", "👥 مشتری‌ها", "📊 فروش و آمار", "💳 تنظیمات پرداخت", "📢 مدیریت کانال"):
            section_map={"📦 سفارش‌ها":"orders","💳 پرداخت‌های در انتظار":"pending","🛠 مدیریت محصولات":"products","🎫 پشتیبانی":"tickets","👥 مشتری‌ها":"customers","📊 فروش و آمار":"sales","💳 تنظیمات پرداخت":"payment","📢 مدیریت کانال":"channel"}
            section=section_map[clean_admin_text]
            if section=="products": await show_admin_products(api,chat_id)
            elif section=="payment": await show_admin_banks(api,chat_id)
            elif section=="tickets": await show_support_tickets(api,chat_id,uid,None,True)
            elif section=="channel": await show_admin_channel(api,chat_id)
            else: await show_admin_section(api,chat_id,section)
            return
        if msg.get("photo") or msg.get("document"):
            await api.send(chat_id, "برای بررسی رسیدها از پنل مدیریت استفاده کن.", admin_keyboard())
            return
        await api.send(chat_id, "برای مدیریت فروشگاه از دکمه‌های پنل استفاده کن یا /admin را بفرست.", admin_keyboard())
        return

    if text.startswith("/start"):
        CHECKOUT.pop(uid, None)
        try: await asyncio.to_thread(db_register_customer, uid, user.get("username", ""))
        except Exception: log.exception("Could not register Bale customer on /start")
        await api.send(chat_id, "🌸 به «تولیدی تنین ایران» خوش آمدی.\n\nاز کاتالوگ محصولات دیدن کن، سبد خریدت را مدیریت کن، سفارش‌هایت را پیگیری کن یا از مرکز پشتیبانی کمک بگیر.", main_keyboard(uid))
    elif text == "/cancel":
        CHECKOUT.pop(uid, None)
        await api.send(chat_id, "عملیات جاری لغو شد.", main_keyboard())
    elif text in ("/admin", "⚙️ پنل مدیریت", "پنل مدیریت"):
        await api.send(chat_id, "دسترسی مدیر لازم است.", main_keyboard())
    elif text.startswith("/") and text not in ("/catalog", "/cart", "/checkout", "/orders", "/support"):
        await api.send(chat_id, "از دکمه‌های منو استفاده کن.", main_keyboard())
    elif await handle_checkout_text(api, msg, uid, chat_id, text):
        return
    elif await handle_customer_support_text(api, msg, uid, chat_id, text):
        return
    elif msg.get("photo") or (msg.get("document") and str((msg.get("document") or {}).get("mime_type", "")).startswith("image/")):
        await handle_receipt(api, msg, uid, chat_id)
    elif text in ("/catalog", "🧾 کاتالوگ محصولات", "کاتالوگ محصولات"):
        await show_catalog(api, chat_id)
    elif text in ("/cart", "🛒 سبد خرید", "سبد خرید"):
        await show_cart(api, chat_id, uid)
    elif text in ("/checkout", "🛍️ ادامه ثبت سفارش", "🛍 ادامه ثبت سفارش"):
        await start_checkout(api, chat_id, uid)
    elif text in ("📦 سفارش‌های من", "/orders"):
        await show_my_orders(api, chat_id, uid)
    elif text in ("💬 مرکز پشتیبانی", "/support"):
        await api.send(chat_id,"💬 <b>مرکز پشتیبانی تنین ایران</b>\n\nبرای پیگیری سفارش یا ثبت درخواست، یکی از گزینه‌ها را انتخاب کن:",support_home_keyboard())
    elif text in ("مرد", "زن") and uid in CHECKOUT and CHECKOUT[uid].get("step") == "gender":
        await handle_checkout_text(api, msg, uid, chat_id, text)


async def on_callback(api, cb):
    cbid = cb.get("id")
    data = cb.get("data", "")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    uid = (cb.get("from") or {}).get("id")
    if chat_id is None or uid is None:
        await api.answer(cbid)
        return
    try:
        restore_checkout(uid)
        # پنل مدیریت فقط برای شناسه‌های موجود در BALE_ADMIN_IDS مجاز است.
        if data.startswith("adm:"):
            if not is_admin(uid):
                await api.answer(cbid, "دسترسی مدیر لازم است.", True)
                return
            action = data.split(":")
            section = action[1] if len(action) > 1 else "home"
            await api.answer(cbid)
            if section == "home":
                await show_admin_home(api, chat_id)
            elif section == "close":
                if mid:
                    try: await api.call("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": "پنل مدیریت بسته شد."})
                    except Exception: await api.send(chat_id, "پنل مدیریت بسته شد.")
            elif section in ("pending", "orders", "products", "customers", "sales", "tickets", "channel", "payment"):
                if section == "products": await show_admin_products(api, chat_id, mid)
                elif section == "payment": await show_admin_banks(api, chat_id, mid)
                elif section == "tickets": await show_support_tickets(api, chat_id, uid, mid, True, action[2] if len(action)>2 else "all")
                elif section == "channel": await show_admin_channel(api,chat_id,mid)
                elif section == "customers": await show_admin_section(api, chat_id, section)
                else: await show_admin_section(api, chat_id, section)
            elif section == "pendingdetail" and len(action) == 3:
                await show_admin_pending_detail(api, chat_id, int(action[2]))
            elif section == "receipt" and len(action) == 3:
                with get_conn() as conn:
                    rec = conn.execute("SELECT receipt_file_id FROM pending_payments WHERE id=%s", (int(action[2]),)).fetchone()
                if rec and rec["receipt_file_id"]:
                    await api.call("sendPhoto", {"chat_id": chat_id, "photo": rec["receipt_file_id"], "caption": f"رسید پرداخت #{action[2]}"})
                else:
                    await api.send(chat_id, "رسید برای این پرداخت ثبت نشده است.", admin_back("pending"))
            elif section == "producttoggle" and len(action) == 3:
                pid = int(action[2])
                with get_conn() as conn:
                    p = conn.execute("SELECT active,name FROM products WHERE id=%s", (pid,)).fetchone()
                    if p:
                        conn.execute("UPDATE products SET active=%s WHERE id=%s", (not bool(p["active"]), pid))
                        conn.commit()
                await api.send(chat_id, f"وضعیت محصول #{pid} تغییر کرد." if p else "محصول پیدا نشد.", admin_back("products"))
            elif section == "orderdetail" and len(action) == 3:
                with get_conn() as conn:
                    o = conn.execute("SELECT * FROM orders WHERE id=%s", (int(action[2]),)).fetchone()
                if not o:
                    await api.send(chat_id, "سفارش پیدا نشد.", admin_back("orders"))
                else:
                    text = (f"📦 <b>سفارش #{o['id']}</b>\n"
                            f"مشتری: {escape(str(o['full_name'] or '-'))}\n"
                            f"تلفن: {escape(str(o['phone'] or '-'))}\n"
                            f"آدرس: {escape(str(o['address'] or '-'))}\n"
                            f"محصولات: {escape(str(o['items_summary'] or '-'))}\n"
                            f"مبلغ: {int(o['total_price'] or 0):,} تومان\n"
                            f"وضعیت: {escape(str(o['status'] or '-'))}")
                    await api.send(chat_id, text, admin_order_buttons(o["id"], o["status"]))
            elif section == "orderstatus" and len(action) == 4:
                oid, new_status = int(action[2]), action[3]
                if new_status not in ("confirmed", "cancelled"):
                    await api.send(chat_id, "وضعیت نامعتبر است.", admin_back("orders"))
                else:
                    status_text = "تأیید شده" if new_status == "confirmed" else "لغو شده"
                    with get_conn() as conn:
                        changed = conn.execute("UPDATE orders SET status=%s WHERE id=%s AND status='در انتظار بررسی' RETURNING user_id,total_price", (status_text, oid)).fetchone()
                        conn.commit()
                    await api.send(chat_id, f"وضعیت سفارش #{oid} به «{status_text}» تغییر کرد.", admin_back("orders"))
                    if changed:
                        customer_text = (f"✅ وضعیت سفارش #{oid} تغییر کرد: {status_text}" if new_status == "confirmed" else f"❌ سفارش #{oid} لغو شد. برای پیگیری با پشتیبانی تماس بگیر.")
                        try: await api.send(int(changed["user_id"]), customer_text, main_keyboard(int(changed["user_id"])))
                        except Exception: log.exception("Could not notify customer for order status change %s", oid)
            elif section == "add_product":
                CHECKOUT[uid] = {"admin_flow":{"type":"add_product","step":"name"}}
                await api.send(chat_id,"➕ افزودن محصول\nنام محصول را وارد کن:")
            elif section == "product" and len(action)==3:
                await show_admin_product(api,chat_id,int(action[2]),mid)
            elif section == "product_field" and len(action)==4:
                pid=int(action[2]); field=action[3]
                if field not in {"name","category","size","color","pack_info","price","photo_url"}: await api.send(chat_id,"فیلد نامعتبر است."); return
                CHECKOUT[uid]={"admin_flow":{"type":"product_field","product_id":pid,"field":field}}
                labels={"name":"نام جدید را وارد کن:","category":"دسته جدید را وارد کن: بچگانه یا زنانه","size":"سایز جدید را وارد کن:","color":"رنگ‌بندی جدید را وارد کن:","pack_info":"اطلاعات پک جدید را وارد کن (یا ندارد):","price":"قیمت جدید را به تومان وارد کن:","photo_url":"لینک عکس جدید را وارد کن (یا ندارد):"}
                await api.send(chat_id,labels[field])
            elif section == "price_adjust" and len(action)==4:
                pid=int(action[2]); delta=int(action[3])
                with get_conn() as conn:
                    p=conn.execute("SELECT price FROM products WHERE id=%s",(pid,)).fetchone()
                    if p:
                        new_price=max(0,int(p["price"] or 0)+delta)
                        conn.execute("UPDATE products SET price=%s WHERE id=%s",(new_price,pid)); conn.commit()
                await api.send(chat_id,(f"قیمت محصول #{pid} به {new_price:,} تومان تغییر کرد." if p else "محصول پیدا نشد."),admin_back("products"))
            elif section == "price_set" and len(action)==3:
                pid=int(action[2]); CHECKOUT[uid]={"admin_flow":{"type":"product_field","product_id":pid,"field":"price"}}
                await api.send(chat_id,"قیمت جدید را به تومان وارد کن:")
            elif section == "toggle_product" and len(action)==3:
                pid=int(action[2])
                with get_conn() as conn:
                    product=conn.execute("SELECT active FROM products WHERE id=%s",(pid,)).fetchone()
                    if product: conn.execute("UPDATE products SET active=%s WHERE id=%s",(not bool(product["active"]),pid)); conn.commit()
                await show_admin_product(api,chat_id,pid)
            elif section == "delete_product" and len(action)==3:
                pid=int(action[2])
                # Keep historical order lines intact: deletion is a safe soft-disable.
                with get_conn() as conn: conn.execute("UPDATE products SET active=FALSE WHERE id=%s",(pid,)); conn.commit()
                await api.send(chat_id,"محصول غیرفعال شد؛ سابقه سفارش‌ها حفظ شده است.",admin_back("products"))
            elif section == "bank_add":
                CHECKOUT[uid]={"admin_flow":{"type":"bank_add","index":0,"data":{}}}
                await api.send(chat_id,"➕ افزودن حساب بانکی\nنام بانک را وارد کن:")
            elif section == "bank_edit" and len(action)==3:
                aid=int(action[2])
                with get_conn() as conn: bank=conn.execute("SELECT * FROM bank_accounts WHERE id=%s",(aid,)).fetchone()
                if not bank: await api.send(chat_id,"حساب پیدا نشد."); return
                CHECKOUT[uid]={"admin_flow":{"type":"bank_edit","account_id":aid,"index":0,"data":{}}}
                await api.send(chat_id,f"ویرایش حساب #{aid} — نام بانک جدید را وارد کن:")
            elif section == "bank" and len(action)==3: await show_admin_bank(api,chat_id,int(action[2]),mid)
            elif section == "bank_toggle" and len(action)==3:
                aid=int(action[2]); now=iran_now_text()
                with get_conn() as conn:
                    bank=conn.execute("SELECT active FROM bank_accounts WHERE id=%s",(aid,)).fetchone()
                    if bank and not bank["active"]:
                        conn.execute("UPDATE bank_accounts SET active=FALSE,updated_at=%s",(now,))
                        conn.execute("UPDATE bank_accounts SET active=TRUE,updated_at=%s WHERE id=%s",(now,aid)); conn.commit(); note="حساب فعال شد و حساب‌های دیگر غیرفعال شدند."
                    elif bank:
                        other=conn.execute("SELECT id FROM bank_accounts WHERE id<>%s AND active=TRUE LIMIT 1",(aid,)).fetchone()
                        if other: conn.execute("UPDATE bank_accounts SET active=FALSE,updated_at=%s WHERE id=%s",(now,aid)); conn.commit(); note="حساب غیرفعال شد."
                        else: note="این تنها حساب فعال است؛ ابتدا حساب دیگری اضافه و فعال کن."
                    else: note="حساب پیدا نشد."
                await api.send(chat_id,note,admin_back("payment"))
            elif section == "bank_delete" and len(action)==3:
                aid=int(action[2]); now=iran_now_text()
                with get_conn() as conn:
                    bank=conn.execute("SELECT active FROM bank_accounts WHERE id=%s",(aid,)).fetchone()
                    if not bank: note="حساب پیدا نشد."
                    elif bank["active"]: note="حساب فعال را نمی‌توان حذف کرد؛ ابتدا حساب دیگری را فعال کن."
                    else: conn.execute("DELETE FROM bank_accounts WHERE id=%s",(aid,)); conn.commit(); note="حساب غیرفعال حذف شد."
                await api.send(chat_id,note,admin_back("payment"))
            elif section == "chpause":
                meta_set("bale_channel_enabled","0"); await show_admin_channel(api,chat_id)
            elif section == "chresume":
                meta_set("bale_channel_enabled","1"); await show_admin_channel(api,chat_id)
            elif section == "chtime" and len(action)==3 and action[2] in ("morning","night"):
                CHECKOUT[uid]={"admin_flow":{"type":"channel_time","kind":action[2]}}
                await api.send(chat_id,f"ساعت پست {'صبح' if action[2]=='morning' else 'شب'} را به شکل HH:MM وارد کن (مثال 08:15):")
            elif section == "chadd" and len(action)==3 and action[2] in ("morning","night"):
                CHECKOUT[uid]={"admin_flow":{"type":"channel_message","kind":action[2]}}
                await api.send(chat_id,"متن جدید پست را ارسال کن:")
            elif section == "chpost" and len(action)==3 and action[2] in ("morning","night"):
                ok,note=await channel_post(api,action[2]); await api.send(chat_id,("✅ " if ok else "❌ ")+note,admin_back("channel"))
            elif section == "chtest":
                if not BALE_CHANNEL_ID: await api.send(chat_id,"❌ متغیر BALE_CHANNEL_ID تنظیم نشده است.",admin_back("channel"))
                else:
                    try:
                        await api.call("sendMessage",{"chat_id":BALE_CHANNEL_ID,"text":"🧪 تست دسترسی ربات تنین ایران به کانال بله","disable_notification":True})
                        await api.send(chat_id,"✅ پیام آزمایشی ارسال شد. دسترسی ارسال پیام تأیید شد.",admin_back("channel"))
                    except Exception as e: await api.send(chat_id,f"❌ ارسال آزمایشی ناموفق بود: {escape(str(e)[:250])}",admin_back("channel"))
            elif section == "chclear":
                deleted=0; errors=0
                for kind in ("morning","night"):
                    key=f"bale_channel_post_id_{kind}"; saved_id=meta_get(key)
                    if saved_id and BALE_CHANNEL_ID:
                        try:
                            await api.call("deleteMessage",{"chat_id":BALE_CHANNEL_ID,"message_id":int(saved_id)})
                            deleted+=1
                        except Exception: errors+=1
                        try:
                            with get_conn() as conn: conn.execute("DELETE FROM app_meta WHERE key=%s",(key,)); conn.commit()
                        except Exception: log.exception("Could not clear channel post marker")
                await api.send(chat_id,f"🧹 حذف آخرین پست‌ها تمام شد. حذف‌شده: {deleted}، خطا: {errors}.",admin_back("channel"))
            elif section == "chlist":
                morning=meta_get("bale_channel_message_morning") or CHANNEL_DEFAULTS["morning"]
                night=meta_get("bale_channel_message_night") or CHANNEL_DEFAULTS["night"]
                await api.send(chat_id,"📋 <b>متن‌های کانال</b>\n\n☀️ <b>صبح:</b>\n"+escape(morning)+"\n\n🌙 <b>شب:</b>\n"+escape(night),admin_back("channel"))
            elif section == "ticket" and len(action)==3: await show_ticket_detail(api,chat_id,uid,int(action[2]),mid,True)
            elif section == "ticket_reply" and len(action)==3:
                tid=int(action[2])
                CHECKOUT[uid]={"admin_flow":{"type":"ticket_reply","ticket_id":tid}}
                await api.send(chat_id,f"💬 پاسخ خود را برای تیکت #{tid} بنویس:")
            elif section == "ticket_close" and len(action)==3:
                tid=int(action[2]); now=iran_now_text()
                with get_conn() as conn:
                    t=conn.execute("SELECT user_id FROM support_tickets WHERE id=%s AND status<>'closed'",(tid,)).fetchone()
                    if t: conn.execute("UPDATE support_tickets SET status='closed',closed_at=%s,updated_at=%s WHERE id=%s",(now,now,tid)); conn.commit()
                if t:
                    try: await api.send(int(t["user_id"]),f"🎫 تیکت #{tid} توسط پشتیبانی بسته شد.",main_keyboard(int(t["user_id"])))
                    except Exception: log.exception("Could not notify ticket closure")
                await api.send(chat_id,f"تیکت #{tid} بسته شد.",admin_back("tickets"))
            elif section == "customer" and len(action)==3: await show_admin_customer(api,chat_id,int(action[2]),mid)
            elif section == "sms_toggle" and len(action)==3:
                customer_id=int(action[2])
                with get_conn() as conn:
                    conn.execute("BEGIN")
                    _ensure_loyalty_profile_for_platform(conn,"bale",customer_id)
                    pref=conn.execute(
                        """SELECT scp.transactional_sms_enabled FROM customer_platform_accounts cpa
                           JOIN sms_customer_preferences scp ON scp.loyalty_customer_id=cpa.loyalty_customer_id
                           WHERE cpa.platform='bale' AND cpa.platform_user_id=%s FOR UPDATE OF scp""",
                        (str(customer_id),)
                    ).fetchone()
                    new_value = not bool(pref["transactional_sms_enabled"]) if pref else False
                    conn.execute(
                        """UPDATE sms_customer_preferences SET transactional_sms_enabled=%s, updated_by=%s, updated_at=NOW()
                           WHERE loyalty_customer_id=(SELECT loyalty_customer_id FROM customer_platform_accounts
                             WHERE platform='bale' AND platform_user_id=%s)""",
                        (new_value,str(uid),str(customer_id))
                    )
                    conn.commit()
                await show_admin_customer(api,chat_id,customer_id)
            else:
                await api.send(chat_id, "این عملیات مدیریتی هنوز پیاده‌سازی نشده است.", admin_keyboard())
            return

        # مدیر نباید بتواند از callbackهای مشتری برای ساخت سبد یا سفارش استفاده کند.
        if is_admin(uid) and data.startswith(("cat:", "add:", "qty:", "remove:", "checkout:", "checkoutqty:", "saved:", "customer_order:", "support:", "nameconfirm:", "checkoutgender:", "pay:receipt:", "pay:card:")):
            await api.answer(cbid, "این حساب فقط برای مدیریت فروشگاه است.", True)
            return

        if data.startswith("checkoutqty:"):
            await api.answer(cbid)
            parts=data.split(":")
            action=parts[1]
            if action in ("plus","minus") and len(parts)==3:
                cid=int(parts[2])
                with get_conn() as conn:
                    row=conn.execute("SELECT ci.id,ci.qty FROM cart_items ci WHERE ci.id=%s AND ci.user_id=%s",(cid,uid)).fetchone()
                    if row:
                        qty=int(row["qty"])+(1 if action=="plus" else -1)
                        if qty<=0: conn.execute("DELETE FROM cart_items WHERE id=%s AND user_id=%s",(cid,uid))
                        else: conn.execute("UPDATE cart_items SET qty=%s WHERE id=%s AND user_id=%s",(qty,cid,uid))
                        conn.commit()
                await show_checkout_quantities(api,chat_id,uid,mid); return
            if action=="noop": return
            if action=="back": await show_cart(api,chat_id,uid); return
            if action=="delete_menu":
                rows=await asyncio.to_thread(db_cart,uid)
                buttons=[[{"text":f"🗑 {str(r['name'])[:28]}","callback_data":f"checkoutqty:delete:{r['cart_id']}"}] for r in rows]
                buttons.append([{"text":"🔙 بازگشت","callback_data":"checkoutqty:refresh"}])
                await api.send(chat_id,"کدام محصول از سبد حذف شود؟",{"inline_keyboard":buttons}); return
            if action=="delete" and len(parts)==3:
                await asyncio.to_thread(db_remove_cart,uid,int(parts[2])); await show_checkout_quantities(api,chat_id,uid); return
            if action=="refresh": await show_checkout_quantities(api,chat_id,uid); return
            if action=="continue":
                state=CHECKOUT.get(uid) or PersistentState(uid,{})
                state["step"]="name"
                saved=await asyncio.to_thread(db_saved_data,uid,"name")
                if saved:
                    await api.send(chat_id,"👤 نام و نام خانوادگی را انتخاب کن یا نام جدید وارد کن:",await asyncio.to_thread(saved_keyboard,uid,"name","name"))
                else: await api.send(chat_id,"👤 نام و نام خانوادگی را وارد کن:")
                return
        if data.startswith("nameconfirm:"):
            await api.answer(cbid)
            state=restore_checkout(uid)
            if not state or state.get("step")!="confirm_name":
                await api.send(chat_id,"این مرحله منقضی شده است؛ دوباره ثبت سفارش را شروع کن.",main_keyboard(uid)); return
            action=data.split(":",1)[1]
            if action=="edit":
                state["step"]="name"; await api.send(chat_id,"نام و نام خانوادگی صحیح را وارد کن:"); return
            state["step"]="phone"
            saved=await asyncio.to_thread(db_saved_data,uid,"phone")
            if saved: await api.send(chat_id,"📱 شماره موبایل را انتخاب کن یا شماره جدید وارد کن:",await asyncio.to_thread(saved_keyboard,uid,"phone","phone"))
            else: await api.send(chat_id,"📱 شماره موبایل ایرانی را وارد کن (مثال: 09123456789).")
            return
        if data.startswith("checkoutgender:"):
            gender=data.split(":",1)[1]
            if gender not in ("مرد","زن"):
                await api.answer(cbid,"گزینه نامعتبر است.",True); return
            state=restore_checkout(uid)
            if not state or state.get("step")!="gender":
                await api.answer(cbid,"این مرحله منقضی شده است؛ دوباره ثبت سفارش را شروع کن.",True); return
            state["gender"]=gender; state["step"]="address"
            await asyncio.to_thread(set_known_gender,uid,state.get("full_name",""),gender)
            await api.answer(cbid,"ثبت شد.")
            await ask_bale_address(api,chat_id,uid); return
        if data.startswith("customer_order:"):
            parts=data.split(":")
            action=parts[1] if len(parts)>1 else "list"
            if action=="close":
                await api.answer(cbid)
                try: await api.call("deleteMessage",{"chat_id":chat_id,"message_id":mid})
                except Exception: await api.edit(chat_id,mid,"منوی سفارش‌ها بسته شد.")
                return
            if action=="list": await api.answer(cbid); await show_my_orders(api,chat_id,uid,mid); return
            if action=="view" and len(parts)==3:
                oid=int(parts[2])
                with get_conn() as conn: order=conn.execute("SELECT * FROM orders WHERE id=%s AND user_id=%s",(oid,uid)).fetchone()
                if not order: await api.answer(cbid,"این سفارش برای حساب شما پیدا نشد.",True); return
                finalized=order.get("finalized_at") or order.get("created_at")
                text=(f"📦 <b>جزئیات سفارش #{oid}</b>\n━━━━━━━━━━━━━━\n"
                      f"📅 تاریخ: {format_iran_jalali_fa(order.get('created_at'),True)}\n"
                      f"✅ نهایی‌شدن: {format_iran_jalali_fa(finalized,True)}\n"
                      f"📌 وضعیت: {escape(str(order.get('status') or '-'))}\n"
                      f"💳 پرداخت: {escape(str(order.get('payment_status') or '-'))}\n\n"
                      f"👤 نام: {escape(str(order.get('full_name') or '-'))}\n📞 تلفن: {escape(str(order.get('phone') or '-'))}\n"
                      f"📍 آدرس: {escape(str(order.get('address') or '-'))}\n\n🛍 محصولات:\n{escape(str(order.get('items_summary') or '-'))}\n\n"
                      f"💰 مبلغ: <b>{int(order.get('total_price') or 0):,} تومان</b>")
                await api.answer(cbid)
                try: await api.edit(chat_id,mid,text,{"inline_keyboard":[[{"text":"🔙 بازگشت به سفارش‌ها","callback_data":"customer_order:list"}]]})
                except Exception: await api.send(chat_id,text,{"inline_keyboard":[[{"text":"🔙 بازگشت به سفارش‌ها","callback_data":"customer_order:list"}]]})
                return
        if data.startswith("support:"):
            parts=data.split(":",2); action=parts[1] if len(parts)>1 else "home"; extra=parts[2] if len(parts)>2 else ""
            await api.answer(cbid)
            if action=="home": await api.send(chat_id,"💬 <b>مرکز پشتیبانی</b>\n\nیک گزینه را انتخاب کن:",support_home_keyboard()); return
            if action=="orders":
                with get_conn() as conn: rows=conn.execute("SELECT id,created_at,status FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT 20",(uid,)).fetchall()
                buttons=[[{"text":f"📦 سفارش #{o['id']} · {format_iran_jalali_fa(o.get('created_at'),False)} · {str(o['status'])[:12]}","callback_data":f"customer_order:view:{o['id']}"}] for o in rows]
                buttons.append([{"text":"🔙 مرکز پشتیبانی","callback_data":"support:home"}])
                await api.send(chat_id,"📦 سفارش موردنظر را برای مشاهده جزئیات انتخاب کن:",{"inline_keyboard":buttons}); return
            if action=="faq":
                await api.send(chat_id,"❓ <b>سؤالات متداول</b>\n\n🛒 ثبت سفارش: محصول را انتخاب و مراحل ثبت سفارش را کامل کن.\n\n💳 پرداخت: کارت‌به‌کارت انجام بده و تصویر رسید را ارسال کن؛ سفارش پس از بررسی نهایی می‌شود.\n\n🚚 ارسال: زمان ارسال پس از ثبت سفارش هماهنگ می‌شود.\n\n🔄 تعویض و مرجوعی: درخواست را از طریق تیکت ثبت کن.",{"inline_keyboard":[[{"text":"🔙 مرکز پشتیبانی","callback_data":"support:home"}]]}); return
            if action=="phone": await api.send(chat_id,"📞 شماره تماس پشتیبانی: 09384853486",{"inline_keyboard":[[{"text":"🔙 مرکز پشتیبانی","callback_data":"support:home"}]]}); return
            if action=="mine": await show_support_tickets(api,chat_id,uid,mid,False); return
            if action=="new":
                CHECKOUT[uid]={"step":"support_new_message","support_topic":"سایر"}
                await api.send(chat_id,"📝 درخواستت را در یک پیام بنویس. برای لغو /cancel بفرست.",{"inline_keyboard":[[{"text":"💳 پرداخت","callback_data":"support:topic:پرداخت"},{"text":"🛍 محصول","callback_data":"support:topic:محصول"}], [{"text":"🚚 ارسال","callback_data":"support:topic:ارسال"},{"text":"🔄 تعویض","callback_data":"support:topic:تعویض"}], [{"text":"❓ سایر","callback_data":"support:topic:سایر"}]]}); return
            if action=="topic":
                state=restore_checkout(uid)
                state["step"]="support_new_message"; state["support_topic"]=extra or "سایر"
                await api.send(chat_id,f"🏷 موضوع: <b>{escape(state['support_topic'])}</b>\n\nحالا متن درخواستت را بفرست:"); return
            if action=="reply":
                try: tid=int(extra)
                except ValueError: await api.send(chat_id,"شناسه تیکت نامعتبر است."); return
                with get_conn() as conn: ticket=conn.execute("SELECT id FROM support_tickets WHERE id=%s AND user_id=%s AND status!='closed'",(tid,uid)).fetchone()
                if not ticket: await api.send(chat_id,"تیکت پیدا نشد یا بسته شده است."); return
                CHECKOUT[uid]={"step":"support_reply","ticket_id":tid}
                await api.send(chat_id,f"💬 پیام جدیدت برای تیکت #{tid} را بفرست:"); return
            if action=="view":
                try: tid=int(extra)
                except ValueError: await api.send(chat_id,"شناسه تیکت نامعتبر است."); return
                await show_ticket_detail(api,chat_id,uid,tid,mid,False); return
        if data.startswith("cat:"):
            await api.answer(cbid)
            category = data.split(":", 1)[1]
            if category not in ("بچگانه", "زنانه"):
                await api.send(chat_id, "دسته‌بندی نامعتبر است.")
            else:
                await show_category(api, chat_id, category)
        elif data.startswith("add:"):
            pid = int(data.split(":", 1)[1])
            product = await asyncio.to_thread(db_product, pid)
            if not product:
                await api.answer(cbid, "این محصول دیگر موجود نیست.", True)
                return
            await api.answer(cbid)
            prompt = f"👕 <b>{escape(str(product['name']))}</b>\n"
            if product["pack_info"]:
                prompt += escape(str(product["pack_info"])) + "\n"
            await api.send(chat_id, prompt + "\nتعداد بسته را انتخاب کن:", quantity_keyboard(pid, 1))
        elif data.startswith("qty:"):
            _, action, pid_s, qty_s = data.split(":")
            pid, qty = int(pid_s), max(1, int(qty_s))
            if action == "plus": qty += 1
            elif action == "minus": qty = max(1, qty - 1)
            elif action == "cancel":
                await api.answer(cbid, "لغو شد.")
                try: await api.call("deleteMessage", {"chat_id": chat_id, "message_id": mid})
                except Exception: pass
                return
            elif action == "confirm":
                product = await asyncio.to_thread(db_product, pid)
                if not product:
                    await api.answer(cbid, "این محصول دیگر موجود نیست.", True); return
                await asyncio.to_thread(db_add_cart, uid, pid, qty)
                await api.answer(cbid, "به سبد خرید اضافه شد.")
                await api.send(chat_id, f"✅ {qty} بسته از «{escape(str(product['name']))}» به سبد خرید اضافه شد.", main_keyboard())
                return
            elif action != "noop":
                await api.answer(cbid); return
            await api.answer(cbid)
            product = await asyncio.to_thread(db_product, pid)
            if product and mid:
                await api.edit(chat_id, mid, f"👕 <b>{escape(str(product['name']))}</b>\n\nتعداد بسته: <b>{qty}</b>", quantity_keyboard(pid, qty))
        elif data.startswith("remove:"):
            cid = int(data.split(":", 1)[1])
            await asyncio.to_thread(db_remove_cart, uid, cid)
            await api.answer(cbid, "از سبد حذف شد.")
            if mid:
                try: await api.edit(chat_id, mid, "🗑 این کالا از سبد خرید حذف شد.")
                except Exception: pass
            await show_cart(api, chat_id, uid)
        elif data == "checkout:start":
            await api.answer(cbid)
            await start_checkout(api, chat_id, uid)
        elif data.startswith("saved:"):
            parts = data.split(":")
            action = parts[1]
            if action == "use":
                _, _, step, row_id = parts
                data_type = step
                rows = await asyncio.to_thread(db_saved_data, uid, data_type)
                row = next((r for r in rows if int(r["id"]) == int(row_id)), None)
                if not row or uid not in CHECKOUT:
                    await api.answer(cbid, "اطلاعات پیدا نشد یا مرحله سفارش منقضی شده است.", True); return
                state = CHECKOUT[uid]
                value = str(row["value"])
                if step == "name":
                    state["full_name"] = normalize_name(value); state["step"] = "confirm_name"
                    await api.answer(cbid, "نام انتخاب شد.")
                    await api.send(chat_id, f"👤 نام ثبت سفارش: <b>{escape(state['full_name'])}</b>\n\nآیا این نام درست است؟", {"inline_keyboard":[[{"text":"✅ تأیید نام","callback_data":"nameconfirm:yes"},{"text":"✏️ ویرایش نام","callback_data":"nameconfirm:edit"}]]})
                elif step == "phone":
                    state["phone"] = value
                    known = await asyncio.to_thread(get_known_gender, uid, state.get("full_name", ""))
                    await api.answer(cbid, "شماره انتخاب شد.")
                    if known:
                        state["gender"] = known; state["step"] = "address"; await ask_bale_address(api, chat_id, uid)
                    else:
                        state["step"] = "gender"
                        await api.send(chat_id, "جنسیت را انتخاب کن:", {"inline_keyboard":[[{"text":"مرد","callback_data":"checkoutgender:مرد"},{"text":"زن","callback_data":"checkoutgender:زن"}]]})
                elif step == "address":
                    state["address"] = value
                    await api.answer(cbid, "آدرس انتخاب شد.")
                    try:
                        pending, bank = await asyncio.to_thread(db_create_pending, uid, state["full_name"], state["phone"], state["gender"], value, (msg.get("from") or {}).get("username", ""))
                    except Exception:
                        log.exception("Could not create pending payment for Bale user %s", uid)
                        await api.send(chat_id, "⚠️ ثبت سفارش انجام نشد؛ لطفاً کمی بعد دوباره تلاش کن.", main_keyboard()); CHECKOUT.pop(uid, None); return
                    CHECKOUT.pop(uid, None)
                    await send_order_sms_once(
                        pending_id=pending["id"],
                        full_name=pending.get("full_name", state["full_name"]),
                        phone=pending.get("phone", state["phone"]),
                        total_price=pending.get("total_price", 0),
                        items_summary=pending.get("items_summary", ""),
                        platform="bale",
                    )
                    await api.send(chat_id, "اطلاعات ذخیره‌شده استفاده شد.")
                    await send_payment_instructions(api, chat_id, pending, bank)
            elif action == "new":
                step = parts[2]
                if uid not in CHECKOUT:
                    await api.answer(cbid, "مرحله سفارش منقضی شده است. دوباره ثبت سفارش را بزن.", True); return
                CHECKOUT[uid]["step"] = step
                await api.answer(cbid)
                prompts = {"name":"👤 نام و نام خانوادگی را وارد کن:", "phone":"📱 شماره موبایل را وارد کن (مثال: 09123456789):", "address":"📍 آدرس کامل پستی، شهر و کدپستی را وارد کن:"}
                await api.send(chat_id, prompts.get(step, "اطلاعات جدید را وارد کن:"))
            elif action == "manage":
                data_type = parts[2]
                rows = await asyncio.to_thread(db_saved_data, uid, data_type)
                buttons=[]
                for row in rows[:8]:
                    label=str(row["value"]); label=label if len(label)<=25 else label[:22]+"..."
                    buttons.append([{"text":"✏️ "+label,"callback_data":f"saved:edit:{data_type}:{row['id']}"}, {"text":"🗑","callback_data":f"saved:delete:{data_type}:{row['id']}"}])
                buttons.append([{"text":"بازگشت","callback_data":f"saved:back:{data_type}"}])
                await api.answer(cbid)
                await api.send(chat_id, "برای ویرایش یا حذف، گزینه مربوط را انتخاب کن:", {"inline_keyboard":buttons})
            elif action == "delete":
                data_type, row_id = parts[2], int(parts[3])
                await asyncio.to_thread(db_delete_saved, uid, row_id, data_type)
                await api.answer(cbid, "اطلاعات حذف شد.")
                await api.send(chat_id, "اطلاعات ذخیره‌شده به‌روزرسانی شد.", main_keyboard())
            elif action == "edit":
                data_type, row_id = parts[2], int(parts[3])
                CHECKOUT[uid] = {"step": "edit_saved", "edit_type": data_type, "edit_id": row_id}
                await api.answer(cbid)
                labels={"name":"نام و نام خانوادگی جدید را وارد کن:","phone":"شماره موبایل جدید را وارد کن:","address":"آدرس جدید را وارد کن:"}
                await api.send(chat_id, labels.get(data_type,"مقدار جدید را وارد کن:"))
            elif action == "back":
                data_type=parts[2]
                await api.answer(cbid)
                await api.send(chat_id, "اطلاعات ذخیره‌شده:", await asyncio.to_thread(saved_keyboard, uid, data_type, data_type))
            else:
                await api.answer(cbid)
        elif data.startswith("pay:card:"):
            parts=data.split(":"); pid=int(parts[2]) if len(parts)>2 else 0
            with get_conn() as conn:
                pending=conn.execute("SELECT id,user_id,payment_status FROM pending_payments WHERE id=%s",(pid,)).fetchone()
                bank=conn.execute("SELECT card_number,bank_name,owner_name FROM bank_accounts WHERE active=TRUE ORDER BY id DESC LIMIT 1").fetchone()
            if not pending or int(pending["user_id"])!=int(uid):
                await api.answer(cbid,"این پرداخت برای حساب شما نیست.",True); return
            await api.answer(cbid)
            if bank and bank.get("card_number"):
                await api.send(chat_id,f"💳 شماره کارت برای کپی:\n<code>{escape(str(bank['card_number']))}</code>\nبانک: {escape(str(bank.get('bank_name') or '-'))}\nبه نام: {escape(str(bank.get('owner_name') or '-'))}\n\nبرای کپی، روی شماره کارت لمس طولانی کن.")
            else: await api.send(chat_id,"شماره کارت فعال تنظیم نشده است.")
        elif data.startswith("pay:receipt:"):
            await api.answer(cbid)
            await api.send(chat_id, "📤 لطفاً عکس رسید بانکی را به‌صورت عکس در همین گفتگو ارسال کن. بهتر است شماره پیگیری و مبلغ خوانا باشند.")
        elif data.startswith("payadmin:"):
            if uid not in ADMIN_IDS:
                await api.answer(cbid, "دسترسی مدیر لازم است.", True); return
            _, action, pid_s = data.split(":")
            pending, result = await asyncio.to_thread(db_review_payment, int(pid_s), uid, action == "approve")
            if not pending:
                await api.answer(cbid, "این پرداخت قبلاً بررسی شده یا پیدا نشد.", True); return
            await api.answer(cbid, "بررسی ثبت شد.")
            if action == "approve":
                result_parts = result.split(":")
                order_id = result_parts[1]
                cashback_amount = int(result_parts[2]) if len(result_parts) > 2 else 0
                await api.send(chat_id, f"✅ پرداخت #{pid_s} تأیید شد و سفارش #{order_id} ثبت شد.")
                cashback_line = f"\n🎁 کش‌بک این سفارش: {cashback_amount:,} تومان" if cashback_amount > 0 else ""
                customer_msg = f"✅ پرداخت شما تأیید شد و سفارش با موفقیت ثبت شد.\nشماره سفارش: #{order_id}{cashback_line}"
            else:
                await api.send(chat_id, f"❌ پرداخت #{pid_s} رد شد.")
                customer_msg = "❌ رسید پرداخت تأیید نشد. لطفاً با فروشگاه تماس بگیر یا رسید معتبر ارسال کن."
            try: await api.send(pending["user_id"], customer_msg, main_keyboard())
            except Exception: log.exception("Could not notify Bale customer after review")
            if mid:
                status_text = "✅ پرداخت تأیید شد و سفارش ثبت شد." if action == "approve" else "❌ پرداخت رد شد."
                original_caption = str(msg.get("caption") or msg.get("text") or "رسید پرداخت")
                try:
                    if msg.get("photo") or msg.get("document"):
                        await api.call("editMessageCaption", {
                            "chat_id": chat_id,
                            "message_id": mid,
                            "caption": f"{original_caption}\n\n<b>{status_text}</b>",
                            "parse_mode": "HTML",
                        })
                    else:
                        await api.edit(chat_id, mid, f"{escape(original_caption)}\n\n<b>{status_text}</b>")
                except Exception:
                    log.exception("Could not mark Bale admin receipt as reviewed")
        else:
            await api.answer(cbid)
    except Exception:
        log.exception("Callback failed: %r", data)
        await api.answer(cbid, "خطا رخ داد؛ دوباره تلاش کن.", True)


async def health(request):
    return web.json_response({"service": "tanin-bale-bot", "status": "ok"})


async def main():
    if not TOKEN:
        raise RuntimeError("متغیر BALE_BOT_TOKEN تنظیم نشده است.")
    if not DB_URL:
        raise RuntimeError("متغیر SUPABASE_DB_URL یا DATABASE_URL تنظیم نشده است.")
    def check_db():
        required = {"products", "customers", "customer_saved_data", "support_tickets", "support_ticket_messages", "cart_items", "bank_accounts", "pending_payments", "orders", "app_meta"}
        with get_conn() as conn:
            found = {r["table_name"] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()}
            missing = sorted(required - found)
            if missing:
                raise RuntimeError("جدول‌های لازم در Supabase پیدا نشدند: " + ", ".join(missing))
            for table, columns in {
                "products": {"id", "name", "category", "price", "active", "photo_url", "pack_info", "size", "color"},
                "cart_items": {"id", "user_id", "product_id", "qty"},
                "bank_accounts": {"id", "bank_name", "owner_name", "card_number", "account_number", "iban", "active", "created_at", "updated_at"},
                "pending_payments": {"id", "user_id", "full_name", "phone", "address", "items_summary", "total_price", "payment_status", "receipt_file_id", "created_at", "reviewed_at", "order_id", "admin_id"},
                "orders": {"id", "user_id", "full_name", "phone", "address", "items_summary", "total_price", "status", "created_at", "finalized_at", "payment_status", "transaction_ref"},
                "customers": {"user_id", "username", "full_name", "phone", "gender", "first_seen", "last_seen"},
                "customer_saved_data": {"id", "user_id", "data_type", "value", "created_at", "updated_at"},
                "support_tickets": {"id", "user_id", "order_id", "topic", "status", "created_at", "updated_at", "closed_at"},
                "support_ticket_messages": {"id", "ticket_id", "sender_id", "sender_type", "message", "created_at"},
                "app_meta": {"key", "value"},
            }.items():
                actual = {r["column_name"] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,)).fetchall()}
                miss = sorted(columns - actual)
                if miss:
                    raise RuntimeError(f"جدول {table} ستون‌های لازم را ندارد: {', '.join(miss)}")
    await asyncio.to_thread(check_db)
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        api = BaleAPI(session)
        try:
            await api.call("deleteWebhook", {"drop_pending_updates": False})
            log.info("Bale webhook cleared for polling.")
        except Exception:
            log.warning("Could not clear a previous Bale webhook; polling may conflict.", exc_info=True)
        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", "10000"))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        log.info("Health server listening on port %s", port)
        offset = None
        log.info("Bale shop bot started; checkout enabled.")
        channel_task = asyncio.create_task(channel_scheduler(api), name="bale-channel-scheduler")
        try:
            while True:
                try:
                    payload = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]}
                    if offset is not None: payload["offset"] = offset
                    updates = await api.call("getUpdates", payload)
                    for upd in updates or []:
                        offset = upd["update_id"] + 1
                        try:
                            if upd.get("message"): await on_message(api, upd["message"])
                            elif upd.get("callback_query"): await on_callback(api, upd["callback_query"])
                        except Exception: log.exception("Update failed: %s", upd.get("update_id"))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Polling error; retrying in 4 seconds")
                    await asyncio.sleep(4)
        finally:
            channel_task.cancel()
            try: await channel_task
            except asyncio.CancelledError: pass
            await runner.cleanup()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log.info("Bot stopped.")
