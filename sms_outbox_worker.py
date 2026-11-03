# -*- coding: utf-8 -*-
"""Tanin transactional SMS outbox worker (Melipayamak approved patterns).

The worker intentionally never retries an ambiguous timeout automatically: the
provider may already have accepted the message. Such rows remain 'sending' for
manual reconciliation, avoiding duplicate customer SMS.
"""
import os
import re
import json
import asyncio
import logging

import aiohttp
import psycopg
from psycopg.rows import dict_row

from sms_templates import TEMPLATE_IDS, TEMPLATE_VARIABLE_COUNTS, normalize_mobile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tanin-sms-worker")

DB_URL = (os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL") or "").strip()
USERNAME = os.getenv("MELIPAYAMAK_USERNAME", "").strip()
PASSWORD = (os.getenv("MELIPAYAMAK_API_KEY") or os.getenv("MELIPAYAMAK_PASSWORD") or "").strip()
SENDER = os.getenv("MELIPAYAMAK_SENDER", "").strip()
SMS_ADMIN_PHONE = normalize_mobile(os.getenv("SMS_ADMIN_PHONE", ""))
SMS_URL = "https://rest.payamak-panel.com/api/SendSMS/SendSMS"
# Melipayamak REST method sendByBaseNumber(text, to, bodyId).
PATTERN_URL = os.getenv(
    "MELIPAYAMAK_PATTERN_URL",
    "https://rest.payamak-panel.com/api/SendSMS/BaseServiceNumber",
).strip()
BATCH_SIZE = 20
TRANSACTIONAL_CATEGORIES = ("transactional", "cashback_notice")


def parse_standard_response(body):
    try:
        data = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return False, None, "Invalid JSON response"
    if not isinstance(data, dict):
        return False, None, "Unexpected response shape"
    status = data.get("RetStatus", data.get("retStatus"))
    try:
        accepted = int(status) == 1
    except (TypeError, ValueError):
        accepted = False
    return accepted, str(data.get("Value") or "")[:200] or None, str(
        data.get("StrRetStatus") or data.get("Message") or ""
    )[:500]


def parse_pattern_response(body):
    """Accept only a real RecId; short numeric provider return codes are errors."""
    raw = str(body or "").strip().strip('"')

    # Some API gateways wrap the result in JSON. Do not trust RetStatus alone:
    # the returned Value must itself be a long numeric RecId.
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        data = None

    if isinstance(data, dict):
        status = data.get("RetStatus", data.get("retStatus"))
        value = str(data.get("Value", data.get("value")) or "").strip()
        if str(status) == "1" and re.fullmatch(r"\d{16,30}", value):
            return True, value[:200], str(data.get("StrRetStatus") or "Pattern accepted")[:500]
        raw_detail = data.get("StrRetStatus") or data.get("Message") or raw
        if value and re.fullmatch(r"-?\d{1,15}", value):
            _, _, mapped = parse_pattern_response(value)
            return False, None, mapped
        return False, None, f"Pattern provider rejected/unknown response: {str(raw_detail)[:400]}"

    if re.fullmatch(r"\d{16,30}", raw):
        return True, raw[:200], "Pattern accepted"

    provider_errors = {
        "-7": "خطای شماره فرستنده؛ با پشتیبانی ملی‌پیامک تماس بگیرید.",
        "-6": "خطای داخلی ملی‌پیامک.",
        "-5": "متغیرهای ارسالی با الگوی تأییدشده مطابقت ندارند.",
        "-4": "کد الگو نامعتبر است یا تأیید نشده است.",
        "-3": "خط ارسال در سامانه تعریف نشده است.",
        "-2": "در هر درخواست فقط یک شماره مجاز است.",
        "-1": "دسترسی وب‌سرویس غیرفعال است.",
        "0": "نام کاربری یا رمز عبور وب‌سرویس نامعتبر است.",
        "2": "اعتبار پنل کافی نیست.",
        "7": "سامانه ملی‌پیامک در حال به‌روزرسانی است.",
        "8": "متن حاوی کلمه فیلترشده است؛ با پشتیبانی ملی‌پیامک تماس بگیرید.",
        "10": "کاربر پنل فعال نیست.",
        "11": "پیامک توسط سرویس ارسال نشده است.",
        "12": "مدارک حساب کاربری کامل نیست.",
        "16": "شماره گیرنده‌ای یافت نشد.",
        "17": "متن پیامک خالی است.",
        "18": "شماره گیرنده نامعتبر است.",
        "19": "محدودیت ساعتی ارسال رعایت نشده است.",
        "-10": "در میان متغیرهای ارسالی لینک وجود دارد.",
    }
    if raw in provider_errors:
        return False, None, f"Melipayamak error {raw}: {provider_errors[raw]}"
    if re.fullmatch(r"-?\d{1,15}", raw):
        return False, None, f"Melipayamak returned an unrecognized short status code: {raw}"
    return False, None, f"Pattern provider rejected/unknown response: {raw[:400]}"


async def claim_batch(conn):
    async with conn.transaction():
        rows = await (await conn.execute(
            """
            SELECT id FROM public.sms_outbox
            WHERE status='queued'
              AND message_category = ANY(%s)
              AND scheduled_at <= NOW()
            ORDER BY scheduled_at, queued_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (list(TRANSACTIONAL_CATEGORIES), BATCH_SIZE),
        )).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            await conn.execute(
                """UPDATE public.sms_outbox
                   SET status='sending', attempts=attempts+1,
                       updated_at=NOW(), last_error=NULL
                   WHERE id=ANY(%s)""",
                (ids,),
            )
    return ids


async def queue_due_order_reminders(conn):
    """Queue one reminder at most 180 minutes after paid order creation."""
    if not SMS_ADMIN_PHONE:
        return 0
    async with conn.transaction():
        rows = await (await conn.execute(
            """
            SELECT id, full_name
            FROM public.orders
            WHERE payment_status='تأیید شده'
              AND display_order_number IS NULL
              AND order_number_assigned_at IS NULL
              AND order_number_reminder_due_at <= NOW()
              AND order_number_reminder_sent_at IS NULL
            ORDER BY order_number_reminder_due_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (BATCH_SIZE,),
        )).fetchall()
        for row in rows:
            oid = int(row["id"])
            await conn.execute(
                """UPDATE public.orders
                   SET order_number_reminder_sent_at=NOW()
                   WHERE id=%s AND order_number_reminder_sent_at IS NULL""",
                (oid,),
            )
            await conn.execute(
                """
                INSERT INTO public.sms_outbox
                    (loyalty_customer_id,message_category,destination_phone,message_text,
                     status,idempotency_key,template_id,template_vars,recipient_type,scheduled_at)
                VALUES(NULL,'transactional',%s,%s,'queued',%s,%s,%s::jsonb,'admin',NOW())
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (SMS_ADMIN_PHONE, f"[Melipayamak pattern {TEMPLATE_IDS['order_reminder_admin']}]",
                 f"order-number-reminder:{oid}", TEMPLATE_IDS["order_reminder_admin"],
                 json.dumps([str(row["full_name"] or "-"), str(oid)], ensure_ascii=False)),
            )
    return len(rows)


async def load_outbox_row(conn, outbox_id):
    return await (await conn.execute(
        """
        SELECT o.id,o.loyalty_customer_id,o.destination_phone,o.message_text,
               o.template_id,o.template_vars,o.recipient_type,
               lc.phone_e164,lc.phone_verified_at,
               COALESCE(scp.transactional_sms_enabled,TRUE) AS sms_enabled
        FROM public.sms_outbox o
        LEFT JOIN public.loyalty_customers lc ON lc.id=o.loyalty_customer_id
        LEFT JOIN public.sms_customer_preferences scp
               ON scp.loyalty_customer_id=o.loyalty_customer_id
        WHERE o.id=%s AND o.status='sending'
        """,
        (outbox_id,),
    )).fetchone()


async def process_one(conn, session, outbox_id):
    row = await load_outbox_row(conn, outbox_id)
    if not row:
        return
    recipient_type = row["recipient_type"] or "customer"

    if recipient_type == "admin":
        phone = normalize_mobile(row["destination_phone"] or SMS_ADMIN_PHONE)
        if not phone:
            reason = "SMS_ADMIN_PHONE/destination_phone is missing or invalid"
    elif recipient_type == "order_customer":
        phone = normalize_mobile(row["destination_phone"])
        if not row["sms_enabled"]:
            phone = ""
            reason = "Transactional SMS disabled by customer preference"
        else:
            reason = "Invalid order customer phone"
    else:
        phone = normalize_mobile(row["phone_e164"])
        if not row["phone_verified_at"] or not row["sms_enabled"]:
            phone = ""
            reason = "No verified phone or transactional SMS disabled"
        else:
            reason = "Invalid verified customer phone"

    if not phone:
        await conn.execute(
            """UPDATE public.sms_outbox SET status='cancelled',last_error=%s,updated_at=NOW()
               WHERE id=%s AND status='sending'""",
            (reason, outbox_id),
        )
        await conn.commit()
        return

    is_pattern = row["template_id"] is not None
    if is_pattern:
        template_id = int(row["template_id"])
        variables = row["template_vars"] or []
        if not isinstance(variables, list):
            variables = []
        expected_count = TEMPLATE_VARIABLE_COUNTS.get(template_id)
        if expected_count is None or len(variables) != expected_count:
            reason = (
                f"Unapproved pattern ID {template_id}"
                if expected_count is None
                else f"Pattern {template_id} requires {expected_count} variables; got {len(variables)}"
            )
            await conn.execute(
                """UPDATE public.sms_outbox
                   SET status='failed',last_error=%s,updated_at=NOW()
                   WHERE id=%s AND status='sending'""",
                (reason, outbox_id),
            )
            await conn.commit()
            log.error("Outbox %s rejected before provider call: %s", outbox_id, reason)
            return
        payload = {
            "username": USERNAME,
            "password": PASSWORD,
            "to": phone,
            "text": ";".join(str(v) for v in variables),
            "bodyId": template_id,
        }
        url = PATTERN_URL
    else:
        payload = {
            "username": USERNAME,
            "password": PASSWORD,
            "to": phone,
            "from": SENDER,
            "text": row["message_text"],
            "isFlash": False,
        }
        url = SMS_URL

    try:
        async with session.post(url, data=payload) as response:
            body = (await response.text())[:1200]
            http_status = response.status
            if is_pattern:
                log.info(
                    "Melipayamak pattern response: outbox_id=%s template_id=%s HTTP=%s body=%s",
                    outbox_id, template_id, http_status, body[:400],
                )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        await conn.execute(
            """UPDATE public.sms_outbox SET last_error=%s,updated_at=NOW()
               WHERE id=%s AND status='sending'""",
            (f"Delivery outcome uncertain; manual reconciliation required: {type(exc).__name__}", outbox_id),
        )
        await conn.commit()
        log.warning("Outbox %s delivery uncertain; no automatic retry.", outbox_id)
        return

    if http_status != 200:
        accepted, provider_id, detail = False, None, f"HTTP {http_status}: {body}"
    elif is_pattern:
        accepted, provider_id, detail = parse_pattern_response(body)
    else:
        accepted, provider_id, detail = parse_standard_response(body)

    if accepted:
        await conn.execute(
            """UPDATE public.sms_outbox
               SET status='accepted',provider_message_id=%s,sent_at=NOW(),
                   updated_at=NOW(),last_error=NULL
               WHERE id=%s AND status='sending'""",
            (provider_id, outbox_id),
        )
    else:
        await conn.execute(
            """UPDATE public.sms_outbox
               SET status='failed',last_error=%s,updated_at=NOW()
               WHERE id=%s AND status='sending'""",
            (str(detail)[:1000], outbox_id),
        )
    await conn.commit()


def validate_configuration():
    missing = []
    for name, value in (
        ("SUPABASE_DB_URL/DATABASE_URL", DB_URL),
        ("MELIPAYAMAK_USERNAME", USERNAME),
        ("MELIPAYAMAK_API_KEY/MELIPAYAMAK_PASSWORD", PASSWORD),
    ):
        if not value:
            missing.append(name)
    if not SMS_ADMIN_PHONE:
        log.warning("SMS_ADMIN_PHONE missing: admin reminders will not be queued.")
    return missing


async def process_due_batch():
    async with await psycopg.AsyncConnection.connect(DB_URL, row_factory=dict_row) as conn:
        reminders = await queue_due_order_reminders(conn)
        ids = await claim_batch(conn)
        if not ids:
            return reminders
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for outbox_id in ids:
                try:
                    await process_one(conn, session, outbox_id)
                except Exception:
                    await conn.execute(
                        """UPDATE public.sms_outbox
                           SET last_error='Unexpected worker error; manual reconciliation required',
                               updated_at=NOW()
                           WHERE id=%s AND status='sending'""",
                        (outbox_id,),
                    )
                    await conn.commit()
                    log.exception("Worker error for outbox id=%s; left in sending state.", outbox_id)
        return reminders + len(ids)


async def run_forever(poll_interval=30):
    missing = validate_configuration()
    if missing:
        log.error("SMS worker disabled; missing environment variables: %s", ", ".join(missing))
        while True:
            await asyncio.sleep(300)
    log.info("SMS worker scheduler started; no local time window is enforced.")
    while True:
        try:
            processed = await process_due_batch()
            if processed:
                log.info("Processed %s reminder/outbox task(s).", processed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("SMS worker batch failed; retrying next tick.")
        await asyncio.sleep(poll_interval)


async def main():
    missing = validate_configuration()
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    processed = await process_due_batch()
    log.info("Processed %s reminder/outbox task(s).", processed)


if __name__ == "__main__":
    asyncio.run(main())
