-- 環境（本番・検証・開発など）。同じ env_group_id を持つシステムが、同じシステムの環境違いになる

ALTER TABLE systems ADD COLUMN env_group_id INTEGER;            -- NULL は環境を分けていない
ALTER TABLE systems ADD COLUMN env_key TEXT NOT NULL DEFAULT '';  -- 例: prod
ALTER TABLE systems ADD COLUMN env_name TEXT NOT NULL DEFAULT ''; -- 例: 本番
CREATE INDEX idx_systems_env_group ON systems (env_group_id);
