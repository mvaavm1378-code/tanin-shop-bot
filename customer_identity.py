# -*- coding: utf-8 -*-
"""Shared customer identity, profile, OTP and wholesaler services for Tanin Iran.

This module is used by both Telegram and Bale bots. The verified Iranian mobile
number is the cross-platform login key; loyalty_customers.id remains the stable
internal customer key for future website integration.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sms_templates import TEMPLATE_IDS, enqueue_pattern_sms

OTP_TTL_MINUTES = 5
OTP_MAX_ATTEMPTS = 5
OTP_MAX_SENDS_PER_HOUR = 5
OTP_RESEND_WAIT_SECONDS = 60


def normalize_mobile_e164(value: str) -> Optional[str]:
    """Return Iranian mobile as +989xxxxxxxxx, or None for invalid input."""
    raw = str(value or "").translate(str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"
    ))
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0098"):
        digits = digits[4:]
    elif digits.startswith("98") and len(digits) == 12:
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("09"):
        digits = digits[1:]
    elif len(digits) == 10 and digits.startswith("9"):
        pass
    else:
        return None
    if len(digits) != 10 or not digits.startswith("9"):
        return None
    return "+98" + digits


def mobile_e164_to_local(value: str) -> str:
    value = str(value or "")
    return "0" + value[3:] if value.startswith("+98") else value


def _hash_otp(salt: str, code: str) -> str:
    return hashlib.sha256((salt + ":" + str(code)).encode("utf-8")).hexdigest()


def ensure_platform_account(conn, platform: str, platform_user_id, display_name=None) -> int:
    """Return/create the central profile and platform link. Caller owns transaction."""
    if platform not in ("telegram", "bale"):
        raise ValueError("پلتفرم نامعتبر است.")
    platform_user_id = str(platform_user_id)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 (f"customer-platform:{platform}:{platform_user_id}",))
    account = conn.execute(
        """SELECT loyalty_customer_id FROM customer_platform_accounts
           WHERE platform=%s AND platform_user_id=%s FOR UPDATE""",
        (platform, platform_user_id),
    ).fetchone()
    if account:
        customer_id = int(account["loyalty_customer_id"])
        conn.execute(
            """UPDATE customer_platform_accounts SET last_seen_at=NOW()
               WHERE platform=%s AND platform_user_id=%s""",
            (platform, platform_user_id),
        )
        if display_name:
            conn.execute(
                """UPDATE loyalty_customers SET display_name=COALESCE(NULLIF(display_name,''),%s),
                   updated_at=NOW() WHERE id=%s""",
                (str(display_name)[:250], customer_id),
            )
    else:
        row = conn.execute(
            "INSERT INTO loyalty_customers(display_name,display_name_updated_at) VALUES(%s,NULL) RETURNING id",
            (str(display_name)[:250] if display_name else None,),
        ).fetchone()
        customer_id = int(row["id"])
        conn.execute(
            """INSERT INTO customer_platform_accounts
               (loyalty_customer_id,platform,platform_user_id,linked_at,last_seen_at)
               VALUES(%s,%s,%s,NOW(),NOW())""",
            (customer_id, platform, platform_user_id),
        )
    conn.execute(
        "INSERT INTO wallet_accounts(loyalty_customer_id) VALUES(%s) ON CONFLICT(loyalty_customer_id) DO NOTHING",
        (customer_id,),
    )
    conn.execute(
        "INSERT INTO sms_customer_preferences(loyalty_customer_id) VALUES(%s) ON CONFLICT(loyalty_customer_id) DO NOTHING",
        (customer_id,),
    )
    return customer_id


def _legacy_user_key(platform: str, platform_user_id: str) -> int:
    uid = int(platform_user_id)
    return -abs(uid) if platform == "bale" else uid


def _upsert_legacy_value(conn, platform: str, platform_user_id: str, data_type: str, value: Optional[str]):
    if not value:
        return
    legacy_uid = _legacy_user_key(platform, platform_user_id)
    same = conn.execute(
        "SELECT id FROM customer_saved_data WHERE user_id=%s AND data_type=%s AND value=%s ORDER BY id DESC LIMIT 1",
        (legacy_uid, data_type, str(value)[:2000]),
    ).fetchone()
    row = conn.execute(
        """SELECT id FROM customer_saved_data WHERE user_id=%s AND data_type=%s
           ORDER BY COALESCE(NULLIF(updated_at,''),created_at) DESC NULLS LAST,id DESC LIMIT 1""",
        (legacy_uid, data_type),
    ).fetchone()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if same:
        # Promote an already-existing identical value to current without violating
        # the legacy (user_id,data_type,value) unique constraint.
        conn.execute("UPDATE customer_saved_data SET updated_at=%s WHERE id=%s", (now, same["id"]))
    elif row:
        # Preserve all legacy rows; only the current/latest row is updated.
        conn.execute(
            "UPDATE customer_saved_data SET value=%s,updated_at=%s WHERE id=%s",
            (str(value)[:2000], now, row["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO customer_saved_data(user_id,data_type,value,created_at,updated_at)
               VALUES(%s,%s,%s,%s,%s)""",
            (legacy_uid, data_type, str(value)[:2000], now, now),
        )


def _sync_legacy_values(conn, loyalty_customer_id: int):
    profile = conn.execute(
        "SELECT display_name,phone_e164,phone_verified_at,address FROM loyalty_customers WHERE id=%s",
        (loyalty_customer_id,),
    ).fetchone()
    if not profile:
        return
    accounts = conn.execute(
        "SELECT platform,platform_user_id FROM customer_platform_accounts WHERE loyalty_customer_id=%s",
        (loyalty_customer_id,),
    ).fetchall()
    for account in accounts:
        _upsert_legacy_value(conn, account["platform"], account["platform_user_id"], "name", profile.get("display_name"))
        _upsert_legacy_value(conn, account["platform"], account["platform_user_id"], "address", profile.get("address"))
        if profile.get("phone_e164") and profile.get("phone_verified_at"):
            _upsert_legacy_value(conn, account["platform"], account["platform_user_id"], "phone", mobile_e164_to_local(profile["phone_e164"]))


def get_profile(conn, loyalty_customer_id: int):
    return conn.execute(
        """SELECT lc.id,lc.display_name,lc.phone_e164,lc.phone_verified_at,lc.birth_date,
                  lc.address,lc.is_wholesaler,lc.merged_into_id,
                  EXISTS(SELECT 1 FROM wholesaler_members wm
                         WHERE wm.loyalty_customer_id=lc.id AND wm.active=TRUE) AS wholesaler_active,
                  (SELECT wr.status FROM wholesaler_requests wr WHERE wr.loyalty_customer_id=lc.id
                   ORDER BY wr.id DESC LIMIT 1) AS wholesaler_request_status
           FROM loyalty_customers lc WHERE lc.id=%s""",
        (int(loyalty_customer_id),),
    ).fetchone()


def get_profile_for_platform(conn, platform: str, platform_user_id, display_name=None):
    customer_id = ensure_platform_account(conn, platform, platform_user_id, display_name)
    return customer_id, get_profile(conn, customer_id)


def start_phone_verification(conn, platform: str, platform_user_id, phone: str, display_name=None):
    """Create OTP and enqueue the approved one-variable Melipayamak pattern 578916."""
    normalized = normalize_mobile_e164(phone)
    if not normalized:
        raise ValueError("شماره موبایل معتبر نیست؛ نمونه: 09123456789")
    customer_id = ensure_platform_account(conn, platform, platform_user_id, display_name)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 (f"phone-otp:{platform}:{platform_user_id}",))
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"customer-phone:{normalized}",))
    conn.execute("DELETE FROM customer_phone_verifications WHERE created_at<NOW()-INTERVAL '30 days' AND (consumed_at IS NOT NULL OR expires_at<NOW())")
    recent = conn.execute(
        """SELECT COUNT(*) AS n, MAX(created_at) AS last_sent
           FROM customer_phone_verifications
           WHERE platform=%s AND platform_user_id=%s AND created_at>NOW()-INTERVAL '1 hour'""",
        (platform, str(platform_user_id)),
    ).fetchone()
    if int(recent["n"] or 0) >= OTP_MAX_SENDS_PER_HOUR:
        raise ValueError("تعداد درخواست کد زیاد است. یک ساعت بعد دوباره تلاش کن.")
    if recent["last_sent"] and (datetime.now(timezone.utc) - recent["last_sent"].astimezone(timezone.utc)).total_seconds() < OTP_RESEND_WAIT_SECONDS:
        raise ValueError("برای ارسال کد جدید، حداقل یک دقیقه صبر کن.")
    phone_recent = conn.execute(
        """SELECT COUNT(*) AS n,MAX(created_at) AS last_sent FROM customer_phone_verifications
           WHERE phone_e164=%s AND created_at>NOW()-INTERVAL '1 hour'""",
        (normalized,),
    ).fetchone()
    if int(phone_recent["n"] or 0) >= OTP_MAX_SENDS_PER_HOUR:
        raise ValueError("برای این شماره تعداد کدهای درخواستی زیاد است. یک ساعت بعد دوباره تلاش کن.")
    if phone_recent["last_sent"] and (datetime.now(timezone.utc)-phone_recent["last_sent"].astimezone(timezone.utc)).total_seconds() < OTP_RESEND_WAIT_SECONDS:
        raise ValueError("برای ارسال کد به این شماره، حداقل یک دقیقه صبر کن.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(16)
    code_hash = _hash_otp(salt, code)
    conn.execute(
        """UPDATE customer_phone_verifications SET consumed_at=NOW()
           WHERE platform=%s AND platform_user_id=%s AND consumed_at IS NULL""",
        (platform, str(platform_user_id)),
    )
    row = conn.execute(
        """INSERT INTO customer_phone_verifications
           (platform,platform_user_id,loyalty_customer_id,phone_e164,code_hash,salt,
            attempts,created_at,expires_at)
           VALUES(%s,%s,%s,%s,%s,%s,0,NOW(),NOW()+INTERVAL '5 minutes') RETURNING id""",
        (platform, str(platform_user_id), customer_id, normalized, code_hash, salt),
    ).fetchone()
    ok = enqueue_pattern_sms(
        conn, phone=mobile_e164_to_local(normalized), template_id=TEMPLATE_IDS["phone_verification"],
        variables=[code], idempotency_key=f"phone-otp:{platform}:{platform_user_id}:{row['id']}",
        recipient_type="verification", loyalty_customer_id=customer_id,
        category="transactional",
    )
    if not ok:
        raise RuntimeError("ثبت پیامک تأیید در صف ارسال ناموفق بود. تنظیمات پیامک را بررسی کن.")
    return {"verification_id": int(row["id"]), "masked_phone": normalized[:5] + "*****" + normalized[-2:]}


def _merge_customer_profiles(conn, source_id: int, target_id: int):
    """Merge platform identities while retaining source row and its audit history."""
    if source_id == target_id:
        return
    ids = sorted((int(source_id), int(target_id)))
    conn.execute("SELECT id FROM loyalty_customers WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (ids,)).fetchall()
    source = conn.execute("SELECT * FROM loyalty_customers WHERE id=%s", (source_id,)).fetchone()
    target = conn.execute("SELECT * FROM loyalty_customers WHERE id=%s", (target_id,)).fetchone()
    if not source or not target:
        raise RuntimeError("پرونده مشتری برای اتصال پیدا نشد.")
    if source.get("phone_e164"):
        _archive_profile_value(conn, source_id, "phone", source.get("phone_e164"))
    # Preserve target data; fill only empty profile fields from source.
    conn.execute(
        """UPDATE loyalty_customers AS target SET
           display_name=CASE WHEN source.display_name IS NOT NULL AND
             (target.display_name IS NULL OR COALESCE(source.display_name_updated_at,'-infinity'::timestamptz) > COALESCE(target.display_name_updated_at,'-infinity'::timestamptz))
             THEN source.display_name ELSE target.display_name END,
           display_name_updated_at=NULLIF(GREATEST(COALESCE(target.display_name_updated_at,'-infinity'::timestamptz),COALESCE(source.display_name_updated_at,'-infinity'::timestamptz)),'-infinity'::timestamptz),
           address=CASE WHEN source.address IS NOT NULL AND
             (target.address IS NULL OR COALESCE(source.address_updated_at,'-infinity'::timestamptz) > COALESCE(target.address_updated_at,'-infinity'::timestamptz))
             THEN source.address ELSE target.address END,
           address_updated_at=NULLIF(GREATEST(COALESCE(target.address_updated_at,'-infinity'::timestamptz),COALESCE(source.address_updated_at,'-infinity'::timestamptz)),'-infinity'::timestamptz),
           birth_date=CASE WHEN source.birth_date IS NOT NULL AND
             (target.birth_date IS NULL OR COALESCE(source.birth_date_updated_at,'-infinity'::timestamptz) > COALESCE(target.birth_date_updated_at,'-infinity'::timestamptz))
             THEN source.birth_date ELSE target.birth_date END,
           birth_date_updated_at=NULLIF(GREATEST(COALESCE(target.birth_date_updated_at,'-infinity'::timestamptz),COALESCE(source.birth_date_updated_at,'-infinity'::timestamptz)),'-infinity'::timestamptz),
           is_wholesaler=(target.is_wholesaler OR source.is_wholesaler), updated_at=NOW()
           FROM loyalty_customers AS source WHERE target.id=%s AND source.id=%s""",
        (target_id, source_id),
    )

    # Merge wallet balances/ledger without deleting the old profile.
    source_wallet = conn.execute(
        "SELECT id,balance_toman FROM wallet_accounts WHERE loyalty_customer_id=%s FOR UPDATE",
        (source_id,),
    ).fetchone()
    target_wallet = conn.execute(
        "SELECT id,balance_toman FROM wallet_accounts WHERE loyalty_customer_id=%s FOR UPDATE",
        (target_id,),
    ).fetchone()
    if source_wallet and not target_wallet:
        conn.execute("UPDATE wallet_accounts SET loyalty_customer_id=%s WHERE id=%s", (target_id, source_wallet["id"]))
    elif source_wallet and target_wallet:
        # Move the old ledger to the canonical wallet, preserve the source wallet
        # row as a zero-balance audit shell, and carry its balance forward.
        conn.execute(
            "UPDATE wallet_transactions SET wallet_account_id=%s WHERE wallet_account_id=%s",
            (target_wallet["id"], source_wallet["id"]),
        )
        conn.execute(
            "UPDATE wallet_accounts SET balance_toman=COALESCE(balance_toman,0)+%s,updated_at=NOW() WHERE id=%s",
            (int(source_wallet.get("balance_toman") or 0), target_wallet["id"]),
        )
        conn.execute(
            "UPDATE wallet_accounts SET balance_toman=0,updated_at=NOW() WHERE id=%s",
            (source_wallet["id"],),
        )

    source_pref = conn.execute(
        "SELECT * FROM sms_customer_preferences WHERE loyalty_customer_id=%s FOR UPDATE", (source_id,)
    ).fetchone()
    target_pref = conn.execute(
        "SELECT * FROM sms_customer_preferences WHERE loyalty_customer_id=%s FOR UPDATE", (target_id,)
    ).fetchone()
    if source_pref and target_pref:
        conn.execute(
            """UPDATE sms_customer_preferences SET
               transactional_sms_enabled=(transactional_sms_enabled AND %s),
               marketing_enabled_by_admin=(marketing_enabled_by_admin AND %s),
               marketing_opted_out_at=COALESCE(marketing_opted_out_at,%s), updated_at=NOW()
               WHERE loyalty_customer_id=%s""",
            (bool(source_pref.get("transactional_sms_enabled", True)),
             bool(source_pref.get("marketing_enabled_by_admin", False)),
             source_pref.get("marketing_opted_out_at"), target_id),
        )
        conn.execute("DELETE FROM sms_customer_preferences WHERE loyalty_customer_id=%s", (source_id,))
    elif source_pref:
        conn.execute("UPDATE sms_customer_preferences SET loyalty_customer_id=%s WHERE loyalty_customer_id=%s", (target_id, source_id))

    # Repoint all known customer-owned records; keep the source loyalty row as a tombstone.
    for table in ("loyalty_order_links", "orders", "pending_payments", "sms_outbox", "customer_profile_history", "customer_phone_verifications", "wholesaler_requests"):
        conn.execute(f"UPDATE {table} SET loyalty_customer_id=%s WHERE loyalty_customer_id=%s", (target_id, source_id))
    member = conn.execute("SELECT id FROM wholesaler_members WHERE loyalty_customer_id=%s AND active=TRUE", (source_id,)).fetchone()
    target_member = conn.execute("SELECT id FROM wholesaler_members WHERE loyalty_customer_id=%s AND active=TRUE", (target_id,)).fetchone()
    if member and not target_member:
        conn.execute("UPDATE wholesaler_members SET loyalty_customer_id=%s,updated_at=NOW() WHERE id=%s", (target_id, member["id"]))
    elif member and target_member:
        # A duplicate legacy membership must not leave the old phone active.
        conn.execute("UPDATE wholesaler_members SET loyalty_customer_id=NULL,active=FALSE,updated_at=NOW() WHERE id=%s", (member["id"],))
    conn.execute(
        "UPDATE loyalty_customers SET is_wholesaler=EXISTS(SELECT 1 FROM wholesaler_members WHERE loyalty_customer_id=%s AND active=TRUE),updated_at=NOW() WHERE id=%s",
        (target_id, target_id),
    )
    conn.execute(
        "UPDATE customer_platform_accounts SET loyalty_customer_id=%s WHERE loyalty_customer_id=%s",
        (target_id, source_id),
    )
    conn.execute(
        "UPDATE loyalty_customers SET phone_e164=NULL,phone_verified_at=NULL,merged_into_id=%s,updated_at=NOW() WHERE id=%s",
        (target_id, source_id),
    )


def verify_phone_code(conn, platform: str, platform_user_id, code: str, display_name=None):
    code = str(code or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).strip()
    if not re.fullmatch(r"\d{6}", code):
        return {"status": "invalid_format"}
    row = conn.execute(
        """SELECT * FROM customer_phone_verifications
           WHERE platform=%s AND platform_user_id=%s AND consumed_at IS NULL
           ORDER BY id DESC LIMIT 1 FOR UPDATE""",
        (platform, str(platform_user_id)),
    ).fetchone()
    if not row:
        return {"status": "not_found"}
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"customer-phone:{row['phone_e164']}",))
    if row["expires_at"] <= datetime.now(timezone.utc):
        conn.execute("UPDATE customer_phone_verifications SET consumed_at=NOW() WHERE id=%s", (row["id"],))
        return {"status": "expired"}
    if int(row["attempts"] or 0) >= OTP_MAX_ATTEMPTS:
        conn.execute("UPDATE customer_phone_verifications SET consumed_at=NOW() WHERE id=%s", (row["id"],))
        return {"status": "locked"}
    if not hmac.compare_digest(str(row["code_hash"]), _hash_otp(str(row["salt"]), code)):
        conn.execute("UPDATE customer_phone_verifications SET attempts=attempts+1 WHERE id=%s", (row["id"],))
        return {"status": "wrong"}

    current = ensure_platform_account(conn, platform, platform_user_id, display_name)
    existing = conn.execute(
        """SELECT id FROM loyalty_customers WHERE phone_e164=%s AND phone_verified_at IS NOT NULL
           AND merged_into_id IS NULL FOR UPDATE""",
        (row["phone_e164"],),
    ).fetchone()
    canonical = int(existing["id"]) if existing else int(current)
    if current != canonical:
        _merge_customer_profiles(conn, current, canonical)
    current_phone = conn.execute("SELECT phone_e164 FROM loyalty_customers WHERE id=%s FOR UPDATE", (canonical,)).fetchone()
    if current_phone and current_phone.get("phone_e164") and current_phone["phone_e164"] != row["phone_e164"]:
        _archive_profile_value(conn, canonical, "phone", current_phone["phone_e164"])
    conn.execute(
        """UPDATE loyalty_customers SET phone_e164=%s,phone_verified_at=NOW(),updated_at=NOW()
           WHERE id=%s""",
        (row["phone_e164"], canonical),
    )
    conn.execute(
        """UPDATE customer_phone_verifications SET consumed_at=NOW(),verified_at=NOW()
           WHERE id=%s""",
        (row["id"],),
    )
    # Keep a manager-added membership attached if this verified customer changes
    # their phone number; the phone remains the one and only membership key.
    current_member = conn.execute(
        "SELECT id,phone_e164 FROM wholesaler_members WHERE loyalty_customer_id=%s AND active=TRUE FOR UPDATE",
        (canonical,),
    ).fetchone()
    new_phone_member = conn.execute(
        "SELECT id,loyalty_customer_id FROM wholesaler_members WHERE phone_e164=%s AND active=TRUE FOR UPDATE",
        (row["phone_e164"],),
    ).fetchone()
    if current_member and current_member["phone_e164"] != row["phone_e164"]:
        if new_phone_member and int(new_phone_member["id"]) != int(current_member["id"]):
            conn.execute("UPDATE wholesaler_members SET active=FALSE,updated_at=NOW() WHERE id=%s", (current_member["id"],))
            conn.execute("UPDATE wholesaler_members SET loyalty_customer_id=%s,active=TRUE,updated_at=NOW() WHERE id=%s", (canonical, new_phone_member["id"]))
        else:
            conn.execute("UPDATE wholesaler_members SET phone_e164=%s,updated_at=NOW() WHERE id=%s", (row["phone_e164"], current_member["id"]))
    elif new_phone_member and (new_phone_member.get("loyalty_customer_id") in (None, canonical)):
        conn.execute("UPDATE wholesaler_members SET loyalty_customer_id=%s,updated_at=NOW() WHERE id=%s", (canonical, new_phone_member["id"]))
    conn.execute("UPDATE loyalty_customers SET is_wholesaler=EXISTS(SELECT 1 FROM wholesaler_members WHERE loyalty_customer_id=%s AND active=TRUE),updated_at=NOW() WHERE id=%s",
                 (canonical, canonical))
    conn.execute("UPDATE wholesaler_requests SET phone_e164=%s WHERE loyalty_customer_id=%s AND status='pending'",
                 (row["phone_e164"], canonical))
    _sync_legacy_values(conn, canonical)
    profile = get_profile(conn, canonical)
    return {"status": "verified", "loyalty_customer_id": canonical, "profile": profile}


def _archive_profile_value(conn, loyalty_customer_id: int, data_type: str, old_value):
    if old_value in (None, ""):
        return
    conn.execute(
        """INSERT INTO customer_profile_history
           (loyalty_customer_id,legacy_platform,data_type,value,archived_at)
           VALUES(%s,'profile',%s,%s,NOW())""",
        (int(loyalty_customer_id), data_type, str(old_value)[:2000]),
    )


def admin_update_wholesaler_profile(conn, member_id: int, field: str, value: str):
    if field not in ("name", "address"):
        raise ValueError("فیلد ویرایش نامعتبر است.")
    member = conn.execute("SELECT loyalty_customer_id FROM wholesaler_members WHERE id=%s AND active=TRUE FOR UPDATE", (int(member_id),)).fetchone()
    if not member or not member.get("loyalty_customer_id"):
        raise ValueError("این شماره هنوز به پروفایل تأییدشده مشتری متصل نشده است.")
    customer_id = int(member["loyalty_customer_id"])
    profile = conn.execute("SELECT display_name,address FROM loyalty_customers WHERE id=%s FOR UPDATE", (customer_id,)).fetchone()
    if field == "name":
        clean = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(clean.split()) < 2 or len(clean) < 5:
            raise ValueError("نام و نام خانوادگی را کامل وارد کن.")
        if profile.get("display_name") != clean[:250]:
            _archive_profile_value(conn, customer_id, "name", profile.get("display_name"))
        conn.execute("UPDATE loyalty_customers SET display_name=%s,display_name_updated_at=NOW(),updated_at=NOW() WHERE id=%s", (clean[:250], customer_id))
    else:
        clean = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(clean) < 10:
            raise ValueError("آدرس باید حداقل ۱۰ نویسه باشد.")
        if profile.get("address") != clean[:2000]:
            _archive_profile_value(conn, customer_id, "address", profile.get("address"))
        conn.execute("UPDATE loyalty_customers SET address=%s,address_updated_at=NOW(),updated_at=NOW() WHERE id=%s", (clean[:2000], customer_id))
    _sync_legacy_values(conn, customer_id)
    return customer_id

def save_profile_field(conn, platform: str, platform_user_id, field: str, value, display_name=None):
    allowed = {"name", "address", "birth_date"}
    if field not in allowed:
        raise ValueError("فیلد پروفایل نامعتبر است.")
    customer_id = ensure_platform_account(conn, platform, platform_user_id, display_name)
    current_profile = conn.execute("SELECT display_name,address,birth_date,phone_e164 FROM loyalty_customers WHERE id=%s FOR UPDATE", (customer_id,)).fetchone()
    if field == "name":
        clean = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(clean.split()) < 2 or len(clean) < 5:
            raise ValueError("نام و نام خانوادگی را کامل وارد کن.")
        if current_profile.get("display_name") != clean[:250]:
            _archive_profile_value(conn, customer_id, "name", current_profile.get("display_name"))
        conn.execute("UPDATE loyalty_customers SET display_name=%s,display_name_updated_at=NOW(),updated_at=NOW() WHERE id=%s", (clean[:250], customer_id))
    elif field == "address":
        clean = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(clean) < 10:
            raise ValueError("آدرس باید حداقل ۱۰ نویسه باشد.")
        if current_profile.get("address") != clean[:2000]:
            _archive_profile_value(conn, customer_id, "address", current_profile.get("address"))
        conn.execute("UPDATE loyalty_customers SET address=%s,address_updated_at=NOW(),updated_at=NOW() WHERE id=%s", (clean[:2000], customer_id))
    else:
        birth = value if isinstance(value, date) else parse_jalali_date(str(value))
        if not birth:
            raise ValueError("تاریخ تولد معتبر نیست. یکی از این دو قالب را بفرست: ۱۳۷۵/۰۶/۲۰ یا ۲۰/۰۶/۱۳۷۵ (سال/ماه/روز یا روز/ماه/سال).")
        if current_profile.get("birth_date") != birth:
            _archive_profile_value(conn, customer_id, "birth_date", current_profile.get("birth_date"))
        conn.execute("UPDATE loyalty_customers SET birth_date=%s,birth_date_updated_at=NOW(),updated_at=NOW() WHERE id=%s", (birth, customer_id))
    _sync_legacy_values(conn, customer_id)
    return customer_id


def request_wholesaler(conn, loyalty_customer_id: int):
    profile = get_profile(conn, loyalty_customer_id)
    if not profile or not profile.get("phone_verified_at") or not profile.get("phone_e164"):
        return "phone_unverified"
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"wholesaler-request:{int(loyalty_customer_id)}",))
    if profile.get("wholesaler_active"):
        return "already_active"
    row = conn.execute(
        """SELECT id FROM wholesaler_requests WHERE loyalty_customer_id=%s AND status='pending'
           ORDER BY id DESC LIMIT 1""",
        (int(loyalty_customer_id),),
    ).fetchone()
    if row:
        return "pending"
    created = conn.execute(
        """INSERT INTO wholesaler_requests(loyalty_customer_id,phone_e164,status,created_at)
           VALUES(%s,%s,'pending',NOW()) ON CONFLICT DO NOTHING RETURNING id""",
        (int(loyalty_customer_id), profile["phone_e164"]),
    ).fetchone()
    return "submitted" if created else "pending"


def _queue_welcome_sms(conn, phone_e164: str, loyalty_customer_id=None, idempotency_key=None):
    phone = mobile_e164_to_local(phone_e164)
    message = ("عضویت شما در جمع بنکداران تنین ایران فعال شد. از این پس با ورود به ربات تلگرام یا بله "
               "و تأیید همین شماره موبایل، قیمت‌های مخصوص بنکداران را مشاهده می‌کنید.")
    with conn.transaction():
        conn.execute(
            """INSERT INTO sms_outbox
               (loyalty_customer_id,message_category,destination_phone,message_text,status,
                idempotency_key,recipient_type,scheduled_at)
               VALUES(%s,'transactional',%s,%s,'queued',%s,'order_customer',NOW())
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (loyalty_customer_id, phone, message, idempotency_key or f"wholesaler-welcome:{phone_e164}"),
        )


def approve_wholesaler_request(conn, request_id: int, admin_id):
    request = conn.execute(
        "SELECT * FROM wholesaler_requests WHERE id=%s FOR UPDATE", (int(request_id),)
    ).fetchone()
    if not request or request["status"] != "pending":
        return False
    profile = get_profile(conn, int(request["loyalty_customer_id"]))
    if not profile or not profile.get("phone_verified_at"):
        raise ValueError("شماره مشتری دیگر تأییدشده نیست؛ ابتدا تأیید شماره را بررسی کن.")
    member_before = conn.execute("SELECT active FROM wholesaler_members WHERE phone_e164=%s FOR UPDATE", (request["phone_e164"],)).fetchone()
    conn.execute(
        """INSERT INTO wholesaler_members(phone_e164,loyalty_customer_id,active,added_by,created_at,updated_at)
           VALUES(%s,%s,TRUE,%s,NOW(),NOW())
           ON CONFLICT(phone_e164) DO UPDATE SET loyalty_customer_id=EXCLUDED.loyalty_customer_id,
             active=TRUE,updated_at=NOW()""",
        (request["phone_e164"], request["loyalty_customer_id"], str(admin_id)),
    )
    conn.execute("UPDATE loyalty_customers SET is_wholesaler=TRUE,updated_at=NOW() WHERE id=%s", (request["loyalty_customer_id"],))
    conn.execute(
        "UPDATE wholesaler_requests SET status='approved',reviewed_at=NOW(),reviewed_by=%s WHERE id=%s",
        (str(admin_id), int(request_id)),
    )
    if not member_before or not member_before.get("active"):
        _queue_welcome_sms(conn, request["phone_e164"], int(request["loyalty_customer_id"]), f"wholesaler-welcome-request:{request_id}")
    return True


def reject_wholesaler_request(conn, request_id: int, admin_id):
    cur = conn.execute(
        """UPDATE wholesaler_requests SET status='rejected',reviewed_at=NOW(),reviewed_by=%s
           WHERE id=%s AND status='pending' RETURNING id""",
        (str(admin_id), int(request_id)),
    ).fetchone()
    return bool(cur)


def add_wholesaler_by_phone(conn, phone: str, admin_id):
    normalized = normalize_mobile_e164(phone)
    if not normalized:
        raise ValueError("شماره موبایل معتبر نیست؛ نمونه: 09123456789")
    profile = conn.execute(
        "SELECT id FROM loyalty_customers WHERE phone_e164=%s AND phone_verified_at IS NOT NULL AND merged_into_id IS NULL",
        (normalized,),
    ).fetchone()
    customer_id = int(profile["id"]) if profile else None
    old = conn.execute("SELECT id,loyalty_customer_id,active FROM wholesaler_members WHERE phone_e164=%s FOR UPDATE", (normalized,)).fetchone()
    conn.execute(
        """INSERT INTO wholesaler_members(phone_e164,loyalty_customer_id,active,added_by,created_at,updated_at)
           VALUES(%s,%s,TRUE,%s,NOW(),NOW())
           ON CONFLICT(phone_e164) DO UPDATE SET loyalty_customer_id=EXCLUDED.loyalty_customer_id,
             active=TRUE,added_by=EXCLUDED.added_by,updated_at=NOW()""",
        (normalized, customer_id, str(admin_id)),
    )
    if old and old.get("loyalty_customer_id") and int(old["loyalty_customer_id"]) != (customer_id or -1):
        old_id=int(old["loyalty_customer_id"])
        conn.execute("UPDATE loyalty_customers SET is_wholesaler=EXISTS(SELECT 1 FROM wholesaler_members WHERE loyalty_customer_id=%s AND active=TRUE),updated_at=NOW() WHERE id=%s",(old_id,old_id))
    if customer_id:
        conn.execute("UPDATE loyalty_customers SET is_wholesaler=TRUE,updated_at=NOW() WHERE id=%s", (customer_id,))
        conn.execute("UPDATE wholesaler_requests SET status='approved',reviewed_at=NOW(),reviewed_by=%s WHERE loyalty_customer_id=%s AND status='pending'",
                     (str(admin_id), customer_id))
    if not old or not old.get("active"):
        _queue_welcome_sms(conn, normalized, customer_id, f"wholesaler-welcome-admin:{normalized}:{secrets.token_hex(4)}")
    return normalized


def remove_wholesaler(conn, member_id: int):
    row = conn.execute("UPDATE wholesaler_members SET active=FALSE,updated_at=NOW() WHERE id=%s RETURNING loyalty_customer_id", (int(member_id),)).fetchone()
    if row and row["loyalty_customer_id"]:
        conn.execute(
            """UPDATE loyalty_customers SET is_wholesaler=FALSE,updated_at=NOW()
               WHERE id=%s AND NOT EXISTS(SELECT 1 FROM wholesaler_members WHERE loyalty_customer_id=%s AND active=TRUE)""",
            (row["loyalty_customer_id"], row["loyalty_customer_id"]),
        )
    return bool(row)


def update_wholesaler_phone(conn, member_id: int, phone: str):
    normalized = normalize_mobile_e164(phone)
    if not normalized:
        raise ValueError("شماره موبایل معتبر نیست.")
    old = conn.execute("SELECT loyalty_customer_id FROM wholesaler_members WHERE id=%s FOR UPDATE", (int(member_id),)).fetchone()
    if not old:
        raise ValueError("بنکدار پیدا نشد.")
    profile = conn.execute(
        "SELECT id FROM loyalty_customers WHERE phone_e164=%s AND phone_verified_at IS NOT NULL AND merged_into_id IS NULL",
        (normalized,),
    ).fetchone()
    new_customer_id = int(profile["id"]) if profile else None
    conn.execute(
        "UPDATE wholesaler_members SET phone_e164=%s,loyalty_customer_id=%s,updated_at=NOW() WHERE id=%s",
        (normalized, new_customer_id, int(member_id)),
    )
    if old and old.get("loyalty_customer_id") and int(old["loyalty_customer_id"]) != (new_customer_id or -1):
        old_id=int(old["loyalty_customer_id"])
        conn.execute("UPDATE loyalty_customers SET is_wholesaler=EXISTS(SELECT 1 FROM wholesaler_members WHERE loyalty_customer_id=%s AND active=TRUE),updated_at=NOW() WHERE id=%s",(old_id,old_id))
    if new_customer_id:
        conn.execute("UPDATE loyalty_customers SET is_wholesaler=TRUE,updated_at=NOW() WHERE id=%s",(new_customer_id,))
    return normalized


def parse_jalali_date(value: str, allow_future: bool = False) -> Optional[date]:
    """Parse Jalali date in either YYYY/MM/DD or DD/MM/YYYY order.

    Persian and Arabic-Indic digits are accepted. Separators may be /, - or .
    The four-digit year determines the order, so the two formats are unambiguous.
    Returns the corresponding Gregorian ``date`` for PostgreSQL DATE storage.
    """
    text = str(value or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    text = text.replace("\u200c", "").replace("\u200f", "").replace("\ufeff", "").strip()
    match = re.fullmatch(r"(\d{1,4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,4})", text)
    if not match:
        return None
    first, second, third = match.groups()
    if len(first) == 4:
        jy, jm, jd = int(first), int(second), int(third)
    elif len(third) == 4:
        jd, jm, jy = int(first), int(second), int(third)
    else:
        return None
    if not (1200 <= jy <= 1500 and 1 <= jm <= 12 and 1 <= jd <= (31 if jm <= 6 else 30)):
        return None
    try:
        jy2 = jy + 1595
        days = -355668 + 365 * jy2 + (jy2 // 33) * 8 + ((jy2 % 33 + 3) // 4) + jd
        if jm < 7:
            days += (jm - 1) * 31
        else:
            days += (jm - 7) * 30 + 186
        gy = 400 * (days // 146097)
        days %= 146097
        if days > 36524:
            gy += 100 * ((days - 1) // 36524)
            days = (days - 1) % 36524
            if days >= 365:
                days += 1
        gy += 4 * (days // 1461)
        days %= 1461
        if days > 365:
            gy += (days - 1) // 365
            days = (days - 1) % 365
        gd = days + 1
        month_days = [31, 29 if (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0 else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        gm = 1
        while gm <= 12 and gd > month_days[gm - 1]:
            gd -= month_days[gm - 1]
            gm += 1
        result = date(gy, gm, gd)
        if gregorian_to_jalali(result) != f"{jy:04d}/{jm:02d}/{jd:02d}":
            return None
        if not allow_future and result > date.today():
            return None
        return result
    except (ValueError, OverflowError):
        return None


def gregorian_to_jalali(gdate: date) -> str:
    gy, gm, gd = gdate.year, gdate.month, gdate.day
    g_days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    j_days = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
    gy2, gm2, gd2 = gy - 1600, gm - 1, gd - 1
    gdn = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    for i in range(gm2):
        gdn += g_days[i]
    if gm2 > 1 and ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0):
        gdn += 1
    gdn += gd2
    jdn = gdn - 79
    jnp = jdn // 12053
    jdn %= 12053
    jy = 979 + 33 * jnp + 4 * (jdn // 1461)
    jdn %= 1461
    if jdn >= 366:
        jy += (jdn - 1) // 365
        jdn = (jdn - 1) % 365
    for i in range(11):
        if jdn < j_days[i]:
            return f"{jy:04d}/{i+1:02d}/{jdn+1:02d}"
        jdn -= j_days[i]
    return f"{jy:04d}/12/{jdn+1:02d}"


def customer_is_wholesaler(conn, platform: str, platform_user_id) -> bool:
    row = conn.execute(
        """SELECT EXISTS(
             SELECT 1 FROM customer_platform_accounts cpa
             JOIN wholesaler_members wm ON wm.loyalty_customer_id=cpa.loyalty_customer_id
             WHERE cpa.platform=%s AND cpa.platform_user_id=%s AND wm.active=TRUE
           ) AS active""",
        (platform, str(platform_user_id)),
    ).fetchone()
    return bool(row and row["active"])


def queue_delivery_sms(conn, order, delivery_date_text: str):
    phone = normalize_mobile_e164(order.get("phone"))
    if not phone:
        return False
    message = f"تاریخ تحویل سفارش {order.get('display_order_number') or ('#'+str(order['id']))} برای شما {delivery_date_text} تعیین شد."
    with conn.transaction():
        conn.execute(
            """INSERT INTO sms_outbox(loyalty_customer_id,message_category,destination_phone,message_text,
               status,idempotency_key,recipient_type,scheduled_at)
               VALUES(%s,'transactional',%s,%s,'queued',%s,'order_customer',NOW())
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (order.get("loyalty_customer_id"), mobile_e164_to_local(phone), message,
             f"delivery-date:{order['id']}:{delivery_date_text}"),
        )
    return True
