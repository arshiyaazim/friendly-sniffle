# Fazle Core

Fazle Core is a WhatsApp-oriented operations backend with employee, attendance,
payroll, escort-program, recruitment, and AI-assisted conversation workflows.
It is a Python application built with FastAPI, asyncpg, and PostgreSQL. Optional
integrations include WhatsApp bridge services, Redis, Qdrant, and external or
local AI providers.

## Requirements

- Python 3.11 or newer
- PostgreSQL
- Any optional services enabled by the deployment, such as Redis, Qdrant, a
  WhatsApp bridge, or an AI provider

## Local setup

From the repository root in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Set the remaining environment variables for the services and features you
intend to use. Keep credentials in an untracked environment file or a secrets
manager; never put production values in source control.
For a starting template, copy `.env.example` to `.env` and replace its
placeholder values before running the application.

Review pending migrations, then apply them to a new, empty PostgreSQL database:

```powershell
python db/migrate.py --dry-run
python db/migrate.py
```

The dry run lists pending migration files; it does not validate their SQL.
Always rehearse a clean install against a disposable database before using a
production database.

Start the API locally:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8200
```

## Tests

```powershell
python -m pip install -r tests/requirements-test.txt
python -m pytest tests/unit/ -m unit
```

Integration, database, and end-to-end tests may require additional services;
consult the test configuration before running them.

## Configuration and deployment

Application settings are defined in [`app/config.py`](app/config.py). Use
environment variables or a deployment secrets manager for credentials,
provider API keys, bridge addresses, and contact identifiers. The app should
remain in a non-sending mode until bridge configuration, authorization, and
message-routing tests have passed.

For the staged clean-install, migration, bridge, AI-provider, verification, and
rollback checklist, see [`MASTER_PRODUCTION_PLAN.md`](MASTER_PRODUCTION_PLAN.md).
