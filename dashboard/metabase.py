"""Metabase query runner for live webinar payment totals."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

# Cache query results briefly so the 3s UI poll doesn't hammer Metabase.
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SEC = 45.0

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_\-:./]+$")


@dataclass
class MetabaseConfig:
    url: str
    database_id: int
    api_key: str = ""
    username: str = ""
    password: str = ""
    session_token: str = ""

    @property
    def configured(self) -> bool:
        if not self.url or not self.database_id:
            return False
        return bool(self.api_key or self.session_token or (self.username and self.password))


def load_config() -> MetabaseConfig:
    return MetabaseConfig(
        url=(os.environ.get("METABASE_URL") or "").rstrip("/"),
        database_id=int(os.environ.get("METABASE_DATABASE_ID") or "0"),
        api_key=os.environ.get("METABASE_API_KEY") or "",
        username=os.environ.get("METABASE_USERNAME") or "",
        password=os.environ.get("METABASE_PASSWORD") or "",
        session_token=os.environ.get("METABASE_SESSION") or "",
    )


def _validate_date(d: str) -> str:
    d = (d or "").strip()
    if not _DATE_RE.match(d):
        raise ValueError(f"invalid date: {d!r} (want YYYY-MM-DD)")
    return d


def _normalize_link_ids(raw: str | list[str]) -> list[str]:
    if isinstance(raw, list):
        parts = [str(x).strip() for x in raw]
    else:
        parts = re.split(r"[\s,]+", (raw or "").strip())
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        # Allow full payment URLs — store path slug as filter value too
        if p.startswith("http://") or p.startswith("https://"):
            p = p.rstrip("/").split("/")[-1]
        if not _ID_RE.match(p) and not _UUID_RE.match(p):
            raise ValueError(f"invalid payment link id: {p!r}")
        out.append(p)
    if not out:
        raise ValueError("payment_link_id is required")
    return out


def _sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def build_payments_sql(
    payment_link_ids: list[str],
    start_date: str,
    end_date: str,
    currency: str = "",
) -> str:
    start_date = _validate_date(start_date)
    end_date = _validate_date(end_date)
    ids = _normalize_link_ids(payment_link_ids)

    # Match Metabase paymentLink_id filter against orders.paymentLinkId (uuid)
    # OR paymentLinks public slug / id text — covers both UUID and short codes.
    id_list = ", ".join(_sql_literal(i) for i in ids)
    currency_clause = ""
    if currency and currency.strip():
        cur = currency.strip().upper()
        if not re.match(r"^[A-Z]{3}$", cur):
            raise ValueError("currency must be 3-letter code")
        currency_clause = f"AND orders.currency = {_sql_literal(cur)}"

    return f"""
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
    orders."updatedAt" AT TIME ZONE 'Asia/Kolkata' AS updated_at_ist,
    orders."createdAt" AT TIME ZONE 'Asia/Kolkata' AS created_at_ist
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
    AND (
        orders."paymentLinkId"::text IN ({id_list})
        OR "paymentLinks".id::text IN ({id_list})
    )
    {currency_clause}
    AND date(orders."createdAt" AT TIME ZONE 'Asia/Kolkata') >= DATE {_sql_literal(start_date)}
    AND date(orders."createdAt" AT TIME ZONE 'Asia/Kolkata') <= DATE {_sql_literal(end_date)}
ORDER BY "orderPayments"."gatewayPaymentId", orders."createdAt" DESC
""".strip()


def _http_json(
    method: str,
    url: str,
    headers: dict[str, str],
    body: Optional[dict[str, Any]] = None,
    timeout: float = 25.0,
) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Metabase HTTP {e.code}: {err_body}") from e


def _auth_headers(cfg: MetabaseConfig) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["X-API-KEY"] = cfg.api_key
        return headers
    if cfg.session_token:
        headers["X-Metabase-Session"] = cfg.session_token
        return headers
    # Password login once per process if needed
    if cfg.username and cfg.password:
        sess = _http_json(
            "POST",
            f"{cfg.url}/api/session",
            {"Content-Type": "application/json"},
            {"username": cfg.username, "password": cfg.password},
        )
        token = (sess or {}).get("id")
        if not token:
            raise RuntimeError("Metabase login failed — no session id")
        cfg.session_token = token
        headers["X-Metabase-Session"] = token
        return headers
    raise RuntimeError("Metabase not configured (need API key or username/password)")


def _parse_dataset(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data") or {}
    cols = [c.get("name") or c.get("display_name") for c in (data.get("cols") or [])]
    rows = data.get("rows") or []
    records: list[dict[str, Any]] = []
    total = 0.0
    currency = "INR"
    for row in rows:
        rec = {cols[i]: row[i] for i in range(min(len(cols), len(row)))}
        records.append(rec)
        amt = rec.get("amount_paid")
        try:
            total += float(amt or 0)
        except (TypeError, ValueError):
            pass
        if rec.get("currency"):
            currency = str(rec["currency"])
    return {
        "ok": True,
        "count": len(records),
        "total_amount": round(total, 2),
        "currency": currency,
        "payments": records[:50],  # cap payload for UI
        "error": None,
    }


def fetch_payments(
    payment_link_ids: str | list[str],
    start_date: str,
    end_date: str,
    currency: str = "",
    *,
    force: bool = False,
) -> dict[str, Any]:
    cfg = load_config()
    if not cfg.configured:
        return {
            "ok": False,
            "count": 0,
            "total_amount": 0,
            "currency": "INR",
            "payments": [],
            "error": "Metabase not configured. Set METABASE_URL + METABASE_DATABASE_ID + METABASE_API_KEY (or username/password).",
            "configured": False,
        }

    try:
        ids = _normalize_link_ids(payment_link_ids)
        start_date = _validate_date(start_date)
        end_date = _validate_date(end_date)
    except ValueError as e:
        return {
            "ok": False,
            "count": 0,
            "total_amount": 0,
            "currency": "INR",
            "payments": [],
            "error": str(e),
            "configured": True,
        }

    cache_key = json.dumps(
        {"ids": ids, "start": start_date, "end": end_date, "cur": currency},
        sort_keys=True,
    )
    now = time.time()
    if not force and cache_key in _CACHE:
        ts, cached = _CACHE[cache_key]
        if now - ts < _CACHE_TTL_SEC:
            return cached

    # Prefer a simpler filter first (paymentLinkId only). If Metabase errors on
    # missing slug columns, retry with UUID-only predicate.
    sql_variants = [
        build_payments_sql(ids, start_date, end_date, currency),
        _build_simple_sql(ids, start_date, end_date, currency),
    ]

    last_err = "unknown"
    for sql in sql_variants:
        try:
            headers = _auth_headers(cfg)
            payload = {
                "database": cfg.database_id,
                "type": "native",
                "native": {"query": sql},
            }
            result = _http_json("POST", f"{cfg.url}/api/dataset", headers, payload)
            parsed = _parse_dataset(result or {})
            parsed["configured"] = True
            _CACHE[cache_key] = (now, parsed)
            return parsed
        except Exception as e:  # noqa: BLE001 — surface to UI
            last_err = str(e)
            # If it's an auth error, don't bother with SQL variants
            if "401" in last_err or "403" in last_err:
                break

    out = {
        "ok": False,
        "count": 0,
        "total_amount": 0,
        "currency": "INR",
        "payments": [],
        "error": last_err,
        "configured": True,
    }
    _CACHE[cache_key] = (now, out)
    return out


def _build_simple_sql(
    ids: list[str],
    start_date: str,
    end_date: str,
    currency: str = "",
) -> str:
    """Fallback without optional paymentLinks slug columns."""
    id_list = ", ".join(_sql_literal(i) for i in ids)
    currency_clause = ""
    if currency and currency.strip():
        cur = currency.strip().upper()
        currency_clause = f"AND orders.currency = {_sql_literal(cur)}"
    return f"""
SELECT DISTINCT ON ("orderPayments"."gatewayPaymentId")
    orders."paymentLinkId",
    "orderPayments"."gatewayPaymentId",
    orders."userFullName",
    orders."userEmail",
    "orderPayments"."gatewayPaymentAmount"/100.0 AS amount_paid,
    orders.currency AS currency,
    orders."createdAt" AT TIME ZONE 'Asia/Kolkata' AS created_at_ist
FROM orders
LEFT JOIN "orderPayments"
    ON orders.id = "orderPayments"."orderId"
WHERE 1=1
    AND "orderPayments"."gatewayPaymentStatus" IN ('CAPTURED','SUCCEEDED','SUCCESS','captured')
    AND orders."paymentLinkId"::text IN ({id_list})
    {currency_clause}
    AND date(orders."createdAt" AT TIME ZONE 'Asia/Kolkata') >= DATE {_sql_literal(start_date)}
    AND date(orders."createdAt" AT TIME ZONE 'Asia/Kolkata') <= DATE {_sql_literal(end_date)}
ORDER BY "orderPayments"."gatewayPaymentId", orders."createdAt" DESC
""".strip()
