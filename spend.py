"""Spend cap. Hard limit + allowlist the model cannot talk itself out of.

Fail closed. No override parameter. Ledger is the tape of spend.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

try:
    import policy as policy_mod
except ImportError:
    policy_mod = None  # type: ignore[assignment]

try:
    from db import now as _now
except ImportError:
    from datetime import datetime, timezone

    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

CENT = Decimal("0.01")
DEFAULT_ALLOWLIST = ["recommend"]

SPEAK_NO_POLICY = (
    "I can't spend that yet. They have no live policy on the switchboard."
)
SPEAK_NO_CAP = (
    "I can't spend that yet. They have no spend cap on the switchboard."
)
SPEAK_NOT_ALLOWLISTED = (
    "I can't spend that yet. That action is not on their allowlist."
)
SPEAK_CAP_EXCEEDED = (
    "I can't spend that yet. It would go over their spend cap."
)
SPEAK_INVALID_AMOUNT = "I can't spend that yet. The amount is not valid."


def parse_allowlist(raw: object) -> list[str]:
    """Lowercased action names. Empty or unreadable input is []."""
    if raw is None:
        return []
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    items: list[object]
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        parsed: object = text
        if text[:1] in "[{":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = text
        if isinstance(parsed, list):
            items = parsed
        elif isinstance(parsed, str):
            items = [part.strip() for part in parsed.split(",")]
        else:
            return []
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item is None or isinstance(item, (dict, list, tuple, bool)):
            continue
        name = str(item).strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _allowlist(raw: object) -> list[str]:
    names = parse_allowlist(raw)
    return names if names else list(DEFAULT_ALLOWLIST)


def _row_get(row: object, key: str, default: object = None) -> object:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        keys = row.keys()  # type: ignore[union-attr]
        if key not in keys:
            return default
        return row[key]  # type: ignore[index]
    except (AttributeError, KeyError, IndexError, TypeError):
        return default


def _org_out(org: sqlite3.Row | None) -> dict | None:
    if org is None:
        return None
    if policy_mod is not None and hasattr(policy_mod, "_org_public"):
        try:
            return policy_mod._org_public(org)
        except Exception:
            pass
    return {
        "id": org["id"],
        "name": org["name"],
        "domain": org["domain"],
        "website": _row_get(org, "website"),
        "category": _row_get(org, "category"),
        "attested": bool(_row_get(org, "attested", 0)),
        "is_seed": bool(_row_get(org, "is_seed", 0)),
    }


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(con, table):
        return set()
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS spend_ledger (
               id TEXT PRIMARY KEY,
               org_id TEXT NOT NULL,
               amount_usd REAL NOT NULL,
               action TEXT NOT NULL,
               target_url TEXT,
               receipt_id TEXT,
               created_at TEXT NOT NULL
           )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_spend_ledger_org ON spend_ledger(org_id)"
    )
    if _table_exists(con, "policies"):
        cols = _columns(con, "policies")
        if "spend_cap_usd" not in cols:
            con.execute("ALTER TABLE policies ADD COLUMN spend_cap_usd REAL")
        if "allowlist_json" not in cols:
            con.execute("ALTER TABLE policies ADD COLUMN allowlist_json TEXT")


def _money(val: object) -> Decimal | None:
    if val is None or isinstance(val, bool):
        return None
    text = str(val).strip()
    if not text:
        return None
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    return d


def _cents(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _out_money(d: Decimal | None) -> float | None:
    if d is None:
        return None
    return float(_cents(d))


def _spent_usd(con: sqlite3.Connection, org_id: str) -> Decimal | None:
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) AS spent FROM spend_ledger WHERE org_id = ?",
            (org_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    spent = _money(_row_get(row, "spent", 0) if row is not None else 0)
    return _cents(spent) if spent is not None else Decimal("0.00")


def _verdict(
    *,
    ok: bool,
    reason: str,
    speak: str,
    cap_usd: Decimal | None,
    spent_usd: Decimal | None,
    remaining_usd: Decimal | None,
    allowlist: list[str],
    action: str,
    org: dict | None,
    policy_id: str | None,
    policy_hash: str | None,
) -> dict:
    return {
        "pass": ok,
        "reason": reason,
        "cap_usd": _out_money(cap_usd),
        "spent_usd": _out_money(spent_usd),
        "remaining_usd": _out_money(remaining_usd),
        "allowlist": list(allowlist),
        "action": action,
        "speak": speak,
        "org": org,
        "policy_id": policy_id,
        "policy_hash": policy_hash,
    }


def spend_check(
    con: sqlite3.Connection,
    url: str,
    amount_usd: float = 0,
    action: str = "recommend",
) -> dict:
    """Fail closed. No override. The model cannot talk this off."""
    _ensure_schema(con)
    action_name = ("" if action is None else str(action)).strip().lower()
    allowlist: list[str] = []
    org_out: dict | None = None
    policy_id: str | None = None
    policy_hash: str | None = None
    spent: Decimal | None = None
    cap: Decimal | None = None
    remaining: Decimal | None = None

    if policy_mod is None or not hasattr(policy_mod, "org_by_url"):
        return _verdict(
            ok=False,
            reason="no_live_policy",
            speak=SPEAK_NO_POLICY,
            cap_usd=None,
            spent_usd=None,
            remaining_usd=None,
            allowlist=allowlist,
            action=action_name,
            org=None,
            policy_id=None,
            policy_hash=None,
        )

    org = policy_mod.org_by_url(con, url if isinstance(url, str) else str(url or ""))
    org_out = _org_out(org)
    org_id = str(_row_get(org, "id") or "") if org is not None else ""
    pol = None
    if org is not None and hasattr(policy_mod, "latest_policy") and org_id:
        pol = policy_mod.latest_policy(con, org_id)
    if pol is not None:
        policy_id = str(_row_get(pol, "id") or "") or None
        policy_hash = str(_row_get(pol, "policy_hash") or "") or None
        allowlist = _allowlist(_row_get(pol, "allowlist_json"))
        cap = _money(_row_get(pol, "spend_cap_usd"))
        if cap is not None:
            cap = _cents(cap)

    if org is None or pol is None or str(_row_get(pol, "status") or "") != "active":
        return _verdict(
            ok=False,
            reason="no_live_policy",
            speak=SPEAK_NO_POLICY,
            cap_usd=cap,
            spent_usd=None,
            remaining_usd=None,
            allowlist=allowlist,
            action=action_name,
            org=org_out,
            policy_id=policy_id,
            policy_hash=policy_hash,
        )

    if cap is None:
        return _verdict(
            ok=False,
            reason="no_spend_cap",
            speak=SPEAK_NO_CAP,
            cap_usd=None,
            spent_usd=None,
            remaining_usd=None,
            allowlist=allowlist,
            action=action_name,
            org=org_out,
            policy_id=policy_id,
            policy_hash=policy_hash,
        )

    if action_name not in allowlist:
        return _verdict(
            ok=False,
            reason="not_allowlisted",
            speak=SPEAK_NOT_ALLOWLISTED,
            cap_usd=cap,
            spent_usd=None,
            remaining_usd=None,
            allowlist=allowlist,
            action=action_name,
            org=org_out,
            policy_id=policy_id,
            policy_hash=policy_hash,
        )

    amount = Decimal("0") if amount_usd is None else _money(amount_usd)
    if amount is None or amount < 0:
        return _verdict(
            ok=False,
            reason="invalid_amount",
            speak=SPEAK_INVALID_AMOUNT,
            cap_usd=cap,
            spent_usd=None,
            remaining_usd=None,
            allowlist=allowlist,
            action=action_name,
            org=org_out,
            policy_id=policy_id,
            policy_hash=policy_hash,
        )
    amount = _cents(amount)

    spent = _spent_usd(con, org_id)
    if spent is None:
        return _verdict(
            ok=False,
            reason="cap_exceeded",
            speak=SPEAK_CAP_EXCEEDED,
            cap_usd=cap,
            spent_usd=None,
            remaining_usd=None,
            allowlist=allowlist,
            action=action_name,
            org=org_out,
            policy_id=policy_id,
            policy_hash=policy_hash,
        )
    remaining = cap - spent
    if amount > remaining:
        return _verdict(
            ok=False,
            reason="cap_exceeded",
            speak=SPEAK_CAP_EXCEEDED,
            cap_usd=cap,
            spent_usd=spent,
            remaining_usd=remaining,
            allowlist=allowlist,
            action=action_name,
            org=org_out,
            policy_id=policy_id,
            policy_hash=policy_hash,
        )

    remaining_after = remaining - amount
    return _verdict(
        ok=True,
        reason="under_cap",
        speak=(
            f"Spend allowed. Remaining {float(remaining_after)} USD of {float(cap)} USD."
        ),
        cap_usd=cap,
        spent_usd=spent,
        remaining_usd=remaining,
        allowlist=allowlist,
        action=action_name,
        org=org_out,
        policy_id=policy_id,
        policy_hash=policy_hash,
    )


def record_spend(
    con: sqlite3.Connection,
    org_id: str,
    amount_usd: float,
    action: str,
    target_url: str,
    receipt_id: str | None,
) -> dict:
    """Insert a ledger row. Does not take an override. Negative amounts refused."""
    _ensure_schema(con)
    oid = (org_id or "").strip()
    if not oid:
        raise ValueError("org_id required")
    amount = _money(amount_usd)
    if amount is None or amount < 0:
        raise ValueError("amount_usd must be a non-negative number")
    amount = _cents(amount)
    action_name = ("" if action is None else str(action)).strip().lower()
    if not action_name:
        raise ValueError("action required")
    lid = "spd_" + uuid.uuid4().hex[:16]
    created_at = _now()
    con.execute(
        """INSERT INTO spend_ledger
           (id, org_id, amount_usd, action, target_url, receipt_id, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (
            lid,
            oid,
            float(amount),
            action_name,
            (target_url if target_url is None else str(target_url)) or None,
            (receipt_id if receipt_id is None else str(receipt_id)) or None,
            created_at,
        ),
    )
    con.commit()
    return {
        "id": lid,
        "org_id": oid,
        "amount_usd": float(amount),
        "action": action_name,
        "target_url": (target_url if target_url is None else str(target_url)) or None,
        "receipt_id": (receipt_id if receipt_id is None else str(receipt_id)) or None,
        "created_at": created_at,
    }
