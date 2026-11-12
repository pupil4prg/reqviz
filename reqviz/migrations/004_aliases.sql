-- パラメータの別名（実装上の名前、論理名など）

ALTER TABLE params ADD COLUMN aliases TEXT NOT NULL DEFAULT '[]';  -- JSON [{label, value}]
