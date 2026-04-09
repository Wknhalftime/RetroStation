api:    uv run python -m backend.run_server
worker: uv run python -m huey.bin.huey_consumer backend.tasks.huey_app.huey -w 1
web:    cd frontend && npm run dev
