#!/bin/bash
source .venv/bin/activate
uvicorn main:app --app-dir apps/slide-api --host 0.0.0.0 --port 8001 --reload
