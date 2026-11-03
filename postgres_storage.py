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

