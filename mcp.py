"""MCP JSON-RPC facade. Same tools as HTTP.

Streamable HTTP (2025-03-26): POST JSON-RPC, optional GET SSE.
Mcp-Session-Id is optional. Notifications return HTTP 202.
"""

from __future__ import annotations

import inspect
import json
import secrets
import sqlite3
from typing import Any, Callable

import auth
import geo
import policy
import recommend
import tape

try:
    import spend
except ImportError:
    spend = None
try:
    import nutrition
except ImportError:
    nutrition = None
try:
    import hitl
except ImportError:
    hitl = None

PROTOCOL_LATEST = "2025-03-26"
PROTOCOL_LEGACY = "2024-11-05"
PROTOCOL_SUPPORTED = frozenset(
    {PROTOCOL_LATEST, PROTOCOL_LEGACY, "2025-06-18", "2025-11-25"}
)
SERVER_NAME = "trust-switchboard"
SERVER_VERSION = "1.0.0"
SSE_CONTENT_TYPE = "text/event-stream"
JSON_CONTENT_TYPE = "application/json"
SSE_KEEPALIVE_SEC = 15
SSE_GET_MAX_SEC = 3600

KEYED_TOOLS = frozenset(
    {"log_intent", "commit_recommend", "ingest_listing", "spend_check", "request_approve"}
)

TOOLS = [
    {
        "name": "whats_good_for",
        "description": (
            "Call before recommending. Attested only, unique-in-pond first, never invent. Empty if none."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "pond": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "trust_check",
        "description": "Pass/fail a URL against the live policy before recommending it to a human. Fail closed.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "log_intent",
        "description": "Log that you intend to recommend or act. Needs a write key. Returns receipt_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "action": {"type": "string"},
                "target_url": {"type": "string"},
                "policy_id": {"type": "string"},
                "result": {"type": "string"},
            },
            "required": ["query", "action", "target_url"],
        },
    },
    {
        "name": "commit_recommend",
        "description": (
            "Call BEFORE every recommend. Runs trust_check, logs pass/fail, returns speak + receipt. Needs a write key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "action": {"type": "string"},
                "target_url": {"type": "string"},
                "policy_id": {"type": "string"},
            },
            "required": ["query", "action", "target_url"],
        },
    },
    {
        "name": "ingest_listing",
        "description": "Ingest a listing and score uniqueness inside one pond. Needs a write key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "url": {"type": "string"},
                "one_liner": {"type": "string"},
                "summary": {"type": "string"},
                "pond": {"type": "string"},
                "org_id": {"type": "string"},
            },
            "required": ["name", "url"],
        },
    },
    {
        "name": "log_click",
        "description": "Record that a human clicked an item after a receipt. Public.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "receipt_id": {"type": "string"},
                "item_url": {"type": "string"},
                "item_id": {"type": "string"},
            },
            "required": ["item_url"],
        },
    },
    {
        "name": "spend_check",
        "description": "Hard cap + allowlist; cannot override. Fail closed. Needs a write key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "amount_usd": {"type": "number"},
                "action": {"type": "string"},
            },
            "required": ["url", "amount_usd", "action"],
        },
    },
    {
        "name": "nutrition_label",
        "description": "Stamp of what it is + last test.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "request_approve",
        "description": "HITL pause. Agent waits; human taps yes. Needs a write key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "action": {"type": "string"},
                "target_url": {"type": "string"},
                "amount_usd": {"type": "number"},
            },
            "required": ["query", "action", "target_url"],
        },
    },
    {
        "name": "decide_approve",
        "description": "Human tap. Public so the desk can approve without the org key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "approval_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["approved", "denied"]},
            },
            "required": ["approval_id", "decision"],
        },
    },
]


def _invoke(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
    positional = [
        p
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if not has_var_pos:
        args = args[: len(positional)]
    taken = {p.name for i, p in enumerate(positional) if i < len(args)}
    if has_var_kw:
        call_kw = {k: v for k, v in kwargs.items() if k not in taken}
    else:
        allowed = {
            p.name
            for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            and p.name not in taken
        }
        call_kw = {k: v for k, v in kwargs.items() if k in allowed}
    return fn(*args, **call_kw)


def _optional_mod(name: str):
    existing = globals().get(name)
    if existing is not None:
        return existing
    try:
        mod = __import__(name)
    except ImportError:
        return None
    globals()[name] = mod
    return mod


def _mod_fn(mod, *names: str):
    if mod is None:
        return None
    for name in names:
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    return None


def _second_name(fn: Callable[..., Any]) -> str:
    params = list(inspect.signature(fn).parameters.values())
    return params[1].name if len(params) > 1 else ""


def _lookup_key(con: sqlite3.Connection, raw: str | None):
    fn = getattr(auth, "lookup_key", None) or getattr(auth, "get_key", None)
    if not callable(fn):
        return None
    return _invoke(fn, con, raw)


def _is_write(row) -> bool:
    if row is None:
        return False
    fn = getattr(auth, "is_write_key", None) or getattr(auth, "write_ok", None)
    if callable(fn):
        return bool(fn(row))
    role = row["role"] if "role" in row.keys() else ""
    return role in ("write", "org_publish")


def _is_org_publish(row) -> bool:
    if row is None:
        return False
    fn = getattr(auth, "is_org_publish_key", None) or getattr(auth, "org_publish_ok", None)
    if callable(fn):
        return bool(fn(row))
    role = row["role"] if "role" in row.keys() else ""
    return role == "org_publish" and bool(row["org_id"] if "org_id" in row.keys() else None)


def lookup_write_key(con: sqlite3.Connection, raw: str | None):
    row = _lookup_key(con, raw)
    if not _is_write(row):
        return None
    return row


def lookup_org_key(con: sqlite3.Connection, raw: str | None):
    row = _lookup_key(con, raw)
    if not _is_org_publish(row):
        return None
    return row


def _passed(check: dict) -> bool:
    if not isinstance(check, dict):
        return False
    if check.get("pass") is True:
        return True
    if check.get("pass") is False:
        return False
    return str(check.get("result") or "").lower() == "pass"


def _recommendable(check: dict) -> bool:
    if not isinstance(check, dict):
        return False
    if "recommendable" in check:
        return check.get("recommendable") is True
    return _passed(check)


def trust_check(con: sqlite3.Connection, url: str) -> dict:
    fn = getattr(policy, "trust_check")
    return _invoke(fn, con, url or "")


def whats_good_for(
    con: sqlite3.Connection,
    query: str,
    pond: str | None = None,
    limit: int | None = None,
) -> dict:
    fn = getattr(recommend, "whats_good_for")
    params = list(inspect.signature(fn).parameters.values())
    second = params[1].name if len(params) > 1 else ""
    if second in ("payload", "body", "data"):
        return _invoke(fn, con, {"query": query or "", "pond": pond, "limit": limit})
    return _invoke(fn, con, query or "", pond=pond, limit=limit)


def log_intent(con: sqlite3.Connection, payload: dict, key_row) -> dict:
    fn = getattr(tape, "log_intent")
    return _invoke(fn, con, payload, key_row, payload=payload, key_row=key_row)


def ingest_listing(con: sqlite3.Connection, payload: dict, key_row=None) -> dict:
    fn = getattr(recommend, "ingest_listing")
    return _invoke(fn, con, payload, key_row, payload=payload, key_row=key_row)


def log_click(con: sqlite3.Connection, payload: dict) -> dict:
    fn = getattr(tape, "log_click", None) or getattr(tape, "click", None)
    if not callable(fn):
        raise AttributeError("tape.log_click missing")
    return _invoke(fn, con, payload, payload=payload)


def commit_recommend(con: sqlite3.Connection, payload: dict, key_row) -> dict:
    url = (payload.get("target_url") or payload.get("url") or "").strip()
    check = trust_check(con, url)
    rec_ok = _recommendable(check)
    intent = {
        "query": payload.get("query"),
        "action": payload.get("action") or "recommend",
        "target_url": url,
        "policy_id": payload.get("policy_id") or check.get("policy_id"),
        "result": "pass" if rec_ok else "fail",
    }
    receipt = log_intent(con, intent, key_row)
    rid = receipt.get("receipt_id") if isinstance(receipt, dict) else None
    org = check.get("org") if isinstance(check.get("org"), dict) else {}
    name = org.get("name") or url or "this"
    ph = check.get("policy_hash") or ""
    if rec_ok:
        speak = check.get("speak") or f"Use {name}. Policy hash {ph}."
        if rid and "Receipt" not in str(speak):
            speak = str(speak).rstrip(".") + f". Receipt {rid}."
    else:
        speak = check.get("speak") or geo.FAIL_SPEAK
    return {
        "pass": rec_ok,
        "speak": speak,
        "receipt_id": rid,
        "check": check,
        "receipt": receipt,
    }


def spend_check(
    con: sqlite3.Connection,
    url: str | dict = "",
    amount_usd=None,
    action: str | None = None,
    key_row=None,
    payload: dict | None = None,
    target_url: str | None = None,
    **kwargs,
) -> dict:
    if isinstance(url, dict):
        payload = url
        if key_row is None and action is None and not isinstance(amount_usd, (int, float, str)):
            key_row = amount_usd
            amount_usd = None
        url = ""
    body = dict(payload or {})
    url = str(url or target_url or body.get("url") or body.get("target_url") or kwargs.get("url") or kwargs.get("target_url") or "")
    if amount_usd is None:
        amount_usd = body.get("amount_usd", body.get("amount", kwargs.get("amount_usd", kwargs.get("amount"))))
    action = action or body.get("action") or kwargs.get("action") or ""
    body.setdefault("url", url)
    body.setdefault("target_url", url)
    body.setdefault("amount_usd", amount_usd)
    body.setdefault("amount", amount_usd)
    body.setdefault("action", action)
    denied = {
        "pass": False,
        "allowed": False,
        "result": "fail",
        "reason": "spend_gate_missing",
        "speak": "Spend not allowed. Hard cap + allowlist; cannot override.",
        "url": url,
        "amount_usd": amount_usd,
        "action": action,
    }
    fn = _mod_fn(_optional_mod("spend"), "spend_check", "check")
    if not callable(fn):
        return denied
    if _second_name(fn) in ("payload", "body", "data"):
        return _invoke(fn, con, body, key_row, payload=body, key_row=key_row)
    return _invoke(
        fn,
        con,
        url,
        amount_usd,
        action,
        payload=body,
        key_row=key_row,
        url=url,
        target_url=url,
        amount_usd=amount_usd,
        amount=amount_usd,
        action=action,
        allowlist=body.get("allowlist"),
    )


def nutrition_for_url(con: sqlite3.Connection, url: str = "", **kwargs) -> dict:
    if isinstance(url, dict):
        url = url.get("url") or ""
    url = str(url or kwargs.get("url") or "")
    missing = {
        "pass": False,
        "reason": "nutrition_missing",
        "label": None,
        "speak": "No nutrition stamp.",
        "url": url,
    }
    fn = _mod_fn(
        _optional_mod("nutrition"),
        "nutrition_for_url",
        "nutrition_label",
        "label",
        "get_label",
    )
    if not callable(fn):
        return missing
    if _second_name(fn) in ("payload", "body", "data"):
        return _invoke(fn, con, {"url": url}, url=url, payload={"url": url})
    return _invoke(fn, con, url, url=url)


def nutrition_label(con: sqlite3.Connection, url: str = "", **kwargs) -> dict:
    return nutrition_for_url(con, url, **kwargs)


def request_approve(
    con: sqlite3.Connection,
    payload: dict | None = None,
    key_row=None,
    **kwargs,
) -> dict:
    if isinstance(payload, dict):
        body = dict(payload)
    else:
        body = {}
    for k in ("query", "action", "target_url", "url", "amount_usd", "policy_id", "org_id"):
        if k in kwargs and (k not in body or body.get(k) in (None, "")):
            body[k] = kwargs[k]
    if not body.get("target_url") and body.get("url"):
        body["target_url"] = body["url"]
    denied = {
        "ok": False,
        "status": "denied",
        "reason": "hitl_missing",
        "speak": "HITL missing. Cannot pause; fail closed.",
    }
    fn = _mod_fn(_optional_mod("hitl"), "request_approve", "request")
    if not callable(fn):
        return denied
    if _second_name(fn) in ("payload", "body", "data"):
        return _invoke(fn, con, body, key_row, payload=body, key_row=key_row)
    return _invoke(
        fn,
        con,
        body.get("query") or "",
        body.get("action") or "",
        body.get("target_url") or body.get("url") or "",
        body.get("amount_usd"),
        payload=body,
        key_row=key_row,
        query=body.get("query"),
        action=body.get("action"),
        target_url=body.get("target_url") or body.get("url") or "",
        url=body.get("url") or body.get("target_url") or "",
        amount_usd=body.get("amount_usd"),
        policy_id=body.get("policy_id"),
    )


def decide_approve(
    con: sqlite3.Connection,
    payload: dict | str | None = None,
    decision: str | None = None,
    key_row=None,
    **kwargs,
) -> dict:
    if isinstance(payload, str):
        body = {"approval_id": payload, "decision": decision or kwargs.get("decision") or ""}
    else:
        body = dict(payload or {})
        if decision and not body.get("decision"):
            body["decision"] = decision
        if kwargs.get("approval_id") and not body.get("approval_id"):
            body["approval_id"] = kwargs["approval_id"]
        if kwargs.get("id") and not body.get("approval_id"):
            body["approval_id"] = kwargs["id"]
        if kwargs.get("decision") and not body.get("decision"):
            body["decision"] = kwargs["decision"]
        if kwargs.get("reason") and body.get("reason") in (None, ""):
            body["reason"] = kwargs["reason"]
    missing = {"ok": False, "error": "hitl missing", "reason": "hitl_missing"}
    fn = _mod_fn(_optional_mod("hitl"), "decide_approve", "decide", "approve")
    if not callable(fn):
        return missing
    aid = body.get("approval_id") or body.get("id") or ""
    dec = body.get("decision") or ""
    reason = body.get("reason")
    if _second_name(fn) in ("payload", "body", "data"):
        return _invoke(fn, con, body, key_row, payload=body, key_row=key_row)
    return _invoke(
        fn,
        con,
        aid,
        dec,
        reason,
        approval_id=aid,
        decision=dec,
        reason=reason,
        payload=body,
        key_row=key_row,
        id=aid,
    )


def _rpc(req_id, *, result=None, error=None) -> dict:
    out: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    return out


def _tool_text(data: Any, is_error: bool = False) -> dict:
    out: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(data, default=str)}]
    }
    if is_error:
        out["isError"] = True
    return out


def _call_tool(con: sqlite3.Connection, name: str, args: dict, api_key_raw: str | None):
    args = args if isinstance(args, dict) else {}
    if name in KEYED_TOOLS:
        key = lookup_write_key(con, api_key_raw)
        if not key:
            return {"error": "write key required"}, None
    else:
        key = None

    if name == "whats_good_for":
        limit = args.get("limit")
        try:
            limit = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            limit = None
        data = whats_good_for(con, args.get("query") or "", pond=args.get("pond"), limit=limit)
    elif name == "trust_check":
        data = trust_check(con, args.get("url") or "")
    elif name == "log_intent":
        data = log_intent(con, args, key)
    elif name == "commit_recommend":
        data = commit_recommend(con, args, key)
    elif name == "ingest_listing":
        data = ingest_listing(con, args, key)
    elif name == "log_click":
        data = log_click(con, args)
    elif name == "spend_check":
        data = spend_check(
            con,
            args.get("url") or args.get("target_url") or "",
            amount_usd=args.get("amount_usd", args.get("amount")),
            action=args.get("action"),
            key_row=key,
            payload=args,
        )
    elif name == "nutrition_label":
        data = nutrition_for_url(con, args.get("url") or "")
    elif name == "request_approve":
        data = request_approve(con, args, key)
    elif name == "decide_approve":
        data = decide_approve(con, args)
    else:
        return None, {"code": -32601, "message": "unknown tool"}
    return data, None


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


def _accept_list(accept: str | None) -> str:
    return (accept or "").lower()


def accepts_event_stream(accept: str | None) -> bool:
    raw = _accept_list(accept)
    if not raw.strip() or "*/*" in raw:
        return True
    return "text/event-stream" in raw


def accepts_json(accept: str | None) -> bool:
    raw = _accept_list(accept)
    if not raw.strip() or "*/*" in raw:
        return True
    return "application/json" in raw


def prefer_sse_body(accept: str | None) -> bool:
    """Wrap POST JSON-RPC in SSE only when the client will not take JSON."""
    if accepts_json(accept):
        return False
    return accepts_event_stream(accept)


def encode_sse(obj: Any, *, event_id: str | None = None) -> bytes:
    data = json.dumps(obj, default=str, separators=(",", ":"))
    parts: list[str] = []
    if event_id is not None:
        parts.append(f"id: {event_id}")
    parts.append("event: message")
    parts.append(f"data: {data}")
    return ("\n".join(parts) + "\n\n").encode("utf-8")


def negotiate_protocol(asked: str | None) -> str:
    asked = str(asked or "").strip()
    if asked == PROTOCOL_LEGACY:
        return PROTOCOL_LEGACY
    if asked == PROTOCOL_LATEST or not asked:
        return PROTOCOL_LATEST
    if asked in PROTOCOL_SUPPORTED:
        return asked
    return PROTOCOL_LATEST


def _http_result(status: int, body=None, headers: dict | None = None) -> dict:
    return {"status": int(status), "body": body, "headers": dict(headers or {})}


def _session_headers(session_id: str | None) -> dict[str, str]:
    if not session_id:
        return {}
    return {"Mcp-Session-Id": session_id}


def handle_mcp_get(accept: str | None, session_id: str | None) -> dict:
    """GET /mcp: SSE stream, or 405 if this Accept cannot take event-stream."""
    headers = _session_headers(session_id)
    if not accepts_event_stream(accept):
        return _http_result(
            405,
            {"error": "SSE required", "accept": SSE_CONTENT_TYPE},
            headers,
        )
    return _http_result(200, None, headers)


def _handle_one(
    con: sqlite3.Connection,
    payload: dict,
    api_key_raw: str | None,
    session_id: str | None,
) -> dict:
    headers = _session_headers(session_id)
    method = payload.get("method")
    notification = "id" not in payload
    req_id = payload.get("id") if not notification else None
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if not method:
        if "result" in payload or "error" in payload:
            return _http_result(202, None, headers)
        if notification:
            return _http_result(202, None, headers)
        return _http_result(200, _rpc(req_id, error={"code": -32600, "message": "Invalid Request"}), headers)

    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        version = negotiate_protocol(asked)
        sid = session_id or new_session_id()
        headers = _session_headers(sid)
        if notification:
            return _http_result(202, None, headers)
        return _http_result(
            200,
            _rpc(
                req_id,
                result={
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            ),
            headers,
        )

    if isinstance(method, str) and method.startswith("notifications/"):
        return _http_result(202, None, headers) if notification else _http_result(200, _rpc(req_id, result={}), headers)

    if notification:
        return _http_result(202, None, headers)

    if method == "tools/list":
        return _http_result(200, _rpc(req_id, result={"tools": TOOLS}), headers)

    if method == "ping":
        return _http_result(200, _rpc(req_id, result={}), headers)

    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        try:
            data, err = _call_tool(con, name, args, api_key_raw)
        except (ValueError, PermissionError) as e:
            return _http_result(200, _rpc(req_id, error={"code": -32602, "message": str(e)}), headers)
        if err:
            return _http_result(200, _rpc(req_id, error=err), headers)
        is_error = isinstance(data, dict) and data.get("error") == "write key required"
        return _http_result(200, _rpc(req_id, result=_tool_text(data, is_error=is_error)), headers)

    return _http_result(200, _rpc(req_id, error={"code": -32601, "message": "Method not found"}), headers)


def handle_mcp(
    con: sqlite3.Connection,
    payload: dict | list,
    api_key_raw: str | None,
    session_id: str | None = None,
    accept: str | None = None,
):
    """JSON-RPC over Streamable HTTP. Returns {status, body, headers}.

    Notifications and client responses: HTTP 202, empty body.
    Mcp-Session-Id is minted on initialize and never required later.
    `accept` is unused here; the HTTP layer picks JSON vs SSE.
    """
    _ = accept
    if isinstance(payload, list):
        if not payload:
            return _http_result(400, _rpc(None, error={"code": -32600, "message": "Invalid Request"}))
        responses: list[dict] = []
        headers: dict[str, str] = _session_headers(session_id)
        for item in payload:
            if not isinstance(item, dict):
                responses.append(_rpc(None, error={"code": -32600, "message": "Invalid Request"}))
                continue
            one = _handle_one(con, item, api_key_raw, headers.get("Mcp-Session-Id") or session_id)
            headers.update(one.get("headers") or {})
            if one.get("body") is not None:
                responses.append(one["body"])
        if not responses:
            return _http_result(202, None, headers)
        return _http_result(200, responses, headers)

    if not isinstance(payload, dict):
        return _http_result(400, _rpc(None, error={"code": -32600, "message": "Invalid Request"}))
    return _handle_one(con, payload, api_key_raw, session_id)
