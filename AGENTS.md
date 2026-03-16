# Repository Guidelines

## Project Structure & Module Organization
- `backend/`: FastAPI service.
- `backend/app/api/`: REST routes (`health.py`, `clusters.py`, `tasks.py`).
- `backend/app/core/`: config, logging, remote execution utilities.
- `backend/app/drivers/`: database HA driver abstractions and Oracle implementation.
- `backend/app/models/`: Pydantic request/response schemas.
- `frontend/src/`: React + TypeScript UI (`pages/`, `components/`, `api/`, `store/`, `hooks/`, `utils/`).
- Root-level orchestration: `docker-compose.yml`, docs (`README.md`, `DOCS.md`, `TROUBLESHOOTING.md`).

## Build, Test, and Development Commands
- Backend setup: `cd backend && python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Run backend locally: `cd backend && uvicorn app.main:app --reload --port 8000`
- Run backend tests: `cd backend && pytest`
- Frontend setup: `cd frontend && npm install`
- Run frontend locally: `cd frontend && npm run dev`
- Frontend production build: `cd frontend && npm run build`
- Frontend lint: `cd frontend && npm run lint`
- Full stack with containers: `docker-compose up -d` (stop with `docker-compose down`).

## Coding Style & Naming Conventions
- Python: 4-space indentation, type hints for public interfaces, snake_case for modules/functions, PascalCase for classes.
- TypeScript/React: follow existing style (2-space indentation, single quotes, no semicolons).
- Component files use PascalCase (example: `ClusterCard.tsx`); hooks use `useXxx` (example: `useClusterStatus.ts`).
- Use alias imports `@/` for frontend source paths (configured in `frontend/tsconfig.json`).

## Testing Guidelines
- Current snapshot has no committed test suite. Add tests with each feature/fix.
- Backend: use `pytest` (+ `pytest-asyncio` for async APIs), place tests under `backend/tests/`, name files `test_*.py`.
- Frontend: add component/page tests under `frontend/src/**/__tests__/` or `*.test.tsx`.
- Minimum expectation for PRs: cover changed logic and at least one failure path.

## Commit & Pull Request Guidelines
- `.git` metadata is not present in this workspace snapshot; history-based conventions cannot be verified locally.
- Use Conventional Commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`.
- PRs should include: purpose summary, key changes, test evidence (commands run), and related issue/task ID.
- For UI/API changes, include screenshots or example request/response payloads.

## Security & Configuration Tips
- Never commit secrets or real host credentials; `config.yaml`, `.env`, logs, and DB files are ignored for this reason.
- Keep production CORS and SSH settings restrictive; defaults are development-friendly.
