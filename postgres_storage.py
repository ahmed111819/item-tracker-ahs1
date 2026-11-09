"""Small PostgreSQL persistence adapter for the Amazon coupon scanner.

The tracking snapshot is gzip-compressed and upserted into one row per bot.
This module never creates a dated snapshot or silently initializes an absent
tracking history. The SQL schema must be installed first.
"""

from __future__ import annotations

import gzip
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is missing from the runtime environment.")
    if url.startswith(("postgres://", "postgresql://")) and "sslmode=" not in url:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"
    return url


def _connect():
    return psycopg.connect(_database_url(), connect_timeout=15, row_factory=dict_row)


def load_state(bot_id: str) -> dict[str, Any]:
    """Load existing bot state; fail closed if it has not been imported."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload_gzip FROM public.bot_state WHERE bot_id = %s",
            (bot_id,),
        ).fetchone()

    if row is None:
        raise RuntimeError(
            f"No saved tracking state exists for {bot_id}. Import the existing "
            "tracking JSON before enabling scans; refusing to start with an empty history."
        )

    try:
        value = json.loads(gzip.decompress(bytes(row["payload_gzip"])).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Saved PostgreSQL state for {bot_id} is unreadable.") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Saved PostgreSQL state for {bot_id} is not a JSON object.")
    value.setdefault("products", {})
    value.setdefault("scan_state", {})
    return value


def save_state(bot_id: str, payload: dict[str, Any]) -> int:
    """Overwrite the single state row and return compressed byte count."""
    packed = gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=6,
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO public.bot_state (bot_id, payload_gzip, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (bot_id) DO UPDATE
                SET payload_gzip = EXCLUDED.payload_gzip,
                    updated_at = EXCLUDED.updated_at
            """,
            (bot_id, packed),
        )
    return len(packed)


def reserve_alert(
    alert_key: str,
    bot_id: str,
    token: str | None = None,
    ttl_seconds: int = 1800,
) -> str | None:
    """Atomically reserve an alert; return its token, or None if already sent."""
    token = token or str(uuid.uuid4())
    with _connect() as conn:
        row = conn.execute(
            "SELECT public.reserve_alert(%s, %s, %s, %s, %s) AS reserved",
            (alert_key, bot_id, token, datetime.now(timezone.utc), ttl_seconds),
        ).fetchone()
    return token if row and row["reserved"] else None


def finish_alert(alert_key: str, bot_id: str, token: str, success: bool) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT public.finish_alert(%s, %s, %s, %s, %s) AS finished",
            (alert_key, bot_id, token, success, datetime.now(timezone.utc)),
        ).fetchone()
    return bool(row and row["finished"])



def assert_product_blocks_ready() -> None:
    """Fail with an actionable message until the one-time block migration is applied."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT to_regclass('public.product_blocks') AS blocks, "
            "to_regclass('public.telegram_callback_state') AS callbacks"
        ).fetchone()
    if not row or not row["blocks"] or not row["callbacks"]:
        raise RuntimeError(
            "Temporary product-block tables are missing. Run PRODUCT_BLOCKS_SETUP.sql "
            "on coupon_storage as the database owner, then retry."
        )


def load_product_blocks() -> dict[str, dict[str, Any]]:
    """Load active/releasing blocks once per scanner process."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT asin, mode, blocked_price, resume_price, created_at, status, "
            "message_chat_id, message_id FROM public.product_blocks "
            "WHERE status IN ('active', 'release_pending', 'released')"
        ).fetchall()
    return {str(row["asin"]): dict(row) for row in rows}


def check_product_block(asin: str, current_price: float | None) -> dict[str, Any]:
    """Suppress alerts while blocked and atomically claim a threshold release."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT asin, mode, blocked_price, resume_price, created_at, status, "
            "message_chat_id, message_id FROM public.product_blocks "
            "WHERE asin = %s FOR UPDATE",
            (asin,),
        ).fetchone()
        if row is None:
            return {"suppress_alert": False, "released": False}

        block = dict(row)
        if block["status"] != "active":
            return {"suppress_alert": True, "released": False, "block": block}
        if current_price is None:
            return {"suppress_alert": True, "released": False, "block": block}

        if float(current_price) <= float(block["resume_price"]):
            claimed = conn.execute(
                "UPDATE public.product_blocks SET status = 'release_pending', updated_at = now() "
                "WHERE asin = %s AND status = 'active' RETURNING asin",
                (asin,),
            ).fetchone()
            if claimed:
                return {"suppress_alert": False, "released": True, "block": block}

        return {"suppress_alert": True, "released": False, "block": block}


def finish_product_block_release(
    asin: str, created_at: Any, success: bool
) -> None:
    """Keep a released row until this scan ends so parallel bots suppress duplicates."""
    with _connect() as conn:
        if success:
            conn.execute(
                "UPDATE public.product_blocks SET status = 'released', updated_at = now() "
                "WHERE asin = %s AND created_at = %s AND status = 'release_pending'",
                (asin, created_at),
            )
        else:
            conn.execute(
                "UPDATE public.product_blocks SET status = 'active', updated_at = now() "
                "WHERE asin = %s AND created_at = %s AND status = 'release_pending'",
                (asin, created_at),
            )


def clear_released_product_blocks() -> int:
    """Remove auto-released rows after all three scanner processes have finished."""
    with _connect() as conn:
        row = conn.execute(
            "WITH removed AS (DELETE FROM public.product_blocks "
            "WHERE status = 'released' RETURNING 1) SELECT count(*) AS count FROM removed"
        ).fetchone()
    return int(row["count"])



def recover_pending_product_blocks() -> int:
    """Re-arm release claims left behind by a cancelled previous workflow run."""
    with _connect() as conn:
        row = conn.execute(
            "WITH recovered AS (UPDATE public.product_blocks "
            "SET status = 'active', updated_at = now() "
            "WHERE status = 'release_pending' RETURNING 1) "
            "SELECT count(*) AS count FROM recovered"
        ).fetchone()
    return int(row["count"])


def get_latest_known_product_price(asin: str) -> float | None:
    """Read the newest stored observation from the three compressed bot snapshots."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT payload_gzip FROM public.bot_state "
            "WHERE bot_id IN ('bot1', 'bot2', 'bot3')"
        ).fetchall()

    best_seen = ""
    best_price: float | None = None
    for row in rows:
        try:
            payload = json.loads(gzip.decompress(bytes(row["payload_gzip"])).decode("utf-8"))
            record = payload.get("products", {}).get(asin, {})
            value = record.get("current_price")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                continue
            seen = str(record.get("last_seen") or "")
            if best_price is None or seen >= best_seen:
                best_seen, best_price = seen, float(value)
        except Exception:
            continue
    return best_price


def create_or_get_product_block(
    asin: str,
    blocked_price: float,
    updated_by: str,
    message_chat_id: str,
    message_id: int,
) -> dict[str, Any]:
    """Create a 5% temporary block; clicking again never resets an active threshold."""
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO public.product_blocks
                (asin, mode, blocked_price, resume_price, status, updated_by,
                 message_chat_id, message_id, created_at, updated_at)
            VALUES (%s, 'temporary', %s, %s, 'active', %s, %s, %s, now(), now())
            ON CONFLICT (asin) DO UPDATE SET
                blocked_price = CASE WHEN product_blocks.status = 'released'
                                     THEN EXCLUDED.blocked_price
                                     ELSE product_blocks.blocked_price END,
                resume_price = CASE WHEN product_blocks.status = 'released'
                                    THEN EXCLUDED.resume_price
                                    ELSE product_blocks.resume_price END,
                mode = 'temporary',
                status = CASE WHEN product_blocks.status = 'released'
                              THEN 'active' ELSE product_blocks.status END,
                updated_by = EXCLUDED.updated_by,
                message_chat_id = EXCLUDED.message_chat_id,
                message_id = EXCLUDED.message_id,
                created_at = CASE WHEN product_blocks.status = 'released'
                                  THEN now() ELSE product_blocks.created_at END,
                updated_at = now()
            RETURNING asin, mode, blocked_price, resume_price, created_at, status,
                      message_chat_id, message_id
            """,
            (asin, blocked_price, blocked_price * 0.95, updated_by, message_chat_id, message_id),
        ).fetchone()
    return dict(row)


def get_product_block(asin: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT asin, mode, blocked_price, resume_price, created_at, status "
            "FROM public.product_blocks WHERE asin = %s",
            (asin,),
        ).fetchone()
    return dict(row) if row else None


def remove_product_block(asin: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "DELETE FROM public.product_blocks WHERE asin = %s RETURNING asin",
            (asin,),
        ).fetchone()
    return bool(row)


def get_telegram_update_offset() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT next_update_id FROM public.telegram_callback_state "
            "WHERE state_key = 'main'"
        ).fetchone()
    return int(row["next_update_id"]) if row else 0


def save_telegram_update_offset(next_update_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO public.telegram_callback_state (state_key, next_update_id, updated_at)
            VALUES ('main', %s, now())
            ON CONFLICT (state_key) DO UPDATE
               SET next_update_id = GREATEST(
                       public.telegram_callback_state.next_update_id,
                       EXCLUDED.next_update_id
                   ),
                   updated_at = now()
            """,
            (int(next_update_id),),
        )


def alert_was_sent(alert_key: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM public.alert_registry WHERE alert_key = %s AND status = 'sent'",
            (alert_key,),
        ).fetchone()
    return bool(row)


def import_registry_file(path: str | Path, bot_id: str = "bot1") -> int:
    """One-time import of the legacy amazon_deals_sent_registry.json file.

    Existing keys are preserved: importing never overwrites a key already in
    PostgreSQL. Only records marked `sent` or `reserved` are imported.
    """
    source = Path(path)
    try:
        legacy = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not read registry JSON at {source}.") from exc
    alerts = legacy.get("alerts") if isinstance(legacy, dict) else None
    if not isinstance(alerts, dict):
        raise RuntimeError("Registry JSON must contain an alerts object.")

    rows: list[tuple[Any, ...]] = []
    for key, record in alerts.items():
        if not isinstance(record, dict) or record.get("status") not in {"sent", "reserved"}:
            continue
        status = record["status"]
        sent_at = (record.get("sent_at") or datetime.now(timezone.utc)) if status == "sent" else None
        reserved_at = record.get("reserved_at") if status == "reserved" else None
        details = {
            k: v for k, v in record.items()
            if k not in {"status", "sent_at", "reserved_at", "sent_by", "reserved_by"}
        }
        if record.get("sent_by"):
            row_bot = record["sent_by"]
        elif record.get("reserved_by"):
            row_bot = record["reserved_by"]
        else:
            row_bot = bot_id
        rows.append((
            key, status, row_bot, reserved_at, sent_at,
            json.dumps(details, ensure_ascii=False),
            record.get("sent_at") or record.get("reserved_at") or datetime.now(timezone.utc),
        ))

    with _connect() as conn:
        with conn.cursor() as cursor:
            for offset in range(0, len(rows), 500):
                cursor.executemany(
                    """
                    INSERT INTO public.alert_registry
                        (alert_key, status, bot_id, reserved_at, sent_at, details, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (alert_key) DO NOTHING
                    """,
                    rows[offset : offset + 500],
                )
        conn.execute(
            """
            INSERT INTO public.storage_migrations (migration_key)
            VALUES ('legacy_registry_imported')
            ON CONFLICT (migration_key) DO NOTHING
            """
        )
    return len(rows)


def assert_storage_ready(bot_id: str) -> None:
    """Require both imported tracking state and the one-time registry import."""
    with _connect() as conn:
        state = conn.execute(
            "SELECT 1 FROM public.bot_state WHERE bot_id = %s", (bot_id,)
        ).fetchone()
        registry = conn.execute(
            "SELECT 1 FROM public.storage_migrations "
            "WHERE migration_key = 'legacy_registry_imported'"
        ).fetchone()
    if state is None:
        raise RuntimeError(
            f"No existing tracking state is loaded for {bot_id}; refusing to scan.")
    if registry is None:
        raise RuntimeError(
            "The legacy Telegram registry has not been imported; refusing to scan.")
    assert_product_blocks_ready()


def import_tracking_file(path: str | Path, bot_id: str = "bot1") -> int:
    """Import one existing bot tracking JSON as the initial state row."""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not read tracking JSON at {source}.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), dict):
        raise RuntimeError("Tracking JSON must be an object containing a products object.")

    packed = gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=6,
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO public.bot_state (bot_id, payload_gzip, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (bot_id) DO NOTHING
            """,
            (bot_id, packed),
        )
        row = conn.execute(
            "SELECT octet_length(payload_gzip) AS size FROM public.bot_state WHERE bot_id=%s",
            (bot_id,),
        ).fetchone()
    return int(row["size"])

