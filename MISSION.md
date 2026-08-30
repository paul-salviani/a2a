# A2A — the company

Source: `MailBox - Paul to Grok/A2A_TRUST_SWITCHBOARD_CODEX_BRIEF.txt` and the 28 Aug thread. Do not shrink this.

## Mission

When a human asks an AI “what should I use / who can I trust / what’s actually new,” the AI must not guess from old training or ads. It calls this switchboard.

A2A discovers the organisation. Other AIs tell the human what is best. Not a human CEO picking winners.

The CODE is copyable. The LOG is the company. First serious buyer pays for the corpus + the default slot in other agents’ prompts.

Someone creates a thing → A2A researches what question it answers and how unique it is → unique attested items list first → people and AIs click. Cheap / running cost. Attention. A giant buys the junction (same pattern as NVIDIA/HF distribution, Stripe/OpenRouter routing).

Humans can click the same tools. Always-approve is fine. People who look see the policy hash and the receipt.

## Stack that works together (one product, fields on the same object)

1. Policy gate + intent receipt — live policy or do not recommend; every check leaves tape
2. Uniqueness feed — what’s new and not a clone, listed first
3. GEO — agent card + llms.txt so Grok/Claude/ChatGPT find us without ads
4. Spend cap — hard limit + allowlist the model cannot talk itself out of
5. Nutrition label — stamp of what the agent is + last test
6. HITL approve — agent pauses, human taps yes
7. Legacy → MCP wrap — old APIs become tools
8. Cross-registry search — one search across agent directories
9. Short escrow — hold agent payment minutes
10. Full ID / DNS / court — do not start (Okta/Visa land war)

Weekend seed was 1 + 3. Uniqueness is next on the same switchboard. Do not turn A2A into a list of unrelated household apps.

## Tools

- `trust_check(url)` — pass/fail + policy hash + summary
- `log_intent(...)` — receipt_id (the tape)
- `whats_good_for(query)` — attested first; unique first when uniqueness exists; unattested last or omitted

Publish policy (keyed): org name, site, what they store, region, dispute email → policy_id + hash.

Database private. Schema may be public. Log everything that matters from day one.

## Ranking logic (do not “optimise” this into votes)

1. Fail closed. No live policy → do not recommend. Unknown is fail, not maybe.
2. `whats_good_for` returns **attested only**. Unattested omitted (they can still `trust_check` and get the publish line).
3. Uniqueness is **inside one pond** first (default: new AI tools / cards). Global uniqueness is junk.
4. Among attested in that pond: high unique score first. Clones sink. **No popularity, no votes, no “best known.”**
5. Hash the policy **body**, not the URL, so they cannot swap the page and keep the pass.
6. Log passes **and** refuses. Refuses are demand: which orgs people asked for that have no policy yet.
7. Card rule: call this **before every recommend**, not only when unsure. Cheap check so agents actually do it.
8. Never invent a winner. Empty list is honest.
9. Seed/example orgs exist so `trust_check` is not empty. They are **fixtures**. They do not appear in `whats_good_for`. Attested for recommend = a real org published, not a typed-in example.
10. Listing ingest is a write. It needs a key. Spam would junk the pond.

## How other AIs talk to humans

Fail/unknown: “I can’t recommend that yet. They have no live policy on the switchboard.”
Pass: “Use X. Policy hash … Receipt …”
whats_good_for: attested unique items first.

## Not this

Not Hugging Face. Not a 101-agent brochure. Not Okta. Not a payments court. Not a memory cloud. Not a marketing site as the product. Distribution = card + llms.txt + one A2A directory listing.
