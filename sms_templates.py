# -*- coding: utf-8 -*-
"""Transactional SMS pattern queue helpers for Tanin Iran.

All messages are queued in Supabase; the standalone worker performs provider I/O.
"""
import json
import re
import logging

log = logging.getLogger("tanin-sms-templates")

TEMPLATE_IDS = {
    "order_waiting_customer": 575427,
    "order_admin_new": 575422,
    "order_reminder_admin": 575426,
    "order_assigned_customer": 575439,
    "order_confirmed_customer": 575428,
    "wallet_notice": 575528,
    "cashback_notice": 575530,
}

def normalize_mobile(value):
    value = str(value or "").translate(str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"
    ))
    value = re.sub(r"\D", "", value)
    if value.startswith("0098"):
        value = "0" + value[4:]
    elif value.startswith("98") and len(value) == 12:
        value = "0" + value[2:]
    return value if len(value) == 11 and value.startswith("09") else ""

def enqueue_pattern_sms(conn, *, phone, template_id, variables, idempotency_key,
                        recipient_type="customer", loyalty_customer_id=None,
                        category="wallet_payment_notice", scheduled_at=None):
    """Queue one approved pattern. Does not send SMS or affect the caller's transaction."""
    phone = normalize_mobile(phone)
    if not phone:
        return False
    template_id = int(template_id)
    variables = [str(v if v is not None else "-").replace("\r", " ").replace("\n", " ").strip()[:500]
                 for v in (variables or [])]
    if recipient_type not in {"customer", "order_customer", "admin"}:
        raise ValueError("Unsupported SMS recipient_type")
    if not idempotency_key or len(str(idempotency_key)) > 240:
        raise ValueError("Invalid SMS idempotency key")
    # Keep message_category within the existing transactional categories accepted by
    # the production table's CHECK constraint; template_id carries the real purpose.
    if category not in {"cashback_notice", "wallet_adjustment_notice", "wallet_payment_notice"}:
        category = "wallet_payment_notice"
    try:
        # Nested psycopg transaction becomes a SAVEPOINT when the caller already
        # has a transaction. SMS queue failure must never roll back an order/payment.
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO public.sms_outbox
                    (loyalty_customer_id, message_category, destination_phone, message_text,
                     status, idempotency_key, template_id, template_vars, recipient_type, scheduled_at)
                VALUES (%s,%s,%s,%s,'queued',%s,%s,%s::jsonb,%s,COALESCE(%s::timestamptz,NOW()))
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (loyalty_customer_id, category, phone, f"[Melipayamak pattern {template_id}]",
                 str(idempotency_key), template_id, json.dumps(variables, ensure_ascii=False),
                 recipient_type, scheduled_at),
            )
        return True
    except Exception:
        log.exception("Could not enqueue SMS pattern %s (key=%s)", template_id, idempotency_key)
        return False

def queue_order_created_sms(conn, order, admin_phone):
    """Queue customer waiting notice and admin new-order alert, once per order."""
    oid = int(order["id"])
    name = str(order.get("full_name") or "-")
    phone = order.get("phone") or ""
    products = str(order.get("items_summary") or "-")
    customer_ok = enqueue_pattern_sms(
        conn, phone=phone, template_id=TEMPLATE_IDS["order_waiting_customer"],
        variables=[name, products], idempotency_key=f"order-waiting-customer:{oid}",
        recipient_type="order_customer",
    )
    admin_ok = enqueue_pattern_sms(
        conn, phone=admin_phone, template_id=TEMPLATE_IDS["order_admin_new"],
        variables=[name, phone, products], idempotency_key=f"order-admin-new:{oid}",
        recipient_type="admin",
    )
    return customer_ok, admin_ok

def queue_order_assigned_sms(conn, order):
    return enqueue_pattern_sms(
        conn, phone=order.get("phone"), template_id=TEMPLATE_IDS["order_assigned_customer"],
        variables=[order.get("full_name") or "-", order.get("display_order_number") or "-", order.get("items_summary") or "-"],
        idempotency_key=f"order-number-assigned:{int(order['id'])}",
        recipient_type="order_customer",
    )

def queue_order_confirmed_sms(conn, order):
    return enqueue_pattern_sms(
        conn, phone=order.get("phone"), template_id=TEMPLATE_IDS["order_confirmed_customer"],
        variables=[order.get("full_name") or "-", order.get("display_order_number") or "-", order.get("items_summary") or "-"],
        idempotency_key=f"order-confirmed:{int(order['id'])}",
        recipient_type="order_customer",
    )
