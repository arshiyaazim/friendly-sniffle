# Fazle Core

Fazle Core is a WhatsApp-oriented operations backend with employee, attendance,
payroll, escort-program, recruitment, and AI-assisted conversation workflows.
It is a Python application built with FastAPI, asyncpg, and PostgreSQL. Optional
integrations include WhatsApp bridge services, Redis, Qdrant, and external or
local AI providers.

## Requirements

- Python 3.11 or newer (`python:3.11-slim` in the `Dockerfile`)
- PostgreSQL — required; the application reads `DATABASE_URL` at startup
- Optional, only for the features a deployment actually uses: Redis, Qdrant,
  a WhatsApp bridge, a Tesseract/ffmpeg toolchain for media processing, and an
  AI provider (local Ollama or a hosted API)

## Local setup

The commands below are POSIX. The Windows/PowerShell equivalents are noted at
the end of this section.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

`requirements.txt` is sufficient to run the application. It includes the
scheduler dependency (`apscheduler`, imported while `app/main.py` is imported),
the Redis client, and the media/document libraries. The only value you must
supply yourself is the one marked required in [`app/config.py`](app/config.py)
— at minimum `DATABASE_URL`.

Configuration: copy `.env.example` to `.env` and fill in what you need. Every
field in `app/config.py`, plus every environment variable that a module reads
directly, is documented there. The advanced entries are commented out and show
the default the code already applies, so an unmodified copy changes nothing
(and an uncommented empty value such as `KEY=` does override a default with an
empty string — uncomment only what you intend to set). Keep real credentials in
an untracked `.env` or a secrets manager; never put production values in source
control.

Review pending migrations, then apply them to a new, empty PostgreSQL database:

```bash
python db/migrate.py --dry-run                              # inventory only — no database needed
DATABASE_URL=postgresql://… python db/migrate.py --dry-run  # what is actually pending
DATABASE_URL=postgresql://… python db/migrate.py            # apply
```

The dry run never applies SQL and does not validate it. With `DATABASE_URL` set
it is read-only as well: it compares against `schema_migrations` without
creating that table just to look. Without `DATABASE_URL` it prints the local
inventory and says plainly that "pending" can only be answered by a database.
Always rehearse a clean install against a disposable database before using a
production one.

Start the API locally:

```bash
python run.py                     # honours APP_PORT / DEBUG / LOG_LEVEL from settings
# or
python -m uvicorn app.main:app --host 127.0.0.1 --port 8200
```

`run.py` binds loopback, which is what you want locally or behind a reverse
proxy. The container image binds `0.0.0.0:8200` (see `Dockerfile`).

**Windows / PowerShell:** use `py -3.11 -m venv .venv` and
`.\.venv\Scripts\Activate.ps1` for the first two lines; every other command is
identical.

### Docker and the VPS installer

```bash
docker build -t fazle-core .
docker run --env-file .env -p 8200:8200 fazle-core
```

`install.sh` is the VPS path: it creates a venv, installs `requirements.txt`,
and registers a systemd unit. It assumes the checkout sits at a fixed path and
that a `fazle-core.service` unit file exists next to it — verify both before
running it. For a real deployment, follow
[`MASTER_PRODUCTION_PLAN.md`](MASTER_PRODUCTION_PLAN.md) instead of the script
alone.

## Tests

Test tooling lives in a separate file, and the suite imports the application, so
install both:

```bash
python -m pip install -r requirements.txt -r tests/requirements-test.txt
python -m pytest tests/unit/ -m unit
```

`make install` does the same, and the Makefile wraps the common runs
(`make test-unit`, `make test-integration`, `make test-resilience`,
`make test-docker`). Two pytest configs exist by design: the root `pytest.ini`
covers root-level invocations and restricts collection to the non-e2e test
directories, while `tests/pytest.ini` applies when you pass `tests/`-prefixed
paths. Both set `asyncio_mode = auto`; `tests/conftest.py` carries the
collection guard that keeps e2e tests out of ordinary runs.

Integration, database, and end-to-end tests require real services — PostgreSQL,
Redis, and the mock bridges. See `tests/conftest.py` and
`docker-compose.test.yml` before running them.

## Configuration and deployment

Application settings are defined in [`app/config.py`](app/config.py). Use
environment variables or a deployment secrets manager for credentials, provider
API keys, bridge addresses, and contact identifiers. The app should remain in a
non-sending mode until bridge configuration, authorization, and message-routing
tests have passed.

For the staged clean-install, migration, bridge, AI-provider, verification, and
rollback checklist, see [`MASTER_PRODUCTION_PLAN.md`](MASTER_PRODUCTION_PLAN.md).
