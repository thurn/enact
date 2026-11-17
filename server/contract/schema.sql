CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
-- keys: seq (global change counter), cutover (before|paused|after),
-- cutover_at, cutover_seq, token_hashes (hash to machine)

CREATE TABLE tickets (
  id          TEXT PRIMARY KEY,          -- 'q44s.7.24', no project prefix
  parent      TEXT,                      -- id with the last dotted part removed
  next_child  INTEGER NOT NULL DEFAULT 1,-- next number handed to a child
  title       TEXT NOT NULL,
  body        TEXT NOT NULL DEFAULT '',
  acceptance  TEXT NOT NULL DEFAULT '',
  design      TEXT NOT NULL DEFAULT '',
  notes       TEXT NOT NULL DEFAULT '',
  type        TEXT NOT NULL DEFAULT 'task',   -- task|epic|bug|feature|chore|decision|spike|story|milestone
  status      TEXT NOT NULL DEFAULT 'open',   -- open|in_progress|blocked|deferred|closed
  priority    INTEGER NOT NULL DEFAULT 2,     -- 0..4
  assignee    TEXT,
  last        INTEGER NOT NULL DEFAULT 0,     -- 1: runs after its siblings (section 4)
  labels      TEXT NOT NULL DEFAULT '[]',     -- JSON array of strings
  metadata    TEXT NOT NULL DEFAULT '{}',     -- JSON object (Enact's enact_* keys)
  defer_until TEXT,
  close_reason TEXT,
  created_at  TEXT NOT NULL, created_by TEXT NOT NULL,
  updated_at  TEXT NOT NULL, closed_at TEXT,
  version     INTEGER NOT NULL                -- value of seq at the last change
) WITHOUT ROWID;
CREATE INDEX tickets_live ON tickets(status) WHERE status != 'closed';
CREATE INDEX tickets_parent ON tickets(parent) WHERE parent IS NOT NULL;

CREATE TABLE edges (                       -- src depends on dst
  src TEXT NOT NULL, dst TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'blocks',     -- blocks|conditional-blocks|waits-for|related|discovered-from
  PRIMARY KEY (src, dst)
) WITHOUT ROWID;
CREATE INDEX edges_dst ON edges(dst);

CREATE TABLE comments (
  ticket TEXT NOT NULL, n INTEGER NOT NULL,
  author TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL,
  legacy_id TEXT,                          -- ID of an imported Beads comment
  PRIMARY KEY (ticket, n)
) WITHOUT ROWID;

CREATE TABLE history (                     -- the changelog
  ticket TEXT NOT NULL, seq INTEGER NOT NULL,
  actor TEXT NOT NULL, at TEXT NOT NULL,
  change TEXT NOT NULL,                    -- JSON: {field: [old, new], ...}
  PRIMARY KEY (ticket, seq)
) WITHOUT ROWID;

CREATE TABLE leases (
  name TEXT PRIMARY KEY, holder TEXT NOT NULL, machine TEXT NOT NULL,
  expires_at TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
  waiters TEXT NOT NULL DEFAULT '[]'       -- JSON array of {holder, machine, since}, arrival order
) WITHOUT ROWID;

CREATE TABLE events (
  seq INTEGER PRIMARY KEY, at TEXT NOT NULL,
  kind TEXT NOT NULL,                      -- session|ci|machine|cutover|usage
  machine TEXT NOT NULL, actor TEXT NOT NULL, data TEXT NOT NULL  -- JSON
);
CREATE INDEX events_kind ON events(kind, seq);

CREATE TABLE aliases (alias TEXT PRIMARY KEY, id TEXT NOT NULL) WITHOUT ROWID;
CREATE INDEX aliases_id ON aliases(id);
