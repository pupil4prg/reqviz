-- パラメータのタグ（構成項目には 002 で追加済み）

ALTER TABLE params ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';  -- JSON [タグ]
