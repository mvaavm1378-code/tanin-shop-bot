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
# Checkout conversation state is kept in memory. If Render restarts, the customer can send /checkout again.
CHECKOUT = {}
ADMIN_FLOW = {}  # In-memory admin input steps; cancelled with /cancel


def get_conn():
    if not DB_URL:
        raise RuntimeError("SUPABASE_DB_URL یا DATABASE_URL تنظیم نشده است.")
    return psycopg.connect(DB_URL, row_factory=dict_row, prepare_threshold=None, connect_timeout=15)


def main_keyboard():
    return {"keyboard": [[{"text": "🧾 کاتالوگ محصولات"}],
                         [{"text": "🛒 سبد خرید"}, {"text": "🛍️ ادامه ثبت سفارش"}]], "resize_keyboard": True}


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


def payment_keyboard(pid):
    return {"inline_keyboard": [[{"text": "📤 راهنمای ارسال رسید", "callback_data": f"pay:receipt:{pid}"}]]}


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
         {"text": "👥 مشتری‌های بله", "callback_data": "adm:customers"}],
        [{"text": "📊 فروش بله", "callback_data": "adm:sales"},
         {"text": "🎫 پشتیبانی بله", "callback_data": "adm:tickets"}],
        [{"text": "💳 تنظیمات پرداخت", "callback_data": "adm:payment"}],
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
            "pending": count("SELECT COUNT(*) AS n FROM pending_payments WHERE user_id < 0 AND payment_status='در انتظار بررسی'"),
            "orders": count("SELECT COUNT(*) AS n FROM orders WHERE user_id < 0 AND status='در انتظار بررسی'"),
            "tickets": count("SELECT COUNT(*) AS n FROM support_tickets WHERE user_id < 0 AND status IN ('new','admin_waiting')"),
            "products": count("SELECT COUNT(*) AS n FROM products WHERE active=TRUE"),
            "customers": count("SELECT COUNT(*) AS n FROM customers WHERE user_id < 0"),
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


def admin_now():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M")


def admin_product_text(p):
    state = "فعال ✅" if p["active"] else "غیرفعال ⛔"
    return (
        f"🛍 <b>{escape(str(p['name'] or '-'))}</b>\n"
        f"شناسه: #{p['id']}\n"
        f"دسته: {escape(str(p['category'] or '-'))}\n"
        f"سایز: {escape(str(p['size'] or '-'))}\n"
        f"رنگ‌بندی: {escape(str(p['color'] or '-'))}\n"
        f"پک: {escape(str(p['pack_info'] or '-'))}\n"
        f"قیمت: {int(p['price'] or 0):,} تومان\n"
        f"عکس: {'ثبت شده' if p['photo_url'] else 'ندارد'}\n"
        f"وضعیت: {state}"
    )


def admin_product_detail_keyboard(pid, active):
    toggle_label = "⛔ غیرفعال کردن" if active else "🟢 فعال کردن"
    return {"inline_keyboard": [
        [{"text": "✏️ ویرایش نام", "callback_data": f"adm:pfield:{pid}:name"},
         {"text": "🗂 ویرایش دسته", "callback_data": f"adm:pfield:{pid}:category"}],
        [{"text": "📏 ویرایش سایز", "callback_data": f"adm:pfield:{pid}:size"},
         {"text": "🎨 ویرایش رنگ", "callback_data": f"adm:pfield:{pid}:color"}],
        [{"text": "📦 ویرایش پک", "callback_data": f"adm:pfield:{pid}:pack_info"},
         {"text": "💰 تغییر قیمت", "callback_data": f"adm:pfield:{pid}:price"}],
        [{"text": "🖼 تغییر عکس", "callback_data": f"adm:pfield:{pid}:photo_url"}],
        [{"text": "➖۵۰ هزار", "callback_data": f"adm:pprice:{pid}:-50000"},
         {"text": "➕۵۰ هزار", "callback_data": f"adm:pprice:{pid}:50000"}],
        [{"text": toggle_label, "callback_data": f"adm:producttoggle:{pid}"},
         {"text": "🗄 آرشیو محصول", "callback_data": f"adm:productarchive:{pid}"}],
        [{"text": "🔙 فهرست محصولات", "callback_data": "adm:products"}],
    ]}


async def show_admin_product_detail(api, chat_id, pid, message_id=None):
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=%s", (pid,)).fetchone()
    if not p:
        await api.send(chat_id, "❌ محصول پیدا نشد.", admin_back("products"))
        return
    text = admin_product_text(p)
    markup = admin_product_detail_keyboard(pid, bool(p["active"]))
    if message_id:
        try:
            await api.edit(chat_id, message_id, text, markup)
            return
        except Exception:
            pass
    await api.send(chat_id, text, markup)


async def show_admin_products(api, chat_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,name,category,price,active FROM products ORDER BY active DESC,id DESC LIMIT 50"
        ).fetchall()
    buttons = [[{"text": "➕ افزودن محصول", "callback_data": "adm:padd"}]]
    for p in rows:
        state = "🟢" if p["active"] else "⚪"
        label = f"{state} #{p['id']} {str(p['name'] or '-')[:22]} · {int(p['price'] or 0):,}"
        buttons.append([{"text": label[:60], "callback_data": f"adm:product:{p['id']}"}])
    buttons += [[{"text": "🔄 بروزرسانی", "callback_data": "adm:products"}],
                [{"text": "🔙 پنل اصلی", "callback_data": "adm:home"}]]
    await api.send(chat_id, f"🛠 <b>مدیریت محصولات</b>\nتعداد نمایش‌داده‌شده: {len(rows)}\n\nمحصول را برای مشاهده و ویرایش انتخاب کن.", {"inline_keyboard": buttons})


PRODUCT_FIELDS = {
    "name": ("نام محصول", "نام جدید محصول را بفرست:", "name"),
    "category": ("دسته‌بندی", "دسته را دقیقاً «بچگانه» یا «زنانه» بفرست:", "category"),
    "size": ("سایز", "سایز جدید را بفرست:", "size"),
    "color": ("رنگ‌بندی", "رنگ‌بندی جدید را بفرست:", "color"),
    "pack_info": ("اطلاعات پک", "اطلاعات پک را بفرست؛ برای پاک‌کردن بنویس «ندارد»:", "pack_info"),
    "price": ("قیمت", "قیمت جدید را فقط به تومان و با عدد وارد کن:", "price"),
    "photo_url": ("عکس", "لینک عمومی مستقیم عکس با https:// را بفرست؛ برای پاک‌کردن بنویس «ندارد». برای سازگاری تلگرام و بله، file_id اختصاصی بله پذیرفته نمی‌شود:", "photo_url"),
}


def clean_admin_value(field, value):
    value = str(value or "").strip()
    if field == "name" and not value:
        raise ValueError("نام محصول نمی‌تواند خالی باشد.")
    if field == "category" and value not in ("بچگانه", "زنانه"):
        raise ValueError("دسته‌بندی باید دقیقاً «بچگانه» یا «زنانه» باشد.")
    if field == "price":
        value = value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        try:
            price = int(value.replace(",", "").replace("٬", "").replace("،", ""))
        except Exception:
            raise ValueError("قیمت معتبر نیست؛ فقط عدد به تومان وارد کن.")
        if price < 0:
            raise ValueError("قیمت نمی‌تواند منفی باشد.")
        return price
    if field in ("pack_info", "photo_url") and value.lower() in ("ندارد", "ندارم", "-", "no", "none"):
        return ""
    if field == "photo_url" and value and not value.startswith("https://"):
        raise ValueError("برای کاتالوگ مشترک تلگرام و بله، لینک عمومی عکس باید با https:// شروع شود. file_id اختصاصی بله قابل استفاده در تلگرام نیست.")
    return value


async def handle_admin_text(api, msg, uid, chat_id, text):
    flow = ADMIN_FLOW.get(uid)
    if not flow:
        return False
    if text == "/cancel":
        ADMIN_FLOW.pop(uid, None)
        await api.send(chat_id, "عملیات مدیریتی لغو شد.", admin_keyboard())
        return True
    ftype = flow.get("type")
    if ftype in ("product_add", "product_edit_full"):
        steps = ["name", "category", "size", "color", "pack_info", "price", "photo_url"]
        step = flow["step"]
        if ftype == "product_edit_full" and text.strip() in ("بدون تغییر", "همین", "skip"):
            value = flow["data"].get(step, "")
        else:
            try:
                value = clean_admin_value(step, text)
            except ValueError as e:
                await api.send(chat_id, "❌ " + escape(str(e)) + "\n\n" + PRODUCT_FIELDS[step][1])
                return True
        flow["data"][step] = value
        idx = steps.index(step)
        if idx < len(steps) - 1:
            flow["step"] = steps[idx + 1]
            await api.send(chat_id, f"مرحله {idx+2} از {len(steps)} — {PRODUCT_FIELDS[flow['step']][1]}\nبرای لغو /cancel را بفرست.")
            return True
        d = flow["data"]
        with get_conn() as conn:
            if ftype == "product_add":
                row = conn.execute(
                    """INSERT INTO products(name,category,size,color,price,photo_url,active,pack_info)
                       VALUES(%s,%s,%s,%s,%s,%s,TRUE,%s) RETURNING id""",
                    (d["name"], d["category"], d["size"], d["color"], d["price"], d["photo_url"], d["pack_info"]),
                ).fetchone()
                pid = int(row["id"])
                result_text = f"✅ محصول «{escape(d['name'])}» با شناسه #{pid} اضافه شد."
            else:
                pid = int(flow["product_id"])
                conn.execute(
                    """UPDATE products SET name=%s,category=%s,size=%s,color=%s,pack_info=%s,
                       price=%s,photo_url=%s WHERE id=%s""",
                    (d["name"], d["category"], d["size"], d["color"], d["pack_info"], d["price"], d["photo_url"], pid),
                )
                result_text = f"✅ تمام جزئیات محصول #{pid} به‌روزرسانی شد."
            conn.commit()
        ADMIN_FLOW.pop(uid, None)
        await api.send(chat_id, result_text)
        await show_admin_product_detail(api, chat_id, pid)
        return True
    if ftype == "product_field":
        field, pid = flow["field"], int(flow["product_id"])
        try:
            value = clean_admin_value(field, text)
        except ValueError as e:
            await api.send(chat_id, "❌ " + escape(str(e)) + "\n\n" + PRODUCT_FIELDS[field][1])
            return True
        column = PRODUCT_FIELDS[field][2]
        with get_conn() as conn:
            exists = conn.execute("SELECT id FROM products WHERE id=%s", (pid,)).fetchone()
            if not exists:
                ADMIN_FLOW.pop(uid, None)
                await api.send(chat_id, "محصول پیدا نشد.", admin_back("products"))
                return True
            conn.execute(f"UPDATE products SET {column}=%s WHERE id=%s", (value, pid))
            conn.commit()
        ADMIN_FLOW.pop(uid, None)
        await api.send(chat_id, f"✅ {PRODUCT_FIELDS[field][0]} محصول #{pid} تغییر کرد.")
        await show_admin_product_detail(api, chat_id, pid)
        return True
    if ftype in ("bank_add", "bank_edit"):
        fields = ["bank_name", "owner_name", "card_number", "account_number", "iban"]
        prompts = {
            "bank_name": "نام بانک را بفرست:",
            "owner_name": "نام صاحب حساب را بفرست:",
            "card_number": "شماره کارت را بفرست (یا «ندارد»):",
            "account_number": "شماره حساب را بفرست (یا «ندارد»):",
            "iban": "شماره شبا را بفرست (یا «ندارد»):",
        }
        step = flow["step"]
        value = text.strip()
        if not value and step in ("bank_name", "owner_name"):
            await api.send(chat_id, "این مورد نمی‌تواند خالی باشد.\n" + prompts[step]); return True
        if value.lower() in ("ندارد", "ندارم", "-", "no"):
            value = ""
        flow["data"][step] = value
        idx = fields.index(step)
        if idx < len(fields) - 1:
            flow["step"] = fields[idx + 1]
            await api.send(chat_id, prompts[flow["step"]] + "\nبرای لغو /cancel را بفرست.")
            return True
        d = flow["data"]
        now = admin_now()
        with get_conn() as conn:
            if ftype == "bank_add":
                has_active = conn.execute("SELECT id FROM bank_accounts WHERE active=TRUE LIMIT 1").fetchone()
                active = not bool(has_active)
                row = conn.execute(
                    """INSERT INTO bank_accounts(bank_name,owner_name,card_number,account_number,iban,active,created_at,updated_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (d["bank_name"], d["owner_name"], d["card_number"], d["account_number"], d["iban"], active, now, now),
                ).fetchone()
                account_id = int(row["id"])
                msg_text = f"✅ حساب بانکی #{account_id} اضافه شد. وضعیت: {'فعال' if active else 'غیرفعال'}"
            else:
                account_id = int(flow["account_id"])
                conn.execute(
                    """UPDATE bank_accounts SET bank_name=%s,owner_name=%s,card_number=%s,
                       account_number=%s,iban=%s,updated_at=%s WHERE id=%s""",
                    (d["bank_name"], d["owner_name"], d["card_number"], d["account_number"], d["iban"], now, account_id),
                )
                msg_text = f"✅ اطلاعات حساب بانکی #{account_id} ویرایش شد."
            conn.commit()
        ADMIN_FLOW.pop(uid, None)
        await api.send(chat_id, msg_text)
        await show_admin_bank_detail(api, chat_id, account_id)
        return True
    if ftype == "ticket_reply":
        ticket_id = int(flow["ticket_id"])
        body = text.strip()
        if not body:
            await api.send(chat_id, "متن پاسخ خالی است؛ پاسخ را دوباره بفرست:")
            return True
        now = admin_now()
        with get_conn() as conn:
            ticket = conn.execute("SELECT id,user_id,status FROM support_tickets WHERE id=%s AND user_id<0", (ticket_id,)).fetchone()
            if not ticket:
                ADMIN_FLOW.pop(uid, None)
                await api.send(chat_id, "تیکت بله پیدا نشد.", admin_back("tickets"))
                return True
            conn.execute("INSERT INTO support_ticket_messages(ticket_id,sender_id,sender_type,message,created_at) VALUES(%s,%s,'admin',%s,%s)", (ticket_id, uid, body, now))
            conn.execute("UPDATE support_tickets SET status='customer_waiting',updated_at=%s WHERE id=%s", (now, ticket_id))
            conn.commit()
            customer_id = abs(int(ticket["user_id"]))
        ADMIN_FLOW.pop(uid, None)
        try:
            await api.send(customer_id, f"💬 پاسخ پشتیبانی برای تیکت #{ticket_id}:\n\n{escape(body)}")
        except Exception:
            log.exception("Could not notify Bale ticket customer %s", customer_id)
        await api.send(chat_id, "✅ پاسخ برای مشتری ارسال شد.", admin_back("tickets"))
        return True
    return False


async def show_admin_bank_list(api, chat_id):
    with get_conn() as conn:
        rows = conn.execute("SELECT id,bank_name,owner_name,active FROM bank_accounts ORDER BY active DESC,id DESC LIMIT 30").fetchall()
    buttons = [[{"text": "➕ افزودن حساب", "callback_data": "adm:bank_add"}]]
    for row in rows:
        buttons.append([{"text": f"{'🟢' if row['active'] else '⚪'} #{row['id']} {str(row['bank_name'] or '-')[:22]}", "callback_data": f"adm:bank:{row['id']}"}])
    buttons.append([{"text": "🔙 پنل اصلی", "callback_data": "adm:home"}])
    await api.send(chat_id, "💳 <b>تنظیمات پرداخت و حساب‌های بانکی</b>\nحساب را انتخاب کن یا حساب جدید اضافه کن.", {"inline_keyboard": buttons})


async def show_admin_bank_detail(api, chat_id, account_id, message_id=None):
    with get_conn() as conn:
        a = conn.execute("SELECT * FROM bank_accounts WHERE id=%s", (account_id,)).fetchone()
    if not a:
        await api.send(chat_id, "حساب بانکی پیدا نشد.", admin_back("payment")); return
    text = (f"🏦 <b>حساب بانکی #{a['id']}</b>\n"
            f"بانک: {escape(str(a['bank_name'] or '-'))}\n"
            f"صاحب حساب: {escape(str(a['owner_name'] or '-'))}\n"
            f"کارت: <code>{escape(str(a['card_number'] or '-'))}</code>\n"
            f"حساب: <code>{escape(str(a['account_number'] or '-'))}</code>\n"
            f"شبا: <code>{escape(str(a['iban'] or '-'))}</code>\n"
            f"وضعیت: {'🟢 فعال' if a['active'] else '⚪ غیرفعال'}")
    rows = []
    if a["active"]:
        rows.append([{"text": "⛔ غیرفعال کردن", "callback_data": f"adm:bank_deactivate:{account_id}"}])
    else:
        rows.append([{"text": "🟢 فعال کردن", "callback_data": f"adm:bank_activate:{account_id}"}])
    rows.extend([
        [{"text": "✏️ ویرایش اطلاعات", "callback_data": f"adm:bank_edit:{account_id}"}],
        [{"text": "🗑 حذف حساب", "callback_data": f"adm:bank_delete:{account_id}"}],
        [{"text": "🔙 فهرست حساب‌ها", "callback_data": "adm:payment"}],
    ])
    markup = {"inline_keyboard": rows}
    if message_id:
        try: await api.edit(chat_id, message_id, text, markup); return
        except Exception: pass
    await api.send(chat_id, text, markup)


async def show_admin_customer_detail(api, chat_id, user_id, message_id=None):
    user_id = int(user_id)
    if user_id >= 0:
        await api.send(chat_id, "شناسه مشتری بله نامعتبر است.", admin_back("customers")); return
    with get_conn() as conn:
        customer = conn.execute("SELECT user_id,username,full_name,phone,gender,first_seen,last_seen FROM customers WHERE user_id=%s AND user_id<0", (user_id,)).fetchone()
        orders = conn.execute("SELECT id,total_price,status,payment_status,created_at FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT 8", (user_id,)).fetchall() if customer else []
        saved = conn.execute("SELECT data_type,value FROM customer_saved_data WHERE user_id=%s ORDER BY id DESC LIMIT 12", (user_id,)).fetchall() if customer else []
    if not customer:
        await api.send(chat_id, "مشتری بله پیدا نشد.", admin_back("customers")); return
    lines = [f"👤 <b>مشتری بله #{abs(user_id)}</b>",
             f"نام: {escape(str(customer['full_name'] or '-'))}",
             f"نام کاربری: @{escape(str(customer['username'] or '-'))}",
             f"تلفن: {escape(str(customer['phone'] or '-'))}",
             f"جنسیت: {escape(str(customer['gender'] or '-'))}",
             f"اولین مراجعه: {escape(str(customer['first_seen'] or '-'))}",
             f"آخرین مراجعه: {escape(str(customer['last_seen'] or '-'))}", "", "<b>اطلاعات ذخیره‌شده:</b>"]
    if saved:
        for item in saved:
            lines.append(f"• {escape(str(item['data_type']))}: {escape(str(item['value']))}")
    else:
        lines.append("اطلاعات ذخیره‌شده‌ای ثبت نشده است.")
    lines += ["", "<b>آخرین سفارش‌ها:</b>"]
    buttons = []
    for order in orders:
        lines.append(f"#{order['id']} · {int(order['total_price'] or 0):,} تومان · {escape(str(order['status'] or '-'))}")
        buttons.append([{"text": f"📦 سفارش #{order['id']}", "callback_data": f"adm:orderdetail:{order['id']}"}])
    if not orders:
        lines.append("سفارشی ثبت نشده است.")
    buttons.append([{"text": "🔙 فهرست مشتریان", "callback_data": "adm:customers"}])
    markup = {"inline_keyboard": buttons}
    if message_id:
        try: await api.edit(chat_id, message_id, "\n".join(lines)[:3900], markup); return
        except Exception: pass
    await api.send(chat_id, "\n".join(lines)[:3900], markup)


async def show_admin_ticket_detail(api, chat_id, ticket_id, message_id=None):
    with get_conn() as conn:
        t = conn.execute("SELECT * FROM support_tickets WHERE id=%s AND user_id<0", (ticket_id,)).fetchone()
        msgs = conn.execute("SELECT sender_type,message,created_at FROM support_ticket_messages WHERE ticket_id=%s ORDER BY id ASC LIMIT 30", (ticket_id,)).fetchall() if t else []
    if not t:
        await api.send(chat_id, "تیکت بله پیدا نشد.", admin_back("tickets")); return
    lines = [f"🎫 <b>تیکت #{ticket_id}</b>", f"مشتری: {abs(int(t['user_id']))}", f"موضوع: {escape(str(t['topic'] or '-'))}", f"وضعیت: {escape(str(t['status'] or '-'))}", "", "<b>گفتگو:</b>"]
    for m in msgs[-12:]:
        who = "مدیر" if m["sender_type"] == "admin" else "مشتری"
        lines.append(f"<b>{who}:</b> {escape(str(m['message'] or ''))}")
    rows = []
    if t["status"] != "closed":
        rows.append([{"text": "💬 پاسخ به مشتری", "callback_data": f"adm:ticket_reply:{ticket_id}"}, {"text": "🔒 بستن تیکت", "callback_data": f"adm:ticket_close:{ticket_id}"}])
    rows.append([{"text": "🔙 فهرست تیکت‌ها", "callback_data": "adm:tickets"}])
    markup = {"inline_keyboard": rows}
    if message_id:
        try: await api.edit(chat_id, message_id, "\n".join(lines)[:3900], markup); return
        except Exception: pass
    await api.send(chat_id, "\n".join(lines)[:3900], markup)


async def show_admin_section(api, chat_id, section):
    if section == "pending":
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id,full_name,total_price,created_at,payment_status FROM pending_payments "
                "WHERE user_id < 0 AND payment_status='در انتظار بررسی' ORDER BY id DESC LIMIT 30"
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
                "SELECT id,full_name,total_price,status,created_at FROM orders WHERE user_id < 0 ORDER BY id DESC LIMIT 30"
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
        await show_admin_products(api, chat_id)
    elif section == "customers":
        with get_conn() as conn:
            rows = conn.execute("SELECT user_id,full_name,phone,username FROM customers WHERE user_id < 0 ORDER BY last_seen DESC NULLS LAST LIMIT 30").fetchall()
        buttons = []
        for r in rows:
            label = f"{str(r['full_name'] or r['username'] or 'بدون نام')[:22]} · {abs(int(r['user_id']))}"
            buttons.append([{"text": label[:60], "callback_data": f"adm:customer:{r['user_id']}"}])
        buttons += [[{"text": "🔄 بروزرسانی", "callback_data": "adm:customers"}],
                    [{"text": "🔙 پنل اصلی", "callback_data": "adm:home"}]]
        await api.send(chat_id, "👥 <b>مشتریان بله</b>\nبرای مشاهده اطلاعات و سفارش‌های مشتری، او را انتخاب کن.", {"inline_keyboard": buttons})
    elif section == "sales":
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(total_price),0) AS total FROM orders "
                "WHERE user_id < 0 AND payment_status='تأیید شده'"
            ).fetchone()
        await api.send(chat_id, f"📊 <b>آمار فروش</b>\nسفارش‌های پرداخت‌شده: {row['n']}\nجمع مبلغ: {int(row['total'] or 0):,} تومان", admin_back())
    elif section == "tickets":
        with get_conn() as conn:
            rows = conn.execute("SELECT id,user_id,topic,status,created_at FROM support_tickets WHERE user_id < 0 ORDER BY id DESC LIMIT 30").fetchall()
        buttons = []
        for r in rows:
            label = f"#{r['id']} · {abs(int(r['user_id']))} · {str(r['topic'] or '-')[:15]} · {str(r['status'] or '-')[:12]}"
            buttons.append([{"text": label[:60], "callback_data": f"adm:ticket:{r['id']}"}])
        buttons += [[{"text": "🔄 بروزرسانی", "callback_data": "adm:tickets"}],
                    [{"text": "🔙 پنل اصلی", "callback_data": "adm:home"}]]
        await api.send(chat_id, "🎫 <b>تیکت‌های پشتیبانی بله</b>\nبرای مشاهده و پاسخ، تیکت را انتخاب کن.", {"inline_keyboard": buttons} if buttons else admin_back())
    elif section == "payment":
        await show_admin_bank_list(api, chat_id)
    elif section == "channel":
        await api.send(chat_id, "📢 مدیریت کانال\n\nقابلیت انتشار و زمان‌بندی کانال در نسخه بله هنوز پیاده‌سازی نشده است. این بخش به تنظیم شناسه کانال بله و دسترسی ارسال پیام نیاز دارد؛ تا آماده‌شدن آن دکمه‌ای برای انتشار غیرواقعی نمایش نمی‌دهیم.", admin_back())


async def show_admin_pending_detail(api, chat_id, pid):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pending_payments WHERE id=%s AND user_id < 0", (pid,)).fetchone()
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

    async def remove_inline_keyboard(self, chat_id, message_id):
        """Remove buttons from a clicked inline-keyboard message, leaving its content intact."""
        if chat_id is None or message_id is None:
            return
        try:
            await self.call("editMessageReplyMarkup", {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            })
        except Exception as exc:
            log.debug("Could not clear clicked Bale inline keyboard: %s", exc)


def db_products(category):
    with get_conn() as conn:
        return conn.execute("SELECT id,name,category,size,color,price,photo_url,pack_info FROM products WHERE category=%s AND active=TRUE ORDER BY id DESC", (category,)).fetchall()


def db_product(pid):
    with get_conn() as conn:
        return conn.execute("SELECT id,name,category,size,color,price,photo_url,pack_info FROM products WHERE id=%s AND active=TRUE", (pid,)).fetchone()


def db_user_key(uid):
    """Use negative user IDs for Bale records; Telegram IDs remain positive."""
    return -abs(int(uid))


def db_add_cart(uid, pid, qty):
    uid = db_user_key(uid)
    with get_conn() as conn:
        item = conn.execute("SELECT id,qty FROM cart_items WHERE user_id=%s AND product_id=%s", (uid, pid)).fetchone()
        if item:
            conn.execute("UPDATE cart_items SET qty=%s WHERE id=%s AND user_id=%s", (int(item["qty"]) + qty, item["id"], uid))
        else:
            conn.execute("INSERT INTO cart_items(user_id,product_id,qty) VALUES(%s,%s,%s)", (uid, pid, qty))
        conn.commit()


def db_cart(uid):
    uid = db_user_key(uid)
    with get_conn() as conn:
        return conn.execute("""SELECT ci.id AS cart_id,p.name,p.price,p.pack_info,ci.qty,(p.price*ci.qty) AS line_total
            FROM cart_items ci JOIN products p ON p.id=ci.product_id
            WHERE ci.user_id=%s AND p.active=TRUE ORDER BY ci.id""", (uid,)).fetchall()


def db_remove_cart(uid, cid):
    uid = db_user_key(uid)
    with get_conn() as conn:
        conn.execute("DELETE FROM cart_items WHERE id=%s AND user_id=%s", (cid, uid))
        conn.commit()


# Prefix Bale IDs in customer_saved_data so they cannot collide with Telegram IDs.
def bale_db_user_id(uid):
    """Store Bale customer-owned rows under a negative key; API/chat IDs stay positive."""
    return -abs(int(uid))


def saved_user_key(uid):
    return bale_db_user_id(uid)


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


def db_create_pending(uid, full_name, phone, gender, address, username):
    """Create/reuse an unpaid payment record; return (payment row, bank account)."""
    uid = db_user_key(uid)
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
    uid = db_user_key(uid)
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


def db_review_payment(pid, admin_id, approve):
    with get_conn() as conn:
        now = __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M")
        with conn.transaction():
            pending = conn.execute("SELECT * FROM pending_payments WHERE id=%s AND user_id < 0 FOR UPDATE", (pid,)).fetchone()
            if not pending or pending["payment_status"] != "در انتظار بررسی":
                return None, "already"
            if not approve:
                conn.execute("UPDATE pending_payments SET payment_status='رد شد',reviewed_at=%s,admin_id=%s WHERE id=%s", (now, admin_id, pid))
                return dict(pending), "rejected"
            order = conn.execute("""INSERT INTO orders
                (user_id,full_name,phone,address,items_summary,total_price,status,created_at,finalized_at,payment_status,transaction_ref)
                VALUES(%s,%s,%s,%s,%s,%s,'در انتظار بررسی',%s,%s,'تأیید شده',%s) RETURNING id""",
                (pending["user_id"],pending["full_name"],pending["phone"],pending["address"],pending["items_summary"],pending["total_price"],now,now,f"BALE-CARD-TRANSFER-{pid}")).fetchone()
            conn.execute("DELETE FROM cart_items WHERE user_id=%s", (pending["user_id"],))
            conn.execute("UPDATE pending_payments SET payment_status='تأیید شده',reviewed_at=%s,admin_id=%s,order_id=%s WHERE id=%s", (now, admin_id, order["id"], pid))
            return dict(pending), f"approved:{order['id']}"


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
    CHECKOUT[uid] = {"step": "name"}
    saved = await asyncio.to_thread(db_saved_data, uid, "name")
    if saved:
        await api.send(chat_id, "👤 نام و نام خانوادگی را انتخاب کن یا اطلاعات جدید وارد کن:", await asyncio.to_thread(saved_keyboard, uid, "name", "name"))
    else:
        await api.send(chat_id, "👤 برای ثبت سفارش، نام و نام خانوادگی را وارد کن.\nمثال: محمد احمدی\nبرای لغو بنویس /cancel")


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
    await api.send(chat_id, text, payment_keyboard(pending["id"]))


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
        name = re.sub(r"\s+", " ", text).strip()
        if len(name.split()) < 2 or len(name) < 5:
            await api.send(chat_id, "❌ نام و نام خانوادگی را کامل وارد کن. مثال: محمد احمدی")
            return True
        state["full_name"] = name
        await asyncio.to_thread(db_save_data, uid, "name", name)
        state["step"] = "phone"
        saved = await asyncio.to_thread(db_saved_data, uid, "phone")
        if saved:
            await api.send(chat_id, "📱 شماره موبایل را انتخاب کن یا شماره جدید وارد کن:", await asyncio.to_thread(saved_keyboard, uid, "phone", "phone"))
        else:
            await api.send(chat_id, "📱 شماره موبایل ایرانی را وارد کن (مثال: 09123456789).")
        return True
    if step == "phone":
        digits = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        phone = re.sub(r"[\s\-()]+", "", digits)
        if phone.startswith("+98"):
            phone = "0" + phone[3:]
        elif phone.startswith("0098"):
            phone = "0" + phone[4:]
        elif phone.startswith("98"):
            phone = "0" + phone[2:]
        if len(phone) != 11 or not phone.startswith("09") or not phone.isdigit():
            await api.send(chat_id, "❌ شماره معتبر نیست. شماره را به شکل 09123456789 وارد کن.")
            return True
        state["phone"] = phone
        await asyncio.to_thread(db_save_data, uid, "phone", phone)
        state["step"] = "gender"
        await api.send(chat_id, "جنسیت را انتخاب کن:", {"keyboard": [[{"text": "مرد"}, {"text": "زن"}], [{"text": "/cancel"}]], "resize_keyboard": True})
        return True
    if step == "gender":
        if text not in ("مرد", "زن"):
            await api.send(chat_id, "لطفاً یکی از گزینه‌های «مرد» یا «زن» را انتخاب کن.")
            return True
        state["gender"] = text
        state["step"] = "address"
        # Bale reply keyboards persist until explicitly removed, so remove the gender
        # keyboard before asking for the address.
        await api.send(chat_id, "✅ جنسیت ثبت شد.", {"remove_keyboard": True})
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


async def on_message(api, msg):
    chat_id = (msg.get("chat") or {}).get("id")
    user = msg.get("from") or {}
    uid = user.get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or uid is None:
        return

    # مدیر منوی مستقل دارد و فقط پیام‌های در انتظار یک فرایند مدیریتی را می‌پذیرد.
    if is_admin(uid):
        if text.startswith("/start") or text in ("/admin", "⚙️ پنل مدیریت", "پنل مدیریت"):
            CHECKOUT.pop(uid, None)
            ADMIN_FLOW.pop(uid, None)
            await show_admin_home(api, chat_id)
            return
        if text == "/cancel":
            ADMIN_FLOW.pop(uid, None)
            CHECKOUT.pop(uid, None)
            await api.send(chat_id, "عملیات جاری لغو شد. برای بازکردن پنل /admin را بفرست.", admin_keyboard())
            return
        if uid in ADMIN_FLOW:
            flow = ADMIN_FLOW[uid]
            # Product catalog is shared with Telegram; Bale-specific photo file_id cannot be stored as a shared photo URL.
            if flow.get("type") in ("product_add", "product_edit_full", "product_field") and flow.get("step") == "photo_url" and msg.get("photo"):
                await api.send(chat_id, "برای اینکه عکس در هر دو ربات تلگرام و بله نمایش داده شود، لطفاً لینک عمومی HTTPS عکس را بفرست؛ شناسه فایل عکس بله فقط در بله کار می‌کند. برای لغو /cancel را بفرست.")
                return
            if await handle_admin_text(api, msg, uid, chat_id, text):
                return
        if msg.get("photo") or msg.get("document"):
            await api.send(chat_id, "برای ویرایش عکس، ابتدا از پنل محصول گزینه «تغییر عکس» را بزن؛ برای لغو /cancel را بفرست.", admin_keyboard())
            return
        await api.send(chat_id, "برای مدیریت فروشگاه از دکمه‌های پنل استفاده کن یا /admin را بفرست.", admin_keyboard())
        return

    if text.startswith("/start"):
        CHECKOUT.pop(uid, None)
        await api.send(chat_id, "🌸 به «تولیدی تنین ایران» خوش آمدی.\nبرای دیدن محصولات یا ثبت سفارش از منوی زیر استفاده کن.", main_keyboard())
    elif text == "/cancel":
        CHECKOUT.pop(uid, None)
        await api.send(chat_id, "عملیات جاری لغو شد.", main_keyboard())
    elif text in ("/admin", "⚙️ پنل مدیریت", "پنل مدیریت"):
        await api.send(chat_id, "دسترسی مدیر لازم است.", main_keyboard())
    elif text.startswith("/") and text not in ("/catalog", "/cart", "/checkout"):
        await api.send(chat_id, "از دکمه‌های منو استفاده کن.", main_keyboard())
    elif await handle_checkout_text(api, msg, uid, chat_id, text):
        return
    elif msg.get("photo") or (msg.get("document") and str((msg.get("document") or {}).get("mime_type", "")).startswith("image/")):
        await handle_receipt(api, msg, uid, chat_id)
    elif text in ("/catalog", "🧾 کاتالوگ محصولات", "کاتالوگ محصولات"):
        await show_catalog(api, chat_id)
    elif text in ("/cart", "🛒 سبد خرید", "سبد خرید"):
        await show_cart(api, chat_id, uid)
    elif text in ("/checkout", "🛍️ ادامه ثبت سفارش", "🛍 ادامه ثبت سفارش"):
        await start_checkout(api, chat_id, uid)
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
    # Hide the old inline keyboard as soon as a button is selected; later callback
    # logic may display a new keyboard for the next screen.
    await api.remove_inline_keyboard(chat_id, mid)
    try:
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
                await show_admin_section(api, chat_id, section)
            elif section == "pendingdetail" and len(action) == 3:
                await show_admin_pending_detail(api, chat_id, int(action[2]))
            elif section == "receipt" and len(action) == 3:
                with get_conn() as conn:
                    rec = conn.execute("SELECT receipt_file_id FROM pending_payments WHERE id=%s AND user_id < 0", (int(action[2]),)).fetchone()
                if rec and rec["receipt_file_id"]:
                    await api.call("sendPhoto", {"chat_id": chat_id, "photo": rec["receipt_file_id"], "caption": f"رسید پرداخت #{action[2]}"})
                else:
                    await api.send(chat_id, "رسید برای این پرداخت ثبت نشده است.", admin_back("pending"))
            elif section == "padd":
                ADMIN_FLOW[uid] = {"type": "product_add", "step": "name", "data": {}}
                await api.send(chat_id, "➕ <b>افزودن محصول جدید</b>\nمرحله 1 از 7 — نام محصول را بفرست:\nبرای لغو /cancel را بفرست.")
            elif section == "product" and len(action) == 3:
                await show_admin_product_detail(api, chat_id, int(action[2]), mid)
            elif section == "pedit" and len(action) == 3:
                pid = int(action[2])
                with get_conn() as conn:
                    p = conn.execute("SELECT id,name,category,size,color,pack_info,price,photo_url FROM products WHERE id=%s", (pid,)).fetchone()
                if not p:
                    await api.send(chat_id, "محصول پیدا نشد.", admin_back("products"))
                else:
                    ADMIN_FLOW[uid] = {"type": "product_edit_full", "product_id": pid, "step": "name", "data": {k: (p[k] or "") for k in ("name", "category", "size", "color", "pack_info", "price", "photo_url")}}
                    await api.send(chat_id, f"✏️ ویرایش کامل محصول #{pid}\nمرحله 1 از 7 — نام جدید را بفرست. برای حفظ مقدار فعلی بنویس «بدون تغییر».\nبرای لغو /cancel را بفرست.")
            elif section == "pfield" and len(action) == 4:
                pid, field = int(action[2]), action[3]
                if field not in PRODUCT_FIELDS:
                    await api.send(chat_id, "فیلد نامعتبر است.", admin_back("products"))
                else:
                    with get_conn() as conn:
                        exists = conn.execute("SELECT id FROM products WHERE id=%s", (pid,)).fetchone()
                    if not exists:
                        await api.send(chat_id, "محصول پیدا نشد.", admin_back("products"))
                    else:
                        ADMIN_FLOW[uid] = {"type": "product_field", "product_id": pid, "field": field, "step": field}
                        await api.send(chat_id, f"✏️ {PRODUCT_FIELDS[field][0]} محصول #{pid}\n{PRODUCT_FIELDS[field][1]}\nبرای لغو /cancel را بفرست.")
            elif section == "pprice" and len(action) == 4:
                pid, delta = int(action[2]), int(action[3])
                with get_conn() as conn:
                    p = conn.execute("SELECT price FROM products WHERE id=%s", (pid,)).fetchone()
                    if p:
                        new_price = max(0, int(p["price"] or 0) + delta)
                        conn.execute("UPDATE products SET price=%s WHERE id=%s", (new_price, pid))
                        conn.commit()
                if p:
                    await api.send(chat_id, f"✅ قیمت جدید محصول #{pid}: {new_price:,} تومان")
                    await show_admin_product_detail(api, chat_id, pid)
                else:
                    await api.send(chat_id, "محصول پیدا نشد.", admin_back("products"))
            elif section == "productarchive" and len(action) == 3:
                pid = int(action[2])
                with get_conn() as conn:
                    p = conn.execute("SELECT id,name,active FROM products WHERE id=%s", (pid,)).fetchone()
                if not p:
                    await api.send(chat_id, "محصول پیدا نشد.", admin_back("products"))
                elif not p["active"]:
                    await api.send(chat_id, "این محصول از قبل غیرفعال است.", admin_back("products"))
                else:
                    await api.send(chat_id, f"⚠️ محصول «{escape(str(p['name']))}» از کاتالوگ مشتریان پنهان شود؟\nسابقه سفارش‌ها حفظ می‌شود.", {"inline_keyboard":[[{"text":"🗄 بله، آرشیو کن","callback_data":f"adm:productarchiveconfirm:{pid}"}],[{"text":"لغو","callback_data":f"adm:product:{pid}"}]]})
            elif section == "productarchiveconfirm" and len(action) == 3:
                pid = int(action[2])
                with get_conn() as conn:
                    p = conn.execute("SELECT id,name FROM products WHERE id=%s", (pid,)).fetchone()
                    if p:
                        conn.execute("UPDATE products SET active=FALSE WHERE id=%s", (pid,))
                        conn.commit()
                await api.send(chat_id, f"🗄 محصول «{escape(str(p['name']))}» آرشیو شد؛ سابقه سفارش‌ها حفظ شده است." if p else "محصول پیدا نشد.", admin_back("products"))
            elif section == "bank_add":
                ADMIN_FLOW[uid] = {"type": "bank_add", "step": "bank_name", "data": {}}
                await api.send(chat_id, "➕ افزودن حساب بانکی\nنام بانک را بفرست:\nبرای لغو /cancel را بفرست.")
            elif section == "bank" and len(action) == 3:
                await show_admin_bank_detail(api, chat_id, int(action[2]), mid)
            elif section == "bank_edit" and len(action) == 3:
                account_id = int(action[2])
                with get_conn() as conn:
                    a = conn.execute("SELECT id FROM bank_accounts WHERE id=%s", (account_id,)).fetchone()
                if not a:
                    await api.send(chat_id, "حساب بانکی پیدا نشد.", admin_back("payment"))
                else:
                    ADMIN_FLOW[uid] = {"type": "bank_edit", "account_id": account_id, "step": "bank_name", "data": {}}
                    await api.send(chat_id, f"✏️ ویرایش حساب #{account_id}\nنام بانک جدید را بفرست:\nبرای لغو /cancel را بفرست.")
            elif section == "bank_activate" and len(action) == 3:
                account_id = int(action[2]); now = admin_now()
                with get_conn() as conn:
                    a = conn.execute("SELECT id FROM bank_accounts WHERE id=%s", (account_id,)).fetchone()
                    if a:
                        conn.execute("UPDATE bank_accounts SET active=FALSE,updated_at=%s", (now,))
                        conn.execute("UPDATE bank_accounts SET active=TRUE,updated_at=%s WHERE id=%s", (now, account_id))
                        conn.commit()
                await api.send(chat_id, "حساب فعال شد؛ سایر حساب‌ها غیرفعال شدند." if a else "حساب پیدا نشد.", admin_back("payment"))
            elif section == "bank_deactivate" and len(action) == 3:
                account_id = int(action[2]); now = admin_now()
                with get_conn() as conn:
                    a = conn.execute("SELECT id,active FROM bank_accounts WHERE id=%s", (account_id,)).fetchone()
                    active_count = conn.execute("SELECT COUNT(*) AS n FROM bank_accounts WHERE active=TRUE").fetchone()["n"]
                    if a and a["active"] and int(active_count or 0) <= 1:
                        await api.send(chat_id, "نمی‌توان آخرین حساب فعال را غیرفعال کرد؛ ابتدا حساب دیگری اضافه و فعال کن.", admin_back("payment")); return
                    if a:
                        conn.execute("UPDATE bank_accounts SET active=FALSE,updated_at=%s WHERE id=%s", (now, account_id)); conn.commit()
                await api.send(chat_id, "حساب غیرفعال شد." if a else "حساب پیدا نشد.", admin_back("payment"))
            elif section == "bank_delete" and len(action) == 3:
                account_id = int(action[2])
                with get_conn() as conn:
                    a = conn.execute("SELECT id,bank_name,active FROM bank_accounts WHERE id=%s", (account_id,)).fetchone()
                if not a:
                    await api.send(chat_id, "حساب پیدا نشد.", admin_back("payment"))
                elif a["active"]:
                    await api.send(chat_id, "حساب فعال را حذف نکن؛ ابتدا حساب دیگری را فعال کن.", admin_back("payment"))
                else:
                    await api.send(chat_id, f"⚠️ حذف دائمی حساب #{account_id} ({escape(str(a['bank_name']))})؟", {"inline_keyboard":[[{"text":"🗑 بله، حذف کن","callback_data":f"adm:bank_delete_confirm:{account_id}"}],[{"text":"لغو","callback_data":f"adm:bank:{account_id}"}]]})
            elif section == "bank_delete_confirm" and len(action) == 3:
                account_id = int(action[2])
                with get_conn() as conn:
                    a = conn.execute("SELECT active FROM bank_accounts WHERE id=%s", (account_id,)).fetchone()
                    if a and not a["active"]:
                        conn.execute("DELETE FROM bank_accounts WHERE id=%s", (account_id,)); conn.commit()
                await api.send(chat_id, "حساب حذف شد." if a and not a["active"] else "حساب فعال یا پیدا نشد؛ حذف انجام نشد.", admin_back("payment"))
            elif section == "customer" and len(action) == 3:
                await show_admin_customer_detail(api, chat_id, int(action[2]), mid)
            elif section == "ticket" and len(action) == 3:
                await show_admin_ticket_detail(api, chat_id, int(action[2]), mid)
            elif section == "ticket_reply" and len(action) == 3:
                ticket_id = int(action[2])
                ADMIN_FLOW[uid] = {"type": "ticket_reply", "ticket_id": ticket_id}
                await api.send(chat_id, f"💬 پاسخ تیکت #{ticket_id} را بنویس:\nبرای لغو /cancel را بفرست.")
            elif section == "ticket_close" and len(action) == 3:
                ticket_id = int(action[2]); now = admin_now()
                with get_conn() as conn:
                    t = conn.execute("SELECT user_id FROM support_tickets WHERE id=%s AND user_id<0", (ticket_id,)).fetchone()
                    if t:
                        conn.execute("UPDATE support_tickets SET status='closed',closed_at=%s,updated_at=%s WHERE id=%s", (now, now, ticket_id)); conn.commit()
                if t:
                    try: await api.send(abs(int(t["user_id"])), f"تیکت پشتیبانی #{ticket_id} بسته شد.")
                    except Exception: log.exception("Could not notify ticket customer")
                    await api.send(chat_id, "تیکت بسته شد.", admin_back("tickets"))
                else:
                    await api.send(chat_id, "تیکت بله پیدا نشد.", admin_back("tickets"))
            elif section == "producttoggle" and len(action) == 3:
                pid = int(action[2])
                with get_conn() as conn:
                    p = conn.execute("SELECT active,name FROM products WHERE id=%s", (pid,)).fetchone()
                    if p:
                        conn.execute("UPDATE products SET active=%s WHERE id=%s", (not bool(p["active"]), pid))
                        conn.commit()
                if p:
                    await api.send(chat_id, f"وضعیت محصول #{pid} تغییر کرد.")
                    await show_admin_product_detail(api, chat_id, pid)
                else:
                    await api.send(chat_id, "محصول پیدا نشد.", admin_back("products"))
            elif section == "orderdetail" and len(action) == 3:
                with get_conn() as conn:
                    o = conn.execute("SELECT * FROM orders WHERE id=%s AND user_id < 0", (int(action[2]),)).fetchone()
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
                        order_row = conn.execute("SELECT user_id FROM orders WHERE id=%s AND user_id < 0 AND status='در انتظار بررسی'", (oid,)).fetchone()
                        if order_row:
                            conn.execute("UPDATE orders SET status=%s WHERE id=%s AND user_id < 0 AND status='در انتظار بررسی'", (status_text, oid))
                            conn.commit()
                    if order_row:
                        customer_notice = f"📦 وضعیت سفارش #{oid} به «{status_text}» تغییر کرد."
                        try:
                            await api.send(abs(int(order_row["user_id"])), customer_notice, main_keyboard())
                        except Exception:
                            log.exception("Could not notify Bale customer about order status")
                    await api.send(chat_id, f"وضعیت سفارش #{oid} به «{status_text}» تغییر کرد." if order_row else "سفارش پیدا نشد یا قبلاً بررسی شده است.", admin_back("orders"))
            else:
                await api.send(chat_id, "این عملیات مدیریتی هنوز پیاده‌سازی نشده است.", admin_keyboard())
            return

        # مدیر نباید بتواند از callbackهای مشتری برای ساخت سبد یا سفارش استفاده کند.
        if is_admin(uid) and (data.startswith(("cat:", "add:", "qty:", "remove:", "checkout:", "saved:"))):
            await api.answer(cbid, "این حساب فقط برای مدیریت فروشگاه است.", True)
            return

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
                    state["full_name"] = value; state["step"] = "phone"
                    saved = await asyncio.to_thread(db_saved_data, uid, "phone")
                    await api.answer(cbid, "نام انتخاب شد.")
                    if saved: await api.send(chat_id, "📱 شماره موبایل را انتخاب کن یا شماره جدید وارد کن:", await asyncio.to_thread(saved_keyboard, uid, "phone", "phone"))
                    else: await api.send(chat_id, "📱 شماره موبایل ایرانی را وارد کن (مثال: 09123456789).")
                elif step == "phone":
                    state["phone"] = value; state["step"] = "gender"
                    await api.answer(cbid, "شماره انتخاب شد.")
                    await api.send(chat_id, "جنسیت را انتخاب کن:", {"keyboard": [[{"text": "مرد"}, {"text": "زن"}], [{"text": "/cancel"}]], "resize_keyboard": True})
                elif step == "address":
                    state["address"] = value
                    await api.answer(cbid, "آدرس انتخاب شد.")
                    try:
                        pending, bank = await asyncio.to_thread(db_create_pending, uid, state["full_name"], state["phone"], state["gender"], value, (msg.get("from") or {}).get("username", ""))
                    except Exception:
                        log.exception("Could not create pending payment for Bale user %s", uid)
                        await api.send(chat_id, "⚠️ ثبت سفارش انجام نشد؛ لطفاً کمی بعد دوباره تلاش کن.", main_keyboard()); CHECKOUT.pop(uid, None); return
                    CHECKOUT.pop(uid, None)
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
                await api.send(chat_id, f"✅ پرداخت #{pid_s} تأیید شد و سفارش #{result.split(':',1)[1]} ثبت شد.")
                customer_msg = f"✅ پرداخت شما تأیید شد و سفارش با موفقیت ثبت شد.\nشماره سفارش: #{result.split(':',1)[1]}"
            else:
                await api.send(chat_id, f"❌ پرداخت #{pid_s} رد شد.")
                customer_msg = "❌ رسید پرداخت تأیید نشد. لطفاً با فروشگاه تماس بگیر یا رسید معتبر ارسال کن."
            try: await api.send(abs(int(pending["user_id"])), customer_msg, main_keyboard())
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
        required = {"products", "customers", "cart_items", "bank_accounts", "pending_payments", "orders"}
        with get_conn() as conn:
            found = {r["table_name"] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()}
            missing = sorted(required - found)
            if missing:
                raise RuntimeError("جدول‌های لازم در Supabase پیدا نشدند: " + ", ".join(missing))
            for table, columns in {
                "products": {"id", "name", "category", "price", "active", "photo_url", "pack_info", "size", "color"},
                "cart_items": {"id", "user_id", "product_id", "qty"},
                "bank_accounts": {"id", "bank_name", "owner_name", "card_number", "account_number", "iban", "active"},
                "pending_payments": {"id", "user_id", "full_name", "phone", "address", "items_summary", "total_price", "payment_status", "receipt_file_id", "created_at", "reviewed_at", "order_id", "admin_id"},
                "orders": {"id", "user_id", "full_name", "phone", "address", "items_summary", "total_price", "status", "created_at", "finalized_at", "payment_status", "transaction_ref"},
                "customers": {"user_id", "username", "full_name", "phone", "gender", "first_seen", "last_seen"},
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
            await runner.cleanup()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log.info("Bot stopped.")
