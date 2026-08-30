"""Policy gate. Fail closed. Hash the body, not the URL. Log pass and fail.

Spend cap and nutrition stamp live on the policy version; they are not in the hash.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from urllib.parse import urlparse

import uniqueness
from db import now, sha256_hex

try:
    import spend as spend_mod
except ImportError:
    spend_mod = None

try:
    import nutrition as nutrition_mod
except ImportError:
    nutrition_mod = None

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

SPEAK_FAIL = (
    "I can't recommend that yet. They have no live policy on the switchboard. "
    "They can create an account, publish a policy, and give me the API key."
)
SPEAK_FIXTURE = (
    "{name} has a fixture policy for smoke only, not a recommendation."
)


def extract_domain(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).hostname or ""
    return host.lower().removeprefix("www.")


def _parse_url(url: str) -> tuple[str, int | None, str]:
    raw = (url or "").strip()
    if not raw:
        return "", None, ""
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    port = parsed.port
    if port is None and parsed.scheme == "http":
        port = 80
    elif port is None and parsed.scheme == "https":
        port = 443
    path = (parsed.path or "/").rstrip("/") or "/"
    return host, port, path


def org_by_domain(con: sqlite3.Connection, domain: str) -> sqlite3.Row | None:
    if not domain:
        return None
    row = con.execute("SELECT * FROM orgs WHERE domain = ?", (domain,)).fetchone()
    if row:
        return row
    return con.execute(
        "SELECT * FROM orgs WHERE ? LIKE '%.' || domain OR domain = ?",
        (domain, domain),
    ).fetchone()


def org_by_url(con: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    host, port, path = _parse_url(url)
    domain = extract_domain(url)
    if host not in LOCAL_HOSTS:
        org = org_by_domain(con, domain)
        if org:
            return org
    rows = con.execute("SELECT * FROM orgs").fetchall()
    hits: list[sqlite3.Row] = []
    for row in rows:
        website = (row["website"] or "").strip()
        if not website:
            continue
        wh, wport, wpath = _parse_url(website)
        if not host or host != wh:
            continue
        if host in LOCAL_HOSTS:
            if port is None or wport is None or port != wport:
                continue
        elif port is not None and wport is not None and port != wport:
            continue
        hits.append(row)
    if not hits:
        if host in LOCAL_HOSTS:
            return None
        return org_by_domain(con, domain)
    if len(hits) == 1:
        return hits[0]
    for row in hits:
        _wh, _wport, wpath = _parse_url(row["website"] or "")
        if path == wpath or path.startswith(wpath.rstrip("/") + "/"):
            return row
    return hits[0]


def latest_policy(con: sqlite3.Connection, org_id: str) -> sqlite3.Row | None:
    """Latest version. Caller fails if status is not active."""
    return con.execute(
        "SELECT * FROM policies WHERE org_id = ? ORDER BY version DESC LIMIT 1",
        (org_id,),
    ).fetchone()


def policy_hash(
    title: str | None,
    summary: str | None,
    body: str | None,
    stores_what: str | None,
    data_region: str | None,
    dispute_email: str | None,
) -> str:
    canonical = json.dumps(
        {
            "body": body or "",
            "data_region": data_region or "",
            "dispute_email": dispute_email or "",
            "stores_what": stores_what or "",
            "summary": summary or "",
            "title": title or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256_hex(canonical)


def _col(row: sqlite3.Row | None, key: str, default=None):
    if row is None or key not in row.keys():
        return default
    return row[key]


def _is_fixture(org: sqlite3.Row | None, domain: str | None = None) -> bool:
    if org is not None:
        if bool(_col(org, "is_seed", 0)):
            return True
        org_domain = (org["domain"] or "").lower()
        if org_domain.endswith(".local"):
            return True
    d = (domain or "").lower()
    return d.endswith(".local")


def _org_public(org: sqlite3.Row | None) -> dict | None:
    if org is None:
        return None
    return {
        "id": org["id"],
        "name": org["name"],
        "domain": org["domain"],
        "website": org["website"],
        "category": org["category"],
        "attested": bool(org["attested"]),
        "is_seed": bool(_col(org, "is_seed", 0)),
    }


def _field(payload: dict, key: str, last: sqlite3.Row | None, default: str = "") -> str:
    if key in payload and payload[key] is not None:
        return str(payload[key])
    if last is not None and key in last.keys() and last[key] is not None:
        return str(last[key])
    return default


def _opt_extra(payload: dict, key: str, last: sqlite3.Row | None):
    if key in payload and payload[key] is not None:
        val = payload[key]
        if isinstance(val, str):
            val = val.strip()
            return val or None
        return val
    if last is not None and key in last.keys() and last[key] is not None:
        return last[key]
    return None


def _spend_cap_value(raw):
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else raw
    s = str(raw).strip()
    if not s:
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def _allowlist_stored(raw):
    if raw is None:
        return None
    if spend_mod is not None:
        fn = getattr(spend_mod, "parse_allowlist", None)
        if callable(fn):
            return json.dumps(fn(raw), ensure_ascii=False)
    if isinstance(raw, (list, tuple)):
        return json.dumps(list(raw), ensure_ascii=False)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            return json.dumps([s], ensure_ascii=False)
        if isinstance(parsed, (list, tuple)):
            return json.dumps(list(parsed), ensure_ascii=False)
        return json.dumps(parsed, ensure_ascii=False)
    return json.dumps(raw, ensure_ascii=False)


def _allowlist_out(raw):
    if raw is None:
        return None
    if spend_mod is not None:
        fn = getattr(spend_mod, "parse_allowlist", None)
        if callable(fn):
            return fn(raw)
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            return [s]
        return parsed
    return raw


def _nutrition_label(policy_row: sqlite3.Row | None, org_row: sqlite3.Row | None = None):
    if nutrition_mod is None:
        return None
    fn = getattr(nutrition_mod, "label_from_policy", None)
    if not callable(fn):
        return None
    try:
        return fn(policy_row, org_row)
    except TypeError:
        return fn(policy_row)


def _policy_columns(con: sqlite3.Connection) -> set[str]:
    return {r[1] for r in con.execute("PRAGMA table_info(policies)").fetchall()}


def trust_check(con: sqlite3.Connection, url: str) -> dict:
    raw_url = url if isinstance(url, str) else ("" if url is None else str(url))
    domain = extract_domain(raw_url)
    org = org_by_url(con, raw_url) if raw_url.strip() else None
    policy = latest_policy(con, org["id"]) if org else None
    ok = False
    reason = "no_live_policy"
    summary = "No attested policy for this URL."
    if org is None or policy is None:
        reason = "no_live_policy"
        summary = "No attested policy for this URL."
    elif policy["status"] != "active":
        reason = "policy_not_active"
        summary = f"Policy {policy['status']}: {policy['summary']}"
    elif not org["attested"]:
        reason = "not_attested"
        summary = "Org is not attested. " + (policy["summary"] or "")
    else:
        ok = True
        reason = "active_policy"
        summary = policy["summary"]
    result = "pass" if ok else "fail"
    checked_at = now()
    check_id = "chk_" + uuid.uuid4().hex[:16]
    policy_id = policy["id"] if policy else None
    ph = policy["policy_hash"] if policy else None
    policy_url = _col(policy, "policy_url") if policy else None
    org_domain = (org["domain"] or "") if org is not None else ""
    fixture = _is_fixture(org, domain or org_domain)
    recommendable = bool(ok and org is not None and not fixture)
    if recommendable and ph:
        speak = f"Use {org['name']}. Policy hash {ph}."
    elif ok and org is not None and fixture:
        speak = SPEAK_FIXTURE.format(name=org["name"])
    else:
        speak = SPEAK_FAIL
    con.execute(
        """INSERT INTO checks
           (id, url, domain, result, policy_id, policy_hash, summary, created_at, reason, query)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            check_id,
            raw_url,
            domain or None,
            result,
            policy_id,
            ph,
            summary,
            checked_at,
            reason,
            None,
        ),
    )
    con.commit()
    out = {
        "pass": ok,
        "result": result,
        "reason": reason,
        "policy_id": policy_id,
        "policy_hash": ph,
        "policy_url": policy_url,
        "summary": summary,
        "checked_at": checked_at,
        "check_id": check_id,
        "speak": speak,
        "url": raw_url,
        "domain": domain,
        "org": _org_public(org),
        "recommendable": recommendable,
    }
    if policy is not None:
        if "spend_cap_usd" in policy.keys():
            out["spend_cap_usd"] = policy["spend_cap_usd"]
        if "allowlist_json" in policy.keys():
            out["allowlist"] = _allowlist_out(policy["allowlist_json"])
        elif "allowlist" in policy.keys():
            out["allowlist"] = _allowlist_out(policy["allowlist"])
        if nutrition_mod is not None:
            out["nutrition"] = _nutrition_label(policy, org)
    return out


def _upsert_listing(
    con: sqlite3.Connection,
    org: sqlite3.Row,
    summary: str,
    pond: str,
) -> None:
    url = (org["website"] or "").strip()
    if not url:
        return
    name = org["name"]
    existing = con.execute("SELECT * FROM listings WHERE url = ?", (url,)).fetchone()
    if existing is None:
        existing = con.execute(
            "SELECT * FROM listings WHERE org_id = ?", (org["id"],)
        ).fetchone()
    if existing:
        con.execute(
            """UPDATE listings
               SET name=?, url=?, one_liner=?, org_id=?, pond=?
               WHERE id=?""",
            (name, url, summary, org["id"], pond, existing["id"]),
        )
    else:
        lid = "lst_" + uuid.uuid4().hex[:12]
        con.execute(
            """INSERT INTO listings
               (id, name, url, one_liner, unique_note, unique_score, org_id, pond, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (lid, name, url, summary, None, 0, org["id"], pond, now()),
        )
    uniqueness.rescore_pond(con, pond)


def publish_policy(
    con: sqlite3.Connection, payload: dict, key_row: sqlite3.Row
) -> dict:
    if key_row is None or not key_row["org_id"]:
        raise PermissionError("org publish key required")
    if key_row["role"] != "org_publish":
        raise PermissionError("org publish key required")
    if "revoked" in key_row.keys() and key_row["revoked"]:
        raise PermissionError("org publish key required")
    org_id = key_row["org_id"]
    org = con.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()
    if org is None:
        raise ValueError("org not found")
    last = latest_policy(con, org_id)
    title = _field(payload, "title", last).strip()
    if not title:
        raise ValueError("title required")
    summary = _field(payload, "summary", last)
    body = _field(payload, "body", last)
    stores_what = _field(payload, "stores_what", last)
    data_region = _field(payload, "data_region", last)
    dispute_email = _field(payload, "dispute_email", last)
    policy_url = _field(payload, "policy_url", last)
    status = str(payload["status"]).strip() if payload.get("status") else "active"
    if not status:
        status = "active"
    if "spend_cap_usd" in payload and payload["spend_cap_usd"] is not None:
        spend_cap_usd = _spend_cap_value(payload["spend_cap_usd"])
    elif last is not None and "spend_cap_usd" in last.keys():
        spend_cap_usd = last["spend_cap_usd"]
    else:
        spend_cap_usd = None
    raw_allow = None
    if "allowlist" in payload and payload["allowlist"] is not None:
        raw_allow = payload["allowlist"]
    elif "allowlist_json" in payload and payload["allowlist_json"] is not None:
        raw_allow = payload["allowlist_json"]
    elif last is not None:
        if "allowlist_json" in last.keys() and last["allowlist_json"] is not None:
            raw_allow = last["allowlist_json"]
        elif "allowlist" in last.keys() and last["allowlist"] is not None:
            raw_allow = last["allowlist"]
    allowlist_stored = _allowlist_stored(raw_allow) if raw_allow is not None else None
    agent_kind = _opt_extra(payload, "agent_kind", last)
    if agent_kind is not None and not isinstance(agent_kind, str):
        agent_kind = str(agent_kind)
    last_test_at = _opt_extra(payload, "last_test_at", last)
    if last_test_at is not None and not isinstance(last_test_at, str):
        last_test_at = str(last_test_at)
    last_test_result = _opt_extra(payload, "last_test_result", last)
    if last_test_result is not None and not isinstance(last_test_result, str):
        last_test_result = str(last_test_result)
    last_test_note = _opt_extra(payload, "last_test_note", last)
    if last_test_note is not None and not isinstance(last_test_note, str):
        last_test_note = str(last_test_note)
    version = (last["version"] + 1) if last else 1
    ph = policy_hash(title, summary, body, stores_what, data_region, dispute_email)
    pid = f"pol_{org_id}_v{version}"
    old_cols = (
        pid,
        org_id,
        version,
        title,
        summary,
        body,
        status,
        ph,
        now(),
        stores_what or None,
        data_region or None,
        dispute_email or None,
        policy_url or None,
    )
    old_sql = """INSERT INTO policies
           (id, org_id, version, title, summary, body, status, policy_hash, published_at,
            stores_what, data_region, dispute_email, policy_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    present = _policy_columns(con)
    extras: dict[str, object] = {}
    if "spend_cap_usd" in present:
        extras["spend_cap_usd"] = spend_cap_usd
    if "allowlist_json" in present:
        extras["allowlist_json"] = allowlist_stored
    elif "allowlist" in present:
        extras["allowlist"] = allowlist_stored
    if "agent_kind" in present:
        extras["agent_kind"] = agent_kind
    if "last_test_at" in present:
        extras["last_test_at"] = last_test_at
    if "last_test_result" in present:
        extras["last_test_result"] = last_test_result
    if "last_test_note" in present:
        extras["last_test_note"] = last_test_note
    extra_names = list(extras)
    try:
        if extra_names:
            names = (
                "id, org_id, version, title, summary, body, status, policy_hash, "
                "published_at, stores_what, data_region, dispute_email, policy_url, "
                + ", ".join(extra_names)
            )
            placeholders = ",".join("?" * (13 + len(extra_names)))
            con.execute(
                f"INSERT INTO policies ({names}) VALUES ({placeholders})",
                old_cols + tuple(extras[n] for n in extra_names),
            )
        else:
            con.execute(old_sql, old_cols)
    except sqlite3.OperationalError:
        con.execute(old_sql, old_cols)
    attested = bool(org["attested"])
    is_seed = bool(_col(org, "is_seed", 0))
    if status == "active" and not is_seed:
        con.execute("UPDATE orgs SET attested = 1 WHERE id = ?", (org_id,))
        attested = True
        pond = (org["category"] or "").strip() or "ai-tools"
        _upsert_listing(con, org, summary, pond)
    con.commit()
    return {
        "policy_id": pid,
        "version": version,
        "policy_hash": ph,
        "status": status,
        "attested": attested,
        "spend_cap_usd": spend_cap_usd,
        "allowlist": _allowlist_out(allowlist_stored),
        "agent_kind": agent_kind,
        "last_test_at": last_test_at,
        "last_test_result": last_test_result,
        "last_test_note": last_test_note,
    }
