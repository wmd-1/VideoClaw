-- VideoClaw user-space metadata schema (PostgreSQL 16).
-- All statements are safe to run through the migration runner more than once.

CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id         TEXT PRIMARY KEY,
  owner              TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'idle',
  current_stage      TEXT,
  status_json        JSONB NOT NULL DEFAULT '{}'::jsonb,
  stage_progress_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  meta_json          JSONB NOT NULL DEFAULT '{}'::jsonb,
  boot_id            TEXT,
  created_at         BIGINT NOT NULL,
  updated_at         BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_owner_updated ON sessions(owner, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_sessions_updated       ON sessions(updated_at DESC);

CREATE TABLE IF NOT EXISTS session_stage_artifacts (
  session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  stage        TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  error        TEXT,
  boot_id      TEXT,
  version      INTEGER NOT NULL DEFAULT 0,
  started_at   BIGINT,
  completed_at BIGINT,
  updated_at   BIGINT NOT NULL,
  PRIMARY KEY (session_id, stage)
);
CREATE INDEX IF NOT EXISTS ix_stage_artifacts_status ON session_stage_artifacts(status);

CREATE TABLE IF NOT EXISTS artifact_files (
  file_id      TEXT PRIMARY KEY,
  owner        TEXT NOT NULL,
  session_id   TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
  task_id      TEXT,
  record_id    TEXT,
  stage        TEXT,
  kind         TEXT NOT NULL,
  storage_kind TEXT NOT NULL,
  bucket       TEXT,
  object_key   TEXT NOT NULL,
  public_path  TEXT NOT NULL UNIQUE,
  local_path   TEXT,
  media_type   TEXT,
  size_bytes   BIGINT,
  missing_at   BIGINT,
  created_at   BIGINT NOT NULL,
  CONSTRAINT ck_artifact_one_parent CHECK (num_nonnulls(session_id, task_id, record_id) = 1)
);
CREATE INDEX IF NOT EXISTS ix_artifact_files_owner   ON artifact_files(owner, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_artifact_files_session ON artifact_files(session_id);
CREATE INDEX IF NOT EXISTS ix_artifact_files_task    ON artifact_files(task_id);
CREATE INDEX IF NOT EXISTS ix_artifact_files_public  ON artifact_files(public_path);

CREATE TABLE IF NOT EXISTS pipeline_tasks (
  task_id          TEXT PRIMARY KEY,
  owner            TEXT NOT NULL,
  pipeline         TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending',
  progress         INTEGER NOT NULL DEFAULT 0,
  message          TEXT,
  input_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
  output_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
  artifacts_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
  error            TEXT,
  created_at       BIGINT NOT NULL,
  updated_at       BIGINT NOT NULL,
  started_at       BIGINT,
  completed_at     BIGINT,
  duration_seconds DOUBLE PRECISION,
  output_dir       TEXT,
  boot_id          TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasks_owner_created ON pipeline_tasks(owner, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_tasks_status        ON pipeline_tasks(status);

CREATE TABLE IF NOT EXISTS sandbox_records (
  record_id   TEXT PRIMARY KEY,
  owner       TEXT NOT NULL,
  tool        TEXT NOT NULL,
  model       TEXT NOT NULL,
  input_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
  output_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  files_json  JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at  BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sandbox_owner_created ON sandbox_records(owner, created_at DESC);

CREATE TABLE IF NOT EXISTS sandbox_paths (
  path      TEXT PRIMARY KEY,
  record_id TEXT NOT NULL REFERENCES sandbox_records(record_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_sandbox_paths_record ON sandbox_paths(record_id);

CREATE TABLE IF NOT EXISTS saved_items (
  item_id       TEXT PRIMARY KEY,
  owner         TEXT NOT NULL,
  work_id       TEXT NOT NULL,
  domain        TEXT NOT NULL,
  title         TEXT,
  source_bucket TEXT NOT NULL,
  source_key    TEXT NOT NULL,
  bucket        TEXT NOT NULL,
  object_key    TEXT NOT NULL,
  size_bytes    BIGINT,
  saved_at      BIGINT NOT NULL,
  UNIQUE(owner, work_id)
);
CREATE INDEX IF NOT EXISTS ix_saved_owner_saved ON saved_items(owner, saved_at DESC);

-- Child FKs are added after both parent tables exist. DO blocks keep the runner re-entrant.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_files_task') THEN
    ALTER TABLE artifact_files
      ADD CONSTRAINT fk_files_task FOREIGN KEY (task_id)
      REFERENCES pipeline_tasks(task_id) ON DELETE CASCADE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_files_record') THEN
    ALTER TABLE artifact_files
      ADD CONSTRAINT fk_files_record FOREIGN KEY (record_id)
      REFERENCES sandbox_records(record_id) ON DELETE CASCADE;
  END IF;
END $$;

CREATE OR REPLACE FUNCTION enforce_artifact_owner() RETURNS trigger AS $$
DECLARE
  expected_owner TEXT;
BEGIN
  IF NEW.session_id IS NOT NULL THEN
    SELECT owner INTO expected_owner FROM sessions WHERE session_id = NEW.session_id;
  ELSIF NEW.task_id IS NOT NULL THEN
    SELECT owner INTO expected_owner FROM pipeline_tasks WHERE task_id = NEW.task_id;
  ELSIF NEW.record_id IS NOT NULL THEN
    SELECT owner INTO expected_owner FROM sandbox_records WHERE record_id = NEW.record_id;
  END IF;

  IF expected_owner IS NULL OR expected_owner <> NEW.owner THEN
    RAISE EXCEPTION 'artifact owner mismatch: artifact=% parent=%',
      NEW.owner, COALESCE(expected_owner, '<missing>');
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_artifact_owner ON artifact_files;
CREATE TRIGGER trg_artifact_owner
BEFORE INSERT OR UPDATE OF owner, session_id, task_id, record_id
ON artifact_files
FOR EACH ROW EXECUTE FUNCTION enforce_artifact_owner();

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1')
ON CONFLICT (key) DO NOTHING;
