# -*- coding: utf-8 -*-
"""One-time migration helper: SQLite shop.db -> Supabase PostgreSQL.

Required environment variable:
  SUPABASE_DB_URL (or DATABASE_URL / POSTGRES_URL)

Run from the directory that contains shop.db:
  python migrate_sqlite_to_supabase.py
"""
import os
import sqlite3
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

DB_URL = (
    os.getenv("SUPABASE_DB_URL", "").strip()
    or os.getenv("DATABASE_URL", "").strip()
    or os.getenv("POSTGRES_URL", "").strip()
)
SQLITE_PATH = os.getenv("SQLITE_DB_PATH", os.path.join(os.path.dirname(__file__), "shop.db"))

if not DB_URL:
    raise SystemExit("SUPABASE_DB_URL (or DATABASE_URL) is not set.")
if not os.path.exists(SQLITE_PATH):
    raise SystemExit(f"SQLite database not found: {SQLITE_PATH}")


def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")


src = sqlite3.connect(SQLITE_PATH)
src.row_factory = sqlite3.Row

dst = psycopg.connect(DB_URL, row_factory=dict_row, prepare_threshold=None, connect_timeout=15)

try:
    # Make sure the destination has the schema expected by the converted bot.
    required = {"products", "customers", "cart_items", "bank_accounts", "pending_payments", "orders", "app_meta"}
    found = {
        r["table_name"]
        for r in dst.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name = ANY(%s)",
            (list(required),),
        ).fetchall()
    }
    missing = required - found
    if missing:
        raise RuntimeError("Missing Supabase tables: " + ", ".join(sorted(missing)))

    # customers
    for r in src.execute("SELECT * FROM customers").fetchall():
        dst.execute(
            """INSERT INTO customers(user_id, username, full_name, phone, first_seen, last_seen)
               VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=EXCLUDED.username, full_name=EXCLUDED.full_name,
                 phone=EXCLUDED.phone, first_seen=EXCLUDED.first_seen, last_seen=EXCLUDED.last_seen""",
            (r["user_id"], r["username"], r["full_name"], r["phone"], r["first_seen"], r["last_seen"]),
        )

    # products: stock existed in SQLite but is not part of the Supabase schema, so it is intentionally ignored.
    for r in src.execute("SELECT id,name,category,size,color,price,photo_url,active,pack_info FROM products").fetchall():
        dst.execute(
            """INSERT INTO products(id,name,category,size,color,price,photo_url,active,pack_info)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(id) DO UPDATE SET
                 name=EXCLUDED.name, category=EXCLUDED.category, size=EXCLUDED.size,
                 color=EXCLUDED.color, price=EXCLUDED.price, photo_url=EXCLUDED.photo_url,
                 active=EXCLUDED.active, pack_info=EXCLUDED.pack_info""",
            (r["id"], r["name"], r["category"], r["size"], r["color"] or "رنگ‌بندی طبق ژورنال موجود",
             r["price"], r["photo_url"], bool(r["active"]), r["pack_info"] or ""),
        )

    # cart_items: the target has a unique(user_id, product_id). Preserve IDs where possible.
    for r in src.execute("SELECT id,user_id,product_id,qty FROM cart_items").fetchall():
        dst.execute(
            """INSERT INTO cart_items(id,user_id,product_id,qty)
               VALUES(%s,%s,%s,%s)
               ON CONFLICT(id) DO UPDATE SET user_id=EXCLUDED.user_id, product_id=EXCLUDED.product_id, qty=EXCLUDED.qty""",
            (r["id"], r["user_id"], r["product_id"], r["qty"] or 1),
        )

    # bank_accounts: SQLite called the flag is_active; Supabase calls it active.
    bank_rows = src.execute(
        "SELECT id,bank_name,owner_name,card_number,account_number,iban,is_active,created_at,updated_at FROM bank_accounts ORDER BY id"
    ).fetchall()
    active_seen = False
    for r in bank_rows:
        active = bool(r["is_active"]) and not active_seen
        active_seen = active_seen or active
        dst.execute(
            """INSERT INTO bank_accounts(id,bank_name,owner_name,card_number,account_number,iban,active,created_at,updated_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(id) DO UPDATE SET
                 bank_name=EXCLUDED.bank_name, owner_name=EXCLUDED.owner_name,
                 card_number=EXCLUDED.card_number, account_number=EXCLUDED.account_number,
                 iban=EXCLUDED.iban, active=EXCLUDED.active,
                 created_at=EXCLUDED.created_at, updated_at=EXCLUDED.updated_at""",
            (r["id"], r["bank_name"], r["owner_name"], r["card_number"], r["account_number"],
             r["iban"], active, r["created_at"] or now_text(), r["updated_at"] or now_text()),
        )

    # Legacy payment_settings -> bank_accounts, only if the destination has no accounts.
    if not bank_rows:
        try:
            legacy = src.execute("SELECT * FROM payment_settings WHERE id=1").fetchone()
        except sqlite3.OperationalError:
            legacy = None
        if legacy and any(legacy[k] for k in ("bank_name", "owner_name", "card_number", "account_number", "iban")):
            dst.execute(
                """INSERT INTO bank_accounts(bank_name,owner_name,card_number,account_number,iban,active,created_at,updated_at)
                   VALUES(%s,%s,%s,%s,%s,TRUE,%s,%s)""",
                (legacy["bank_name"], legacy["owner_name"], legacy["card_number"], legacy["account_number"],
                 legacy["iban"], now_text(), legacy["updated_at"] or now_text()),
            )

    # pending_payments
    for r in src.execute(
        "SELECT id,user_id,full_name,phone,address,items_summary,total_price,payment_status,receipt_file_id,created_at,reviewed_at,order_id,admin_id FROM pending_payments"
    ).fetchall():
        dst.execute(
            """INSERT INTO pending_payments(id,user_id,full_name,phone,address,items_summary,total_price,payment_status,receipt_file_id,created_at,reviewed_at,order_id,admin_id)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(id) DO UPDATE SET
                 user_id=EXCLUDED.user_id, full_name=EXCLUDED.full_name, phone=EXCLUDED.phone,
                 address=EXCLUDED.address, items_summary=EXCLUDED.items_summary, total_price=EXCLUDED.total_price,
                 payment_status=EXCLUDED.payment_status, receipt_file_id=EXCLUDED.receipt_file_id,
                 created_at=EXCLUDED.created_at, reviewed_at=EXCLUDED.reviewed_at, order_id=EXCLUDED.order_id, admin_id=EXCLUDED.admin_id""",
            tuple(r),
        )

    # orders
    for r in src.execute(
        "SELECT id,user_id,full_name,phone,address,items_summary,total_price,status,created_at,payment_status,transaction_ref FROM orders"
    ).fetchall():
        dst.execute(
            """INSERT INTO orders(id,user_id,full_name,phone,address,items_summary,total_price,status,created_at,payment_status,transaction_ref)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(id) DO UPDATE SET
                 user_id=EXCLUDED.user_id, full_name=EXCLUDED.full_name, phone=EXCLUDED.phone,
                 address=EXCLUDED.address, items_summary=EXCLUDED.items_summary, total_price=EXCLUDED.total_price,
                 status=EXCLUDED.status, created_at=EXCLUDED.created_at, payment_status=EXCLUDED.payment_status,
                 transaction_ref=EXCLUDED.transaction_ref""",
            tuple(r),
        )

    # app_meta
    for r in src.execute("SELECT key,value FROM app_meta").fetchall():
        dst.execute(
            """INSERT INTO app_meta(key,value) VALUES(%s,%s)
               ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""",
            (r["key"], r["value"]),
        )

    # Advance sequences after importing explicit IDs.
    for table in ("products", "cart_items", "bank_accounts", "pending_payments", "orders"):
        dst.execute(
            """SELECT setval(
                pg_get_serial_sequence(%s, 'id'),
                COALESCE((SELECT MAX(id) FROM """ + table + """), 1),
                true
            )""",
            (f"public.{table}",),
        )

    dst.commit()
    print("Migration completed successfully.")
finally:
    src.close()
    dst.close()
