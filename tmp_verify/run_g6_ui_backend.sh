#!/usr/bin/env bash
set -euo pipefail
ROOT=/home/wmd/projects/VideoClaw
BACKEND="$ROOT/video-claw/video-claw/backend"
docker run -d --name vc-g6-ui --network videoclaw_default -p 18501:8501 \
  --env-file "$ROOT/.env" \
  -e BACKEND_PORT=8501 \
  -e VC_DATABASE__DSN=postgresql://videoclaw:videoclaw@postgres:5432/videoclaw_test \
  -e VC_STORAGE__KIND=minio \
  -e VC_STORAGE__ENDPOINT=minio:9000 \
  -e VC_STORAGE__ACCESS_KEY=videoclaw \
  -e VC_STORAGE__SECRET_KEY=videoclaw-secret \
  -e VC_STORAGE__PUBLIC_ENDPOINT=http://minio:9000 \
  -e VC_STORAGE__BUCKET=videoclaw-test \
  -e VC_STORAGE__SAVED_BUCKET=videoclaw-test-saved \
  -v "$BACKEND/api_server.py:/app/api_server.py:ro" \
  -v "$BACKEND/config.py:/app/config.py:ro" \
  -v "$BACKEND/session.py:/app/session.py:ro" \
  -v "$BACKEND/api:/app/api:ro" \
  -v "$BACKEND/core:/app/core:ro" \
  -v "$BACKEND/db:/app/db:ro" \
  -v "$BACKEND/storage:/app/storage:ro" \
  -v "$BACKEND/models:/app/models:ro" \
  -v "$BACKEND/pipelines:/app/pipelines:ro" \
  -v "$BACKEND/prompts:/app/prompts:ro" \
  -v "$BACKEND/templates:/app/templates:ro" \
  video-claw-backend:latest
