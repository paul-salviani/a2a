"""SQLite connection, migrate, seed. Schema may be public; rows stay private."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import envfile

ROOT = Path(__file__).parent
envfile.load_env(ROOT)
_SCHEMA_PATH = ROOT / "schema.sql"
_SEED_PATH = ROOT / "seed.json"
_CATALOG_PATH = ROOT / "catalog.json"


def db_path() -> Path:
    env = os.environ.get("A2A_DB")
    if env:
        return Path(env)
    return ROOT / "data" / "a2a.sqlite"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def db() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL").fetchone()
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(con: sqlite3.Connection, table: str, name: str, spec: str) -> None:
    if name in _columns(con, table):
        return
    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


def migrate(con: sqlite3.Connection) -> None:
    _add_column(con, "policies", "stores_what", "TEXT")
    _add_column(con, "policies", "data_region", "TEXT")
    _add_column(con, "policies", "dispute_email", "TEXT")
    _add_column(con, "policies", "policy_url", "TEXT")
    _add_column(con, "receipts", "caller_id", "TEXT")
    _add_column(con, "receipts", "result", "TEXT")
    _add_column(con, "receipts", "idempotency_key", "TEXT")
    _add_column(con, "checks", "reason", "TEXT")
    _add_column(con, "checks", "query", "TEXT")
    _add_column(con, "api_keys", "revoked", "INTEGER NOT NULL DEFAULT 0")
    _add_column(con, "orgs", "is_seed", "INTEGER NOT NULL DEFAULT 0")
    _add_column(con, "listings", "pond", "TEXT NOT NULL DEFAULT 'ai-tools'")
    _add_column(con, "policies", "spend_cap_usd", "REAL")
    _add_column(con, "policies", "allowlist_json", "TEXT")
    _add_column(con, "policies", "agent_kind", "TEXT")
    _add_column(con, "policies", "last_test_at", "TEXT")
    _add_column(con, "policies", "last_test_result", "TEXT")
    _add_column(con, "policies", "last_test_note", "TEXT")
    con.execute(
        """CREATE TABLE IF NOT EXISTS spend_ledger (
          id TEXT PRIMARY KEY,
          org_id TEXT,
          amount_usd REAL,
          action TEXT,
          target_url TEXT,
          receipt_id TEXT,
          created_at TEXT NOT NULL
        )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_spend_ledger_org ON spend_ledger(org_id)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_spend_ledger_created ON spend_ledger(created_at)"
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS approvals (
          id TEXT PRIMARY KEY,
          query TEXT,
          action TEXT,
          target_url TEXT,
          policy_id TEXT,
          org_id TEXT,
          amount_usd REAL,
          status TEXT NOT NULL DEFAULT 'pending',
          reason TEXT,
          decided_at TEXT,
          created_at TEXT NOT NULL
        )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_approvals_created ON approvals(created_at)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_receipts_idempotency ON receipts(key_id, idempotency_key)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_checks_result_domain ON checks(result, domain)"
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _policy_hash(
    title: str,
    summary: str,
    body: str,
    stores_what: str | None = None,
    data_region: str | None = None,
    dispute_email: str | None = None,
) -> str:
    # Lazy import: db.seed runs after modules load; policy.py imports db.
    import policy as policy_mod

    return policy_mod.policy_hash(
        title, summary, body, stores_what, data_region, dispute_email
    )


def _ensure_write_key(con: sqlite3.Connection, seed: dict) -> None:
    raw = os.environ.get("A2A_WRITE_KEY") or seed.get("write_key")
    if not raw:
        return
    digest = sha256_hex(str(raw))
    existing = con.execute(
        "SELECT id FROM api_keys WHERE key_hash = ?", (digest,)
    ).fetchone()
    if existing:
        return
    con.execute(
        """INSERT INTO api_keys (id, key_hash, role, org_id, label, created_at, revoked)
           VALUES (?,?,?,?,?,?,0)""",
        ("key_write", digest, "write", None, "write", now()),
    )


def _seed(con: sqlite3.Connection, seed: dict, skip_domains: set[str], skip_ids: set[str]) -> None:
    write_key = seed.get("write_key")
    if write_key:
        con.execute(
            """INSERT OR IGNORE INTO api_keys
               (id, key_hash, role, org_id, label, created_at, revoked)
               VALUES (?,?,?,?,?,?,0)""",
            (
                "key_write_demo",
                sha256_hex(str(write_key)),
                "write",
                None,
                "demo write (log_intent)",
                now(),
            ),
        )
    ts = now()
    for org in seed.get("orgs") or []:
        oid = org["id"]
        domain = (org.get("domain") or "").lower()
        if oid in skip_ids or domain in skip_domains:
            continue
        con.execute(
            """INSERT OR IGNORE INTO orgs
               (id, name, domain, website, category, attested, is_seed, created_at)
               VALUES (?,?,?,?,?,?,1,?)""",
            (
                oid,
                org["name"],
                domain,
                org.get("website"),
                org.get("category"),
                int(org.get("attested", 0)),
                ts,
            ),
        )
        pub = org.get("publish_key")
        if pub:
            con.execute(
                """INSERT OR IGNORE INTO api_keys
                   (id, key_hash, role, org_id, label, created_at, revoked)
                   VALUES (?,?,?,?,?,?,0)""",
                (
                    "key_" + oid,
                    sha256_hex(str(pub)),
                    "org_publish",
                    oid,
                    "publish " + org["name"],
                    ts,
                ),
            )
        p = org.get("policy") or {}
        status = org.get("status", "active")
        title = p.get("title") or "Policy"
        summary = p.get("summary") or ""
        body = p.get("body") or ""
        stores_what = p.get("stores_what")
        data_region = p.get("data_region")
        dispute_email = p.get("dispute_email")
        policy_url = p.get("policy_url")
        ph = _policy_hash(title, summary, body, stores_what, data_region, dispute_email)
        pid = "pol_" + oid + "_v1"
        con.execute(
            """INSERT OR IGNORE INTO policies
               (id, org_id, version, title, summary, body, status, policy_hash, published_at,
                stores_what, data_region, dispute_email, policy_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pid,
                oid,
                1,
                title,
                summary,
                body,
                status,
                ph,
                ts,
                stores_what,
                data_region,
                dispute_email,
                policy_url,
            ),
        )


def _has_policy(con: sqlite3.Connection, org_id: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM policies WHERE org_id = ? LIMIT 1", (org_id,)
        ).fetchone()
        is not None
    )


def _upsert_catalog(con: sqlite3.Connection, ideas: list[dict]) -> None:
    ts = now()
    for idea in ideas:
        iid = idea["id"]
        domain = (idea.get("domain") or "").lower()
        need_json = json.dumps(idea.get("need") or [], ensure_ascii=False)
        attested = int(idea.get("attested", 0))
        name = idea.get("name") or iid
        website = idea.get("website")
        category = idea.get("category")
        summary = idea.get("summary")
        existing_idea = con.execute("SELECT id FROM ideas WHERE id = ?", (iid,)).fetchone()
        if existing_idea:
            con.execute(
                """UPDATE ideas
                   SET name=?, domain=?, website=?, category=?, need_json=?, summary=?, attested=?
                   WHERE id=?""",
                (name, domain, website, category, need_json, summary, attested, iid),
            )
        else:
            con.execute(
                """INSERT INTO ideas
                   (id, name, domain, website, category, need_json, summary, attested, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (iid, name, domain, website, category, need_json, summary, attested, ts),
            )

        org = con.execute("SELECT id FROM orgs WHERE id = ?", (iid,)).fetchone()
        clash = con.execute(
            "SELECT id FROM orgs WHERE domain = ? AND id != ?",
            (domain, iid),
        ).fetchone()
        if org:
            if not clash:
                con.execute(
                    """UPDATE orgs
                       SET name=?, domain=?, website=?, category=?, attested=?, is_seed=1
                       WHERE id=?""",
                    (name, domain, website, category, attested, iid),
                )
        elif clash:
            pass
        else:
            con.execute(
                """INSERT INTO orgs
                   (id, name, domain, website, category, attested, is_seed, created_at)
                   VALUES (?,?,?,?,?,?,1,?)""",
                (iid, name, domain, website, category, attested, ts),
            )

        present = con.execute("SELECT id FROM orgs WHERE id = ?", (iid,)).fetchone()
        pol = idea.get("policy") or {}
        if present and pol and not _has_policy(con, iid):
            title = pol.get("title") or "Policy"
            psummary = pol.get("summary") or ""
            body = pol.get("body") or ""
            stores_what = pol.get("stores_what")
            data_region = pol.get("data_region")
            dispute_email = pol.get("dispute_email")
            policy_url = pol.get("policy_url")
            ph = _policy_hash(title, psummary, body, stores_what, data_region, dispute_email)
            pid = f"pol_{iid}_v1"
            con.execute(
                """INSERT OR IGNORE INTO policies
                   (id, org_id, version, title, summary, body, status, policy_hash, published_at,
                    stores_what, data_region, dispute_email, policy_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pid,
                    iid,
                    1,
                    title,
                    psummary,
                    body,
                    "active",
                    ph,
                    ts,
                    stores_what,
                    data_region,
                    dispute_email,
                    policy_url,
                ),
            )


def _mark_seed_flags(con: sqlite3.Connection, seed: dict, ideas: list[dict]) -> None:
    # seed.json and catalog.json ids are typed-in fixtures, not recommendable orgs.
    seed_ids = tuple(o["id"] for o in (seed.get("orgs") or []) if o.get("id"))
    if seed_ids:
        q = ",".join("?" * len(seed_ids))
        con.execute(f"UPDATE orgs SET is_seed = 1 WHERE id IN ({q})", seed_ids)
    catalog_ids = tuple(i["id"] for i in ideas if i.get("id"))
    if catalog_ids:
        q = ",".join("?" * len(catalog_ids))
        con.execute(f"UPDATE orgs SET is_seed = 1 WHERE id IN ({q})", catalog_ids)


def init_db() -> None:
    seed = _load_json(_SEED_PATH) if _SEED_PATH.is_file() else {"orgs": [], "write_key": None}
    ideas: list[dict] = []
    if _CATALOG_PATH.is_file():
        catalog = _load_json(_CATALOG_PATH)
        raw_ideas = catalog.get("ideas") or []
        ideas = [i for i in raw_ideas if isinstance(i, dict) and i.get("id")]
    catalog_domains = {(i.get("domain") or "").lower() for i in ideas if i.get("domain")}
    catalog_ids = {i["id"] for i in ideas}

    con = db()
    try:
        con.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        migrate(con)
        n = con.execute("SELECT COUNT(*) AS c FROM orgs").fetchone()["c"]
        if n == 0:
            _seed(con, seed, skip_domains=catalog_domains, skip_ids=catalog_ids)
        _upsert_catalog(con, ideas)
        _mark_seed_flags(con, seed, ideas)
        _ensure_write_key(con, seed)
        con.commit()
    finally:
        con.close()
