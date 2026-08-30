"""Uniqueness inside one pond. TF-IDF cosine on words + char 3-grams.
No GPU. Optional later: real embeddings if an API key is present — not required.
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter

DEFAULT_POND = "ai-tools"
TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
STOP = frozenset(
    "a an the and or of to for in on at is it as be by with ai tool app new".split()
)


def tokens(text: str) -> list[str]:
    words = [m.group(0).lower() for m in TOKEN_RE.finditer(text or "")]
    words = [w for w in words if w not in STOP and len(w) > 1]
    compact = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    grams = [compact[i : i + 3] for i in range(max(0, len(compact) - 2))]
    return words + grams


def listing_blob(name: str | None, one_liner: str | None) -> str:
    return ((name or "") + " " + (one_liner or "")).strip()


def tf(toks: list[str]) -> dict[str, float]:
    c = Counter(toks)
    n = sum(c.values()) or 1
    return {k: v / n for k, v in c.items()}


def idf(docs: list[list[str]]) -> dict[str, float]:
    df: Counter[str] = Counter()
    for d in docs:
        for t in set(d):
            df[t] += 1
    n = len(docs) or 1
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


def tfidf(tf_map: dict[str, float], idf_map: dict[str, float]) -> dict[str, float]:
    return {k: tf_map[k] * idf_map.get(k, 1.0) for k in tf_map}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values())) or 1.0
    nb = math.sqrt(sum(v * v for v in b.values())) or 1.0
    return dot / (na * nb)


def _field(row, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _row_pond(row) -> str:
    return (_field(row, "pond") or DEFAULT_POND)


def _rowid(row) -> int | None:
    val = _field(row, "rowid")
    if val is None:
        val = _field(row, "oid")
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _is_earlier(peer, subject_created: str | None, subject_id: str | None, subject_rowid: int | None) -> bool:
    """True if peer already existed when subject arrived. created_at, then rowid, then id."""
    pc = _field(peer, "created_at") or ""
    sc = subject_created or ""
    if pc != sc:
        return pc < sc
    pr = _rowid(peer)
    if pr is not None and subject_rowid is not None:
        return pr < subject_rowid
    return (_field(peer, "id") or "") < (subject_id or "")


def _load_listings(con: sqlite3.Connection) -> list:
    try:
        return con.execute(
            "SELECT id, name, one_liner, pond, created_at, rowid FROM listings"
        ).fetchall()
    except sqlite3.OperationalError:
        pass
    try:
        return con.execute(
            "SELECT id, name, one_liner, pond, created_at FROM listings"
        ).fetchall()
    except sqlite3.OperationalError:
        pass
    try:
        return con.execute("SELECT id, name, one_liner, pond FROM listings").fetchall()
    except sqlite3.OperationalError:
        pass
    try:
        return con.execute("SELECT id, name, one_liner FROM listings").fetchall()
    except sqlite3.OperationalError:
        return []


def _subject_cutoff(
    con: sqlite3.Connection, exclude_id: str | None
) -> tuple[str | None, str | None, int | None] | None:
    """(created_at, id, rowid) of the listing being scored, or None if not in the pond yet."""
    if not exclude_id:
        return None
    try:
        row = con.execute(
            "SELECT id, created_at, rowid FROM listings WHERE id = ?",
            (exclude_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = con.execute(
            "SELECT id FROM listings WHERE id = ?", (exclude_id,)
        ).fetchone()
    if row is None:
        return None
    return (_field(row, "created_at"), _field(row, "id"), _rowid(row))


def pond_peers(
    con: sqlite3.Connection,
    pond: str = DEFAULT_POND,
    exclude_id: str | None = None,
    before_created_at: str | None = None,
    before_id: str | None = None,
    before_rowid: int | None = None,
) -> list[tuple[str, str, list[str]]]:
    """Earlier listings in this pond as (id, name, tokens). Later clones are not peers."""
    pond = pond or DEFAULT_POND
    cutoff = None
    if before_created_at is not None or before_id is not None or before_rowid is not None:
        cutoff = (before_created_at, before_id, before_rowid)
    elif exclude_id:
        cutoff = _subject_cutoff(con, exclude_id)
    rows = _load_listings(con)
    peers: list[tuple[str, str, list[str]]] = []
    for r in rows:
        rid = _field(r, "id")
        if exclude_id and rid == exclude_id:
            continue
        if _row_pond(r) != pond:
            continue
        if cutoff is not None and not _is_earlier(r, cutoff[0], cutoff[1], cutoff[2]):
            continue
        blob = listing_blob(_field(r, "name"), _field(r, "one_liner"))
        peers.append((rid, _field(r, "name") or "", tokens(blob)))
    return peers


def score_in_pond(
    con: sqlite3.Connection,
    text: str,
    pond: str = "ai-tools",
    exclude_id: str | None = None,
) -> tuple[int, str]:
    """0–100. High = unlike what already existed in this pond."""
    pond = pond or DEFAULT_POND
    peers = pond_peers(con, pond=pond, exclude_id=exclude_id)
    mine = tokens(text)
    docs = [mine] + [p[2] for p in peers]
    weights = idf(docs)
    my_vec = tfidf(tf(mine), weights)
    nearest_name = ""
    nearest = 0.0
    for _pid, name, toks in peers:
        sim = cosine(my_vec, tfidf(tf(toks), weights))
        if sim > nearest:
            nearest = sim
            nearest_name = name
    score = max(0, min(100, int(round(100 * (1.0 - nearest)))))
    if not nearest_name:
        return 100, "First listing in this pond."
    if score >= 70:
        note = f"Unlike nearest in pond ({nearest_name})."
    else:
        note = f"Close to {nearest_name} in this pond; not listed first."
    return score, note


def rescore_pond(con: sqlite3.Connection, pond: str = "ai-tools") -> int:
    """Recompute unique_score vs earlier peers only. Original stays unique; later clones sink."""
    pond = pond or DEFAULT_POND
    rows = _load_listings(con)
    n = 0
    for r in rows:
        if _row_pond(r) != pond:
            continue
        blob = listing_blob(_field(r, "name"), _field(r, "one_liner"))
        rid = _field(r, "id")
        score, note = score_in_pond(con, blob, pond=pond, exclude_id=rid)
        con.execute(
            "UPDATE listings SET unique_score = ?, unique_note = ? WHERE id = ?",
            (score, note, rid),
        )
        n += 1
    return n
