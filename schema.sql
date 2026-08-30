-- A2A trust switchboard. SQLite. Do not open-source the database.

CREATE TABLE IF NOT EXISTS orgs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  domain TEXT NOT NULL UNIQUE,
  website TEXT,
  category TEXT,
  attested INTEGER NOT NULL DEFAULT 0,
  is_seed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policies (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  policy_hash TEXT NOT NULL,
  published_at TEXT NOT NULL,
  FOREIGN KEY (org_id) REFERENCES orgs(id)
);

CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  key_hash TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL,
  org_id TEXT,
  label TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
  id TEXT PRIMARY KEY,
  query TEXT,
  action TEXT,
  target_url TEXT,
  policy_id TEXT,
  key_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checks (
  id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  domain TEXT,
  result TEXT NOT NULL,
  policy_id TEXT,
  policy_hash TEXT,
  summary TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ideas (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  domain TEXT NOT NULL,
  website TEXT,
  category TEXT,
  need_json TEXT NOT NULL DEFAULT '[]',
  summary TEXT,
  attested INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policies_org ON policies(org_id, version);
CREATE INDEX IF NOT EXISTS idx_orgs_domain ON orgs(domain);
CREATE INDEX IF NOT EXISTS idx_receipts_created ON receipts(created_at);
CREATE INDEX IF NOT EXISTS idx_ideas_domain ON ideas(domain);

-- Uniqueness feed (A2A idea 2). New things that are not clones, listed first.
CREATE TABLE IF NOT EXISTS listings (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  one_liner TEXT,
  unique_note TEXT,
  unique_score INTEGER NOT NULL DEFAULT 0,
  org_id TEXT,
  pond TEXT NOT NULL DEFAULT 'ai-tools',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listings_score ON listings(unique_score);
CREATE INDEX IF NOT EXISTS idx_listings_pond ON listings(pond);

-- Human/AI click after a receipt. The log is the company.
CREATE TABLE IF NOT EXISTS clicks (
  id TEXT PRIMARY KEY,
  receipt_id TEXT,
  item_url TEXT NOT NULL,
  item_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clicks_created ON clicks(created_at);
CREATE INDEX IF NOT EXISTS idx_clicks_receipt ON clicks(receipt_id);

-- Every whats_good_for call. Zero matches = demand for a needed-but-missing thing.
CREATE TABLE IF NOT EXISTS searches (
  id TEXT PRIMARY KEY,
  query TEXT NOT NULL,
  pond TEXT,
  match_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_searches_created ON searches(created_at);
CREATE INDEX IF NOT EXISTS idx_searches_query ON searches(query);

CREATE INDEX IF NOT EXISTS idx_checks_created ON checks(created_at);
CREATE INDEX IF NOT EXISTS idx_receipts_key ON receipts(key_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_org ON api_keys(org_id);
-- Extra columns live in db.migrate (ALTER if missing): policies.stores_what/data_region/dispute_email/policy_url; receipts.caller_id/result/idempotency_key; checks.reason/query; api_keys.revoked.

-- Extra policy columns (A2A 4–6: spend cap, nutrition, last test). ALTER in db.migrate if missing.
-- policies.spend_cap_usd REAL
-- policies.allowlist_json TEXT  -- JSON array of allowed actions e.g. ["recommend","call","pay"]
-- policies.agent_kind TEXT      -- nutrition: what this agent/org is
-- policies.last_test_at TEXT
-- policies.last_test_result TEXT  -- pass|fail|untested
-- policies.last_test_note TEXT

-- Spend ledger (A2A 4). Hard cap sits outside the model.
CREATE TABLE IF NOT EXISTS spend_ledger (
  id TEXT PRIMARY KEY,
  org_id TEXT,
  amount_usd REAL,
  action TEXT,
  target_url TEXT,
  receipt_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spend_ledger_org ON spend_ledger(org_id);
CREATE INDEX IF NOT EXISTS idx_spend_ledger_created ON spend_ledger(created_at);

-- HITL approve (A2A 6). Agent pauses; human taps yes.
CREATE TABLE IF NOT EXISTS approvals (
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
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_approvals_created ON approvals(created_at);
