"""whats_good_for and listing ingest. Attested + unique-in-pond. Never invent."""
from __future__ import annotations

import re
import sqlite3
import uuid
from urllib.parse import urlparse

import db
import policy
import tape
import uniqueness

WHATS_GOOD_CAP = int(getattr(db, "WHATS_GOOD_CAP", 24) or 24)
DEFAULT_POND = getattr(uniqueness, "DEFAULT_POND", "ai-tools")
TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
STOPWORDS = frozenset(
    "a an the and or of to for in on at is it as be by with".split()
)

EMPTY_QUERY_NOTE = "Need a query. Nothing invented."
EMPTY_QUERY_SPEAK = "I need a query. Nothing invented."
EMPTY_MATCH_NOTE = "No attested listing matched those terms. Nothing invented."
EMPTY_MATCH_SPEAK = "I can't recommend anything attested for that yet. Nothing invented."
NONEMPTY_NOTE = (
    "Attested only. Unique within pond first. Unattested omitted. Nothing invented."
)


def now() -> str:
    return db.now()


def _field(row, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in TOKEN_RE.finditer(text or "")]


def query_tokens(query: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokenize(query):
        if tok in STOPWORDS or tok in seen or len(tok) < 2:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _norm_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = (parsed.path or "").rstrip("/")
    return host + path


def _is_local_domain(domain: str | None) -> bool:
    d = (domain or "").lower().strip()
    return d.endswith(".local")


def _is_seed(org) -> bool:
    if org is None:
        return True
    return bool(_field(org, "is_seed", 0))


def _eligible_org(org) -> bool:
    if org is None:
        return False
    if not _field(org, "attested"):
        return False
    if _is_seed(org):
        return False
    if _is_local_domain(_field(org, "domain")):
        return False
    return True


def _live_policy(con: sqlite3.Connection, org_id: str):
    if not org_id:
        return None
    pol = policy.latest_policy(con, org_id)
    if not pol:
        return None
    status = (_field(pol, "status") or "").strip().lower()
    if status != "active":
        return None
    if not _field(pol, "policy_hash"):
        return None
    return pol


def _match_score(
    qtoks: list[str],
    name: str,
    category: str,
    summary: str,
    domain: str,
) -> tuple[int, list[str]]:
    if not qtoks:
        return 0, []
    name_toks = set(tokenize(name))
    cat_toks = set(tokenize(category))
    sum_toks = set(tokenize(summary))
    dom_toks = set(tokenize(domain))
    matched: list[str] = []
    score = 0
    for tok in qtoks:
        hit = False
        if tok in name_toks:
            score += 4
            hit = True
        if tok in cat_toks:
            score += 3
            hit = True
        if tok in dom_toks:
            score += 3
            hit = True
        if tok in sum_toks:
            score += 1
            hit = True
        if hit:
            matched.append(tok)
    return score, matched


def _speak(name: str, policy_hash: str) -> str:
    return f"Use {name}. Policy hash {policy_hash}."


def _org_by_id(con: sqlite3.Connection, org_id: str):
    if not org_id:
        return None
    return con.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()


def _listing_pond(row) -> str:
    return (_field(row, "pond") or DEFAULT_POND)


def _best_listing_for_org(listings: list, org_id: str, pond: str | None):
    best = None
    best_score = -1
    for r in listings:
        if _field(r, "org_id") != org_id:
            continue
        if pond and _listing_pond(r) != pond:
            continue
        sc = int(_field(r, "unique_score") or 0)
        if sc > best_score:
            best_score = sc
            best = r
    return best


def listing_blob_text(name: str, one_liner: str) -> str:
    blob = getattr(uniqueness, "listing_blob", None)
    if callable(blob):
        return blob(name, one_liner)
    return ((name or "") + " " + (one_liner or "")).strip()


def _cap(limit) -> int:
    try:
        n = WHATS_GOOD_CAP if limit is None else int(limit)
    except (TypeError, ValueError):
        n = WHATS_GOOD_CAP
    return max(0, min(n, WHATS_GOOD_CAP))


def _load_listings(con: sqlite3.Connection, pond: str | None) -> list:
    try:
        rows = con.execute("SELECT * FROM listings").fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        if pond and _listing_pond(r) != pond:
            continue
        out.append(r)
    return out


def whats_good_for(
    con: sqlite3.Connection,
    query: str,
    pond: str | None = None,
    limit: int = 24,
) -> dict:
    qtoks = query_tokens(query)
    if not (query or "").strip() or not qtoks:
        return {
            "query": query,
            "matches": [],
            "note": EMPTY_QUERY_NOTE,
            "speak": EMPTY_QUERY_SPEAK,
        }

    pond_filter = (pond or "").strip() or DEFAULT_POND
    cap = _cap(limit)
    seen: set[tuple[str, str]] = set()
    scored: list[dict] = []

    listing_rows = _load_listings(con, pond_filter)
    all_listings = _load_listings(con, None)

    for r in listing_rows:
        org_id = _field(r, "org_id")
        if not org_id:
            continue
        org = _org_by_id(con, org_id)
        if not _eligible_org(org):
            continue
        pol = _live_policy(con, org_id)
        if not pol:
            continue
        name = _field(r, "name") or ""
        summary = _field(r, "one_liner") or ""
        website = _field(r, "url") or ""
        domain = _field(org, "domain") or policy.extract_domain(website) or ""
        category = _field(org, "category") or _listing_pond(r)
        score, matched = _match_score(qtoks, name, category, summary, domain)
        if score <= 0:
            continue
        key = (org_id, _norm_url(website))
        seen.add(key)
        ph = _field(pol, "policy_hash")
        scored.append(
            {
                "id": _field(r, "id"),
                "name": name,
                "website": website,
                "domain": domain,
                "category": category,
                "summary": summary,
                "unique_score": int(_field(r, "unique_score") or 0),
                "unique_note": _field(r, "unique_note") or "",
                "pond": _listing_pond(r),
                "trust": "attested",
                "policy_id": _field(pol, "id"),
                "policy_hash": ph,
                "kind": "listing",
                "score": score,
                "matched_terms": matched,
                "speak": _speak(name, ph),
            }
        )

    try:
        orgs = con.execute("SELECT * FROM orgs").fetchall()
    except sqlite3.OperationalError:
        orgs = []

    for org in orgs:
        if not _eligible_org(org):
            continue
        org_id = _field(org, "id")
        pol = _live_policy(con, org_id)
        if not pol:
            continue
        website = _field(org, "website") or ""
        domain = _field(org, "domain") or policy.extract_domain(website) or ""
        if not website and domain:
            website = "https://" + domain
        key = (org_id, _norm_url(website))
        if key in seen:
            continue
        name = _field(org, "name") or ""
        category = _field(org, "category") or ""
        summary = _field(pol, "summary") or ""
        score, matched = _match_score(qtoks, name, category, summary, domain)
        if score <= 0:
            continue
        best = _best_listing_for_org(all_listings, org_id, pond_filter)
        if best is None:
            best = _best_listing_for_org(all_listings, org_id, None)
        unique_score = int(_field(best, "unique_score") or 0) if best else 0
        unique_note = (_field(best, "unique_note") or "") if best else ""
        listing_pond = _listing_pond(best) if best else None
        ph = _field(pol, "policy_hash")
        seen.add(key)
        scored.append(
            {
                "id": org_id,
                "name": name,
                "website": website,
                "domain": domain,
                "category": category,
                "summary": summary,
                "unique_score": unique_score,
                "unique_note": unique_note,
                "pond": listing_pond,
                "trust": "attested",
                "policy_id": _field(pol, "id"),
                "policy_hash": ph,
                "kind": "org",
                "score": score,
                "matched_terms": matched,
                "speak": _speak(name, ph),
            }
        )

    scored.sort(
        key=lambda x: (
            -int(x.get("unique_score") or 0),
            -int(x.get("score") or 0),
            (x.get("name") or "").lower(),
        )
    )
    matches = scored[:cap]
    tape.record_search(con, query, pond_filter, len(matches))
    con.commit()

    if not matches:
        return {
            "query": query,
            "matches": [],
            "note": EMPTY_MATCH_NOTE,
            "speak": EMPTY_MATCH_SPEAK,
        }
    return {
        "query": query,
        "matches": matches,
        "note": NONEMPTY_NOTE,
    }


def ingest_listing(con: sqlite3.Connection, payload: dict) -> dict:
    payload = payload or {}
    name = (payload.get("name") or "").strip()
    url = (payload.get("url") or "").strip()
    if not name or not url:
        raise ValueError("name and url required")
    pond = (payload.get("pond") or DEFAULT_POND).strip() or DEFAULT_POND
    one = (payload.get("one_liner") or payload.get("summary") or "").strip()
    existing = con.execute("SELECT * FROM listings WHERE url = ?", (url,)).fetchone()
    old_pond = _listing_pond(existing) if existing else None
    if "org_id" in payload:
        org_id = payload.get("org_id")
        if org_id is not None:
            org_id = str(org_id).strip() or None
    else:
        org_id = _field(existing, "org_id") if existing else None
    exclude_id = _field(existing, "id") if existing else None
    score, note = uniqueness.score_in_pond(
        con, listing_blob_text(name, one), pond=pond, exclude_id=exclude_id
    )
    lid = exclude_id if existing else "lst_" + uuid.uuid4().hex[:12]
    if existing:
        con.execute(
            """UPDATE listings
               SET name=?, one_liner=?, unique_note=?, unique_score=?, pond=?, org_id=?
               WHERE id=?""",
            (name, one, note, score, pond, org_id, lid),
        )
    else:
        con.execute(
            """INSERT INTO listings
               (id, name, url, one_liner, unique_note, unique_score, org_id, pond, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (lid, name, url, one, note, score, org_id, pond, now()),
        )
    uniqueness.rescore_pond(con, pond)
    if old_pond and old_pond != pond:
        uniqueness.rescore_pond(con, old_pond)
    con.commit()
    row = con.execute("SELECT * FROM listings WHERE id = ?", (lid,)).fetchone()
    stored_score = _field(row, "unique_score") if row is not None else None
    stored_note = _field(row, "unique_note") if row is not None else None
    return {
        "id": lid,
        "name": name,
        "url": url,
        "one_liner": one,
        "unique_score": int(stored_score if stored_score is not None else score),
        "unique_note": stored_note or note,
        "pond": pond,
        "org_id": _field(row, "org_id") if row is not None else org_id,
    }
