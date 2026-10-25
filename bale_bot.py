
import os
import asyncio
import logging
import httpx

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TOKEN = os.environ["BALE_BOT_TOKEN"]
API = f"https://tapi.bale.ai/bot{TOKEN}"

async def main():
    offset = 0
    timeout = httpx.Timeout(40.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        log.info("Bale bot started")

        while True:
            try:
                response = await client.post(
                    f"{API}/getUpdates",
                    json={"offset": offset, "timeout": 30},
                )
                response.raise_for_status()
                data = response.json()

                if not data.get("ok"):
                    log.error("Bale API returned an error")
                    await asyncio.sleep(5)
                    continue

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message", {})
                    text = message.get("text", "")
                    chat = message.get("chat", {})
                    chat_id = chat.get("id")

                    if not chat_id or not text:
                        continue

                    if text.strip().lower() in ("/start", "start"):
                        await client.post(
                            f"{API}/sendMessage",
                            json={
                                "chat_id": chat_id,
                                "text": (
                                    "سلام! به فروشگاه تنین خوش آمدید.\n"
                                    "ربات بله با موفقیت راه‌اندازی شده است."
                                ),
                            },
                        )

            except (httpx.HTTPError, ValueError):
                log.exception("Bale API connection failed")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
