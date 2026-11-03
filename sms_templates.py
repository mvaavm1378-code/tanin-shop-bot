# -*- coding: utf-8 -*-
"""Transactional SMS pattern queue helpers for Tanin Iran.

All messages are queued in Supabase; the standalone worker performs provider I/O.
"""
import json
import re
import logging

log = logging.getLogger("tanin-sms-templates")

# IDs below are the seven approved Melipayamak patterns supplied by the
# business owner. Keep variable order aligned with the approved pattern text.
TEMPLATE_IDS = {
    "order_waiting_customer": 575477,   # {0}=customer name, {1}=requested products
    "order_admin_new": 575951,          # approved replacement: {0}=buyer name, {1}=mobile, {2}=products
    "order_reminder_admin": 575486,     # {0}=customer name, {1}=request number
    "order_assigned_customer": 575478,  # {0}=customer name, {1}=order number, {2}=details
    "order_confirmed_customer": 575449, # {0}=customer name, {1}=order number, {2}=details
    "wallet_notice": 575528,            # {0}=customer name, {1}=transaction type, {2}=description
    "cashback_notice": 575530,          # {0}=customer name, {1}=order number, {2}=credit description
}

TEMPLATE_VARIABLE_COUNTS = {
    575477: 2,
    575951: 3,
    575486: 2,
    575478: 3,
    575449: 3,
    575528: 3,
    575530: 3,
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
                        category="transactional", scheduled_at=None):
    """Queue one approved pattern. Does not send SMS or affect the caller's transaction."""
    phone = normalize_mobile(phone)
    if not phone:
        return False
    template_id = int(template_id)
    variables = [str(v if v is not None else "-").replace("\r", " ").replace("\n", " ").strip()[:500]
                 for v in (variables or [])]
    expected_count = TEMPLATE_VARIABLE_COUNTS.get(template_id)
    if expected_count is None:
        log.error("Refusing unapproved Melipayamak pattern ID %s", template_id)
        return False
    if len(variables) != expected_count:
        log.error(
            "Refusing pattern %s: expected %s variables, received %s",
            template_id, expected_count, len(variables),
        )
        return False
    if recipient_type not in {"customer", "order_customer", "admin"}:
        raise ValueError("Unsupported SMS recipient_type")
    if not idempotency_key or len(str(idempotency_key)) > 240:
        raise ValueError("Invalid SMS idempotency key")
    # Keep message_category aligned with the production CHECK constraint:
    # cashback_notice, inactive_customer, campaign, transactional.
    # Legacy wallet-specific labels are aliases; template_id carries the precise purpose.
    category_aliases = {
        "wallet_adjustment_notice": "transactional",
        "wallet_payment_notice": "transactional",
    }
    category = category_aliases.get(category, category)
    if category not in {"cashback_notice", "inactive_customer", "campaign", "transactional"}:
        category = "transactional"
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
