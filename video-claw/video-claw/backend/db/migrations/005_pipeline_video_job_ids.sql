-- 6.7: keep every managed video job of a multi-segment Pipeline task.
ALTER TABLE pipeline_tasks
  ADD COLUMN IF NOT EXISTS video_job_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
