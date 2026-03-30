# Backend

FastAPI backend for the initial smart-class attendance vertical slice.

## Features

- `POST /api/auth/login`
- health endpoint
- student device registration/list/delete
- attendance eligibility endpoint backed by PresenceService
- Postgres persistence

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

## Dev login

- student example: `20201234` / `devpass123`
- professor example: `PRF001` / `devpass123`
- admin example: `ADM001` / `devpass123`
