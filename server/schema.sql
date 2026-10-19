PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, number TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 nickname TEXT NOT NULL, total_kop INTEGER NOT NULL CHECK(total_kop>0), currency TEXT NOT NULL DEFAULT 'RUB',
 mode TEXT NOT NULL CHECK(mode IN ('demo','manual')), mvp_confirmed INTEGER NOT NULL DEFAULT 0,
 payment_status TEXT NOT NULL DEFAULT 'unpaid', fulfillment_status TEXT NOT NULL DEFAULT 'new',
 refunded_kop INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
 access_hash TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
 costs_json TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS orders_created ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS orders_nickname ON orders(nickname);
CREATE TABLE IF NOT EXISTS order_items (
 order_id TEXT NOT NULL REFERENCES orders(id), product_id TEXT NOT NULL, name TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity>0), unit_kop INTEGER NOT NULL CHECK(unit_kop>0),
 PRIMARY KEY(order_id,product_id)
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), created_at TEXT NOT NULL,
 actor TEXT NOT NULL, type TEXT NOT NULL, details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_order ON events(order_id,created_at);
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), created_at TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('supplier','payment','fiscal','other')), name TEXT NOT NULL,
 mime TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, shared INTEGER NOT NULL DEFAULT 0,
 data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
);
PRAGMA user_version=1;
