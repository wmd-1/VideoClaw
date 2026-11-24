-- 6.7: persist the managed video job id so Pipeline task status survives process restart.
ALTER TABLE pipeline_tasks ADD COLUMN IF NOT EXISTS video_job_id TEXT;
