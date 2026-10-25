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
