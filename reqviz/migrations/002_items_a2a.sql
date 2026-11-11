-- 構成項目（列挙データ・不定形データ）と A2A タスク

CREATE TABLE items (
  id INTEGER PRIMARY KEY,
  system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
  layer_id INTEGER NOT NULL REFERENCES layers(id) ON DELETE RESTRICT,
  key TEXT NOT NULL,           -- 参照名は「layer.key」。パラメータと同じ名前空間
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('choice', 'document')),
  options TEXT NOT NULL DEFAULT '[]',   -- choice: JSON [{key, label, description, pros, cons, attributes}]
  selected TEXT,                        -- choice: 採用した選択肢のキー
  content_type TEXT NOT NULL DEFAULT 'text'
    CHECK (content_type IN ('text', 'markdown', 'json', 'yaml')),
  content TEXT,                         -- document: JSON でエンコードした本文
  tags TEXT NOT NULL DEFAULT '[]',
  rationale TEXT NOT NULL DEFAULT '',
  ref_doc TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'in_review', 'approved')),
  row_version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL,
  UNIQUE (system_id, layer_id, key)
);
CREATE INDEX idx_items_system ON items (system_id, kind);

CREATE INDEX idx_revisions_entity ON revisions (system_id, entity, entity_key, id);

CREATE TABLE a2a_tasks (
  id TEXT PRIMARY KEY,
  context_id TEXT NOT NULL,
  state TEXT NOT NULL,
  status_message TEXT,          -- JSON（Message）
  status_at TEXT NOT NULL,
  history TEXT NOT NULL DEFAULT '[]',
  artifacts TEXT NOT NULL DEFAULT '[]',
  agent_state TEXT NOT NULL DEFAULT '{}',  -- 対話エンジンの内部状態（LLM の会話、確認待ちの操作）
  metadata TEXT NOT NULL DEFAULT '{}',
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_a2a_tasks_context ON a2a_tasks (context_id, created_at);
