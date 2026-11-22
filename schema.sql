-- realm schema v2
-- Converged with Codex's spec (2026-11-10). One SQLite file, normalized per kind.
-- Typed nodes in a common table + type-specific side tables enforced by triggers.
-- Two enforcement gates. Integer unix timestamps.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS nodes (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('task','fact','decision','artifact','attempt')),
  title      TEXT NOT NULL,
  body       TEXT NOT NULL DEFAULT '',
  -- materialized taint cache; ALWAYS recomputed by refresh_taint() before it is read
  tainted    INTEGER NOT NULL DEFAULT 0 CHECK (tainted IN (0,1)),
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS nodes_kind_idx ON nodes(kind);

CREATE TABLE IF NOT EXISTS tasks (
  node_id  TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
  state    TEXT NOT NULL DEFAULT 'OPEN' CHECK (state IN ('OPEN','DONE','DROPPED')),
  priority INTEGER NOT NULL DEFAULT 50,
  cost     INTEGER NOT NULL DEFAULT 1 CHECK (cost > 0)
);
CREATE INDEX IF NOT EXISTS tasks_state_priority_idx ON tasks(state, priority DESC);

CREATE TABLE IF NOT EXISTS facts (
  node_id        TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
  claim          TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN
                   ('UNVERIFIED','VERIFIED','UNCHECKABLE','REFUTED','CONTESTED')),
  check_cmd      TEXT,
  check_hash     TEXT,
  check_strength TEXT CHECK (check_strength IN ('OK','TRIVIAL','FLAKY')),
  check_exit     INTEGER,
  check_out      TEXT,
  checked_at     INTEGER,
  attempt_limit  INTEGER NOT NULL DEFAULT 1 CHECK (attempt_limit > 0),
  rerun_after_taint INTEGER NOT NULL DEFAULT 0,
  -- set when an agent explicitly cites this fact with --assume; makes it a taint root
  assumed INTEGER NOT NULL DEFAULT 0 CHECK (assumed IN (0,1)),
  CHECK ((check_cmd IS NULL) = (check_hash IS NULL)),
  CHECK (check_cmd IS NOT NULL OR status IN ('UNVERIFIED','UNCHECKABLE'))
);
CREATE INDEX IF NOT EXISTS facts_status_idx ON facts(status);

CREATE TABLE IF NOT EXISTS decisions (
  node_id   TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
  rationale TEXT NOT NULL DEFAULT '',
  superseded_by TEXT REFERENCES nodes(id)
);

CREATE TABLE IF NOT EXISTS artifacts (
  node_id    TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
  digest     TEXT NOT NULL UNIQUE CHECK (length(digest) = 64
                 AND digest NOT GLOB '*[^0-9a-f]*'),
  byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
  source_path TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
  node_id    TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
  run_id     TEXT NOT NULL,
  run_kind   TEXT NOT NULL CHECK (run_kind IN ('run','verify')),
  target_id  TEXT REFERENCES nodes(id),
  attempt_no INTEGER,
  argv_json  TEXT NOT NULL,
  cwd        TEXT NOT NULL,
  git_rev    TEXT,
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  exit_code  INTEGER,
  stdout_digest TEXT,
  check_hash TEXT,
  input_hash TEXT
);
CREATE INDEX IF NOT EXISTS attempts_target_idx ON attempts(target_id, check_hash, input_hash);

-- from_id is the DEPENDENT, to_id is the PREREQUISITE (Codex's direction)
CREATE TABLE IF NOT EXISTS edges (
  from_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  to_id   TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  kind    TEXT NOT NULL CHECK (kind IN
            ('requires','depends_on','cites','assumes','supports','contradicts',
             'invalidates','evidence','supersedes','produces')),
  at      INTEGER NOT NULL,
  PRIMARY KEY (from_id, to_id, kind),
  CHECK (from_id <> to_id)
);
CREATE INDEX IF NOT EXISTS edges_from_kind_idx ON edges(from_id, kind);
CREATE INDEX IF NOT EXISTS edges_to_kind_idx ON edges(to_id, kind);

CREATE TABLE IF NOT EXISTS leases (
  resource      TEXT PRIMARY KEY,
  owner         TEXT,
  fencing_token INTEGER NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
  acquired_at   INTEGER,
  expires_at    INTEGER,
  CHECK ((owner IS NULL AND acquired_at IS NULL AND expires_at IS NULL)
      OR (owner IS NOT NULL AND acquired_at IS NOT NULL AND expires_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS leases_expiry_idx ON leases(expires_at);

-- (fact, check, input) -> result. This is the cache that makes re-verify cheap.
CREATE TABLE IF NOT EXISTS verify_cache (
  fact_id       TEXT NOT NULL REFERENCES facts(node_id) ON DELETE CASCADE,
  check_hash    TEXT NOT NULL,
  input_hash    TEXT NOT NULL,
  result        TEXT NOT NULL CHECK (result IN ('PASS','FAIL','UNCHECKABLE')),
  attempt_count INTEGER NOT NULL CHECK (attempt_count > 0),
  strength      TEXT,
  updated_at    INTEGER NOT NULL,
  PRIMARY KEY (fact_id, check_hash, input_hash)
);

CREATE TABLE IF NOT EXISTS events (
  cursor   INTEGER PRIMARY KEY AUTOINCREMENT,
  at       INTEGER NOT NULL,
  actor    TEXT NOT NULL,
  op       TEXT NOT NULL,
  node_id  TEXT,
  resource TEXT,
  token    INTEGER,
  detail   TEXT
);
CREATE INDEX IF NOT EXISTS events_node_cursor_idx ON events(node_id, cursor);

-- kind integrity: a side-table row may only reference a node of its own kind
CREATE TRIGGER IF NOT EXISTS tasks_kind_ins BEFORE INSERT ON tasks
  WHEN COALESCE((SELECT kind FROM nodes WHERE id = NEW.node_id), '') <> 'task'
  BEGIN SELECT RAISE(ABORT, 'tasks.node_id must be a task'); END;
CREATE TRIGGER IF NOT EXISTS facts_kind_ins BEFORE INSERT ON facts
  WHEN COALESCE((SELECT kind FROM nodes WHERE id = NEW.node_id), '') <> 'fact'
  BEGIN SELECT RAISE(ABORT, 'facts.node_id must be a fact'); END;
CREATE TRIGGER IF NOT EXISTS decisions_kind_ins BEFORE INSERT ON decisions
  WHEN COALESCE((SELECT kind FROM nodes WHERE id = NEW.node_id), '') <> 'decision'
  BEGIN SELECT RAISE(ABORT, 'decisions.node_id must be a decision'); END;
CREATE TRIGGER IF NOT EXISTS artifacts_kind_ins BEFORE INSERT ON artifacts
  WHEN COALESCE((SELECT kind FROM nodes WHERE id = NEW.node_id), '') <> 'artifact'
  BEGIN SELECT RAISE(ABORT, 'artifacts.node_id must be an artifact'); END;
CREATE TRIGGER IF NOT EXISTS attempts_kind_ins BEFORE INSERT ON attempts
  WHEN COALESCE((SELECT kind FROM nodes WHERE id = NEW.node_id), '') <> 'attempt'
  BEGIN SELECT RAISE(ABORT, 'attempts.node_id must be an attempt'); END;

-- Schema version. Written by the CLI at init and at every migration; read by clients
-- (Realm Lens) inside their snapshot transaction so a migration cannot land underneath them.
-- 0 means "predates versioning" and clients must refuse to guess.
PRAGMA user_version = 2;
