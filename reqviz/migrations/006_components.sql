-- コンポーネント（AP サーバー、ミドルウェア、OS など）と、コンポーネント間の依存関係

CREATE TABLE components (
  id INTEGER PRIMARY KEY,
  system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',          -- 種別（アプリケーション / ミドルウェア / OS など。自由な文字列）
  description TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  row_version INTEGER NOT NULL DEFAULT 1,
  UNIQUE (system_id, key)
);

CREATE TABLE component_deps (              -- from の設計が to に波及する
  id INTEGER PRIMARY KEY,
  system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
  from_component INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
  to_component INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
  note TEXT NOT NULL DEFAULT '',
  notify INTEGER NOT NULL DEFAULT 0,       -- 1: from 側の値が変わったら、to 側の承認済みの項目に確認フラグを付ける
  UNIQUE (from_component, to_component)
);

ALTER TABLE params ADD COLUMN component_id INTEGER REFERENCES components(id) ON DELETE SET NULL;
ALTER TABLE items ADD COLUMN component_id INTEGER REFERENCES components(id) ON DELETE SET NULL;
