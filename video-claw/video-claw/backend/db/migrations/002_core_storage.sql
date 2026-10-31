-- 002: core storage primitives (design D12/D17)
-- 全部语句幂等：迁移 runner 可重复执行（容器内重入验收口径）。

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS content_revision BIGINT NOT NULL DEFAULT 0;

-- 工作层：已保存但不能晋升 completed 的用户工作稿 / 预览 / 执行态
CREATE TABLE IF NOT EXISTS session_stage_working (
  session_id     TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  stage          TEXT NOT NULL,
  working_json   JSONB NULL,
  preview_json   JSONB NULL,
  execution_json JSONB NULL,
  execution_id   TEXT NULL,
  base_version   INTEGER NOT NULL DEFAULT 0,
  updated_at     BIGINT NOT NULL,
  PRIMARY KEY (session_id, stage)
);

-- 对象生命周期日志兼清理队列：无业务父资源 FK，父删除后仍保留清理凭据
CREATE TABLE IF NOT EXISTS object_operations (
  operation_id     TEXT PRIMARY KEY,
  owner            TEXT NOT NULL,
  domain           TEXT,
  resource_id      TEXT,
  stage            TEXT,
  execution_id     TEXT,
  boot_id          TEXT,
  file_id          TEXT,
  target_kind      TEXT NOT NULL,
  bucket           TEXT NOT NULL,
  object_key       TEXT NOT NULL,
  public_path      TEXT,
  local_paths_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  state            TEXT NOT NULL DEFAULT 'prepared',
  reason           TEXT,
  attempts         INTEGER NOT NULL DEFAULT 0,
  last_error       TEXT,
  next_retry_at    BIGINT,
  created_at       BIGINT NOT NULL,
  updated_at       BIGINT NOT NULL,
  CONSTRAINT object_operations_state_check
    CHECK (state IN ('prepared', 'retained', 'delete_pending', 'done')),
  CONSTRAINT object_operations_kind_check CHECK (target_kind IN ('minio', 'local')),
  CONSTRAINT object_operations_target_unique UNIQUE (bucket, object_key)
);

CREATE INDEX IF NOT EXISTS idx_object_operations_due
  ON object_operations (state, next_retry_at);

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
