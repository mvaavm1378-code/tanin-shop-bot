# -*- coding: utf-8 -*-
"""Bale catalog/cart bot using the same Supabase PostgreSQL database as the Telegram shop bot.

Install: pip install aiohttp "psycopg[binary]"
Environment: BALE_BOT_TOKEN and SUPABASE_DB_URL (or DATABASE_URL)
Run: python bale_bot.py

This implements catalog/categories and cart only; it does not implement checkout/payments.
"""
import os
import asyncio
import logging
from html import escape
import aiohttp
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


def get_conn():
    if not DB_URL:
        raise RuntimeError("SUPABASE_DB_URL یا DATABASE_URL تنظیم نشده است.")
    return psycopg.connect(DB_URL, row_factory=dict_row,
                           prepare_threshold=None, connect_timeout=15)


def main_keyboard():
    return {"keyboard": [[{"text": "🧾 کاتالوگ محصولات"}],
                         [{"text": "🛒 سبد خرید"}]], "resize_keyboard": True}


def category_keyboard():
    return {"inline_keyboard": [
        [{"text": "👶 بچگانه", "callback_data": "cat:بچگانه"}],
        [{"text": "👗 زنانه", "callback_data": "cat:زنانه"}],
    ]}


def product_keyboard(pid):
    return {"inline_keyboard": [[{"text": "➕ افزودن به سبد خرید",
                                  "callback_data": f"add:{pid}"}]]}


def quantity_keyboard(pid, qty):
    return {"inline_keyboard": [
        [{"text": "➖", "callback_data": f"qty:minus:{pid}:{qty}"},
         {"text": f"📦 {qty} بسته", "callback_data": f"qty:noop:{pid}:{qty}"},
         {"text": "➕", "callback_data": f"qty:plus:{pid}:{qty}"}],
        [{"text": "✅ تأیید و افزودن", "callback_data": f"qty:confirm:{pid}:{qty}"}],
        [{"text": "لغو", "callback_data": f"qty:cancel:{pid}:{qty}"}],
    ]}


def cart_keyboard(cid):
    return {"inline_keyboard": [[{"text": "🗑 حذف از سبد",
                                  "callback_data": f"remove:{cid}"}]]}


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
        p = {"chat_id": chat_id, "message_id": message_id,
             "text": text, "parse_mode": "HTML"}
        if markup is not None:
            p["reply_markup"] = markup
        return await self.call("editMessageText", p)


def db_products(category):
    with get_conn() as conn:
        return conn.execute(
            """SELECT id,name,category,size,color,price,photo_url,pack_info
               FROM products WHERE category=%s AND active=TRUE ORDER BY id DESC""",
            (category,)).fetchall()


def db_product(pid):
    with get_conn() as conn:
        return conn.execute(
            """SELECT id,name,category,size,color,price,photo_url,pack_info
               FROM products WHERE id=%s AND active=TRUE""", (pid,)).fetchone()


def db_add_cart(uid, pid, qty):
    with get_conn() as conn:
        item = conn.execute(
            "SELECT id,qty FROM cart_items WHERE user_id=%s AND product_id=%s",
            (uid, pid)).fetchone()
        if item:
            conn.execute("UPDATE cart_items SET qty=%s WHERE id=%s AND user_id=%s",
                         (int(item["qty"]) + qty, item["id"], uid))
        else:
            conn.execute("INSERT INTO cart_items(user_id,product_id,qty) VALUES(%s,%s,%s)",
                         (uid, pid, qty))
        conn.commit()


def db_cart(uid):
    with get_conn() as conn:
        return conn.execute(
            """SELECT ci.id AS cart_id,p.name,p.price,ci.qty,
                      (p.price*ci.qty) AS line_total
               FROM cart_items ci JOIN products p ON p.id=ci.product_id
               WHERE ci.user_id=%s AND p.active=TRUE ORDER BY ci.id""", (uid,)).fetchall()


def db_remove_cart(uid, cid):
    with get_conn() as conn:
        conn.execute("DELETE FROM cart_items WHERE id=%s AND user_id=%s", (cid, uid))
        conn.commit()


async def show_catalog(api, chat_id):
    await api.send(chat_id, "یکی از دسته‌ها را انتخاب کن:", category_keyboard())


async def show_category(api, chat_id, category):
    try:
        products = await asyncio.to_thread(db_products, category)
    except Exception:
        log.exception("Cannot read products")
        await api.send(chat_id, "❌ دریافت محصولات از دیتابیس ناموفق بود. تنظیمات اتصال را بررسی کن.")
        return
    if not products:
        await api.send(chat_id, "فعلاً محصول فعالی در این دسته موجود نیست.")
        return
    for p in products:
        caption = (f"👕 <b>{escape(str(p['name'] or ''))}</b>\n"
                   f"دسته: {escape(str(p['category'] or '-'))}\n"
                   f"سایز: {escape(str(p['size'] or '-'))}\n"
                   f"رنگ‌بندی: {escape(str(p['color'] or '-'))}\n")
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
    await api.send(chat_id, "🛒 <b>سبد خرید شما</b>", main_keyboard())
    total = 0
    for item in items:
        amount = int(item["line_total"] or 0)
        total += amount
        text = (f"👕 <b>{escape(str(item['name']))}</b>\n"
                f"تعداد بسته: {item['qty']}\n"
                f"قیمت هر بسته: {int(item['price'] or 0):,} تومان\n"
                f"جمع: {amount:,} تومان")
        await api.send(chat_id, text, cart_keyboard(item["cart_id"]))
    await api.send(chat_id, f"💰 <b>جمع کل سبد: {total:,} تومان</b>")


async def on_message(api, msg):
    chat_id = (msg.get("chat") or {}).get("id")
    user_id = (msg.get("from") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or user_id is None:
        return
    if text.startswith("/start"):
        await api.send(chat_id,
                       "🌸 به «تولیدی تنین ایران» خوش آمدی.\nبرای دیدن محصولات، کاتالوگ را انتخاب کن.",
                       main_keyboard())
    elif text in ("/catalog", "🧾 کاتالوگ محصولات", "کاتالوگ محصولات"):
        await show_catalog(api, chat_id)
    elif text in ("/cart", "🛒 سبد خرید", "سبد خرید"):
        await show_cart(api, chat_id, user_id)
    elif text.startswith("/"):
        await api.send(chat_id, "از دکمه‌های منو استفاده کن.", main_keyboard())


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
        if data.startswith("cat:"):
            await api.answer(cbid)
            category = data.split(":", 1)[1]
            if category not in ("بچگانه", "زنانه"):
                await api.send(chat_id, "دسته‌بندی نامعتبر است.")
                return
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
            prompt += "\nتعداد بسته را انتخاب کن:"
            await api.send(chat_id, prompt, quantity_keyboard(pid, 1))
        elif data.startswith("qty:"):
            _, action, pid_s, qty_s = data.split(":")
            pid, qty = int(pid_s), max(1, int(qty_s))
            if action == "plus":
                qty += 1
            elif action == "minus":
                qty = max(1, qty - 1)
            elif action == "noop":
                pass
            elif action == "cancel":
                await api.answer(cbid, "لغو شد.")
                try:
                    await api.call("deleteMessage", {"chat_id": chat_id, "message_id": mid})
                except Exception:
                    pass
                return
            elif action == "confirm":
                product = await asyncio.to_thread(db_product, pid)
                if not product:
                    await api.answer(cbid, "این محصول دیگر موجود نیست.", True)
                    return
                await asyncio.to_thread(db_add_cart, uid, pid, qty)
                await api.answer(cbid, "به سبد خرید اضافه شد.")
                await api.send(chat_id,
                               f"✅ {qty} بسته از «{escape(str(product['name']))}» به سبد خرید اضافه شد.",
                               main_keyboard())
                return
            else:
                await api.answer(cbid)
                return
            await api.answer(cbid)
            product = await asyncio.to_thread(db_product, pid)
            if product and mid:
                await api.edit(chat_id, mid,
                               f"👕 <b>{escape(str(product['name']))}</b>\n\nتعداد بسته را انتخاب کن:",
                               quantity_keyboard(pid, qty))
        elif data.startswith("remove:"):
            cid = int(data.split(":", 1)[1])
            await asyncio.to_thread(db_remove_cart, uid, cid)
            await api.answer(cbid, "از سبد حذف شد.")
            if mid:
                try:
                    await api.edit(chat_id, mid, "🗑 این کالا از سبد خرید حذف شد.")
                except Exception:
                    pass
        else:
            await api.answer(cbid)
    except Exception:
        log.exception("Callback failed: %r", data)
        await api.answer(cbid, "خطا رخ داد؛ دوباره تلاش کن.", True)


async def main():
    if not TOKEN:
        raise RuntimeError("متغیر BALE_BOT_TOKEN تنظیم نشده است.")
    if not DB_URL:
        raise RuntimeError("متغیر SUPABASE_DB_URL یا DATABASE_URL تنظیم نشده است.")
    # Verify database connectivity and required tables without changing schema.
    def check_db():
        with get_conn() as conn:
            for table in ("products", "cart_items"):
                found = conn.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=%s", (table,)
                ).fetchone()
                if not found:
                    raise RuntimeError(f"جدول public.{table} در دیتابیس پیدا نشد.")
    await asyncio.to_thread(check_db)

    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        api = BaleAPI(session)
        offset = None
        log.info("Bale catalog bot started.")
        while True:
            try:
                payload = {"timeout": POLL_TIMEOUT,
                           "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    payload["offset"] = offset
                updates = await api.call("getUpdates", payload)
                for upd in updates or []:
                    offset = upd["update_id"] + 1
                    try:
                        if upd.get("message"):
                            await on_message(api, upd["message"])
                        elif upd.get("callback_query"):
                            await on_callback(api, upd["callback_query"])
                    except Exception:
                        log.exception("Update failed: %s", upd.get("update_id"))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Polling error; retrying in 4 seconds")
                await asyncio.sleep(4)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
