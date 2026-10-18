@echo off
cd /d %~dp0backend
pip install -r requirements.txt
uvicorn main:app --port 8000
