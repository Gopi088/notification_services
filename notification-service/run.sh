#!/usr/bin/env bash
# Starts the API on http://127.0.0.1:8000 with auto-reload.
set -e
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
