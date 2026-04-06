---
type: "always_apply"
---

# Commands
- Dev: 	d:\PythonStuff\RetroStation\scripts\start.ps1
- Build: unknown
- Test Single: uv run pytest tests/path/to/test.py::test_name
- Test All: uv run pytest
- Typecheck Frontend: uv run mypy backend --strict
- Typecheck Backend:  npm run typecheck (from frontend/)
- Lint: uv run ruff check .
- Dependency Managment: Runtime managed by uv (uv sync, uv run)
- Dev dependencies (pytest, mypy, ruff) are in [project.optional-dependencies] dev

# Code Standards
- Maximum Line Lenght 100 charatures
- Dataclasses for all domain models (no Pydantic models in the domain layer)
- ABCs (abc.ABC + @abstractmethod) for all repository interfaces
- In-memory fakes in tests/fakes/ implement the same ABCs (no mocking the repo layer in unit tests)
- from __future__ import annotations used in files with forward references
- Enums are str, Enum subclasses for JSON-serialization compatibility
- Logging: structlog (JSON in production, ConsoleRenderer in DEBUG); sys.stdout/stderr reconfigured to UTF-8 on Windows before any logging

# Testing Standards
- Framework: pytest>=8.0 with pytest-asyncio (asyncio_mode = "auto")
- No sleep-based ordering tests — use explicit timestamps instead (per the QA plan)
- Integration tests marked with @pytest.mark.integration (require real PostgreSQL)
- Slow tests marked with @pytest.mark.slow (>1s, large fixtures, etc.)
- Fast CI split: run pytest -m "not integration and not slow" for quick feedback
- Integration tests use real DB with a retrostation_test database, cleaned via DROP SCHEMA public CASCADE
- Test fakes live in tests/fakes/, one file per repository

# Workflow
- Create Feature Branch from main
- Run Typecheck and lint before committing
- NEVER commit directly to main
- PR titles follow conventional commits format

# Do Nots
- Never use 'any' type - use unknown and narrow
- Never skip error handling