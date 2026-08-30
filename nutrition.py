"""Nutrition label. Stamp of what the agent/org is + last test."""
from __future__ import annotations

import sqlite3

import policy
from db import now

STAMP_RESULTS = frozenset({"pass", "fail"})
UNTESTED = "untested"


def _col(row, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        if key not in row:
            return default
        val = row[key]
        return default if val is None else val
    try:
        keys = row.keys()
    except (AttributeError, TypeError):
        return default
    if key not in keys:
        return default
    val = row[key]
    return default if val is None else val


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_stamp_columns(con: sqlite3.Connection) -> None:
    cols = _columns(con, "policies")
    if not cols:
        return
    if "last_test_at" not in cols:
        con.execute("ALTER TABLE policies ADD COLUMN last_test_at TEXT")
    if "last_test_result" not in cols:
        con.execute("ALTER TABLE policies ADD COLUMN last_test_result TEXT")
    if "last_test_note" not in cols:
        con.execute("ALTER TABLE policies ADD COLUMN last_test_note TEXT")


def _fail_closed() -> dict:
    return {
        "pass": False,
        "reason": "no_live_policy",
        "speak": policy.SPEAK_FAIL,
        "label": None,
    }


def _is_fixture(org, url: str) -> bool:
    domain = policy.extract_domain(url)
    if hasattr(policy, "_is_fixture"):
        return bool(policy._is_fixture(org, domain))
    if org is not None and bool(_col(org, "is_seed", 0)):
        return True
    org_domain = ((_col(org, "domain") or "") if org is not None else "").lower()
    d = (domain or org_domain).lower()
    return org_domain.endswith(".local") or d.endswith(".local")


def label_from_policy(policy_row, org_row=None) -> dict:
    raw_result = _col(policy_row, "last_test_result", "")
    result = str(raw_result).strip() if raw_result is not None else ""
    if not result:
        result = UNTESTED
    agent_kind = _col(policy_row, "agent_kind") or _col(org_row, "agent_kind") or _col(
        org_row, "category"
    )
    return {
        "agent_kind": agent_kind or "",
        "last_test_at": _col(policy_row, "last_test_at"),
        "last_test_result": result,
        "last_test_note": _col(policy_row, "last_test_note"),
        "title": _col(policy_row, "title") or "",
        "summary": _col(policy_row, "summary") or "",
        "policy_id": _col(policy_row, "id") or _col(policy_row, "policy_id"),
        "policy_hash": _col(policy_row, "policy_hash"),
        "org_name": _col(org_row, "name") or "",
        "org_id": _col(org_row, "id") or _col(policy_row, "org_id"),
    }


def nutrition_for_url(con: sqlite3.Connection, url: str) -> dict:
    raw = url if isinstance(url, str) else ("" if url is None else str(url))
    org = policy.org_by_url(con, raw) if raw.strip() else None
    pol = policy.latest_policy(con, org["id"]) if org else None
    fixture = _is_fixture(org, raw)
    if org is None or pol is None or (pol["status"] or "") != "active":
        out = _fail_closed()
        if fixture:
            out["recommendable"] = False
        return out
    label = label_from_policy(pol, org)
    out = {"pass": True, "label": label}
    if fixture:
        out["recommendable"] = False
    return out


def stamp_test(
    con: sqlite3.Connection, org_id: str, result: str, note: str | None
) -> dict:
    """Stamp last test on the latest policy. Org publish happens elsewhere."""
    result_n = (result or "").strip().lower()
    if result_n not in STAMP_RESULTS:
        raise ValueError("result must be pass or fail")
    oid = (org_id or "").strip()
    if not oid:
        raise ValueError("org_id required")
    _ensure_stamp_columns(con)
    pol = policy.latest_policy(con, oid)
    if pol is None:
        return {"ok": False, "reason": "no_live_policy"}
    stamped_at = now()
    note_s = "" if note is None else str(note)
    con.execute(
        """UPDATE policies
           SET last_test_at=?, last_test_result=?, last_test_note=?
           WHERE id=?""",
        (stamped_at, result_n, note_s, pol["id"]),
    )
    con.commit()
    return {
        "ok": True,
        "org_id": oid,
        "policy_id": pol["id"],
        "last_test_at": stamped_at,
        "last_test_result": result_n,
        "last_test_note": note_s,
    }
