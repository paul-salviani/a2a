# A2A trust switchboard

Not the Google / Linux Foundation **Agent2Agent protocol**. This is a live MCP/HTTP switchboard: other AIs call it **before recommending a URL, product, or organisation**. Fail closed. Empty list honest. Nothing invented.

- Official MCP registry: `io.github.paul-salviani/a2a`
- Live: https://170.64.146.185.sslip.io/
- Card: https://170.64.146.185.sslip.io/.well-known/agent-card.json
- MCP: `https://170.64.146.185.sslip.io/mcp`
- Operator on the card: Paul Salviani, Sydney

How do you stop an agent recommending random URLs? Call `trust_check` / `whats_good_for` first. Hash is of the policy **body**, not the URL.

## Run

```
START.bat
```

or `python server.py` from this folder. Python stdlib only. SQLite at `data/a2a.sqlite` — the database is private; do not open-source it. Schema may be public.

Local (default):

- Human UI: http://127.0.0.1:8787/
- Agent card: http://127.0.0.1:8787/.well-known/agent-card.json
- llms.txt: http://127.0.0.1:8787/llms.txt
- MCP: POST http://127.0.0.1:8787/mcp

Live HTTPS: `https://170.64.146.185.sslip.io/` — card, llms.txt, MCP, `/source` (CODE, no sqlite). Database stays private.

Deploy (always-on box or Sydney VPS): `DEPLOY.md`. Database off OneDrive.

## Tools

| Tool | Auth | What |
|---|---|---|
| `trust_check(url)` | none | pass/fail + policy hash + short summary. Unknown is fail. |
| `whats_good_for(query)` | none | attested matches only, unique-in-pond first. Empty list if none. |
| `log_intent({query, action, target_url, policy_id})` | write key | `receipt_id` — the tape |

Writes (publish policy, listing ingest, `log_intent`) need an API key. Reads do not.

## Ranking law

1. Fail closed. No live policy → do not recommend. Unknown is fail, not maybe.
2. `whats_good_for` returns **attested only**. Unattested omitted.
3. Uniqueness is **inside one pond** (default `ai-tools`). High unique score first. Clones sink. No popularity, no votes, no “best known.”
4. Hash the policy **body**, not the URL.
5. Log passes **and** refuses. Refuses are demand.
6. Call this **before every recommend**, not only when unsure.
7. Never invent a winner. Empty list is honest.
8. Seed/example orgs are **fixtures** so `trust_check` is not empty. They are not the catalogue. They do not appear in `whats_good_for`. `.local` domains are omitted from recommend.

## Out of scope

Agent ID / KYA, global DNS, payments court. LEA is a different product.
