"""Live payment totals via DATABASE_URL (Postgres) — same query shape as Metabase."""
from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import urlparse

# Cache so the 3s UI poll doesn't hammer RDS.
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SEC = 30.0

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _dsn() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    return url


def configured() -> bool:
    return bool(_dsn())


def db_label() -> str | None:
    dsn = _dsn()
    if not dsn:
        return None
    try:
        u = urlparse(dsn)
        host = u.hostname or "?"
        db = (u.path or "/").lstrip("/") or "?"
        return f"{host}/{db}"
    except Exception:
        return "configured"


def _normalize_ids(raw: str | list[str]) -> list[str]:
    if isinstance(raw, list):
        parts = [str(x).strip() for x in raw]
    else:
        parts = re.split(r"[\s,]+", (raw or "").strip())
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        if p.startswith("http://") or p.startswith("https://"):
            p = p.rstrip("/").split("/")[-1]
        out.append(p)
    if not out:
        raise ValueError("payment_link_id is required")
    bad = [p for p in out if not _UUID_RE.match(p)]
    if bad:
        raise ValueError(
            "paymentLink id must be a UUID (paymentLinks.id from Metabase). "
            f"Not a short slug: {', '.join(bad)}"
        )
    return out


def _validate_date(d: str) -> str:
    d = (d or "").strip()
    if not _DATE_RE.match(d):
        raise ValueError(f"invalid date: {d!r} (want YYYY-MM-DD)")
    return d


# Matches the Metabase native query (optional currency / product filters omitted when empty).
_SQL = """
SELECT DISTINCT ON ("orderPayments"."gatewayPaymentId")
    orders."paymentLinkId",
    "orderPayments"."gatewayPaymentId",
    orders."userFullName",
    orders."userEmail",
    orders."userPhone",
    products.name AS product_name,
    products.id AS product_id,
    sku.id AS sku_id,
    sfm."fulfillmentType",
    sfm."fulfillmentId" AS cohort_circle_id,
    orders."orderTotalMinor"/100.0 AS sale_price,
    "orderPayments"."gatewayPaymentAmount"/100.0 AS amount_paid,
    orders.currency AS currency,
    orders."updatedAt" AT TIME ZONE 'asia/kolkata' AS updated_at_ist,
    orders."createdAt" AT TIME ZONE 'asia/kolkata' AS created_at_ist
FROM orders
LEFT JOIN "orderItems"
    ON orders.id = "orderItems"."orderId"
LEFT JOIN products
    ON "orderItems"."productId" = products.id
LEFT JOIN sku
    ON products.id = sku."productId"
LEFT JOIN "skuFulfillmentMapping" sfm
    ON sku.id = sfm."skuId"
LEFT JOIN "paymentLinks"
    ON orders."paymentLinkId" = "paymentLinks".id
LEFT JOIN "orderPayments"
    ON orders.id = "orderPayments"."orderId"
WHERE 1=1
    AND "orderPayments"."gatewayPaymentStatus" IN ('CAPTURED','SUCCEEDED','SUCCESS','captured')
    AND orders."paymentLinkId" = ANY(%(link_ids)s::uuid[])
    AND (
        %(currency)s = ''
        OR orders.currency::text = %(currency)s
    )
    AND date(orders."createdAt" AT TIME ZONE 'asia/kolkata') >= CAST(%(start_date)s AS date)
    AND date(orders."createdAt" AT TIME ZONE 'asia/kolkata') <= CAST(%(end_date)s AS date)
ORDER BY "orderPayments"."gatewayPaymentId", orders."createdAt" DESC
"""


def _diagnose(cursor: Any, ids: list[str], start_date: str, end_date: str) -> dict[str, Any]:
    """Explain zero-result fetches (wrong DB vs empty date range)."""
    cursor.execute(
        'SELECT id::text AS id FROM "paymentLinks" WHERE id = ANY(%s::uuid[])',
        (ids,),
    )
    found = {r["id"] for r in cursor.fetchall()}
    missing = [i for i in ids if i not in found]

    cursor.execute(
        """
        SELECT count(*)::int AS n,
               coalesce(sum("orderPayments"."gatewayPaymentAmount")/100.0, 0) AS amt
        FROM orders
        JOIN "orderPayments" ON orders.id = "orderPayments"."orderId"
        WHERE orders."paymentLinkId" = ANY(%s::uuid[])
          AND "orderPayments"."gatewayPaymentStatus" IN ('CAPTURED','SUCCEEDED','SUCCESS','captured')
        """,
        (ids,),
    )
    all_time = cursor.fetchone() or {"n": 0, "amt": 0}

    cursor.execute(
        """
        SELECT min(date(orders."createdAt" AT TIME ZONE 'asia/kolkata')) AS min_d,
               max(date(orders."createdAt" AT TIME ZONE 'asia/kolkata')) AS max_d
        FROM orders
        JOIN "orderPayments" ON orders.id = "orderPayments"."orderId"
        WHERE orders."paymentLinkId" = ANY(%s::uuid[])
          AND "orderPayments"."gatewayPaymentStatus" IN ('CAPTURED','SUCCEEDED','SUCCESS','captured')
        """,
        (ids,),
    )
    span = cursor.fetchone() or {}

    return {
        "db": db_label(),
        "links_requested": len(ids),
        "links_found_in_paymentLinks": len(found),
        "missing_link_ids": missing,
        "captured_all_time_count": int(all_time.get("n") or 0),
        "captured_all_time_amount": float(all_time.get("amt") or 0),
        "captured_min_date": span.get("min_d").isoformat() if span.get("min_d") else None,
        "captured_max_date": span.get("max_d").isoformat() if span.get("max_d") else None,
        "queried_range": f"{start_date} → {end_date}",
    }


def fetch_payments(
    payment_link_ids: str | list[str],
    start_date: str,
    end_date: str,
    currency: str = "",
    *,
    force: bool = False,
) -> dict[str, Any]:
    if not configured():
        return {
            "ok": False,
            "count": 0,
            "total_amount": 0,
            "currency": "INR",
            "payments": [],
            "error": "DATABASE_URL not set in .env",
            "configured": False,
            "diagnostic": None,
        }

    try:
        ids = _normalize_ids(payment_link_ids)
        start_date = _validate_date(start_date)
        end_date = _validate_date(end_date)
        # Empty currency = no filter (same as leaving Metabase {{currency}} unset)
        cur = (currency or "").strip().upper()
        if cur and not re.fullmatch(r"[A-Z]{3}", cur):
            raise ValueError("currency must be a 3-letter code")
    except ValueError as e:
        return {
            "ok": False,
            "count": 0,
            "total_amount": 0,
            "currency": "INR",
            "payments": [],
            "error": str(e),
            "configured": True,
            "diagnostic": None,
        }

    cache_key = f"{','.join(ids)}|{start_date}|{end_date}|{cur}"
    now = time.time()
    if not force and cache_key in _CACHE:
        ts, cached = _CACHE[cache_key]
        if now - ts < _CACHE_TTL_SEC:
            return cached

    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(_dsn(), connect_timeout=8, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    _SQL,
                    {
                        "link_ids": ids,
                        "start_date": start_date,
                        "end_date": end_date,
                        "currency": cur,
                    },
                )
                rows = list(cursor.fetchall())
                diagnostic = None
                if not rows:
                    diagnostic = _diagnose(cursor, ids, start_date, end_date)
    except Exception as e:  # noqa: BLE001
        out = {
            "ok": False,
            "count": 0,
            "total_amount": 0,
            "currency": "INR",
            "payments": [],
            "error": str(e),
            "configured": True,
            "diagnostic": None,
        }
        _CACHE[cache_key] = (now, out)
        return out

    by_currency: dict[str, float] = {}
    records: list[dict[str, Any]] = []
    for row in rows:
        rec = dict(row)
        for k, v in list(rec.items()):
            if hasattr(v, "isoformat"):
                rec[k] = v.isoformat()
            elif v is not None and not isinstance(v, (str, int, float, bool)):
                rec[k] = str(v)
        code = str(rec.get("currency") or "INR").upper()
        try:
            amt = float(rec.get("amount_paid") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        by_currency[code] = round(by_currency.get(code, 0.0) + amt, 2)
        records.append(rec)

    hint = None
    if diagnostic and diagnostic.get("missing_link_ids"):
        missing = diagnostic["missing_link_ids"]
        hint = (
            f"{len(missing)}/{len(ids)} paymentLink UUID(s) not found in {diagnostic.get('db')}. "
            "Metabase is likely pointed at a different database than DATABASE_URL. "
            f"Missing: {', '.join(missing[:3])}{'…' if len(missing) > 3 else ''}"
        )
    elif diagnostic and diagnostic.get("captured_all_time_count", 0) > 0:
        hint = (
            f"Links exist, but 0 captured payments in {start_date}→{end_date}. "
            f"All-time captured: {diagnostic['captured_all_time_count']} "
            f"(₹{diagnostic['captured_all_time_amount']:.0f}) "
            f"between {diagnostic.get('captured_min_date')} and {diagnostic.get('captured_max_date')}."
        )
    elif diagnostic:
        hint = (
            f"No captured payments for these links in {diagnostic.get('db')} "
            f"(checked paymentLinks + orders)."
        )

    display = format_totals_by_currency(by_currency)
    # Prefer INR as the "primary" currency field when present; else first key
    primary = "INR" if "INR" in by_currency else (next(iter(by_currency), cur or "INR"))

    out = {
        "ok": True,
        "count": len(records),
        "total_amount": round(sum(by_currency.values()), 2),  # raw sum — prefer totals_by_currency
        "totals_by_currency": by_currency,
        "total_display": display,
        "currency": primary,
        "payments": records[:1000],
        "error": hint,  # soft warning when count=0
        "configured": True,
        "diagnostic": diagnostic,
    }
    _CACHE[cache_key] = (now, out)
    return out


def format_totals_by_currency(by_currency: dict[str, float]) -> str:
    """e.g. '$99 + ₹11,994' — currencies kept separate, never mixed."""
    if not by_currency:
        return "—"
    # Stable order: USD then INR then others alpha
    order = {"USD": 0, "INR": 1}

    def sort_key(code: str) -> tuple:
        return (order.get(code, 50), code)

    parts: list[str] = []
    for code in sorted(by_currency.keys(), key=sort_key):
        amt = by_currency[code]
        if amt == int(amt):
            num = f"{int(amt):,}"
        else:
            num = f"{amt:,.2f}"
        if code == "USD":
            parts.append(f"${num}")
        elif code == "INR":
            parts.append(f"₹{num}")
        else:
            parts.append(f"{num} {code}")
    return " + ".join(parts)
