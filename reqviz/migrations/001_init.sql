-- 初期スキーマ

CREATE TABLE systems (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  template_id INTEGER,
  template_version INTEGER,
  current_baseline_id INTEGER,
  row_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE layers (
  id INTEGER PRIMARY KEY,
  system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  name TEXT NOT NULL,
  sort_order INTEGER NOT NULL,
  row_version INTEGER NOT NULL DEFAULT 1,
  UNIQUE (system_id, key)
);

CREATE TABLE params (
  id INTEGER PRIMARY KEY,
  system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
  layer_id INTEGER NOT NULL REFERENCES layers(id) ON DELETE RESTRICT,
  key TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('number', 'string', 'enum', 'bool')),
  enum_options TEXT NOT NULL DEFAULT '[]',
  value TEXT,                -- JSON
  unit TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'formula', 'ai')),
  rationale TEXT NOT NULL DEFAULT '',
  ref_doc TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'ai_evaluated', 'in_review', 'approved')),
  ai_skipped INTEGER NOT NULL DEFAULT 0,
  flag_stale INTEGER NOT NULL DEFAULT 0,
  flag_upstream_changed INTEGER NOT NULL DEFAULT 0,
  proposed_value TEXT,       -- JSON（再計算案）
  proposed_error TEXT,       -- 再計算に失敗した理由
  row_version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL,
  UNIQUE (system_id, layer_id, key)
);

CREATE TABLE relations (     -- 手動の関連（算出式・制約による関連はルールから導出する）
  id INTEGER PRIMARY KEY,
  system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
  from_param INTEGER NOT NULL REFERENCES params(id) ON DELETE CASCADE,
  to_param INTEGER NOT NULL REFERENCES params(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'influences' CHECK (kind IN ('influences')),
  note TEXT NOT NULL DEFAULT '',
  UNIQUE (from_param, to_param)
);

CREATE TABLE rules (
  id INTEGER PRIMARY KEY,
  system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('formula', 'constraint', 'nl')),
  target TEXT,               -- パラメータ参照（layer.key）
  expression TEXT,
  nl_text TEXT,
  severity TEXT NOT NULL DEFAULT 'error' CHECK (severity IN ('error', 'warning')),
  description TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  row_version INTEGER NOT NULL DEFAULT 1,
  UNIQUE (system_id, key)
);

CREATE TABLE revisions (
  id INTEGER PRIMARY KEY,
  system_id INTEGER,
  entity TEXT NOT NULL,
  entity_key TEXT NOT NULL,  -- 参照名やルールキーなど、版をまたいで安定した識別子
  op TEXT NOT NULL CHECK (op IN ('create', 'update', 'delete', 'restore')),
  before_json TEXT,
  after_json TEXT,
  actor TEXT NOT NULL,
  comment TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL
);
CREATE INDEX idx_revisions_system ON revisions (system_id, id);

CREATE TABLE baselines (
  id INTEGER PRIMARY KEY,
  system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
  version_no INTEGER NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('in_review', 'approved', 'rejected')),
  payload TEXT NOT NULL,
  warnings TEXT NOT NULL DEFAULT '[]',
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  decided_by TEXT,
  decided_at TEXT,
  decision_comment TEXT,
  UNIQUE (system_id, version_no)
);

CREATE TABLE templates (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (name, version)
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  system_id INTEGER,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target_type TEXT,
  target_key TEXT,
  detail_json TEXT,
  comment TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL
);
CREATE INDEX idx_audit_system ON audit_log (system_id, id);

CREATE TABLE ai_runs (
  id INTEGER PRIMARY KEY,
  system_id INTEGER REFERENCES systems(id) ON DELETE CASCADE,
  purpose TEXT NOT NULL CHECK (purpose IN ('evaluate', 'suggest_values', 'suggest_rules', 'rationale')),
  targets TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
  model TEXT,
  request_json TEXT,
  response_json TEXT,
  error TEXT,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE ai_suggestions (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES ai_runs(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('finding', 'value', 'rule', 'relation', 'rationale')),
  target TEXT,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'adopted', 'rejected', 'info')),
  decided_by TEXT,
  decided_at TEXT
);

CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  login TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'designer' CHECK (role IN ('designer', 'approver', 'admin')),
  password_hash TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
