#!/usr/bin/env python3
"""A2A HTTP facade. Other AIs call this before they recommend anything to a human."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import auth
import db
import envfile
import geo
import hitl
import mcp
import nutrition
import policy
import recommend
import spend
import tape

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
envfile.load_env(ROOT)
HOST = os.environ.get("A2A_HOST", "127.0.0.1")
PORT = int(os.environ.get("A2A_PORT", "8787"))
MAX_BODY = 262144
SIGNUP_LIMIT = 10
SIGNUP_WINDOW = 3600
CARD_TYPE = "application/a2a+json; charset=utf-8"

_signup_hits: dict[str, list[float]] = {}

lookup_key = auth.lookup_key
extract_domain = policy.extract_domain
trust_check = policy.trust_check
whats_good_for = recommend.whats_good_for
ingest_listing = recommend.ingest_listing
log_intent = tape.log_intent
log_click = tape.log_click


class BodyTooLarge(Exception):
    pass


def connect() -> sqlite3.Connection:
    fn = getattr(db, "db", None) or getattr(db, "connect", None)
    if not callable(fn):
        raise RuntimeError("db.py has no db()")
    return fn()


def db_path() -> Path:
    fn = getattr(db, "db_path", None)
    if callable(fn):
        return Path(fn())
    raw = os.environ.get("A2A_DB") or getattr(db, "DB_PATH", None)
    if raw:
        return Path(raw)
    return ROOT / "data" / "a2a.sqlite"


def init_db() -> None:
    fn = getattr(db, "init_db", None) or getattr(db, "init", None)
    if not callable(fn):
        raise RuntimeError("db.py has no init_db()")
    fn()


def _seed_write_raw() -> str | None:
    env = (os.environ.get("A2A_WRITE_KEY") or "").strip()
    if env:
        return env
    seed_path = ROOT / "seed.json"
    if not seed_path.is_file():
        return None
    try:
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = seed.get("write_key")
    return str(raw) if raw else None


def policy_hash(body: str, policy_url: str | None = None, **kwargs):
    """Hash the policy body. URL is ignored so a swapped page cannot keep the pass."""
    _ = policy_url
    return policy.policy_hash(
        kwargs.get("title") or "Policy",
        kwargs.get("summary") or "",
        kwargs.get("body", body) or "",
        kwargs.get("stores_what"),
        kwargs.get("data_region"),
        kwargs.get("dispute_email"),
    )


def agent_card(root_or_handler=None) -> dict:
    if root_or_handler is None:
        return geo.agent_card(f"http://{HOST}:{PORT}")
    if isinstance(root_or_handler, str):
        return geo.agent_card(root_or_handler)
    if hasattr(root_or_handler, "headers"):
        return geo.agent_card(root_url(root_or_handler))
    return geo.agent_card(str(root_or_handler))


def llms_txt(root_or_handler=None) -> str:
    if root_or_handler is None:
        return geo.llms_txt(f"http://{HOST}:{PORT}")
    if isinstance(root_or_handler, str):
        return geo.llms_txt(root_or_handler)
    if hasattr(root_or_handler, "headers"):
        return geo.llms_txt(root_url(root_or_handler))
    return geo.llms_txt(str(root_or_handler))


def commit_recommend(con, payload=None, key_row=None, **kwargs) -> dict:
    """Call BEFORE every recommend. Accepts (con, url) or (con, payload, key_row)."""
    if isinstance(payload, str):
        url = payload
        payload = {
            "query": kwargs.get("query") or url,
            "action": kwargs.get("action") or "recommend",
            "target_url": url,
            "url": url,
            "policy_id": kwargs.get("policy_id"),
        }
    elif payload is None:
        payload = dict(kwargs)
    if key_row is None:
        key_row = auth.lookup_key(con, _seed_write_raw())
    if key_row is None:
        raise PermissionError("write key required")
    return mcp.commit_recommend(con, payload, key_row)


def root_url(handler: BaseHTTPRequestHandler) -> str:
    public = (os.environ.get("A2A_PUBLIC_URL") or "").strip().rstrip("/")
    if public:
        return public
    proto = handler.headers.get("X-Forwarded-Proto", "http")
    host = handler.headers.get("Host") or f"{HOST}:{PORT}"
    return f"{proto}://{host}"


def _count(con: sqlite3.Connection, sql: str) -> int:
    try:
        row = con.execute(sql).fetchone()
        if row is None:
            return 0
        return int(row[0])
    except sqlite3.Error:
        return 0


def health(con: sqlite3.Connection) -> dict:
    fn = getattr(db, "health", None) or getattr(db, "health_counts", None)
    if callable(fn):
        return mcp._invoke(fn, con)
    return {
        "ok": True,
        "orgs": _count(con, "SELECT COUNT(*) FROM orgs"),
        "policies": _count(con, "SELECT COUNT(*) FROM policies"),
        "receipts": _count(con, "SELECT COUNT(*) FROM receipts"),
        "listings": _count(con, "SELECT COUNT(*) FROM listings"),
        "checks": _count(con, "SELECT COUNT(*) FROM checks"),
        "attested_live": _count(
            con,
            """
            SELECT COUNT(DISTINCT o.id)
            FROM orgs o
            JOIN policies p ON p.org_id = o.id AND p.status = 'active'
            WHERE o.attested = 1
              AND COALESCE(o.is_seed, 0) = 0
              AND o.domain NOT LIKE '%.local'
            """,
        ),
    }


def list_ideas(con: sqlite3.Connection) -> dict:
    fn = getattr(recommend, "list_ideas", None)
    if callable(fn):
        return mcp._invoke(fn, con)
    try:
        rows = con.execute(
            """
            SELECT i.id, i.name, i.domain, i.website, i.category, i.need_json, i.summary, i.attested
            FROM ideas i
            LEFT JOIN orgs o ON o.id = i.id
            WHERE i.attested = 1
              AND COALESCE(o.is_seed, 0) = 0
              AND COALESCE(i.domain, '') NOT LIKE '%.local'
              AND COALESCE(o.domain, '') NOT LIKE '%.local'
            ORDER BY i.category, i.name
            """
        ).fetchall()
    except sqlite3.Error:
        return {"ideas": [], "count": 0}
    ideas = []
    for r in rows:
        need_raw = r["need_json"] if "need_json" in r.keys() else "[]"
        try:
            need = json.loads(need_raw or "[]")
        except json.JSONDecodeError:
            need = []
        ideas.append(
            {
                "id": r["id"],
                "name": r["name"],
                "domain": r["domain"],
                "website": r["website"],
                "category": r["category"],
                "need": need if isinstance(need, list) else [],
                "summary": r["summary"],
                "attested": bool(r["attested"]),
            }
        )
    return {"ideas": ideas, "count": len(ideas)}


def directory(con: sqlite3.Connection) -> dict:
    fn = getattr(recommend, "directory", None) or getattr(recommend, "list_directory", None)
    if callable(fn):
        return mcp._invoke(fn, con)
    orgs: list[dict] = []
    listings: list[dict] = []
    try:
        for r in con.execute(
            """
            SELECT o.id, o.name, o.domain, o.website, o.category,
                   p.id AS policy_id, p.policy_hash, p.summary
            FROM orgs o
            JOIN policies p ON p.org_id = o.id
              AND p.status = 'active'
              AND p.version = (SELECT MAX(version) FROM policies p2 WHERE p2.org_id = o.id)
            WHERE o.attested = 1
              AND COALESCE(o.is_seed, 0) = 0
              AND o.domain NOT LIKE '%.local'
            ORDER BY o.name
            """
        ).fetchall():
            orgs.append(dict(r))
        for r in con.execute(
            """
            SELECT l.id, l.name, l.url, l.one_liner, l.unique_score, l.unique_note, l.pond,
                   p.id AS policy_id, p.policy_hash
            FROM listings l
            JOIN orgs o ON o.id = l.org_id
            JOIN policies p ON p.org_id = o.id
              AND p.status = 'active'
              AND p.version = (SELECT MAX(version) FROM policies p2 WHERE p2.org_id = o.id)
            WHERE o.attested = 1
              AND COALESCE(o.is_seed, 0) = 0
              AND o.domain NOT LIKE '%.local'
            ORDER BY l.unique_score DESC, l.name
            """
        ).fetchall():
            listings.append(dict(r))
    except sqlite3.Error:
        pass
    return {"orgs": orgs, "listings": listings}


def get_receipt(con: sqlite3.Connection, rid: str):
    fn = (
        getattr(tape, "get_receipt", None)
        or getattr(tape, "receipt_by_id", None)
        or getattr(tape, "receipt", None)
    )
    if callable(fn):
        return mcp._invoke(fn, con, rid)
    row = con.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
    return dict(row) if row else None


def demand(con: sqlite3.Connection) -> dict:
    fn = getattr(tape, "demand", None)
    if not callable(fn):
        raise AttributeError("tape.demand missing")
    return mcp._invoke(fn, con)


def recent_tape(con: sqlite3.Connection) -> dict:
    fn = getattr(tape, "recent_tape", None) or getattr(tape, "recent", None)
    if not callable(fn):
        raise AttributeError("tape.recent_tape missing")
    return mcp._invoke(fn, con)


def signup_org(con: sqlite3.Connection, payload: dict) -> dict:
    fn = getattr(auth, "signup_org", None) or getattr(auth, "signup", None)
    if not callable(fn):
        raise AttributeError("auth.signup_org missing")
    payload = dict(payload or {})
    if not payload.get("name") and payload.get("org_name"):
        payload["name"] = payload["org_name"]
    if not payload.get("site_url") and payload.get("website"):
        payload["site_url"] = payload["website"]
    return mcp._invoke(fn, con, payload, payload=payload)


def publish_policy(con: sqlite3.Connection, payload: dict, key_row) -> dict:
    fn = getattr(policy, "publish_policy", None)
    if not callable(fn):
        raise AttributeError("policy.publish_policy missing")
    return mcp._invoke(fn, con, payload, key_row, payload=payload, key_row=key_row)


def _tool_fn(names, module=None):
    """Prefer an mcp wrapper; else the named function on the module."""
    for name in names:
        fn = getattr(mcp, name, None)
        if callable(fn):
            return fn
    if module is not None:
        for name in names:
            fn = getattr(module, name, None)
            if callable(fn):
                return fn
    return None


def spend_check(con: sqlite3.Connection, payload: dict, key_row) -> dict:
    fn = _tool_fn(("spend_check", "check"), spend)
    if not callable(fn):
        raise AttributeError("spend.spend_check missing")
    payload = dict(payload or {})
    url = payload.get("url") or payload.get("target_url") or ""
    return mcp._invoke(
        fn,
        con,
        payload=payload,
        key_row=key_row,
        url=url,
        target_url=url,
        amount_usd=payload.get("amount_usd"),
        action=payload.get("action"),
    )


def nutrition_label(con: sqlite3.Connection, url: str) -> dict:
    fn = _tool_fn(
        ("nutrition_label", "nutrition_for_url", "nutrition", "label", "get_label"),
        nutrition,
    )
    if not callable(fn):
        raise AttributeError("nutrition.nutrition_label missing")
    url = url or ""
    payload = {"url": url}
    return mcp._invoke(fn, con, payload=payload, url=url)


def request_approve(con: sqlite3.Connection, payload: dict, key_row) -> dict:
    fn = _tool_fn(("request_approve", "request"), hitl)
    if not callable(fn):
        raise AttributeError("hitl.request_approve missing")
    payload = dict(payload or {})
    url = payload.get("url") or payload.get("target_url") or ""
    if url and not payload.get("target_url"):
        payload["target_url"] = url
    return mcp._invoke(
        fn,
        con,
        payload=payload,
        key_row=key_row,
        url=url,
        target_url=url,
        query=payload.get("query"),
        action=payload.get("action"),
        amount_usd=payload.get("amount_usd"),
        policy_id=payload.get("policy_id"),
    )


def decide_approve(con: sqlite3.Connection, payload: dict) -> dict:
    fn = _tool_fn(("decide_approve", "decide", "approve"), hitl)
    if not callable(fn):
        raise AttributeError("hitl.decide_approve missing")
    payload = dict(payload or {})
    approval_id = payload.get("approval_id") or payload.get("id") or ""
    return mcp._invoke(
        fn,
        con,
        payload=payload,
        approval_id=approval_id,
        decision=payload.get("decision"),
        reason=payload.get("reason"),
        id=approval_id,
    )


def list_pending(con: sqlite3.Connection) -> dict:
    fn = _tool_fn(("list_pending", "pending", "list_approvals"), hitl)
    if not callable(fn):
        raise AttributeError("hitl.list_pending missing")
    return mcp._invoke(fn, con)


def get_approval(con: sqlite3.Connection, approval_id: str):
    fn = _tool_fn(("get_approval", "approval"), hitl)
    if not callable(fn):
        raise AttributeError("hitl.get_approval missing")
    approval_id = approval_id or ""
    return mcp._invoke(
        fn,
        con,
        approval_id,
        approval_id=approval_id,
        id=approval_id,
    )


def signup_allowed(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _signup_hits.get(ip, []) if now - t < SIGNUP_WINDOW]
    if len(hits) >= SIGNUP_LIMIT:
        _signup_hits[ip] = hits
        return False
    hits.append(now)
    _signup_hits[ip] = hits
    return True


def client_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded = (handler.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return handler.client_address[0]


class Handler(BaseHTTPRequestHandler):
    server_version = "A2A/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Accept, Authorization, X-API-Key, "
            "Mcp-Session-Id, MCP-Protocol-Version, Last-Event-ID",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id, MCP-Protocol-Version")

    def _geo_headers(self) -> None:
        self.send_header("Link", geo.link_header())

    def _extra(self, extra_headers: dict | None) -> None:
        for key, value in (extra_headers or {}).items():
            if key and value is not None:
                self.send_header(str(key), str(value))

    def _send(
        self,
        code: int,
        body,
        content_type: str = "application/json; charset=utf-8",
        extra_headers: dict | None = None,
    ) -> None:
        if body is None:
            raw = b""
        elif isinstance(body, (dict, list)):
            raw = json.dumps(body, indent=2, default=str).encode("utf-8")
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        elif isinstance(body, (bytes, bytearray)):
            raw = bytes(body)
        else:
            raw = json.dumps(body, default=str).encode("utf-8")
        self.send_response(code)
        self._cors()
        self._geo_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self._extra(extra_headers)
        self.end_headers()
        if raw and self.command != "HEAD":
            self.wfile.write(raw)

    def _send_empty(self, code: int, extra_headers: dict | None = None) -> None:
        self.send_response(code)
        self._cors()
        self._geo_headers()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self._extra(extra_headers)
        self.end_headers()

    def _begin_sse(self, extra_headers: dict | None = None) -> None:
        self.close_connection = True
        self.send_response(200)
        self._cors()
        self._geo_headers()
        self.send_header("Content-Type", mcp.SSE_CONTENT_TYPE)
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self._extra(extra_headers)
        self.end_headers()

    def _write_sse(self, chunk: bytes) -> None:
        if not chunk:
            return
        payload = f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n"
        self.wfile.write(payload)
        self.wfile.flush()

    def _end_sse(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _send_sse_messages(self, messages, extra_headers: dict | None = None) -> None:
        items = messages if isinstance(messages, list) else [messages]
        self._begin_sse(extra_headers)
        try:
            for i, item in enumerate(items):
                self._write_sse(mcp.encode_sse(item, event_id=str(i + 1)))
            self._end_sse()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            return

    def _mcp_get_stream(self, extra_headers: dict | None = None) -> None:
        self._begin_sse(extra_headers)
        started = time.monotonic()
        try:
            self._write_sse(b": connected\n\n")
            while (time.monotonic() - started) < mcp.SSE_GET_MAX_SEC:
                time.sleep(mcp.SSE_KEEPALIVE_SEC)
                self._write_sse(b": ping\n\n")
            self._end_sse()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            return

    def _read_body_bytes(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise BodyTooLarge
        if n <= 0:
            return b""
        raw = self.rfile.read(n)
        if len(raw) > MAX_BODY:
            raise BodyTooLarge
        return raw

    def _json_body(self) -> dict:
        raw = self._read_body_bytes()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _mcp_json_body(self):
        raw = self._read_body_bytes()
        if not raw:
            raise ValueError("Invalid Request")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise json.JSONDecodeError("Parse error", "", 0) from exc
        if isinstance(data, (dict, list)):
            return data
        raise ValueError("Invalid Request")

    def _mcp_reply(self, out: dict) -> None:
        status = int(out.get("status") or 200)
        body = out.get("body")
        headers = dict(out.get("headers") or {})
        accept = self.headers.get("Accept")
        if status in (202, 204) or body is None:
            self._send_empty(202 if status not in (202, 204) else status, extra_headers=headers)
            return
        if mcp.prefer_sse_body(accept):
            self._send_sse_messages(body, extra_headers=headers)
            return
        self._send(status, body, extra_headers=headers)

    def _do_mcp_get(self) -> None:
        out = mcp.handle_mcp_get(self.headers.get("Accept"), self.headers.get("Mcp-Session-Id"))
        if out.get("status") != 200:
            extra = dict(out.get("headers") or {})
            extra.setdefault("Allow", "GET, POST, DELETE, OPTIONS")
            self._send(int(out.get("status") or 405), out.get("body"), extra_headers=extra)
            return
        self._mcp_get_stream(out.get("headers"))

    def _do_mcp_post(self) -> None:
        try:
            payload = self._mcp_json_body()
        except BodyTooLarge:
            self._send(413, {"error": "body too large", "max": MAX_BODY})
            return
        except json.JSONDecodeError:
            self._send(
                400,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )
            return
        except ValueError as e:
            self._send(
                400,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": str(e)}},
            )
            return
        con = connect()
        try:
            out = mcp.handle_mcp(
                con,
                payload,
                self._api_key(),
                session_id=self.headers.get("Mcp-Session-Id"),
                accept=self.headers.get("Accept"),
            )
            self._mcp_reply(out)
        except Exception as e:
            self._send(500, {"error": str(e)})
        finally:
            con.close()

    def _api_key(self) -> str | None:
        header = self.headers.get("X-API-Key")
        if header:
            return header
        authz = self.headers.get("Authorization") or ""
        if authz.lower().startswith("bearer "):
            return authz[7:].strip()
        return None

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_empty(204)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _send_source_file(self, rel: str) -> None:
        path = geo.resolve_source(rel)
        if path is None:
            self._send(404, {"error": "not found", "path": "/source/" + rel})
            return
        raw = path.read_bytes()
        ctype = "text/plain; charset=utf-8"
        if path.suffix == ".json":
            ctype = "application/json; charset=utf-8"
        elif path.suffix == ".sql":
            ctype = "application/sql; charset=utf-8"
        elif path.suffix == ".py":
            ctype = "text/x-python; charset=utf-8"
        self._send(200, raw, ctype)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        root = root_url(self)

        if path in ("/", "/index.html", "/dash", "/dash.html", "/for-agents", "/for-agents.html"):
            name = {
                "/": "index.html",
                "/index.html": "index.html",
                "/dash": "dash.html",
                "/dash.html": "dash.html",
                "/for-agents": "for-agents.html",
                "/for-agents.html": "for-agents.html",
            }[path]
            fp = PUBLIC / name
            if not fp.is_file():
                self._send(404, {"error": "not found"})
                return
            html = fp.read_text(encoding="utf-8").replace("{{WRITE_KEY}}", "")
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path in ("/for-agents.txt", "/seed-agents.txt"):
            self._send(200, geo.agent_seed_prompt(root), "text/plain; charset=utf-8")
            return
        if path in (
            "/.well-known/agent-card.json",
            "/agent-card.json",
            "/.well-known/agent.json",
        ):
            self._send(200, geo.agent_card(root), CARD_TYPE)
            return
        if path in ("/llms.txt", "/.well-known/llms.txt"):
            self._send(200, geo.llms_txt(root), "text/plain; charset=utf-8")
            return
        if path in ("/llms-full.txt", "/.well-known/llms-full.txt"):
            self._send(200, geo.llms_full_txt(root), "text/plain; charset=utf-8")
            return
        if path == "/.well-known/ai-catalog.json":
            self._send(
                200,
                geo.ai_catalog(root),
                "application/ai-catalog+json; charset=utf-8",
            )
            return
        if path in (
            "/mcp/server-card",
            "/.well-known/mcp/server-card.json",
            "/.well-known/mcp.json",
        ):
            self._send(
                200,
                geo.mcp_server_card(root),
                "application/mcp-server-card+json; charset=utf-8",
            )
            return
        if path == "/sitemap.xml":
            self._send(200, geo.sitemap_xml(root), "application/xml; charset=utf-8")
            return
        if path == "/source":
            self._send(200, geo.source_index(root), "text/plain; charset=utf-8")
            return
        if path == "/source.tar.gz":
            raw = geo.source_tar_bytes()
            self._send(200, raw, "application/gzip")
            return
        if path.startswith("/source/"):
            self._send_source_file(path[len("/source/") :])
            return
        if path == "/openapi.json":
            self._send(200, geo.openapi_spec(root))
            return
        if path == "/robots.txt":
            self._send(200, geo.robots_txt(root), "text/plain; charset=utf-8")
            return
        if path == "/mcp/tools":
            self._send(200, {"tools": mcp.TOOLS})
            return
        if path == "/mcp.json":
            self._send(200, geo.mcp_client_snippet(root))
            return
        if path == "/mcp":
            if self.command == "HEAD":
                extra = {
                    "Content-Type": mcp.SSE_CONTENT_TYPE,
                    "Allow": "GET, POST, DELETE, OPTIONS",
                }
                self._send_empty(200, extra_headers=extra)
                return
            self._do_mcp_get()
            return
        if path.startswith("/public/"):
            rel = path[len("/public/") :]
            fp = (PUBLIC / rel).resolve()
            if not str(fp).startswith(str(PUBLIC.resolve())) or not fp.is_file():
                self._send(404, {"error": "not found"})
                return
            ctype = "application/octet-stream"
            if fp.suffix == ".css":
                ctype = "text/css; charset=utf-8"
            elif fp.suffix == ".js":
                ctype = "application/javascript; charset=utf-8"
            self._send(200, fp.read_bytes(), ctype)
            return

        con = connect()
        try:
            if path == "/health":
                self._send(200, health(con))
                return
            if path == "/demand":
                self._send(200, demand(con))
                return
            if path in ("/pickup", "/stats"):
                fn = getattr(tape, "pickup", None)
                if not callable(fn):
                    self._send(500, {"error": "tape.pickup missing"})
                    return
                self._send(200, fn(con))
                return
            if path == "/tape":
                self._send(200, recent_tape(con))
                return
            if path == "/ideas":
                self._send(200, list_ideas(con))
                return
            if path == "/directory":
                self._send(200, directory(con))
                return
            if path.startswith("/receipts/"):
                rid = path.split("/", 2)[-1]
                row = get_receipt(con, rid)
                if not row:
                    self._send(404, {"error": "receipt not found"})
                    return
                self._send(200, row)
                return
            if path == "/tools/trust_check":
                url = (qs.get("url") or [""])[0]
                self._send(200, mcp.trust_check(con, url))
                return
            if path in ("/nutrition", "/tools/nutrition_label"):
                url = (qs.get("url") or [""])[0]
                self._send(200, nutrition_label(con, url))
                return
            if path == "/pending":
                self._send(200, list_pending(con))
                return
            if path.startswith("/approvals/"):
                aid = path.split("/", 2)[-1]
                row = get_approval(con, aid)
                if not row:
                    self._send(404, {"error": "approval not found"})
                    return
                self._send(200, row)
                return
            self._send(404, {"error": "not found", "path": path})
        except Exception as e:
            self._send(500, {"error": str(e)})
        finally:
            con.close()

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/mcp":
            extra = {"Allow": "GET, POST, DELETE, OPTIONS"}
            sid = self.headers.get("Mcp-Session-Id")
            if sid:
                extra["Mcp-Session-Id"] = sid
            self.send_response(405)
            self._cors()
            self._geo_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            raw = b'{"error": "session terminate not required"}'
            self.send_header("Content-Length", str(len(raw)))
            self._extra(extra)
            self.end_headers()
            self.wfile.write(raw)
            return
        self._send(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/mcp":
            self._do_mcp_post()
            return
        try:
            payload = self._json_body()
        except BodyTooLarge:
            self._send(413, {"error": "body too large", "max": MAX_BODY})
            return

        con = connect()
        try:
            if path == "/tools/trust_check":
                self._send(200, mcp.trust_check(con, payload.get("url") or ""))
                return
            if path == "/tools/whats_good_for":
                limit = payload.get("limit")
                try:
                    limit = int(limit) if limit is not None else None
                except (TypeError, ValueError):
                    limit = None
                self._send(
                    200,
                    mcp.whats_good_for(
                        con,
                        payload.get("query") or "",
                        pond=payload.get("pond"),
                        limit=limit,
                    ),
                )
                return
            if path == "/tools/log_intent":
                key = mcp.lookup_write_key(con, self._api_key())
                if not key:
                    self._send(401, {"error": "write key required"})
                    return
                self._send(200, mcp.log_intent(con, payload, key))
                return
            if path == "/tools/commit_recommend":
                key = mcp.lookup_write_key(con, self._api_key())
                if not key:
                    self._send(401, {"error": "write key required"})
                    return
                self._send(200, mcp.commit_recommend(con, payload, key))
                return
            if path in ("/listings", "/tools/ingest_listing"):
                key = mcp.lookup_write_key(con, self._api_key())
                if not key:
                    self._send(401, {"error": "write key required"})
                    return
                try:
                    self._send(200, mcp.ingest_listing(con, payload, key))
                except ValueError as e:
                    self._send(400, {"error": str(e)})
                return
            if path == "/orgs":
                ip = client_ip(self)
                if not signup_allowed(ip):
                    self._send(429, {"error": "rate limit", "limit": SIGNUP_LIMIT, "window": "1h"})
                    return
                try:
                    self._send(200, signup_org(con, payload))
                except ValueError as e:
                    self._send(400, {"error": str(e)})
                return
            if path == "/orgs/policy":
                key = mcp.lookup_org_key(con, self._api_key())
                if not key:
                    raw = mcp._lookup_key(con, self._api_key())
                    if not raw:
                        self._send(401, {"error": "org publish key required"})
                    else:
                        self._send(403, {"error": "org publish key required"})
                    return
                try:
                    self._send(200, publish_policy(con, payload, key))
                except PermissionError as e:
                    self._send(403, {"error": str(e)})
                except ValueError as e:
                    self._send(400, {"error": str(e)})
                return
            if path == "/clicks":
                try:
                    self._send(200, mcp.log_click(con, payload))
                except ValueError as e:
                    self._send(400, {"error": str(e)})
                return
            if path == "/tools/spend_check":
                key = mcp.lookup_write_key(con, self._api_key())
                if not key:
                    self._send(401, {"error": "write key required"})
                    return
                try:
                    self._send(200, spend_check(con, payload, key))
                except PermissionError as e:
                    self._send(403, {"error": str(e)})
                except ValueError as e:
                    self._send(400, {"error": str(e)})
                return
            if path == "/tools/nutrition_label":
                try:
                    self._send(200, nutrition_label(con, payload.get("url") or ""))
                except ValueError as e:
                    self._send(400, {"error": str(e)})
                return
            if path == "/tools/request_approve":
                key = mcp.lookup_write_key(con, self._api_key())
                if not key:
                    self._send(401, {"error": "write key required"})
                    return
                try:
                    self._send(200, request_approve(con, payload, key))
                except PermissionError as e:
                    self._send(403, {"error": str(e)})
                except ValueError as e:
                    self._send(400, {"error": str(e)})
                return
            if path in ("/tools/decide_approve", "/approve"):
                try:
                    out = decide_approve(con, payload)
                except PermissionError as e:
                    self._send(403, {"error": str(e)})
                    return
                except ValueError as e:
                    msg = str(e)
                    if "not found" in msg.lower():
                        self._send(404, {"error": msg})
                    else:
                        self._send(400, {"error": msg})
                    return
                if not out:
                    self._send(404, {"error": "approval not found"})
                    return
                self._send(200, out)
                return
            self._send(404, {"error": "not found", "path": path})
        except Exception as e:
            self._send(500, {"error": str(e)})
        finally:
            con.close()


def main() -> None:
    init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    public = (os.environ.get("A2A_PUBLIC_URL") or "").strip().rstrip("/")
    base = public or f"http://{HOST}:{PORT}"
    print(f"A2A  bind {HOST}:{PORT}")
    print(f"  public      {base}")
    print(f"  human UI    {base}/")
    print(f"  agent card  {base}/.well-known/agent-card.json")
    print(f"  llms.txt    {base}/llms.txt")
    print(f"  mcp         {base}/mcp")
    print(f"  db          {db_path()}")
    if not public:
        print("  note        A2A_PUBLIC_URL is empty. Other AIs cannot cite a stable address yet.")
    if HOST in ("127.0.0.1", "localhost"):
        print("  note        bound to localhost only. Deploy uses A2A_HOST=0.0.0.0")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
