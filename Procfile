api:    uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
worker: uv run python -m huey.bin.huey_consumer backend.tasks.huey_app.huey -w 1 -n
web:    cd frontend && npm run dev
