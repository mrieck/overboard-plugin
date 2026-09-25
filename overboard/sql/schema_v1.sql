-- Documented copy of the Mac app's database schema (Overboard/Store/SQLite/Migrations.swift, v1).
-- The app owns and migrates the real file; this copy exists for the plugin's
-- read-only reader (overboard/runhistory.py) and its tests. Keep in sync.
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE ships (id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, builtin TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0, archived_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE agents (id TEXT PRIMARY KEY, ship_id TEXT, kind TEXT NOT NULL DEFAULT 'scheduled', name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0, provider TEXT NOT NULL DEFAULT 'claude', model TEXT, last_fired_at REAL,
  payload TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE runs (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, agent_name TEXT NOT NULL,
  agent_kind TEXT NOT NULL DEFAULT 'scheduled', ship_id TEXT, work_item_id TEXT, dispatch_id TEXT,
  trigger TEXT NOT NULL, provider TEXT NOT NULL DEFAULT 'claude', model TEXT, cwd TEXT NOT NULL, project_dir TEXT,
  started_at REAL NOT NULL, ended_at REAL, outcome TEXT, is_failure INTEGER NOT NULL DEFAULT 0, session_id TEXT,
  launch_run_id TEXT, summary TEXT, completion_message TEXT, minutes REAL, input_tokens INTEGER, output_tokens INTEGER,
  cache_creation_tokens INTEGER, cache_read_tokens INTEGER, work_tokens INTEGER, hidden INTEGER NOT NULL DEFAULT 0,
  payload TEXT NOT NULL);
CREATE TABLE work_items (id TEXT PRIMARY KEY, ship_id TEXT NOT NULL, status TEXT NOT NULL, origin TEXT NOT NULL,
  dispatch_id TEXT, agent_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL,
  payload TEXT NOT NULL);
CREATE TABLE assets (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ship_id TEXT, work_item_id TEXT, path TEXT NOT NULL,
  stored_path TEXT, kind TEXT NOT NULL, mime TEXT, caption TEXT, bytes INTEGER, thumbnail_path TEXT,
  copied INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL, added_at REAL NOT NULL, UNIQUE(run_id, path));
CREATE TABLE journal (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ship_id TEXT, ts REAL NOT NULL,
  type TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(run_id, ts, type));
CREATE TABLE usage_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ship_id TEXT, session_id TEXT,
  ts REAL NOT NULL, model TEXT, input INTEGER NOT NULL, output INTEGER NOT NULL, cache_creation INTEGER NOT NULL,
  cache_read INTEGER NOT NULL, work_tokens INTEGER NOT NULL, source TEXT NOT NULL);
