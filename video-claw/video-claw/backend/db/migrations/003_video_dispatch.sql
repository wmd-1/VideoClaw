-- 003: video resource pool dispatch (jobs / attempts / physical slots)
-- 全部语句幂等：迁移 runner 可重复执行；业务对象删除不得级联解除在途槽位。

CREATE TABLE IF NOT EXISTS video_dispatch_jobs (
  job_id            TEXT PRIMARY KEY,
  owner             TEXT NOT NULL,
  batch_id          TEXT NOT NULL,
  source_kind       TEXT NOT NULL,
  source_id         TEXT,
  execution_id      TEXT,
  item_id           TEXT NOT NULL,
  request_key       TEXT NOT NULL,
  dispatch_mode     TEXT NOT NULL,
  profile_id        TEXT,
  profile_revision  INTEGER,
  snapshot_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
  state             TEXT NOT NULL DEFAULT 'queued',
  cancel_requested  BOOLEAN NOT NULL DEFAULT FALSE,
  accepted_at       BIGINT NOT NULL,
  queue_deadline_at BIGINT NOT NULL,
  error_json        JSONB,
  result_json       JSONB,
  CONSTRAINT video_dispatch_jobs_mode_check
    CHECK (dispatch_mode IN ('auto', 'manual')),
  CONSTRAINT video_dispatch_jobs_state_check
    CHECK (state IN (
      'queued', 'reserved', 'submitting', 'remote_queued', 'running',
      'downloading', 'succeeded', 'failed', 'cancelled', 'unknown', 'reconciling'
    )),
  CONSTRAINT video_dispatch_jobs_request_unique
    UNIQUE (owner, request_key, item_id)
);
CREATE INDEX IF NOT EXISTS ix_video_dispatch_jobs_owner_state
  ON video_dispatch_jobs (owner, state, accepted_at);
CREATE INDEX IF NOT EXISTS ix_video_dispatch_jobs_batch
  ON video_dispatch_jobs (batch_id, state);
CREATE INDEX IF NOT EXISTS ix_video_dispatch_jobs_deadline
  ON video_dispatch_jobs (state, queue_deadline_at);

CREATE TABLE IF NOT EXISTS video_dispatch_attempts (
  attempt_id        TEXT PRIMARY KEY,
  job_id            TEXT NOT NULL REFERENCES video_dispatch_jobs(job_id) ON DELETE CASCADE,
  attempt_no        INTEGER NOT NULL,
  instance_id       TEXT NOT NULL,
  resource_key      TEXT NOT NULL,
  config_revision   TEXT,
  submission_state  TEXT NOT NULL DEFAULT 'prepared',
  remote_job_id     TEXT,
  endpoint_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  started_at        BIGINT,
  finished_at       BIGINT,
  error_json        JSONB,
  CONSTRAINT video_dispatch_attempts_state_check
    CHECK (submission_state IN ('prepared', 'submitting', 'submitted', 'unknown', 'terminal', 'cancelled')),
  CONSTRAINT video_dispatch_attempts_no_unique UNIQUE (job_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS ix_video_dispatch_attempts_resource
  ON video_dispatch_attempts (resource_key, submission_state);
CREATE INDEX IF NOT EXISTS ix_video_dispatch_attempts_remote
  ON video_dispatch_attempts (instance_id, remote_job_id);

-- active_attempt_id 用 RESTRICT：删除业务对象/作业不能顺带清掉仍在途的资源账本。
-- 释放槽位必须走 repository 的条件更新，不能靠 DELETE CASCADE。
CREATE TABLE IF NOT EXISTS video_resource_slots (
  resource_key       TEXT PRIMARY KEY,
  active_attempt_id  TEXT REFERENCES video_dispatch_attempts(attempt_id) ON DELETE RESTRICT,
  token              TEXT,
  boot_id            TEXT,
  heartbeat_at       BIGINT,
  quarantine_reason  TEXT,
  metadata_json      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_video_resource_slots_heartbeat
  ON video_resource_slots (heartbeat_at);

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '3')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
