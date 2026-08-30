"""API keys: hash-only storage, lookup by hash, skip revoked."""
from __future__ import annotations

import secrets
import sqlite3
import uuid

from db import now, sha256_hex
from policy import extract_domain


def lookup_key(con: sqlite3.Connection, raw_key: str | None) -> sqlite3.Row | None:
    if not raw_key or not str(raw_key).strip():
        return None
    digest = sha256_hex(str(raw_key).strip())
    row = con.execute("SELECT * FROM api_keys WHERE key_hash = ?", (digest,)).fetchone()
    if not row:
        return None
    if "revoked" in row.keys() and row["revoked"]:
        return None
    return row


def require_write(con: sqlite3.Connection, raw_key: str | None) -> sqlite3.Row:
    row = lookup_key(con, raw_key)
    if row is None:
        raise PermissionError("write key required")
    if row["role"] not in ("write", "org_publish"):
        raise PermissionError("write key required")
    return row


def require_org_publish(con: sqlite3.Connection, raw_key: str | None) -> sqlite3.Row:
    row = lookup_key(con, raw_key)
    if row is None or row["role"] != "org_publish" or not row["org_id"]:
        raise PermissionError("org publish key required")
    return row


def signup_org(con: sqlite3.Connection, payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    site_url = str(payload.get("site_url") or payload.get("website") or "").strip()
    category = str(payload.get("category") or "").strip() or None
    if not name or not site_url:
        raise ValueError("name and site_url required")
    domain = extract_domain(site_url)
    if not domain:
        raise ValueError("site_url must have a hostname")
    taken = con.execute("SELECT id FROM orgs WHERE domain = ?", (domain,)).fetchone()
    if taken:
        raise ValueError("domain already registered")
    org_id = "org_" + uuid.uuid4().hex[:16]
    publish_key = "a2a_org_" + secrets.token_urlsafe(24)
    ts = now()
    try:
        con.execute(
            """INSERT INTO orgs
               (id, name, domain, website, category, attested, is_seed, created_at)
               VALUES (?,?,?,?,?,0,0,?)""",
            (org_id, name, domain, site_url, category, ts),
        )
        con.execute(
            """INSERT INTO api_keys
               (id, key_hash, role, org_id, label, created_at, revoked)
               VALUES (?,?,?,?,?,?,0)""",
            (
                "key_" + org_id,
                sha256_hex(publish_key),
                "org_publish",
                org_id,
                "publish " + name,
                ts,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("domain already registered") from exc
    con.commit()
    return {
        "org_id": org_id,
        "domain": domain,
        "website": site_url,
        "category": category,
        "publish_key": publish_key,
        "note": "Store this key. We only keep the hash.",
    }


def rotate_org_key(
    con: sqlite3.Connection, org_id: str, write_key_row: sqlite3.Row
) -> dict:
    if write_key_row is None or write_key_row["role"] != "write":
        raise PermissionError("write key required")
    if "revoked" in write_key_row.keys() and write_key_row["revoked"]:
        raise PermissionError("write key required")
    org = con.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()
    if org is None:
        raise ValueError("org not found")
    publish_key = "a2a_org_" + secrets.token_urlsafe(24)
    digest = sha256_hex(publish_key)
    existing = con.execute(
        "SELECT id FROM api_keys WHERE org_id = ? AND role = 'org_publish'",
        (org_id,),
    ).fetchone()
    if existing:
        con.execute(
            "UPDATE api_keys SET key_hash = ?, revoked = 0 WHERE id = ?",
            (digest, existing["id"]),
        )
    else:
        con.execute(
            """INSERT INTO api_keys
               (id, key_hash, role, org_id, label, created_at, revoked)
               VALUES (?,?,?,?,?,?,0)""",
            (
                "key_" + org_id,
                digest,
                "org_publish",
                org_id,
                "publish " + org["name"],
                now(),
            ),
        )
    con.commit()
    return {
        "org_id": org_id,
        "publish_key": publish_key,
        "note": "Store this key. We only keep the hash.",
    }
