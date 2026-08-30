"""Intent receipts, clicks, searches. The log is the company."""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from db import now


def log_intent(
    con: sqlite3.Connection, payload: dict, key_row: sqlite3.Row
) -> dict:
    if key_row is None:
        raise PermissionError("write key required")
    if "revoked" in key_row.keys() and key_row["revoked"]:
        raise PermissionError("write key required")
    idem = payload.get("idempotency_key")
    if idem:
        existing = con.execute(
            "SELECT * FROM receipts WHERE key_id = ? AND idempotency_key = ?",
            (key_row["id"], str(idem)),
        ).fetchone()
        if existing:
            return {
                "receipt_id": existing["id"],
                "stored_at": existing["created_at"],
            }
    rid = "rcp_" + uuid.uuid4().hex[:16]
    stored_at = now()
    action = payload.get("action") or "recommend"
    con.execute(
        """INSERT INTO receipts
           (id, query, action, target_url, policy_id, key_id, created_at,
            caller_id, result, idempotency_key)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            rid,
            payload.get("query"),
            action,
            payload.get("target_url"),
            payload.get("policy_id"),
            key_row["id"],
            stored_at,
            payload.get("caller_id"),
            payload.get("result"),
            str(idem) if idem else None,
        ),
    )
    con.commit()
    return {"receipt_id": rid, "stored_at": stored_at}


def log_click(con: sqlite3.Connection, payload: dict) -> dict:
    item_url = str(payload.get("item_url") or "").strip()
    if not item_url:
        raise ValueError("item_url required")
    click_id = "clk_" + uuid.uuid4().hex[:16]
    created_at = now()
    receipt_id = payload.get("receipt_id") or None
    item_id = payload.get("item_id") or None
    con.execute(
        """INSERT INTO clicks (id, receipt_id, item_url, item_id, created_at)
           VALUES (?,?,?,?,?)""",
        (click_id, receipt_id, item_url, item_id, created_at),
    )
    con.commit()
    return {"click_id": click_id, "created_at": created_at}


def record_search(
    con: sqlite3.Connection,
    query: str,
    pond: str | None,
    match_count: int,
) -> str:
    q = (query or "").strip()
    if not q:
        raise ValueError("query required")
    search_id = "sch_" + uuid.uuid4().hex[:16]
    con.execute(
        """INSERT INTO searches (id, query, pond, match_count, created_at)
           VALUES (?,?,?,?,?)""",
        (search_id, q, pond, int(match_count), now()),
    )
    con.commit()
    return search_id


def demand(con: sqlite3.Connection) -> dict:
    failed_domains: list[dict] = []
    missing_queries: list[dict] = []
    try:
        rows = con.execute(
            """SELECT domain, COUNT(*) AS n, MAX(created_at) AS last
               FROM checks
               WHERE result = 'fail' AND domain IS NOT NULL AND domain != ''
               GROUP BY domain
               ORDER BY n DESC, last DESC"""
        ).fetchall()
        failed_domains = [
            {"domain": r["domain"], "n": r["n"], "last": r["last"]} for r in rows
        ]
    except sqlite3.OperationalError:
        failed_domains = []
    try:
        rows = con.execute(
            """SELECT query, COUNT(*) AS n, MAX(created_at) AS last
               FROM searches
               WHERE match_count = 0 AND query IS NOT NULL AND query != ''
               GROUP BY query
               ORDER BY n DESC, last DESC"""
        ).fetchall()
        missing_queries = [
            {"query": r["query"], "n": r["n"], "last": r["last"]} for r in rows
        ]
    except sqlite3.OperationalError:
        missing_queries = []
    return {"failed_domains": failed_domains, "missing_queries": missing_queries}


def _recent(con: sqlite3.Connection, sql: str, limit: int) -> list[dict]:
    try:
        rows = con.execute(sql, (limit,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def _count_since(con: sqlite3.Connection, table: str, since: str | None) -> int:
    try:
        if since:
            row = con.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE created_at >= ?", (since,)
            ).fetchone()
        else:
            row = con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
        return int(row["c"] if row else 0)
    except sqlite3.OperationalError:
        return 0


def pickup(con: sqlite3.Connection) -> dict:
    """Expected vs actual AI pickup. Expected stays 0 until listed + https unless env set."""
    now_dt = datetime.now(timezone.utc)
    since = (now_dt - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    checks_24h = _count_since(con, "checks", since)
    searches_24h = _count_since(con, "searches", since)
    receipts_24h = _count_since(con, "receipts", since)
    clicks_24h = _count_since(con, "clicks", since)
    actual_24h = checks_24h + searches_24h
    actual_all = _count_since(con, "checks", None) + _count_since(con, "searches", None)
    public = (os.environ.get("A2A_PUBLIC_URL") or "").strip()
    https = public.lower().startswith("https://")
    listed = (os.environ.get("A2A_DIRECTORY_LISTED") or "0").strip() in ("1", "true", "yes")
    try:
        expected = int(os.environ.get("A2A_EXPECTED_CALLS_PER_DAY") or "0")
    except ValueError:
        expected = 0
    blockers = []
    if not https:
        blockers.append("No HTTPS/domain yet. Card is on a raw IP over HTTP.")
    if not listed:
        blockers.append("Not listed on an A2A directory. Other AIs will not find us by themselves.")
    if expected == 0 and blockers:
        note = "Expected pickup is 0 until those holes close. Actual is the tape."
    else:
        note = "Expected is what we set. Actual is checks + searches on the log."
    return {
        "as_of": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": "24h",
        "expected_per_day": expected,
        "actual_24h": actual_24h,
        "actual_all": actual_all,
        "gap_24h": actual_24h - expected,
        "https": https,
        "directory_listed": listed,
        "public_url": public,
        "blockers": blockers,
        "note": note,
        "counts_24h": {
            "checks": checks_24h,
            "searches": searches_24h,
            "receipts": receipts_24h,
            "clicks": clicks_24h,
        },
        "counts_all": {
            "checks": _count_since(con, "checks", None),
            "searches": _count_since(con, "searches", None),
            "receipts": _count_since(con, "receipts", None),
            "clicks": _count_since(con, "clicks", None),
        },
    }


def recent_tape(con: sqlite3.Connection, limit: int = 50) -> dict:
    n = max(0, int(limit))
    return {
        "checks": _recent(
            con, "SELECT * FROM checks ORDER BY created_at DESC LIMIT ?", n
        ),
        "receipts": _recent(
            con, "SELECT * FROM receipts ORDER BY created_at DESC LIMIT ?", n
        ),
        "clicks": _recent(
            con, "SELECT * FROM clicks ORDER BY created_at DESC LIMIT ?", n
        ),
        "searches": _recent(
            con, "SELECT * FROM searches ORDER BY created_at DESC LIMIT ?", n
        ),
    }
