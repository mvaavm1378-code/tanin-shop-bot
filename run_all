# -*- coding: utf-8 -*-
"""Single Render Web Service launcher for Telegram webhook, Bale polling and SMS queue."""
import asyncio
import logging
import os

import bot_tanin_payment as telegram_bot
import bale_bot as bale_bot
import sms_outbox_worker as sms_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def main():
    # Telegram webhook binds the one public Render PORT.
    # Bale is polling-only and its duplicate HTTP health listener has been disabled.
    tasks = [
        asyncio.create_task(telegram_bot.async_main(), name="telegram-webhook"),
        asyncio.create_task(bale_bot.main(), name="bale-polling"),
        asyncio.create_task(sms_worker.run_forever(), name="sms-outbox-scheduler"),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc:
                raise exc
        # If any service exits unexpectedly, stop the container so Render restarts all.
        raise RuntimeError("A child service exited unexpectedly: " +
                           ", ".join(t.get_name() for t in done))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Combined service stopped.")
