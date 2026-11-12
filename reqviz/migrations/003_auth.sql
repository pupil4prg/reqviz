-- ログイン認証（セッション、API トークン、アカウントの状態）

ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN updated_at TEXT;

CREATE TABLE sessions (
  id INTEGER PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,     -- Cookie の値の SHA-256
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE api_tokens (              -- REST API・MCP・A2A 用の個人トークン
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,     -- トークンの SHA-256（平文は保存しない）
  prefix TEXT NOT NULL,                -- 一覧で見分けるための先頭部分
  created_at TEXT NOT NULL,
  last_used_at TEXT
);
