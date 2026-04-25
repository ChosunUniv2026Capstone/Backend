# Backend

FastAPI backend for the smart-class LMS and attendance/exam local MVP.

## Features

- auth/session endpoints: login, refresh, bootstrap, me, logout
- student/professor course list endpoints
- notice list/detail/create endpoints
- admin user/classroom/classroom-network endpoints
- student device registration/list/delete
- attendance eligibility endpoint backed by PresenceService
- professor attendance timeline/report/student stats/session/roster/history endpoints
- student active attendance session, check-in, and semester matrix endpoints
- objective exam workflow endpoints for professor authoring/publish/close and student start/answer/submit
- Postgres persistence through SQLAlchemy models

## Default Port

- `8000`

## Environment

- `SERVICE_PORT`
- `DATABASE_URL`
- `PRESENCE_SERVICE_URL`

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Run tests

Run from the `Backend` directory with the app package on `PYTHONPATH`:

```bash
PYTHONPATH=. pytest -q
```

## Dev login

- student example: `20201234` / `devpass123`
- professor example: `PRF001` / `devpass123`
- admin example: `ADM001` / `devpass123`
