"""HITL approve. Agent pauses; human taps yes on the switchboard."""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
import uuid

from db import now

SPEAK_PENDING = "Paused. Waiting for a human tap on the switchboard."
SPEAK_APPROVED = "Approved. The agent may continue."
SPEAK_DENIED = "Denied. The agent must not continue."

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_DDL = """
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  query TEXT,
  action TEXT,
  target_url TEXT,
  policy_id TEXT,
  org_id TEXT,
  amount_usd REAL,
  status TEXT NOT NULL DEFAULT 'pending',
  reason TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
"""


def _ensure_table(con: sqlite3.Connection) -> None:
    con.executescript(_DDL)


def _text(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _amount(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _speak_for(status: str) -> str:
    if status == "approved":
        return SPEAK_APPROVED
    if status == "denied":
        return SPEAK_DENIED
    return SPEAK_PENDING


def _row_dict(row) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["approval_id"] = out.get("id")
    return out


def _notify(approval_id: str, target_url: str | None) -> None:
    token = (os.environ.get("A2A_TELEGRAM_TOKEN") or "").strip()
    chat_id = (os.environ.get("A2A_TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return
    text = f"A2A HITL pending\napproval_id: {approval_id}\ntarget_url: {target_url or ''}"
    url = _TELEGRAM_API.format(token=token)
    body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return
    except Exception:
        return


def request_approve(con: sqlite3.Connection, payload: dict) -> dict:
    _ensure_table(con)
    payload = payload or {}
    aid = "apv_" + uuid.uuid4().hex[:16]
    created = now()
    target_url = _text(payload.get("target_url"))
    con.execute(
        """INSERT INTO approvals
           (id, query, action, target_url, policy_id, org_id, amount_usd,
            status, reason, created_at, decided_at)
           VALUES (?,?,?,?,?,?,?,'pending',NULL,?,NULL)""",
        (
            aid,
            _text(payload.get("query")),
            _text(payload.get("action")),
            target_url,
            _text(payload.get("policy_id")),
            _text(payload.get("org_id")),
            _amount(payload.get("amount_usd")),
            created,
        ),
    )
    con.commit()
    _notify(aid, target_url)
    return {"approval_id": aid, "status": "pending", "speak": SPEAK_PENDING}


def decide(
    con: sqlite3.Connection,
    approval_id: str,
    decision: str,
    reason: str | None = None,
) -> dict:
    _ensure_table(con)
    aid = _text(approval_id)
    if not aid:
        raise ValueError("approval_id required")
    status = _text(decision)
    if status:
        status = status.lower()
    if status not in ("approved", "denied"):
        raise ValueError("decision must be approved or denied")
    row = con.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
    if row is None:
        raise ValueError("approval not found")
    if row["status"] != "pending":
        raise ValueError("approval already decided")
    decided = now()
    reason_s = None if reason is None else str(reason)
    cur = con.execute(
        """UPDATE approvals
           SET status=?, reason=?, decided_at=?
           WHERE id=? AND status='pending'""",
        (status, reason_s, decided, aid),
    )
    if cur.rowcount != 1:
        raise ValueError("approval already decided")
    con.commit()
    out = get_approval(con, aid)
    if out is None:
        raise ValueError("approval not found")
    out["speak"] = _speak_for(status)
    return out


def get_approval(con: sqlite3.Connection, approval_id: str) -> dict | None:
    _ensure_table(con)
    aid = _text(approval_id)
    if not aid:
        return None
    row = con.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
    return _row_dict(row)


def list_pending(con: sqlite3.Connection, limit: int = 50) -> list[dict]:
    _ensure_table(con)
    try:
        n = max(0, int(limit))
    except (TypeError, ValueError):
        n = 50
    rows = con.execute(
        """SELECT * FROM approvals
           WHERE status='pending'
           ORDER BY created_at ASC
           LIMIT ?""",
        (n,),
    ).fetchall()
    return [_row_dict(r) for r in rows]
