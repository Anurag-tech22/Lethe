#!/usr/bin/env bash
# Embedded mode (no Docker). For Docker mode use: docker compose up --build
cd "$(dirname "$0")/backend" && pip install -r requirements.txt && uvicorn main:app --port 8000
