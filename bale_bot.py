
import os
import logging
from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tanin-bale")

TOKEN = os.environ["BALE_BOT_TOKEN"]
PUBLIC_URL = os.environ["BALE_PUBLIC_URL"].rstrip("/")
WEBHOOK_PATH = os.environ["BALE_WEBHOOK_PATH"].strip("/")

if not WEBHOOK_PATH or "/" in WEBHOOK_PATH:
    raise RuntimeError("BALE_WEBHOOK_PATH must be a non-empty secret path")

API = f"https://tapi.bale.ai/bot{TOKEN}"
TIMEOUT = ClientTimeout(total=20)


async def health(request):
    return web.json_response({"service": "tanin-bale-bot", "status": "ok"})


async def receive_update(request):
    if request.match_info["secret"] != WEBHOOK_PATH:
        raise web.HTTPNotFound()

    try:
        update = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Invalid JSON")

    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")

    if chat_id and text.split("@")[0].lower() == "/start":
        async with ClientSession(timeout=TIMEOUT) as session:
            async with session.post(
                f"{API}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        "سلام! به فروشگاه تنین خوش آمدید.\n"
                        "ربات بله با موفقیت راه‌اندازی شده است."
                    ),
                },
            ) as response:
                result = await response.json()
                if response.status >= 400 or not result.get("ok"):
                    log.error("Bale sendMessage failed; HTTP %s", response.status)
                    return web.Response(status=500, text="Send failed")

    return web.Response(text="OK")


async def on_startup(app):
    url = f"{PUBLIC_URL}/bale/update/{WEBHOOK_PATH}"

    async with ClientSession(timeout=TIMEOUT) as session:
        async with session.post(
            f"{API}/setWebhook",
            json={"url": url},
        ) as response:
            result = await response.json()
            if response.status >= 400 or not result.get("ok"):
                raise RuntimeError("Bale webhook setup failed")

    log.info("Bale webhook registered")


app = web.Application(client_max_size=1024 * 1024)
app.router.add_get("/health", health)
app.router.add_post("/bale/update/{secret}", receive_update)
app.on_startup.append(on_startup)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)
