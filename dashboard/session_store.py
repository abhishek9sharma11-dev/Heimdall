"""Durable worker registration metadata backed by the configured Postgres DB.

The filesystem manifest remains a compatibility/runtime mirror for the existing
runner.  This store is deliberately separate from payment tables.
"""
from __future__ import annotations

import json
import os
from typing import Any


TABLE = "heimdall_session_registrations"


def _dsn() -> str:
    value = (os.environ.get("DATABASE_URL") or "").strip()
    if value.startswith("postgres://"):
        return "postgresql://" + value[len("postgres://") :]
    return value


def configured() -> bool:
    return bool(_dsn())


def _connect():
    import psycopg

    return psycopg.connect(_dsn(), connect_timeout=8)


def ensure_schema() -> None:
    if not configured():
        return
    with _connect() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                session_id TEXT PRIMARY KEY,
                payload JSONB NOT NULL,
                state TEXT NOT NULL DEFAULT 'registered',
                claim_owner TEXT,
                claimed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


def upsert(record: dict[str, Any]) -> None:
    """Persist one registration. Values are never printed by this module."""
    if not configured():
        return
    ensure_schema()
    session_id = str(record["session"]["id"])
    payload = json.dumps(record)
    with _connect() as conn:
        conn.execute(
            f"""
            INSERT INTO {TABLE} (session_id, payload, state, updated_at)
            VALUES (%s, %s::jsonb, 'registered', now())
            ON CONFLICT (session_id) DO UPDATE SET
                payload = EXCLUDED.payload,
                updated_at = now(),
                state = CASE WHEN {TABLE}.state = 'running'
                             THEN {TABLE}.state ELSE 'registered' END
            """,
            (session_id, payload),
        )


def list_records() -> list[dict[str, Any]]:
    if not configured():
        return []
    ensure_schema()
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT payload FROM {TABLE} WHERE state <> 'completed' ORDER BY updated_at"
        ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, dict):
            records.append(payload)
    return records


def claim(session_id: str, owner: str) -> bool:
    if not configured():
        return True
    ensure_schema()
    with _connect() as conn:
        row = conn.execute(
            f"""
            UPDATE {TABLE}
               SET state='claimed', claim_owner=%s, claimed_at=now(), updated_at=now()
             WHERE session_id=%s AND state='registered'
             RETURNING session_id
            """,
            (owner, str(session_id)),
        ).fetchone()
    return row is not None


def mark_running(session_id: str, owner: str) -> None:
    if not configured():
        return
    ensure_schema()
    with _connect() as conn:
        conn.execute(
            f"UPDATE {TABLE} SET state='running', claim_owner=%s, updated_at=now() "
            "WHERE session_id=%s AND claim_owner=%s",
            (owner, str(session_id), owner),
        )


def release_claim(session_id: str, owner: str) -> None:
    if not configured():
        return
    ensure_schema()
    with _connect() as conn:
        conn.execute(
            f"UPDATE {TABLE} SET state='registered', claim_owner=NULL, claimed_at=NULL, updated_at=now() "
            "WHERE session_id=%s AND claim_owner=%s AND state='claimed'",
            (str(session_id), owner),
        )
