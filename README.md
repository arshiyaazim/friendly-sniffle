# Fazle Core

Fazle Core is a WhatsApp-oriented operations backend with employee, attendance,
payroll, escort-program, recruitment, and AI-assisted conversation workflows.

It is a Python application built with FastAPI, asyncpg, and PostgreSQL. Admin
work is done from a set of single-file browser SPAs served by the same process;
workflow decisions happen in the inbound message pipeline. Optional integrations
include WhatsApp bridge services, Redis, Qdrant, and external or local AI
providers.

| | |
|---|---|
| Runtime | Python 3.11+, FastAPI + uvicorn |
| Storage | PostgreSQL (all business state), Redis (cooldowns/queues, optional) |
| Inbound | WhatsApp bridges (bridge1/2/3) + Meta webhooks |
| Outbound | `modules.outbound` queue → bridge → WhatsApp |
| UI | 12 served single-page apps + 2 design previews (no build step), Bengali + English |
| Modules | 82 domain packages under `modules/` |
| Migrations | 92 ordered SQL files under `db/migrations/`, plus two prerequisite schema sets (see [Local setup](#local-setup)) |

---

## Contents

- [Requirements](#requirements) · [Local setup](#local-setup) · [Docker, the VPS installer and the restart gate](#docker-the-vps-installer-and-the-restart-gate)
- [Installing alongside an existing app, bridge and database](#installing-alongside-an-existing-app-bridge-and-database) · [First login](#first-login-bootstrap-credentials) · [Tests](#tests)
- [Folder structure](#folder-structure) · [Ports and services](#ports-and-services)
- [Pages and URLs](#pages-and-urls) — every screen, its tabs, tables, buttons, forms and modals
- [API surface](#api-surface)
- [Inbound message pipeline](#inbound-message-pipeline) — identity → intent → routing → safety gates
- [Outbound messages, bridges and AI provenance](#outbound-messages-bridges-and-ai-provenance) — what each stored row can prove
- [Connecting a WhatsApp bridge (WhatsApp MCP) and the first import](#connecting-a-whatsapp-bridge-whatsapp-mcp-and-the-first-import)
- [AI providers: local and hosted](#ai-providers-local-and-hosted)
- [Identity brain and identity-based domains](#identity-brain-and-identity-based-domains)
- [Payroll and accounts](#payroll-and-accounts)
- [Escort duty roster and dispatch](#escort-duty-roster-and-dispatch)
- [Recruitment funnel](#recruitment-funnel)
- [Scheduled jobs](#scheduled-jobs) · [Reports](#reports)
- [Operational readiness audit](#operational-readiness-audit-2026-11-06) — git, systemd, permissions, network, database, migration, API, RBAC, jobs, WhatsApp, resources, recovery, test suite
- [Configuration and deployment](#configuration-and-deployment)

---

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

### Building the schema: three sets, one order

`db/migrations/` is **not** sufficient on its own for a virgin database — it
asserts tables that live in two older, standalone sets:

| Order | Set | Creates / why it has to come first |
|---|---|---|
| 0 | `db/migrations/000_virgin_baseline.sql` | The 29 tables that predate the tracked history (members, contacts, drafts, knowledge base, admins/roles, heartbeats…), plus `wbom_relation_types` and `fazle_outbound_queue`. Applied first and **only** while the database is virgin; on an existing database it is recorded as already satisfied. |
| 1 | `modules/fazle_payroll_engine/migrations/*.sql` (14 files) | `fpe_employees`, `fpe_cash_transactions`, `fpe_income_transactions`, `fpe_employee_ledger`. `db/migrations/030` indexes `fpe_employees` and `046` references it — without these the run stops at file 30. |
| 2 | `migrations/escort_history_schema.sql`, `migrations/escort_roster_schema.sql` | `escort_order_groups`, `escort_order_lighters`, `escort_roster_entries`. `db/migrations/024` adds a foreign key onto `escort_order_groups`. |
| 3 | `db/migrations/*.sql` (the remaining 92 files) | Everything the application itself reads and writes. |

`scripts/apply_schema.py` applies that order — 109 files in total — and records every file in the same
`schema_migrations` table `db/migrate.py` uses, so the two runners never
disagree about what is applied. The sets are not cleanly layered, so the builder
works in dependency rounds — a file whose target table has not been created yet is
deferred and retried instead of aborting the whole run:

```bash
DATABASE_URL=postgresql://… python scripts/apply_schema.py --dry-run   # the plan
DATABASE_URL=postgresql://… python scripts/apply_schema.py             # apply
DATABASE_URL=postgresql://… python scripts/apply_schema.py --only db   # db/migrations only
DATABASE_URL=postgresql://… python scripts/apply_schema.py --only baseline  # step 0 alone
```

Sets 1–2 contain **data statements** (salary normalisation, backfills), so the
script applies them only while the database is still virgin — that is, when
`fpe_employees` does not exist. On a database that already has the payroll
schema they are skipped and only set 3 runs; `--prereqs always|never` overrides
that decision explicitly. Each file runs in its own transaction and is recorded
only after it succeeds, so a failure is resumable: fix the cause, re-run, and it
continues from the file that failed.

### Building a virgin database (roadmap item 1)

A database that has never seen this application can now be built from the
repository alone. Verified on 2026-11-06 against a real PostgreSQL 16 by
running the builder on an empty cluster (`DROP DATABASE` → `CREATE DATABASE` →
`apply_schema.py`): **109 planned files, 109 recorded, exit code 0.**

The piece that was missing was not a migration but the *starting point*. The
tracked history begins mid-story: it alters tables it never creates. That state
now lives in `db/migrations/000_virgin_baseline.sql`, a plain `CREATE TABLE IF
NOT EXISTS` document describing the 29 objects the first 29 migrations assume
(`wbom_candidates`, `wbom_candidate_conversations`, `wbom_contacts`,
`fazle_knowledge_base` with `key`/`value`, `fazle_payment_drafts.employee_phone`,
`fazle_admins`/`fazle_roles`/`fazle_admin_roles`, `fazle_service_heartbeats`,
`fazle_inbound_messages`, …). Its header states the provenance and the rule for
editing it. Two more objects had no `CREATE` anywhere in the repository and are
included for the same reason: `wbom_relation_types` (only ever inserted into by
`031`/`090`, read by the contact-relation and identity-brain code) and
`fazle_outbound_queue` (produced by `029`, assumed by `018`/`088`).

Outcome on a fresh database:

| Outcome | Files | Meaning |
|---|---|---|
| applied | 105 | SQL ran and was recorded |
| assumed | `003c_schema_align.sql` | its `RENAME COLUMN employee_phone → employee_mobile` target no longer exists on a fresh schema (`002` creates it with the new name). Recorded, with an explicit note, instead of failing a build that is otherwise complete |
| no-op | `086_k32_recruitment_reconciliation.sql` | reconciles 66 legacy FAQ rows and 9 governance rows that only exist in production; raises a precondition error when absent → recorded as a no-op |
| partial | `023_c0d_003_fpe_missing_indexes.sql`, `029_bootstrap_core_tables_schema_record.sql` | everything ran except the `pg_trgm` statements (see below) |

**The one real dependency: `pg_trgm`.** Two files create `gin_trgm_ops` indexes.
The sandbox used for this verification ran PostgreSQL 16.2 from `pgserver`,
which ships **without contrib**, so those indexes were skipped, the file was
recorded as `partial`, and the run printed:

```
schema build complete — every file is recorded, but 2 file(s) ran partially:
  partial 023_c0d_003_fpe_missing_indexes.sql: [0A000] extension "pg_trgm" is not available
  partial 029_bootstrap_core_tables_schema_record.sql: [0A000] extension "pg_trgm" is not available
  Install the missing extension (on Debian/Ubuntu: postgresql-contrib) and re-run this script on a fresh database for full parity.
```

On any normal host (`apt install postgresql-contrib`, or the `postgres:16`
Docker image) `CREATE EXTENSION pg_trgm` succeeds and both files apply fully —
so the correct statement is: **a virgin build is complete on a PostgreSQL with
contrib, and complete-except-two-trigram-indexes without it.** Nothing in the
application's read/write paths needs those indexes; they make fuzzy name search
fast.

How the builder reaches that result, and what it refuses to hide:

* **`_apply_statements`** — for a file that fails with a *capability* error
  (`0A000` feature not supported, `42704` undefined object, `42883` undefined
  function) the script retries statement by statement, records what succeeded,
  and reports the file as `partial` with the error text. A real syntax or
  constraint error is never salvaged this way: it fails the file.
* **`HISTORICAL_ASSUMPTIONS`** — files that describe a database state which no
  longer exists (`003c`), recorded with the reason rather than silently skipped.
* **`VIRGIN_NOOP_FILES`** — files whose precondition is production data (`086`).
* End-of-run report lists every non-`applied` outcome and the `postgresql-contrib`
  hint. Exit codes: `0` complete, `1` SQL error, `3` incomplete schema.

What this does **not** claim: the baseline was reconstructed from the schema
recorded in `029` plus what the tests and the application actually read — not
from a `pg_dump` of production. A production dump remains the gold standard for
a new deployment (see the two options below), because it carries the rows, the
owners and any hand-made object that never became a migration. Use the baseline
when you need a *working, empty, structurally complete* database — a dev box, CI,
a staging rehearsal, or a restore-drill target (`make restore-drill`).

Two honest ways to populate a *new production* database, in order of preference:

1. **Restore the dump** and let the builder apply whatever is still pending:
   ```bash
   pg_dump --no-owner --no-privileges "$PROD_URL" | psql "$NEW_URL"
   DATABASE_URL="$NEW_URL" python scripts/apply_schema.py    # usually only 091…093
   ```
2. **Build from the repository** (`apply_schema.py` on the empty database) for a
   dev/CI environment, then import only the data you mean to import.

`db/migrate.py` remains the runner for the core set alone (`--dry-run` works
without a database). Its dry run never applies SQL and does not validate it;
with `DATABASE_URL` set it is read-only, comparing against `schema_migrations`
without creating that table just to look. Always rehearse a clean install
against a disposable database before using a production one.

`db/migrate.py` remains the runner for the core set alone (`--dry-run` works
without a database). Its dry run never applies SQL and does not validate it;
with `DATABASE_URL` set it is read-only, comparing against `schema_migrations`
without creating that table just to look. Always rehearse a clean install
against a disposable database before using a production one.

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

## Docker, the VPS installer and the restart gate

```bash
docker build -t fazle-core .
docker run --env-file .env -p 8200:8200 fazle-core
```

`install.sh` is the VPS path. It runs six steps and stops at the first real
problem instead of leaving a half-configured host:

1. venv + `requirements.txt`;
2. `.env` — created from `.env.example` on first run with a generated
   `INTERNAL_API_KEY` and `BOOTSTRAP_ADMIN_PASSWORD` (an existing `.env` is
   never modified);
3. preflight — `DATABASE_URL` present and reachable, `pg_trgm` available, and
   `APP_PORT` still free (so it cannot steal a port from an app that is already
   running);
4. schema — a `pg_dump` rollback point goes to `backups/` first, then
   `scripts/apply_schema.py` applies the pending files (see
   [Operational readiness audit](#operational-readiness-audit-2026-11-06));
5. unit — `fazle-core.service` is rendered for wherever the checkout actually
   lives (`@APP_DIR@` / `@RUN_USER@` are substituted, never shipped raw),
   installed and started;
6. verification — the restart gate must pass before the script reports success.

```bash
bash install.sh --preflight           # checks only (may create .env on a first run)
bash install.sh                       # full install
# APP_DIR / RUN_USER / PYTHON_BIN / APP_PORT / DATABASE_URL / UNIT_NAME may be
# overridden; SKIP_DB_CHECKS=1 skips step 3's database probe.
```

The unit writes uvicorn's stderr to `logs/fazle-core.error.log` and reads
`<repo>/.env` (plus the app's own `.env` loading). The image and `install.sh`
both leave the schema to `db/migrate.py`: the app applies no migration at
startup, so schema changes never happen behind an operator's back. (Startup only
creates its own small runtime tables — `fazle_runtime_settings`, chat groups —
with `CREATE TABLE IF NOT EXISTS`, which is why a missing migration surfaces as a
query error rather than a silent half-upgrade.)

```bash
docker build -t fazle-core .
docker run --rm --env-file .env fazle-core python db/migrate.py   # once
docker run --env-file .env -p 8200:8200 fazle-core                # then the app
```

For a real deployment, follow
[`MASTER_PRODUCTION_PLAN.md`](MASTER_PRODUCTION_PLAN.md) rather than the script
alone.

### Restart gate

`systemctl restart` returning is not proof of a working deploy. The gate checks
the process state, the restart counter (crash-loop detection), the stderr log
for a completed startup or an unhandled traceback, and `/health`:

```bash
BEFORE=$(systemctl show -p NRestarts --value fazle-core)
sudo systemctl restart fazle-core
python -m scripts.restart_gate_readiness --restarts-before "$BEFORE"
```

Exit code 0 means ready. Crucially, a 503 whose only failing probes are `cpu`,
`disk`, or `mem` counts as **ready** — those are host-level (`ambient`) signals,
reported but never treated as a deploy failure. Failing core probes (`db`,
bridges, `outbound`, `scheduler`) do fail the gate, after a bounded settle
window so a transient blip during startup is not fatal either.

## Installing alongside an existing app, bridge and database

This deployment is additive. Nothing here replaces the WhatsApp bridge, the
application already connected to it, its database or its public URL — this app is
a **second process**, a **second database on the same PostgreSQL server**, and a
**second client of the same bridge**.

### The bridge is shared, not taken over

Do **not** stop `mcp-whatsapp-bridge` and do not re-pair the two numbers for this
install. The app only talks to the bridge over HTTP (`BRIDGE*_URL`) and, when you
opt into history import, reads the bridge's SQLite store **read-only**
(`file:…?mode=ro`, see `modules/bridge_poller/__init__.py`). It never writes to
the store, never logs a device out, and never sends a pairing request. There is
no pairing screen in this app at all: pairing, unpairing and session health stay
the bridge's job. What this app does notice is a bridge that reports a
non-sending ("pairing") state — `modules/outbound._pairing_held` then refuses to
enqueue outbound for that bridge instead of talking into a dead session.

The other app keeps running throughout: separate unit (`fazle-core`), separate
port, separate database, separate logs.

### Ports

Give this install its own port; the bridge and the existing app keep theirs.

| Service | Default here | Where it is set |
|---|---|---|
| This app | `127.0.0.1:8200` | `APP_PORT` (`run.py`); container binds `0.0.0.0:8200` |
| Existing app | unchanged | its own config |
| WhatsApp bridge | `BRIDGE1_URL` / `BRIDGE2_URL` / `BRIDGE3_URL` (defaults `:8082`, `:8081`, `:8083`) | `.env` — point at the **running** bridge |
| Ollama (optional) | `http://localhost:11434` | `OLLAMA_URL` |
| PostgreSQL | `5432` (shared cluster) | inside `DATABASE_URL` |
| Redis | not required at startup | `REDIS_URL` (test stack only today) |

`bash install.sh --preflight` fails with the owning process if `APP_PORT` is
already taken, so a re-install cannot silently fight the other app for a port.
If `8200` is busy, set e.g. `APP_PORT=8210` in `.env`. Users never reach that
port directly: the reverse proxy that already serves the existing app's domain
gets one more `location`/server block pointing at `127.0.0.1:$APP_PORT`, on a
different hostname or path — that is the "URL" this install owns.

### Database

Use a **new database** on the existing PostgreSQL server; do not point this app
at the other app's database.

```sql
CREATE ROLE fazle LOGIN PASSWORD 'choose-a-real-password';
CREATE DATABASE fazle_core OWNER fazle;
\c fazle_core
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- needs superuser on some clusters
```

```bash
# .env
DATABASE_URL=postgresql://fazle:choose-a-real-password@127.0.0.1:5432/fazle_core
```

Two situations, two answers:

- **The database already exists** (a previous deployment of this app, a clone, or
  a restore): point `DATABASE_URL` at it. `scripts/apply_schema.py` — run by
  `install.sh` — sees `fpe_employees` and applies only the genuinely pending
  `db/migrations` files, almost always just `091`…`093`.
- **A brand-new database**: the builder creates the pre-history tables from
  `000_virgin_baseline.sql` first, then applies the rest — **109 files planned,
  109 recorded, exit 0** on PostgreSQL 16 with `pg_trgm` available (without
  contrib: exit 0 with two files `partial`, which only skips `gin_trgm_ops`
  indexes). See [Building a virgin database](#building-a-virgin-database-roadmap-item-1)
  for what full parity requires and where a `pg_dump` restore is still better.

Three migrations deserve a look before the first run on a shared server:

- `003b` and `042` contain `DROP TABLE IF EXISTS` on **this app's own** tables
  (fresh database ⇒ no-op);
- `029` runs `CREATE EXTENSION pg_trgm` and adds constraints inside guarded
  blocks — if `pg_trgm` cannot be created, run the `CREATE EXTENSION` above as a
  superuser first;
- the prerequisite sets are only applied to a virgin database because they
  contain data statements (`--prereqs never` skips them outright).

### Foreign keys and dependencies

There are **no cross-database foreign keys and no shared tables** with the other
app or the bridge: every `REFERENCES` in `db/migrations` points at a table in the
same database, created by these migrations. The only external objects are the
`pg_trgm` extension (optional index acceleration) and the triggers created by
`023`/`029` inside this database. Nothing in this app expects to own the bridge's
SQLite files or the other app's tables.

At startup the app requires only PostgreSQL. The bridge, Ollama and the hosted AI
providers are contacted lazily; a bridge that is down at boot degrades `/health`
for bridges but does not block the API.

### Go-live checklist

```bash
# 1. schema + first-boot credential (creates .env if missing, applies migrations)
bash install.sh --preflight           # env / database / pg_trgm / port free?
bash install.sh                       # install, migrate, start, verify

# 2. log in at http://127.0.0.1:8200/ with the username/password form,
#    then change the bootstrap password when the dashboard asks for it.

# 3. decide what this instance owns (both are separate switches)
#    SCHEDULER_ENABLED  — periodic jobs; set false if the other app runs yours
#    OUTBOUND_ENABLED   — sending through the bridge; false until tested

# 4. point the bridge settings at the running bridge, then restart
sudo systemctl restart fazle-core
```

## First login (bootstrap credentials)

A fresh database has no accounts, so the app creates one on the first start:

| Variable | Default | Meaning |
|---|---|---|
| `BOOTSTRAP_ADMIN_USERNAME` | `admin` | Username for the first account |
| `BOOTSTRAP_ADMIN_PASSWORD` | *(empty)* | Password; empty ⇒ one is generated and printed **once** in the startup log |
| `BOOTSTRAP_ADMIN_PHONE` | `8800000000000` | Phone for the owner row when `ADMIN_NUMBERS` is empty |
| `BOOTSTRAP_ADMIN_CREDENTIALS` | `true` | `false` disables the whole step |

Migration `093` seeds the RBAC role catalogue (`viewer` 10, `office_assistant` 20,
`operator` 30, `accountant` 50, `admin` 70, `superadmin` 100), and the bootstrap
grants `superadmin` to the account it creates — otherwise a fresh install could
log in but not administer anything. Existing role levels are never rewritten.

The rule is *create once, never overwrite*: if **any** admin already has a
username and password, `rbac.ensure_bootstrap_credentials()` returns
`already_configured` and changes nothing — a restart cannot reset a password an
owner has already chosen. Once the owner has changed it, the value still sitting
in `.env` is inert; delete the line or leave it, either way it is not a backdoor. The bootstrap row is also marked
`must_change_password = true`, so the default is a one-time door:

```bash
# what the dashboard does behind its username/password form
curl -sX POST http://127.0.0.1:8200/api/admin/login \
  -H 'Content-Type: application/json' \
  -d '{"mode":"password","username":"admin","password":"<from .env or the log>"}'
# → {"credential":{"type":"token","value":"fk_…"},"must_change_password":true, …}

curl -sX POST http://127.0.0.1:8200/api/admin/change-password \
  -H "X-Internal-Key: fk_…" -H 'Content-Type: application/json' \
  -d '{"current_password":"<old>","new_password":"<new, min 8 chars>"}'
# → {"ok":true,"must_change_password":false}
```

The dashboard (`/`) shows the username/password form first and switches to a
"change this password" pane while `must_change_password` is true. The flag is
advisory, honestly stated: it is reported by the login response and the UI nags
until it is cleared, but no other endpoint is blocked by it — an operator who
automates against `INTERNAL_API_KEY` is never locked out by a pending password
change. Treat "default password still active" as an operational task, not as an
enforced policy. Both
migrations and the bootstrap are additive: an installation that already has
accounts keeps working untouched (migration `092` only adds columns and a
case-insensitive unique index on `username`).

### What the login does not do

There is **no second factor** in this codebase — no TOTP, no SMS challenge. The
password gate is one factor, plus optional 8-failure/15-minute throttling per
username+IP (`POST /api/admin/login` answers `429` with `Retry-After`). On a
public host, put the dashboard behind the reverse proxy with TLS and, if it must
be internet-facing, an IP allowlist or basic auth; the app's own loopback bind
(`127.0.0.1:8200`) already assumes a proxy in front of it.

### `INTERNAL_API_KEY` is not a login

`INTERNAL_API_KEY` grants full owner access to every route and is meant for
server-to-server calls, not for people. The app refuses to be quiet about it: an
empty or obviously weak value produces a `[security]` warning at startup, and
`POST /api/admin/change-password` rejects it outright because no account stands
behind it. Generate it with `openssl rand -hex 32` (the installer does this for
you), keep it in `.env` (mode `600`), and hand operators usernames/passwords
instead. Rotating it logs nobody out — it just stops being valid itself.

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

---

## Folder structure

```
friendly-sniffle/
├── app/
│   ├── main.py                  FastAPI app: 145 routes, lifespan, mounts every router
│   ├── config.py                Settings (163 fields) — the single source of configuration
│   ├── database.py              asyncpg pool: fetch_all / fetch_val / fetch_one / execute
│   ├── llm.py, ollama.py        reply generation + provider fallback chain
│   ├── *_provider.py            groq / minimax / nine-router / omniroute / openai-compatible
│   ├── unified_routing.py       workload → provider routing
│   ├── bridge.py, outbound client, logging_setup.py, error_log.py
│   └── static/                  12 served SPAs (+2 previews), no bundler, no node_modules
├── modules/                     82 domain packages — one package per feature area
├── shared/                      cross-cutting helpers: auth, drafts, phone, events, queue arbiter,
│                                sql_split.py (statement splitter used by apply_schema.py and conftest)
├── db/
│   ├── migrate.py               migration runner (see Local setup)
│   └── migrations/              000_virgin_baseline.sql (pre-history state) + 92 tracked migrations
├── migrations/                  standalone schema sets (escort history/roster) — prerequisites, see apply_schema.py
├── scripts/                     apply_schema.py, rbac_write_audit.py, verify_isolation.py,
│                                restore_drill.py, bridge_smoke.py, restart_gate_readiness.py, secret_scan.sh
├── tools/                       one-off repair CLIs (e.g. repair_fpe_transactions.py)
├── resources/                   CSV templates / seed material
├── tests/                       unit · integration · db · workflows · resilience · e2e · load
├── run.py · Dockerfile · fazle-core.service · install.sh · Makefile
```

`modules/` grouped by concern (package names are literal):

| Group | Packages |
|---|---|
| Identity & contacts | `identity_brain`, `number_identity`, `phone_normalizer`, `contact_relations`, `contact_roles`, `contact_sync`, `owner_contact_registry`, `user_role` *(deprecated)* |
| Conversation & AI | `message_router`, `intent`, `role_classifier`, `conversation_policy`, `conversation_memory`, `conversation_canonical`, `conversation_layer`, `reviewed_reply_memory`, `memory_extractor`, `reply_templates`, `route_b`, `route_b_handoff`, `message_archive`, `ollama_memory`, `rag` |
| Payroll & money | `fazle_payroll_engine`, `payroll`, `payroll_logic`, `payment`, `payment_ingest`, `payment_workflow`, `payment_relay`, `payment_relay_accounting`, `payment_correction`, `drafts`, `draft_approval`, `draft_quality`, `accountant_summary`, `billing`, `client_billing` |
| HR | `admin_employees`, `admin_transactions`, `attendance`, `attendance_parser`, `employee_conversation`, `employee_verification` |
| Escort & dispatch | `escort`, `escort_lifecycle`, `escort_roster`, `escort_slip_extractor`, `dispatch` |
| Recruitment | `recruitment_flow`, `recruitment_ai`, `recruitment_assistant`, `recruitment_detector`, `recruitment_turn_decision`, `recruitment_recovery_sweep` |
| Ops & platform | `scheduler`, `backup`, `health_monitor`, `operations_health`, `observability`, `reports`, `preflight_guard`, `rbac`, `outbound`, `bridge_poller`, `history_backfill`, `ai_provenance.py`, `kb_upload`, `knowledge_base`, `ai_console`, `ai_readonly_tools`, `business_read_service`, `admin_chat_lab`, `admin_commands`, `admin_directives`, `hermes_kernel`, `hermes_tasks`, `social_auto_reply`, `media_normalization`, `ocr_processor`, `voice_processor`, `image_hash`, `location_adapter`, `wa_chat_frontend` |

## Ports and services

| Port | Service | Notes |
|---|---|---|
| **8200** | Fazle Core itself | `APP_PORT`; the URL prefix in every example below |
| 8082 | bridge1 | `BRIDGE1_URL` default `http://localhost:8082` |
| 8081 | bridge2 | `BRIDGE2_URL` default `http://localhost:8081` |
| 8083 | bridge3 | `BRIDGE3_URL` default `http://localhost:8083` |
| 11434 | Ollama | `OLLAMA_URL` default `http://localhost:11434` |
| 6379 | Redis (optional) | `REDIS_URL` default `redis://localhost:6379/9` |
| 5432 | PostgreSQL | app DB; tests default to **5433** (`TEST_DATABASE_URL`) |

Every UI URL below is relative to `http://127.0.0.1:8200`.

---

## Pages and URLs

All pages are static single-file SPAs served by `app/main.py` — no build step.
Each one asks for the internal API key on first load (the key is stored in the
browser) and calls the JSON API with it.

| URL | Page file | What it is |
|---|---|---|
| `/`, `/dashboard` | `dashboard.html` | Admin console — 13 tabs |
| `/dashboard/legacy` | server-rendered | Legacy summary page with live counts |
| `/payroll`, `/payroll/<tab>` | `payroll.html` | HR & Admin (FPE) — 15 tabs |
| `/escort-roster`, `/escort-roster.html` | `escort-roster.html` | Escort duty roster — 8 sections |
| `/dispatch` | `dispatch.html` | Availability, assignment, release slips |
| `/wa-chat`, `/dashboard/wa-chat` | `wa_chat.html` | WhatsApp-style operator inbox |
| `/open-chat`, `/chat-lab` | `open-chat.html` | AI operating console |
| `/drafts` | `drafts.html` | Reply-draft approval queue |
| `/kb` | `kb.html` | Knowledge base |
| `/admin/knowledge/recruitment` | `recruitment-knowledge.html` | Recruitment authority text |
| `/unmatched-confirmations`, `.html` | `unmatched-confirmations.html` | Draft-confirmation review |
| `/wbom-create-link`, `/wbom-link-review` | `wbom-*.html` | One-time FPE↔WBOM linking tools |
| `/health`, `/health/deep`, `/health/c1b` | JSON | Probes (the restart gate reads `/health`) |
| `/docs`, `/openapi.json` | FastAPI | Swagger UI and schema |

### `/` — Admin dashboard (13 tabs)

| Tab | Contents |
|---|---|
| **Overview** | Stat cards (`System Overview`), **Bridges** table, **Scheduler — Next Jobs**, **Backup** status |
| **Drafts** | `Pending Reply Drafts` and `Pending Payment Drafts` lists with inline approve/cancel |
| **Reports (B17)** | Report picker + `Args JSON` box + **Run** — renders the JSON/CSV/text of any report |
| **Users (B19)** | `Admin Users` table (phone, name, role, status) + `Add User` form (Phone 8801…, Name, Role) |
| **Identity–Role** | Search verified/display names; panels: **Selected identity**, **Metadata correction** (approved vs verified name + reason), **Identifier link** (type + JID/mobile/Meta ID), **Assign role** (role + business domain), **Place hold** |
| **🤝 সম্পর্ক** (Relations) | Add/edit contact relations: phone, relation, trust, auto-reply on/off, notes; lists **শ্রেণিবিহীন কন্টাক্ট** and **সব সম্পর্ক**; search by number or relation |
| **Audit** | `Audit Log (B19)` with command filter, limit and **Apply** |
| **Backups (B18)** | Backup list, **Run Backup Now**, **Refresh List** |
| **Scheduler** | `Scheduler Jobs (B16)` — every job, last status, last run, next run |
| **RAG (B21)** | Index status, **Rebuild Index**, semantic `Search` with top-k |
| **Observability (B22)** | `HTTP Summary` counters and `Error Log (recent)` |
| **💬 Chat Lab** | Model picker, toggles for app context / DB read / internet, chat thread with **Send** / **Clear** |
| **AI & Phone Settings** | AI provider settings; `WhatsApp Identity Numbers` — save accountant number, escort client numbers |

Login gate: username + password (the first-boot account, or any admin created
with one) **or** a raw internal API key / `fk_…` token. While the account still
uses the bootstrap password the same box shows a **change password** pane.
**Logout** clears the stored credential from `localStorage`.

### `/payroll` — HR & Admin (15 tabs)

Deep links exist for most tabs (`/payroll/overview`, `/payroll/transactions`,
`/payroll/search`, `/payroll/employees`, `/payroll/addemployee`,
`/payroll/unmatched`, `/payroll/review`, `/payroll/sync`, `/payroll/manual`,
`/payroll/operator`, `/payroll/cash`, `/payroll/income`, `/payroll/admin`), plus
`/payroll/transactions/{employee_id}` for an employee-scoped view. **Office
Attendance** and **Employee Edit Requests** open from inside the SPA only.

| Tab | Contents |
|---|---|
| **Overview** | `This Month's Summary` cards, `Recent Transactions`; quick ranges **Today / Last 3 days / Last 7 days / Last 30 days / All time**, date range `ovFrom`–`ovTo`, employee search, pager (First / Prev / Go / Next / Last) |
| **Transactions** | Full transaction list with **Add Payment**, **Filter**/**Clear**, period presets, `txnFrom`–`txnTo`, method filter, jump-to-page, page size |
| **Search** | Fuzzy `Employee Search` (type-ahead), date range, pager |
| **Employees** | `Employees` table; **Add Employee**, status/sort filters, search by name/alias/phone, pager |
| **Add New Employee** | Form: mobile `01XXXXXXXXX`, full name, staff designation, initial status; **Create Employee** / **Clear** |
| **Unmatched** | `Unmatched Messages` queue, **Refresh**, "show reviewed" toggle |
| **Review** | `Pending Accounting Candidates` — **Reject**, **Mark Duplicate**, **Requeue Parser**, status/confidence filters, pager |
| **Sync** | `Sync & Processing Status` + `Module Health`; **Trigger Sync Now**, **Refresh** |
| **Manual** | `Create Manual Transaction` and `Reverse Transaction` forms (employee lookup, amount, method, date, category, note; reversal by txn id) |
| **Operator** | `Operator Submission` (same shape as manual, submitted for approval) + `Pending Submissions` |
| **Cash** | `Cash Transactions` table (Date, Employee, Amount ৳, Sender, Method, Ref, Period) with search, date range, **Export CSV**, pager; plus `WhatsApp Shorthand Staging` list |
| **Income** | `Income Transactions` table (Date, Employee, Amount ৳, Reported By, Period, Ref) and `New Income Entry` form (date, head, name, mobile, amount, ref) |
| **Office Attendance** | `Attendance` CRUD (date range, source filter, employee picker, add/edit) + `Payroll Eligibility Preview` with **Run Preview**, **Run Dry Run**, **Run Reconciliation** |
| **Employee Edit Requests** | Requests raised by employees, status filter, approve/reject |
| **Admin** | `Admin Console` (users, roles), `Payroll Salary Baseline` (**Apply For All Employees**), `Activity Log`, `Client Settings`, `Generate Bill`, `Outstanding Balance` |

### `/escort-roster` — Escort duty roster

Header actions: **🔄 Sync**, **📜 History**, **⬇ Export CSV**, **➕ Add New Program**.

| Section | Contents |
|---|---|
| **📋 Roster** | `rosterTable` — S/N, Mother Vessel, Lighter, 📞, Escort Name, Start Date, End Date, Days; pager (← Prev / Next →); **➕ Add Client** |
| **🗂️ Kanban** | Board view of programs by status |
| **📄 Slip Queue** | Pending release slips awaiting review |
| **🧮 Shift Calc** | Shift/pay calculator — **Calculate**, **Save** |
| **⚙️ Config** | `configTable` — destination, conveyance (BDT), shift rate (BDT), updated at |
| **🕒 Attendance** | Attendance rows generated from programs |
| **💰 Net Salary** | Per-program totals: Total, Food Bill, Net Payable |
| **👤 Admin** | `adminUsersTable` (name, phone, username, status, role, last seen) + user modal (create/edit, search, status, role) |
| Client list | Client phone + actions (add/edit/delete escort clients) |

### `/dispatch` — Availability & controlled assignment

Cards: **Check Availability** → **Availability Results** → **Ranked Suggestions**;
**Assign / Reassign Escort** (with override reason + checkbox);
**Existing Overlaps**; **Same-Vessel Review Queue**; **Safe Unassignment**;
**Incomplete Drafts**; **Release-Slip Review** (candidate matching); **Manual
Completion**. Everything except assign/unassign/complete is read-only.

### `/wa-chat` — WhatsApp-style operator inbox

Three-column layout with a conversation list, message thread and composer
(😊 emoji, 📎 attach, 🎤 voice, ➤ send). Top bar: **💬 Contacts**, **👥 Groups**,
**✏️ Drafts**, **⚙️ Settings ▾**, **⟳ Sync**, **Logout**.

- Modals: `contactModal` (name, phone, role, relation, relation notes,
  auto-reply, blocked, **🚫 Block Auto-Reply** / **✅ Unblock**, **🗑️ Delete
  Contact**, **💾 Save**), `broadcastModal` (platform, recipients, text,
  **📤 Send All**), `groupsModal`.
- Bulk bar appears on selection with **Send to Selected** / **📤 Broadcast**.

### `/open-chat` and `/chat-lab` — AI operating console

Tabs: **Response · Evidence · Trace · Runtime · Memory**. Composer supports
📎 file attach and 🎤 voice recording (with transcript review modal). Side
actions: **Refresh Memory**, **Clear Chat**, history list, left-panel toggle.
The Evidence/Trace tabs show which probes, tables and policy steps produced the
answer — useful when a reply looks wrong.

### `/drafts` — Reply-draft approval queue

Table columns: **ID, প্রাপক (recipient), Draft Message, Role / Intent, Status,
সময়, Action**. Filters: status select + free-text search; row count in
`statsBar`. `editModal` edits body/meta/note; actions approve or **বাতিল**
(cancel), **সংরক্ষণ করুন** (save).

### `/kb` and `/admin/knowledge/recruitment`

`/kb`: **Upload to KB** form; table **ID, Category, Key, Content Preview, Active,
Action** (edit/toggle/delete). `/admin/knowledge/recruitment`: the recruitment
authority text with **Save draft** / **Cancel**.

### `/unmatched-confirmations` — Draft-confirmation review

Review queue with detail pane and actions: link (with reason), reject (reason),
defer (reason), new-acknowledgement; progress indicator and action status.

### `/wbom-create-link` and `/wbom-link-review`

One-time cleanup tools for linking payroll-engine employees to `wbom_employees`:
create+link a missing row, or accept/reject suggested pairs
(table: FPE #, Pair (FPE name / WBOM name), Phone, WBOM #, Action).

---

## API surface

FastAPI mounts 145 routes in `app/main.py` plus the domain routers. Grouped by
prefix:

| Prefix | Purpose |
|---|---|
| `/api/fpe/*` (51 endpoints) | Payroll engine: `employees`, `transactions`, `cash`, `income`, `operator`, `unmatched`, `review`, `sync`, `normalization`, `staging-payments`, `ledger`, `stats`, `admin` |
| `/api/admin/*`, `/admin/users/*` | Admin employees/transactions/users, `login` (key or username/password), `change-password`, knowledge upload |
| `/admin/attendance/*`, `/admin/payroll/*` | Attendance CRUD, eligibility preview, dry run, reconciliation |
| `/api/escort-roster/*` | Roster summary/active/export, config, escort clients, accountant phone, sync-all |
| `/api/escort/*` | Escort program and slip review |
| `/api/dispatch/*` | Availability, conflict check, suggestions, overlaps, assign/replace/unassign/complete, release slips |
| `/api/payment-drafts/*`, `/api/drafts/*` | Draft approval queues |
| `/api/tasks/*`, `/api/actions/*` | Hermes task state and action approvals |
| `/api/social/*` | Social auto-reply backlog and sending |
| `/admin/identity-role`, `/admin/contact-roles`, `/admin/contact-relations` | Identity metadata, roles, relations |
| `/api/assistant/ops/*` | Operational reads: `ai-routes`, `business-read`, `system-secrets` |
| `/api/rag/*`, `/api/memory/*`, `/api/runtime/*`, `/api/identity/*` | Retrieval, memory, runtime truth, identity |
| `/api/queue/*`, `/api/outbound/*`, `/api/bridges/*`, `/api/whatsapp/*` | Outbound queue, bridge status, WhatsApp helpers |
| `/api/bridge/history-backfill`, `/api/bridge/history-backfill/status` | One-time bridge-store import (POST starts/resumes, GET reports progress) |
| `/webhook/meta/*`, `/webhook/mcp1`, `/webhook/mcp2` | Inbound webhooks (signature-checked) |
| `/reports`, `/reports/{name}` | Reporting layer — `?fmt=` json / csv / text, `?no_cache=true` |
| `/health`, `/health/deep`, `/health/c1b` | Liveness/readiness probes |

---

## Inbound message pipeline

Every inbound WhatsApp message — regardless of bridge or webhook — flows through
one path. Nothing replies before the identity step and no branch bypasses the
safety gates.

```
bridge_poller / Meta webhook
   → normalize phone, dedupe, cooldown, injection & flood checks
   → identity_brain.detect_identity()        who is this?
   → intent.detect_intent()                  what do they want?
   → message_router.process_message()        role-based branch (below)
   → reply generated (rules → KB → reviewed memory → AI)
   → draft gate / outbound queue → bridge → WhatsApp
```

### Identity (who)

`modules/identity_brain` resolves a sender against, in order:

1. `seed_rule` — explicit row in `fazle_contact_roles` (highest trust)
2. `db_employee` — `wbom_employees.employee_mobile` match
3. `escort_roster` — active duty in `wbom_escort_programs`
4. `env_phone` — `ESCORT_CLIENT_PHONES` explicit match
5. `db_contact` — `wbom_contacts.relation` or an "escort" name token
6. `text_hint` — keyword only, **never persisted** as identity
7. `unknown`

Roles, highest priority first: `admin` → `family`/`family_ceo` →
`accountant` → `internal_coordinator`/`internal_operation` → `vip_client` →
`security_service_client`/`client_escort_buyer`/`escort_client` → `employee`/
`escort`/`security_guard` → `supervisor` → `employee_internal`/`repeat_client`
→ `vendor` → `political_contact`/`government_contact` → `personal_business` →
`candidate` → `unknown`.

### Intent (what)

`modules/intent` is rule-first with a Bengali/English keyword layer and a fuzzy
match, falling back to the model only when the rules are inconclusive:
`recruitment`, `salary_query`, `payment_due`, `escort_duty`, `attendance`,
`complaint`, `client_order`, `leave`, `join`, `slip_submission`, `voice_note`,
`greeting`, `unknown`.

### Routing order

`message_router.process_message()` evaluates branches in this fixed order
(first match wins):

| # | Branch | Typical sender |
|---|---|---|
| 1 | Family — personal, no business workflow | `family`, `family_ceo` |
| 2 | Escort client — strict order gate; admin gets a draft, never an auto-reply | `client_escort_buyer`, `vip_client`, `repeat_client` |
| 3 | Admin commands | `admin` |
| 4 | Attendance — creates an approval draft | any role saying হাজির / Present |
| 5 | Intent classification | everyone |
| 6 | Accountant — cash shorthand, reconciliation replies | `accountant` |
| 7/8 | Recruitment — one eligibility decision per candidate | `candidate`, `unknown` |
| 9 | Escort order (non-registered senders) | `unknown` |
| 10 | Employee workflow — duty, salary, leave, documents | `employee`, `escort`, `security_guard`, `supervisor` |
| 11 | Advance / payment request | any unhandled role |
| 11.5 | **Route B** — Ollama-only reply | `escort`, `security_guard`, `candidate`, `unknown` |
| 12 | Office-location fast path (KB only, no AI) | everyone |
| 13 | Knowledge base | everyone |
| 14 | Reviewed reply memory | everyone |
| 15 | AI fallback | everyone |

### What each role gets

| Sender | Behaviour |
|---|---|
| **Employee / escort / guard** | Duty, attendance, salary, advance and leave questions answered from the DB (`payroll_logic` never fabricates an amount — missing data returns `under_review`); payment and attendance outcomes go to the admin as drafts |
| **Job applicant (candidate)** | Recruitment funnel: name → age → area → job preference → experience → phone confirmation, one question at a time; recovery sweep re-engages stalled sessions |
| **Service-interested sender** (escort order) | Treated as a potential client: quantity/detail questions answered from KB; anything that commits money or people becomes an admin draft |
| **Current client** | Strict order gate: no price/commitment without admin approval; reply drafts are queued for the owner rather than sent automatically |
| **Admin** | Commands (`APPROVE`, `PAID`, `ATTENDANCE SAVE`, report runs, holds, corrections) execute with audit |
| **Accountant** | Cash shorthand (`PAID <id> <amount> <method>`) parsed deterministically into `fpe_cash_transactions` |
| **Family** | Personal conversation only; no business workflow triggered |
| **Unknown** | Recruitment eligibility check, else Route B (Ollama) with read-only context, else KB, else AI fallback |

### Safety gates

Applied to every reply before sending: reply cooldown, keyword-flood and loop
detection, prompt-injection screening, `DRAFT_ALWAYS` (names/phones/roles that
always draft), the financial-draft gate, `AI_SAFE_MODE`, outbound queue
authorisation, and the per-bridge sending state. The system is non-sending
(`AUTO_REPLY_ENABLED=False`) until bridge, authorisation and routing tests pass.

---

## Outbound messages, bridges and AI provenance

`wbom_whatsapp_messages` is the canonical history for **both** directions. Since
2026-11-06 (migration
[`091_outbound_provenance_and_history_backfill.sql`](db/migrations/091_outbound_provenance_and_history_backfill.sql))
an outbound row can answer three questions it previously could not:

1. **Which bridge actually carried it?** — `source_bridge` is written at the
   transport boundary (`modules.outbound.sweep_once()` →
   `modules.bridge_poller.persist_outbound_message()`), i.e. *after*
   `BridgeClient.send_strict()` returned 200. A queue failover (`fallback_channel`)
   therefore records the bridge that accepted the message, not the one first
   requested; the requested bridge stays in `metadata_json.failover_from`.
2. **Did it go?** — `delivery_status` (`sent` / `dlq` / `unknown`) plus
   `external_id` and `outbound_queue_id`.
3. **Was it AI-generated, and by which model?** — `ai_generated`,
   `ai_provider`, `ai_model`, `ai_task`.

### Column contract

| Column | Meaning |
|---|---|
| `sender_number` | The sender as received. **Inbound:** the person. **Outbound:** the counterparty we wrote to (never our own bridge number). |
| `canonical_phone`, `phone_last10` | Normalised identity keys (`8801XXXXXXXXX`, last 10 digits). Filled for every new row in both directions; empty for LID/PSID senders that are not phone numbers. |
| `original_sender_number` | Untouched raw value, kept so normalisation can never lose the original. |
| `receiver_number` | **Our** bridge number (`_source_to_receiver()`), never the counterparty. |
| `source_bridge` | Bridge that carried the message (equals `platform` for new rows). |
| `direction` | `inbound` / `outbound` (transport direction — *not* a business classification). |
| `actor_type` / `actor_id` | Who produced the content: `external_user`, `automation`, `hermes`, `human_device`, … |
| `outbound_queue_id` | `fazle_outbound_queue.id`; unique partial index ⇒ one message row per queue item, even across retries. |
| `delivery_status` | Outbound transport outcome: `sent`, `dlq`, `unknown`, or NULL (row pre-dates a confirmed send). |
| `ai_generated` | **Tri-state.** `true` = an LLM wrote the body, `false` = rule/template/human, `NULL` = unknown (every pre-migration row, and history-imported outbound rows). Query with `IS TRUE`. |
| `ai_provider`, `ai_model`, `ai_task` | Which provider/model answered, and what for (`reply`, `recruitment_reply`, `chat_reply`). |
| `history_imported` | `true` = written by the importer/live capture from a bridge store, never routed. |

### Where the AI flag comes from

`app.llm._record_attempt()` records every provider attempt; on `success` it also
calls `modules.ai_provenance.record_success()`. The same hook exists at each
write point for replies that bypass `app.llm` (`app.ollama`, `app.minimax_provider`,
`app.unified_routing`'s `phase3_ai_routes`). The result is a per-task
`ContextVar`, snapshotted into `fazle_outbound_queue.meta_json` at enqueue time
(so it survives the async send in the dispatcher's own task) and copied onto the
message row at send-success.

Rules, deliberately conservative:

* `actor_type='human_device'` is **never** labelled AI, whatever the context says.
* A generation older than 15 minutes is ignored (no stale leakage onto an
  unrelated message).
* Intent classification, admin RAG answers and internal decision calls are
  excluded — they must not mark a neighbouring rule-based reply as AI-written.
* History-imported outbound rows stay `NULL`: their origin cannot be proven.

### How outbound rows get written

| Path | When | Result |
|---|---|---|
| `modules.outbound.sweep_once()` → `persist_outbound_message()` | Immediately after a bridge accepts a send | Upgrade of the queue-linked row, or a new row if the caller never pre-saved one |
| `bridge_poller._save_message()` | At enqueue time by reply cascades (legacy) | Row carrying `outbound_queue_id`; upgraded by the path above |
| `bridge_poller._poll_bridge_outbound()` | Every `BRIDGE_OUTBOUND_POLL_S` (default 10s) | Messages typed **on the phone itself** (`is_from_me = 1`), read from the bridge store |
| `modules.history_backfill.import_message()` | One-time import (below) | Pre-existing history, `history_imported = true` |

Migration 091 also backfilled legacy rows: `source_bridge` from `platform`,
`canonical_phone`/`phone_last10` for outbound rows that never had them,
`delivery_status='unknown'`, and `ai_generated = NULL` (not `false`) for
everything that existed before it.

### Useful queries

```sql
-- AI-written messages in the last 7 days, per bridge and model
SELECT source_bridge, ai_provider, ai_model, count(*)
  FROM wbom_whatsapp_messages
 WHERE direction = 'outbound' AND ai_generated IS TRUE
   AND received_at > NOW() - INTERVAL '7 days'
 GROUP BY 1,2,3 ORDER BY 4 DESC;

-- outbound volume per bridge (device-typed vs automated vs AI)
SELECT source_bridge,
       count(*) FILTER (WHERE actor_type = 'human_device') AS typed_on_phone,
       count(*) FILTER (WHERE ai_generated IS TRUE)        AS ai_written,
       count(*) FILTER (WHERE ai_generated IS FALSE)        AS rule_or_human,
       count(*) FILTER (WHERE ai_generated IS NULL)         AS unknown
  FROM wbom_whatsapp_messages
 WHERE direction = 'outbound'
 GROUP BY 1 ORDER BY 1;

-- anything that never reached a bridge
SELECT outbound_queue_id, source_bridge, delivery_status, message_body
  FROM wbom_whatsapp_messages
 WHERE direction = 'outbound' AND delivery_status = 'dlq'
 ORDER BY received_at DESC LIMIT 20;
```

### Honest limits

* `delivery_status='sent'` means **the bridge accepted the send** (`HTTP 200`),
  not that the recipient's phone displayed or read it. `external_id` is
  `"<bridge>:<recipient>"` — the bridge API returns no WhatsApp message id.
* Device capture de-duplicates on (body + counterparty + bridge). Two
  identical device-typed messages to the same person (e.g. "ok" twice) collapse
  into the first one; the app never invents a second row.
* Group messages remain outside the canonical history unless the JID is an
  approved `wbom_group_identities` row.
* Rows written before migration 091 keep `ai_generated = NULL` forever; there is
  no way to reconstruct whether an old reply was model-written.

---

## Connecting a WhatsApp bridge (WhatsApp MCP) and the first import

### Topology

Each bridge is a **separate process** (the WhatsApp MCP / whatsmeow bridge) that
owns one WhatsApp session — one phone number — plus its own SQLite store:

```
phone A ── whatsmeow ── bridge1 (HTTP :8082) ── store/bridge1/messages.db   ← read-only
phone B ── whatsmeow ── bridge2 (HTTP :8081) ── store/bridge2/messages.db   ← read-only
phone C ── whatsmeow ── bridge3 (HTTP :8083) ── store/bridge3/messages.db   ← read-only
                                   whatsapp.db (session, LID map, contacts)
```

* **Ingest:** this app opens `messages.db` with `file:…?mode=ro` and never writes
  to it. Live inbound arrives by polling (adaptive 1–30 s).
* **Egress:** sending is the only HTTP direction — `POST {BRIDGE_URL}/api/send`
  with `{"recipient", "message"}`, wrapped by a send-gate (`/api/send-control`)
  and a circuit breaker. Media is fetched with `/api/download`.
* The bridge's own store is preserved even when this app cannot parse a row
  (`bridge_store_preserved` in `metadata_json`).

### Configuration

| Variable | Purpose |
|---|---|
| `BRIDGE1_URL`, `BRIDGE2_URL`, `BRIDGE3_URL` | Bridge HTTP endpoints (defaults `:8082`, `:8081`, `:8083`) |
| `BRIDGE1_NUMBER`, `BRIDGE2_NUMBER`, `BRIDGE3_NUMBER` | The WhatsApp number each bridge is paired with |
| `BRIDGE1_LABEL`, `BRIDGE2_LABEL`, `BRIDGE3_LABEL` | Human labels (`HR`, `OPS`, `REC2`) |
| `BRIDGE1_DB_PATH` … `BRIDGE3_DB_PATH` | `store/bridgeN/messages.db` |
| `BRIDGE1_WHATSAPP_DB_PATH` … `BRIDGE3_WHATSAPP_DB_PATH` | `store/bridgeN/whatsapp.db` |
| `BRIDGE_MEDIA_ACQUISITION_ENABLED`, `BRIDGE_MEDIA_DOWNLOAD_TIMEOUT_S` | Fetch `[media:*]` placeholders through `/api/download` |
| `BRIDGE_OUTBOUND_CAPTURE_ENABLED`, `BRIDGE_OUTBOUND_POLL_S` | Capture messages typed on the phone itself (default on, 10 s) |

### Pairing a new phone

1. Start the bridge process with its store directory; it prints a QR code (or
   writes it to `whatsapp.db`'s companion output).
2. On the phone: **WhatsApp → Linked devices → Link a device**, scan the QR.
3. Wait until the bridge reports the session as connected and
   `messages.db` starts receiving rows.
4. Point `BRIDGEn_DB_PATH` / `BRIDGEn_WHATSAPP_DB_PATH` at that store, set
   `BRIDGEn_NUMBER`, restart the app.
5. Verify: `POST /api/bridges/probe` and `GET /api/bridges/diagnostics`; the
   poller logs `[bridgeN] Poller started` and a heartbeat row appears in
   `fazle_service_heartbeats` (`bridge_poller:bridgeN`).
6. Send one test message **to** the bridge number and confirm it appears in
   `wbom_whatsapp_messages` (`direction='inbound'`, `source_bridge='bridgeN'`).

Nothing is sent before the send gate opens; a freshly paired bridge cannot
auto-reply until identity, routing and the quality gates have run.

### First connection: importing the store's existing history

A fresh link carries the conversation history inside the bridge store
(`from_history_sync = 1` rows and every `is_from_me = 1` row). Live routing
ignores those rows **on purpose** — the 2026-09-02 Bridge3 incident showed that
replaying ~2,444 old messages through routing can auto-answer history. The
importer therefore writes history *without* routing:

| What it does | What it never does |
|---|---|
| Persists every store row to `wbom_whatsapp_messages` with `history_imported = true`, `is_processed = true`, `source_context = 'history_backfill'` | Call `process_message()`, trigger a workflow, intent, admin command or AI reply |
| Resolves identity (`identity_brain.detect_identity`) into `identity_role` / `identity_confidence` | Enqueue or send anything |
| Upserts the counterparty into `fazle_unified_contacts` + `wbom_contacts` (`contact_sync.upsert_contact_from_message`) | Modify the bridge store (still `mode=ro`) |
| Normalises `canonical_phone` / `phone_last10` / `original_sender_number` | Invent provenance: outbound history rows get `ai_generated = NULL` |

Run it one of three ways:

```bash
# 1) CLI (oldest → newest, resumable)
python -m modules.history_backfill --bridge all
python -m modules.history_backfill --bridge bridge2 --since 2025-01-01
python -m modules.history_backfill --bridge bridge1 --dry-run     # count only

# 2) Admin API (runs in the background; poll the status endpoint)
curl -X POST http://localhost:8200/api/bridge/history-backfill      -H "X-Internal-Key: $INTERNAL_API_KEY"      -H 'Content-Type: application/json'      -d '{"bridge": "all", "include_outbound": true}'
curl -H "X-Internal-Key: $INTERNAL_API_KEY"      http://localhost:8200/api/bridge/history-backfill/status

# 3) Automatically at startup
#    BRIDGE_HISTORY_BACKFILL_ON_START=true
#    BRIDGE_HISTORY_BACKFILL_SINCE=2025-01-01
#    BRIDGE_HISTORY_BACKFILL_INCLUDE_OUTBOUND=true
```

Progress and counters live in `wbom_bridge_history_backfill` (one row per
bridge: `pending → running → completed | failed`, plus `last_rowid`,
`inbound_imported`, `outbound_imported`, `skipped_duplicate`,
`contacts_upserted`, `identity_resolved`).

* **Idempotent** — paging is by SQLite `rowid`; every insert is guarded by the
  same `message_hash` uniqueness the live pipeline uses, and outbound rows are
  additionally matched against already-stored messages (same body, counterparty
  and bridge) so an app-sent reply is never duplicated.
* **Resumable** — a run that is interrupted (or stopped with `max_messages`)
  leaves the bridge in `pending`; the next run continues from `last_rowid`.
* **Complete** — reads the whole store (both directions, 200 rows per page), not
  just what arrived after pairing.
* **Narrowed** — `--inbound-only`, `since`/`until`, and groups only when
  `--include-groups` (and then only JIDs approved in `wbom_group_identities`).

Excluded by design: reactions/receipts/revokes/deleted/protocol rows,
newsletters, `status@broadcast`, unapproved groups, and media binaries (the row
keeps its `[media:*]` placeholder and the store still holds the file).

Sanity check after an import:

```sql
SELECT source_bridge,
       count(*) FILTER (WHERE history_imported)                 AS imported,
       count(*) FILTER (WHERE direction = 'inbound')            AS inbound,
       count(*) FILTER (WHERE direction = 'outbound')           AS outbound,
       count(DISTINCT canonical_phone)                          AS people
  FROM wbom_whatsapp_messages
 GROUP BY 1 ORDER BY 1;
```

To start over for one bridge: `--reset` (CLI) or `{"bridge": "bridge2", "reset": true}`
(API). Resetting only clears the import state — it never deletes imported rows.

---

## AI providers: local and hosted

Every reply-producing call goes through one chain, so switching provider is a
configuration change, not a code change.

### Provider order (`app/llm.py`)

| Step | Provider | Enabled by | Notes |
|---|---|---|---|
| 1 | **OmniRoute / Hermes CLI** | `OMNIROUTE_API_KEY` (`OMNIROUTE_URL`, default `http://127.0.0.1:20128/v1`, `OMNIROUTE_MODEL=auto`) | Local gateway in front of Hermes |
| 2 | **Groq** | `GROQ_API_KEY` + `GROQ_MODEL_NAME` (default `allam-2-7b`) | Hosted, fastest fallback |
| 3 | **Local Ollama** | `OLLAMA_URL` (default `http://localhost:11434`) + `OLLAMA_MODEL`; disable with `OLLAMA_REPLY_DISABLED=true` | Default local path, Hermes-family model preferred |
| 4 | **Kimi (Ollama cloud tag)** | `KIMI_MODEL_NAME` (default `kimi-k2.6:cloud`) | Last resort before the safe fallback text |
| — | MiniMax | `MINIMAX_API_KEY`, `MINIMAX_MODEL` | Used by `hermes_dispatch`'s reply + semantic-gate paths |
| — | GitHub Models | `GITHUB_TOKEN*` (+ `GITHUB_MODEL_NAME`) | Legacy; endpoint is confirmed dead and skipped |
| — | 9Router | `NINE_ROUTER_*` | Only when `NINE_ROUTER_SINGLE_UPSTREAM_GUARANTEED=true` |

If every provider fails, the caller receives the existing safe fallback text —
never an exception and never an empty send.

### Model tiers (local Ollama)

`OLLAMA_MODEL` is the base model; the per-task overrides fall back to it when
empty:

| Setting | Used for |
|---|---|
| `OLLAMA_RUNTIME_MODEL` | routing/classification helpers |
| `OLLAMA_BUSINESS_MODEL` | business/commercial answers |
| `OLLAMA_MEMORY_MODEL` | memory summarisation |
| `OLLAMA_GENERAL_MODEL` | general conversation |
| `OLLAMA_DEEP_REASONING_MODEL` | multi-step reasoning |

Tuning: `OLLAMA_NUM_CTX_*`, `OLLAMA_NUM_PREDICT_*`, `OLLAMA_TEMPERATURE_*`,
`OLLAMA_INFER_CONCURRENCY`, `OLLAMA_CHAT_CONCURRENCY`.

### Add a local model (Ollama), step by step

```bash
ollama pull hermes3:3b          # or any model you prefer
ollama list                     # confirm the exact tag
# .env
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=hermes3:3b
OLLAMA_GENERAL_MODEL=hermes3:3b
```

Restart the app; `app/ollama_daemon` warms the model at startup and
`check_ollama_health()` reports availability. If the configured Hermes tag is not
provisioned, the app silently falls back to `OLLAMA_MODEL` instead of failing.

### Add a hosted provider

```bash
# Groq
GROQ_API_KEY=...
GROQ_MODEL_NAME=allam-2-7b
```

Any OpenAI-compatible endpoint can be registered as a **route** without touching
code — this is the supported way to add OpenRouter, Gemini or a private
OpenAI-compatible server:

```bash
curl -X POST http://localhost:8200/api/assistant/ops/ai-routes   -H "X-Internal-Key: $INTERNAL_API_KEY" -H 'Content-Type: application/json'   -d '{"route_id":"my-gemini","display_name":"Gemini 2.5","priority":50,
       "transport":"direct_provider","provider":"gemini",
       "model":"gemini-2.5-flash","base_url":"https://generativelanguage.googleapis.com/v1beta",
       "credential_type":"api_key","credential_ref":"gemini_core_key"}'
```

Supported `transport` values: `omniroute_gateway`, `direct_provider`,
`local_ollama`, `hermes_runner_adapter`; supported `provider` values:
`omniroute`, `groq`, `openrouter`, `gemini`, `ollama-local`. The API key itself
is stored separately in the system secret store
(`POST /api/assistant/ops/system-secrets`) and referenced by `credential_ref`;
listing, updating, deleting and `POST /api/assistant/ops/ai-routes/reorder` work
the same way.

### Adding a provider adapter in code

1. Create `app/<provider>_provider.py` exposing `classify_intent_llm()`,
   `generate_reply()`, `generate_recruitment_reply()` and `decision_call()`
   (see `app/openai_compatible_provider.py` for the minimal shape).
2. Register the provider in `app/unified_routing.py`'s dispatch table.
3. Record provenance on success — call
   `modules.ai_provenance.record_success(provider, model, task="reply")` at the
   point the reply string is returned, otherwise the message will be stored with
   `ai_generated = false`.

### Verifying a provider

* `GET /health` — aggregate health, including `llm`.
* `app.llm.get_last_attempts()` — per-call trace of
  `(provider, model, outcome, detail)` for the current task.
* `wbom_whatsapp_messages.ai_provider` / `ai_model` — what actually answered in
  production (see the outbound section above).
* `phase3_ai_routes` — the authoritative order when an admin-configured route
  matches the workload; unmatched workloads fall back to the chain above.

---

## Identity brain and identity-based domains

Identity is not just a label — it selects the **business domain** and the
**authority lane**:

| Domain | Who | What may happen |
|---|---|---|
| `recruitment` | candidates | funnel answers, eligibility decisions, no money |
| `escort` / `dispatch` | escort clients, buyers, escort workers | orders, roster, availability, release slips |
| `attendance` | employees, guards | attendance drafts |
| `cash` | employees, accountant, admin | payment drafts, cash transactions |
| `office_internal` *(lane category, not a `SUPPORTED_DOMAINS` value)* | coordinators, operations staff | internal coordination, draft-only |

Authority lanes (`modules/authority_lanes`), from most to least autonomous:

| Lane | Meaning |
|---|---|
| `autonomous_operational` | safe read-only answers |
| `owner_supervised_draft` | anything client- or office-facing is drafted, never sent |
| `trusted_admin_execution` | an explicit instruction from a trusted admin |
| `autonomous_monitoring` | the system watching itself (not a conversation) |
| `gated_system_modification` | code/infra changes — always gated |

Two independent lists (the router's `DRAFT_ALWAYS` roles and the lane module's
client/office roles) are cross-checked at test time: a role added to one but not
the other fails CI instead of silently reaching production.

---

## Payroll and accounts

```
WhatsApp message / accountant shorthand / operator form
   → intent + identity
   → payment_ingest            parse cash shorthand deterministically
   → fazle_payroll_engine      employee match, transaction row, dedupe, idempotency
   → review queue              unmatched / low-confidence → human decision
   → fpe_cash_transactions     + ledger, period, ref
   → payment_workflow          payment draft → admin PAID → accountant message
   → reports / accountant_summary / monthly payroll run
```

- **Payroll engine (FPE)** owns employees, cash and income transactions, staging,
  normalization and review queues — 51 API endpoints, all under `/api/fpe/`.
- **Monthly payroll** (`modules/payroll`) has a real state machine —
  `draft → reviewed → approved → locked → paid`, with `cancelled` reachable from
  any non-paid state — and logs every transition.
- **Payment workflow** turns a duty release into a payment draft, sends it to the
  admin, and only records money after `PAID <id> <amount> <method>`.
- **Payment relay** (`modules/payment_relay`) is deliberately narrow: a message
  shaped like the owner's cash shorthand, arriving on the configured inbound
  bridge from the configured source number, is forwarded byte-for-byte. No
  parsing, no LLM, and it does not grow.
- **Attendance → payroll**: attendance drafts become `wbom_attendance` rows, and
  the eligibility preview/dry-run/reconciliation endpoints show what a payroll
  run would do before it does it.

## Escort duty roster and dispatch

```
program created (roster UI / WhatsApp)
   → escort_roster sync      roster entry, shifts, rates, conveyance
   → dispatch (read-only)    availability, conflicts, overlaps, suggestions
   → assignment              assign / replace / unassign / complete (explicit)
   → escort_lifecycle        release intent (text or slip OCR) → close program
                             → backfill attendance → payment draft
```

`escort_lifecycle` is idempotent end-to-end: re-running on a closed program
returns `already_closed` without duplicating drafts. `dispatch` never writes
assignments on its own — suggestions are ranked lists a human accepts.

## Recruitment funnel

`modules/recruitment_flow` keeps one session per phone in
`fazle_recruitment_sessions`, linked to `wbom_candidates` and an append-only
`wbom_candidate_conversations` audit. Steps: name → age → area → job preference
→ experience → phone confirmation. Turn decisions
(`recruitment_turn_decision`) choose between answering from authority text,
asking the next question, or escalating; `recruitment_recovery_sweep` restarts
abandoned sessions.

---

## Scheduled jobs

APScheduler runs inside the app (`SCHEDULER_ENABLED=true`, timezone
`SCHEDULER_TIMEZONE=Asia/Dhaka`). Each job's last status, duration and error are
stored in `fazle_scheduled_jobs` and shown on the dashboard's Scheduler tab.
Every job is read-or-report only unless its name says otherwise.

| Job id | Cadence (default) | Purpose |
|---|---|---|
| `daily_payroll_compute` | 02:00 | Compute the day's payroll |
| `daily_db_backup` | 02:30 | `pg_dump`, rotate daily/weekly |
| `backup_staleness_alert` | 03:00 | Alert if the newest backup is older than 48 h |
| `payment_inactivity_sweep` | 04:00 | Flag stalled payment drafts |
| `daily_admin_digest` | 08:00 | Yesterday's operational digest |
| `stale_escort_reminder` | 09:00 | Programs open > 30 days |
| `business_summary_report` | 09:00 and 21:00 | Business communication summary + approval queue |
| `system_observation_report` | 09:15 and 21:15 | Health, jobs, incidents, financial anomalies (report only) |
| `daily_memory_review` | 09:00 | Review memory candidates |
| `rag_rebuild` | 18:00 | Rebuild the vector index |
| `dlq_alert` | every 15 min | Dead-letter queue alert |
| `lock_cleanup` | every 5 min | Release stale locks |
| `bridge_watchdog` | every 5 min | Bridge liveness |
| `draft_ttl_cleanup` | every 30 min | Expire old reply drafts |
| `payment_reconciliation` | hourly | Staging vs posted cash |
| `combined_draft_cleanup` | hourly | Draft housekeeping |
| `health_summary` | every 6 h | Probe summary to admin |
| `agent_incident_summary` | every 6 h | Grouped monitoring incidents |
| `recruitment_recovery_sweep` | every 30 min | Restart stalled candidate sessions |

## Reports

`modules/reports` is read-only, bounded and body-free (no message content ever
leaves in a report). Reachable from the dashboard's **Reports** tab, the
`report` admin command, `/reports/{name}`, and the scheduled jobs above.

| Report | What it answers |
|---|---|
| `daily_summary` | Traffic, drafts and cash for one day |
| `monthly_payroll` | Per-employee payouts for a month |
| `cash_position` | Cash movement over N days |
| `payment_reconciliation` | Staging rows vs posted transactions |
| `escort_utilization` | Programs and escorts in a date range |
| `business_communication_summary` | Categories, pending drafts and the owner's approval queue |
| `system_observation` | Health, stale/obsolete jobs, incidents, financial anomalies, priority |
| `unresolved_fpe_employees` | Employee identities still unresolved |

Every run is recorded in `fazle_report_runs` (who asked, duration, row count,
error). `system_observation` never contains a write statement — it reports, it
does not fix.

---

## Operational readiness audit (2026-11-06)

Everything below was checked against the code in this repository and, where a
database was needed, against a real PostgreSQL 16.2 instance. Where something is
**not** covered, it says so instead of implying coverage.

### Git

| Check | Result |
|---|---|
| Branch | `arena/bff986c3-friendly-sniffle`, working tree clean before this audit |
| Scope | earlier credentials/installer work is merged (`41fea6f`); this audit adds only the files listed in its commit |
| Secrets in git | `.gitignore` covers `.env`, `.env.*`, `logs/`, `*.log`, `backups/`, `venv/`; `scripts/secret_scan.sh` exists for a pre-push sweep |
| Ignored-but-required files | `/scripts/*` is a blanket ignore. It silently swallowed `scripts/apply_schema.py`, so the merged tree shipped an `install.sh` that referenced a file that did not exist — the install died at step 4. Fixed with an explicit `!/scripts/apply_schema.py` **and** a `die` guard in the installer. Any new script the installer calls needs its own `!` line; `git check-ignore -v <path>` is the check |

### systemd (`fazle-core.service`)

| Check | Result |
|---|---|
| Service user | `User=`/`Group=@RUN_USER@` rendered by `install.sh`; never root, no `ProtectSystem` because the unit legitimately writes inside its own directory |
| Restart policy | `Restart=on-failure`, `RestartSec=5`, `TimeoutStopSec=30`, `KillMode=mixed` |
| Crash-loop | `StartLimitIntervalSec=120` + `StartLimitBurst=10` (systemd's 5/10s default trips during a slow database start and leaves the unit failed) |
| Environment | `EnvironmentFile=-@APP_DIR@/.env` (leading `-` = a missing `.env` is a warning, not a failed start); `PYTHONUNBUFFERED=1`, `PYTHONPATH=@APP_DIR@` |
| Logs | stdout/stderr appended to `logs/fazle-core.{stdout,error}.log`; the app's own rotating log is `logs/fazle-core.log` (10 MB × 5) |
| Hardening | `NoNewPrivileges=true`, `PrivateTmp=true`, `UMask=0077` |

Verify a rendered unit before installing it:
`sed -e "s|@APP_DIR@|/srv/fazle|g" -e "s|@RUN_USER@|fazle|g" fazle-core.service > /tmp/u.service && systemd-analyze verify /tmp/u.service`.

### File permissions

| File | Mode | Set by |
|---|---|---|
| `.env` (holds `INTERNAL_API_KEY`, `DATABASE_URL`, bootstrap password) | `600` | `install.sh` creates it `600` and tightens an existing one |
| `logs/fazle-core.log` | `600` | `app/logging_setup.py` (it can contain the one-time bootstrap password) |
| `backups/`, `backups/**` | `700` / `600` | `modules/backup`, and the pre-migration dump in `install.sh` |
| Everything else the service creates | `0600`/`0700` | unit `UMask=0077` |

Database credentials live only in `.env`; they are never logged (audited: no
`log.*DATABASE_URL` call sites). The pre-migration restore hint is printed with
the password replaced by `***`.

### Network

| Check | Result |
|---|---|
| Bind address | `run.py` binds `127.0.0.1:APP_PORT` (default 8200). The container binds `0.0.0.0:8200`; publish it as `-p 127.0.0.1:8200:8200` to keep parity |
| Public surface | 397 routes: **147** enforce a role, **195** require any valid key, **55** need no credential |
| The 55 | 36 static page shells (HTML only), `/health` (app + payroll), `/metrics`, `/api/public-config`, `/api/state-version`, `/api/frontend/heartbeat`, `/login`, `/webhook/meta` (GET verify, POST signature-checked by default), `/webhook/mcp1|2`, and five endpoints that verify a service bearer inside the handler (`/datasets`, `/query`, `/admin-full-read`, `/ai-policy/*`) |
| `/metrics` | Loopback-only by design. Set `METRICS_TOKEN` and the endpoint requires `Authorization: Bearer <token>` (or `X-Metrics-Token`); unset, it stays open so a scrape on 127.0.0.1 keeps working. **Do not proxy `/metrics` publicly** |
| Reverse proxy | `deploy/nginx.conf.example` is the reference site: metrics/observability/`debug` denied, `/webhook/mcp*` restricted to private networks, `limit_req` on login and the API, `client_max_body_size 12m`. nginx is where the throttling and body limits live, because the app has none |
| Bridge webhooks | HMAC is optional (`WEBHOOK_SIGNATURE_ENFORCEMENT=false` by default) → unsigned POSTs are accepted and logged. Keep `/webhook/*` off the public proxy, or set `BRIDGE_WEBHOOK_SECRET` + enable enforcement; the app warns about this at startup |
| Firewall / nginx | Only 80/443 public. `location /` → `127.0.0.1:8200`; `deny all` for `/metrics`, `/webhook/mcp*` and `/api/public-config` if they are not needed externally |

### Database

| Check | Result |
|---|---|
| Separation | One database per deployment (`fazle_core`); nothing writes outside it |
| Schema | `public` only — no `CREATE SCHEMA`, no custom `search_path` anywhere in migrations or code |
| Cross-database FKs | None: every `REFERENCES` targets a table in the same database (verified by parsing all migrations) |
| Roles | The app needs one login role that owns its database (no `SUPERUSER`, no `CREATEDB`). The first migration needs `pg_trgm`, which may require a superuser — `install.sh --preflight` checks this up front |
| AI read path | Uses the deliberate `fazle_ai_reader` (`NOLOGIN`) role with `SELECT` grants on views only — lazy, least-privilege access to financial data |
| Isolation proof | `scripts/verify_isolation.py` (also `make db-isolation`, and run automatically by `install.sh` step 3b-ii): refuses a cluster-default database, a `SUPERUSER`/`CREATEROLE`/`CREATEDB` role, tables in `public` owned by another role, and a redirected `search_path`. Verified against a real PostgreSQL 16.2: a plain role on its own database passes, `postgres` fails with all four elevated attributes named |

### Migration

| Check | Result |
|---|---|
| Coverage | `scripts/apply_schema.py` applies all 109 files (baseline 1 → payroll 14 → escort 2 → `db/migrations` 92); every one parses against the PostgreSQL 16 grammar (`pglast`/`libpg_query`) |
| Atomicity | Every file runs in its own transaction, recorded in `schema_migrations` only after success |
| Partial failure | `exit 1` names the exact file + SQL error; re-running resumes from there. Exit `3` means "incomplete, missing objects that only exist in production" |
| Rollback | **No down-migrations exist.** The rollback is a restore, which is why `install.sh` takes a `pg_dump` (gzipped, `600`) into `backups/pre-migrate-<ts>.sql.gz` before touching the schema. `SKIP_PRE_MIGRATION_BACKUP=1` opts out |
| Re-run safety | Second run applies nothing — every file is already recorded in `schema_migrations` |
| Dependency rounds | Files are retried up to 12 rounds when they fail on a not-yet-created object (`42P01`/`42703`/`42704`-class), because `db/migrations` is not topologically sorted |
| Virgin-build evidence | Run for real against PostgreSQL 16.2 on 2026-11-06. The first runs exposed the real gap (a virgin database applied **56** of 108 files and exited **3**: the history alters tables it never creates); `000_virgin_baseline.sql` was written from that evidence, and the same run now finishes **109/109, exit 0**, with `003c` recorded as an assumption, `086` as a no-op and two files `partial` for the missing `pg_trgm` extension. Both runs are described in [Building a virgin database](#building-a-virgin-database-roadmap-item-1) |

### API

| Check | Result |
|---|---|
| Authentication | `X-Internal-Key` (server key, or an `fk_…` token from `/api/admin/login`); `Authorization: Bearer` for the service-to-service endpoints |
| Authorization | 147 routes are role-gated (`_require_superadmin`, `_require_admin_or_above`, `_require_operator_or_above`, `_require_accountant_or_above`, `_require_office_assistant_or_above`, `_require_viewer_or_above`, `_permission(...)`, `require_command`) |
| Rate limiting | `POST /api/admin/login`: 8 failures / 15 min per username+IP → `429` with `Retry-After`. **No other route is throttled** in-app — `deploy/nginx.conf.example` adds `limit_req` zones for the rest |
| Write identity | `RBAC_WRITE_GUARD` (on) resolves a presented `fk_` token's role level once, cached 60 s, and compares it with `RBAC_WRITE_MIN_LEVEL`. Default **0 = audit only**: a roleless token still writes, but each such write logs `[auth][audit] token with no role wrote …`, naming exactly who would break. Set `RBAC_WRITE_MIN_LEVEL=10` (or 20/70) to refuse them with 403. Requests without a token are untouched — the route's own dependency decides. Before raising the level, run `make rbac-audit` (see *Turning the write guard on* below): the guard can only enforce roles the accounts actually have. Enforcing by default was rejected deliberately: the project's own suite proves `/api/fpe/operator/submit`, `/admin/users/{phone}/enable` and `/scheduler/run/{job}` currently write with roleless tokens |
| Body/page limits | None in-app (uvicorn reads the whole body); cap `client_max_body_size` in nginx |

### RBAC — what each role can actually do

| Role | Level | Enforced by |
|---|---|---|
| `viewer` | 10 | `_require_viewer_or_above`, read-only report/status routes |
| `office_assistant` | 20 | attendance draft entry only (`_require_office_assistant_or_above`); no approve/edit |
| `operator` | 30 | dispatch/roster and draft decisions (`_require_operator_or_above`) |
| `accountant` | 50 | payroll/cash/income and payment review (`_require_accountant_or_above`) |
| `admin` | 70 | employee + configuration writes (`_require_admin_or_above`, `_require_admin_role`) |
| `superadmin` | 100 | destructive/billing/client-profile deletes (`_require_superadmin`), user management |

`INTERNAL_API_KEY` bypasses all of the above as owner — it is a server secret, not
a person's login — and since this audit it is **accepted from loopback only**
(`owner_key_scope` middleware; a reverse proxy on the same host still works
because it connects from 127.0.0.1). `INTERNAL_KEY_ALLOW_REMOTE=1` restores the
old behaviour for a genuinely trusted remote host, and the app logs a warning
while it is on. The 195 key-only routes still accept any *role-carrying* token:
the write guard rejects tokens whose account is disabled or has no role, but a
`viewer` token can still write where the route only asks for a key.
`/api/admin/login` returns the actor's permissions so the UI hides what the API
would refuse.

#### Turning the write guard on: role coverage first

Enforcement can only refuse what the accounts cannot satisfy, so the order is
**audit → assign → enforce**, and each step has a command:

```bash
# 1. who holds a token but no role? (exit 1 while any active admin is roleless)
DATABASE_URL=… make rbac-audit        # = scripts/rbac_write_audit.py --strict
DATABASE_URL=… python scripts/rbac_write_audit.py --json

# 2. give them a role — one account, or every roleless account
python scripts/rbac_write_audit.py --grant viewer --username office01 --dry-run
python scripts/rbac_write_audit.py --grant operator --phone 8801700000012
python scripts/rbac_write_audit.py --grant viewer --all-roleless --yes

# 3. enforce, restart, then re-run step 1 (it should print ✓)
RBAC_WRITE_MIN_LEVEL=10              # in .env  → 403 for roleless tokens
```

The application also says this at boot: with `RBAC_WRITE_MIN_LEVEL > 0` and a
roleless token holder still present it logs an **error** naming the fix; in audit
mode it logs a warning with the count. Accounts created by
`ensure_bootstrap_admins()` / `create_admin()` / migration `092` already carry a
role — the roleless ones are older rows and hand-made inserts, which is exactly
what the script finds.

**A drift that mattered (found while writing the test for the above):**
`tests/conftest.py` seeded `fazle_roles` as `viewer=1, operator=2, accountant=3,
admin=4, superadmin=5` and had no `office_assistant`, while migration `093` and
this table use `10/20/30/50/70/100`. Every level comparison in the suite therefore
ran against a ladder that exists only inside tests — with the old fixture
`RBAC_WRITE_MIN_LEVEL=10` would have refused *everybody*, superadmin included, and
`live_adapters`' per-tool `min_role` checks would have refused every AI tool. The
fixture now seeds the production ladder, and `tests/unit/test_rbac_role_ladder.py`
parses the migration, the fixture and this README table and fails on any future
divergence.

### Background jobs and duplicate workers

| Worker | Two instances safe? | Why |
|---|---|---|
| Scheduler (APScheduler) | **No** | In-memory job store, no distributed lock: two instances both fire every job (payroll compute, backups, digests). Keep `SCHEDULER_ENABLED=true` on exactly one instance |
| Outbound queue worker | Yes | Claims rows with `FOR UPDATE SKIP LOCKED` |
| Social send queue | Yes | Same claim pattern |
| Escort queue | Yes | `pg_try_advisory_lock` per item |
| Bridge poller | Yes | Shared cursor + `processed_bridge_messages` dedup |
| History import | Single-shot | State row per bridge (`schema_migrations` 091) |

### WhatsApp access

| Check | Result |
|---|---|
| Store access | Read-only: `sqlite3.connect("file:…?mode=ro")` everywhere — this app never writes the bridge store |
| Pairing | No pairing UI and no pairing call; pairing stays the bridge's job |
| Outbound | `OUTBOUND_ENABLED=false` by default; `bridge.ensure_enabled()` refuses sends, and `_pairing_held()` additionally blocks a bridge that reports a non-sending state |
| Ownership | The bridge is a shared neighbour: `BRIDGE*_URL` points at the running bridge, nothing is restarted or re-paired |

### Resource usage (measured on this checkout)

| Item | Value |
|---|---|
| Repository | 13 MB (2.8 MB `.git`) |
| Python | 3.11; import of `app.main` takes ~2.3 s and peaks at **82 MiB RSS** |
| Dependencies | `pip install -r requirements.txt` with the default PyPI `torch` wheel: **≈6.1 GB** (`nvidia-*` 3.0 GB + `torch` 1.2 GB + `triton` 1.0 GB). `install.sh` pre-installs the CPU wheel (`download.pytorch.org/whl/cpu`, `NO_CPU_TORCH=1` opts out), which drops the venv to a few hundred MB |
| Disk growth | Logs: 10 MB × 5 rotations. Backups: one `pg_dump` per day per `BACKUP_KEEP_DAILY` |
| Docker image | **Not measured** — no Docker daemon in this environment. The `Dockerfile` is `python:3.11-slim` + tesseract/ffmpeg/libpq/gcc; expect roughly the venv size plus ~300 MB of OS packages, so build with the CPU torch step above |

### Recovery

| Scenario | Behaviour |
|---|---|
| Process crash | systemd restarts after 5 s; `Restart=on-failure` plus the crash-loop window above |
| Failed deploy | `scripts/restart_gate_readiness.py` fails the install when the unit restarts, crash-loops, logs a traceback, or `/health` returns 503 for a core probe |
| Database loss | `gunzip -c backups/pre-migrate-<ts>.sql.gz | psql "$DATABASE_URL"` (the newest pre-migration dump is the rollback point; daily dumps are separate) |
| Restore **drill** | `make restore-drill` (or `scripts/restore_drill.py`): picks the newest dump, checks gzip integrity and that the file ends with pg_dump's completion line, restores into a scratch `<db>_drill_<utc>` database, verifies `schema_migrations`/table/row counts, then drops it. Tolerates exactly the production-only objects and fails on anything else. `--dry-run` prints the commands with the password masked. **Executed for real on 2026-11-06**: 57 migrations and 83 tables restored into a scratch database, verdict PASS — the first attempt failed and exposed a URL-building bug (`str.replace("/postgres", …)` also rewrote the scheme), which is why the drill exists |
| Schema half-applied | Re-run `scripts/apply_schema.py`: it resumes from the first unrecorded file |
| Duplicate jobs after a bad restart | Check `fazle_scheduled_jobs` heartbeats; ensure only one instance has `SCHEDULER_ENABLED=true` |

### Operator checks (all read-only unless stated)

| Command | What it proves |
|---|---|
| `make db-isolation` (`scripts/verify_isolation.py`) | the target database is dedicated and the role is not a superuser; also run by `install.sh` step 3b-ii (`STRICT_DB_ISOLATION=1` to make it fatal) |
| `make restore-drill` (`scripts/restore_drill.py`) | a backup actually restores: gzip + pg_dump completion check, scratch database, row/table counts, then drop |
| `make bridge-smoke` (`scripts/bridge_smoke.py`) | the real bridge stores open read-only, stay byte-identical while read, and inbound is fresh within `--stale-minutes` |
| `make rbac-audit` (`scripts/rbac_write_audit.py`) | every active admin has a role, so `RBAC_WRITE_MIN_LEVEL` can be raised without locking anybody out; exits 1 while a roleless token holder remains |
| `deploy/nginx.conf.example` | the reverse-proxy half the app does not implement: metrics denied, `/webhook/mcp*` restricted, `limit_req`, body cap |

Each script has `--selftest` (no database, no bridge) and is exercised by
`tests/unit/test_access_control_hardening.py` / `tests/unit/test_rbac_write_audit.py`.

### Test suite reality

The audit measured this instead of repeating "82 tests pass":

| What was run | Result | Database needed? |
|---|---|---|
| `test_llm_provider_order`, `test_operational_hardening`, `test_access_control_hardening`, `test_apply_schema_plan`, `test_admin_bootstrap_credentials`, `test_phase8b_open_chat_ui` | **134 passed** | `test_admin_bootstrap_credentials` yes, the rest no |
| The whole `tests/unit` against a real PostgreSQL 16.2 (schema built by `tests/conftest.py`: 107 migration files) | reached ~84% before the sandbox reclaimed the process: **175 failed, 20 errors** | yes |
| The same DB-backed files on the *untouched* base commit (`086fea5`) | **identical failures** (`test_payment_relay`: 14/14) | yes |
| The whole `tests/unit` again *after* the triage below (107 migration files, one session, same DB) | **32 failed, 0 errors** (was 175 failed, 20 errors) | yes |
| The same run *after* per-test database isolation and the module-object restore (below) | **19 failed, 0 errors** — **the same 19 the files produce when each one runs alone**, i.e. the suite is finally order-independent | yes |

So a green number never meant "the business logic is proven": the DB-backed
majority needs a database, and a real chunk of it failed on `master` independently
of this work (payment relay, recruitment recovery sweep, Hermes capability
boundary, RAG authority). Those are pre-existing and are **not** hidden by this
document — they are simply out of scope for a deployment audit, and they say the
app's own suite cannot be used as a release gate until they are triaged.

#### What the triage found (2026-11-06)

Every file that failed the full run was re-run individually. The suite is now at
**19 failed, 0 errors** (from 175 failed, 20 errors), it produces that same 19
whether you run one file or the whole directory, and every remaining one is a
*missing input* or a *documented divergence* — not an unexplained red:

| Remaining failures | Count | Cause | Action |
|---|---|---|---|
| `test_rag_knowledge_base` (3), `test_reply_policy` (1), `test_k32_recruitment_reconciliation` (2) | 6 | need `knowledge_base/**` / the private FAQ corpus, which `.gitignore` excludes | skips when absent where the test allows it; the rest need the corpus |
| `test_k32_recruitment_reconciliation` (2) | 2 | need `scripts/seed_recruitment_kb.sql` and `scripts/reconcile_recorded_k32.py`, which are **referenced by the tests and by migration 086 but were never committed** (`/scripts/*` is ignored) | needs the original migration run (Owner decision) |
| `test_media_normalization_phase3` (3) | 3 | the test's premise is obsolete: `app/main.py` now stores `[media:<type>]` instead of dropping unsupported Meta messages | Owner decision |
| `test_phase_c_wireins` (1), `test_conversation_memory` (1), `test_fpe_rules` (1) | 3 | assert **deployment configuration** (recruitment answer-first flag, stage-2/3 flags, `FPE_SYNC_CHAT_JIDS`), which is not in the repo by design | the tests should read config, not assume `.env` |
| `test_scheduler_phase5b_trigger_wiring` (1), `test_escort_source_of_truth_refactor` (1) | 2 | behavioural drift between the test's expectation and current code (bridge3 probe list, unmatched-confirmation queueing) | each needs a decision |
| `test_recruitment_ai_restricted` (1), `test_rag_startup_sync_boundary` (1) | 2 | Meta safe-mode path returns before `enqueue`, and the blanket `/knowledge_base/` ignore contradicts the lock-file-only expectation | documented; needs a decision on each |
| `test_stage_h_financial_operations` (1) | 1 | needs `/home/user/hermes-runner/server.py`, an external install | environment, not code |

#### Why the suite is now order-independent (2026-11-09)

Thirteen tests used to pass on their own and fail in a full session. None of
them was wrong; the *runner* was. Three leaks were found and closed:

- **Database rows survived their test.** `tests/support/db_isolation.py` resets
  the schema *before* each test: it counts the rows of every table in one
  statement and truncates only the ones that are dirty (a full-schema
  `TRUNCATE` costs 2.4 s here — 20 ms × 130 relations — so only the dirty
  tables are touched, ~25 ms per test), then restores the `fazle_roles`
  ladder. A failing test keeps its rows for inspection instead of having them
  wiped by the next test's setup. Opt out with `FAZLE_TEST_DB_ISOLATION=off` or
  `@pytest.mark.no_db_isolation`.
- **`sys.modules` surgery leaked.** `test_phase_c_wireins` pops `app` /
  `app.config` to prove the config gate fails closed and restores only
  `app.config`; the `app` package object came back without the submodules
  already imported (`AttributeError: module 'app' has no attribute 'ollama'`),
  and any production module imported while its fake `app.database` was
  installed bound the fakes forever (`TypeError: object list can't be used in
  'await' expression`). `tests/conftest.py` now restores replaced or missing
  `sys.modules` entries after every test, and that module purges whatever was
  imported under its own stubs.
- **Ambient configuration was assumed.** `test_bridge_send_control_maintenance`
  asserted three poll tasks while `start_pollers()` defaults
  `BRIDGE_OUTBOUND_CAPTURE_ENABLED` to *true*; it now pins the flag it is not
  testing.

Proof: 32 failed → **19 failed, 0 errors**, and the whole directory now
produces exactly the same 19 as the per-file runs.

Fixed during the triage (not test-blanket-work — real code or real test defects):

- `modules/outbound/__init__.py::_send_with_channel` had its **docstring after the
  `raise`**, so it was dead code; moved above the authorization guard.
- `modules/admin_directives/router.py` — four cooldown sentinels used
  `dict.get(job, 0.0)`, so on a freshly started host the monotonic clock was
  younger than the cooldown and every alert investigation looked permanently
  blocked; replaced with `None` sentinels.
- `tests/unit/test_outbound_dead_letters.py` walked `app.routes` assuming every
  entry has `.path`; FastAPI 0.115 keeps included routers as `_IncludedRouter`
  wrappers. The test now walks the table recursively (the route really is
  registered — the assertion was checking the wrong thing, and would have
  crashed on any FastAPI upgrade).
- `tests/unit/test_llm_provider_order.py` leaked settings *between modules*:
  `test_phase_c_wireins` swaps `sys.modules["app.config"]` for a stub, and
  `app.llm` keeps the `get_settings` it captured at import time, so clearing the
  cache had no effect and the chain silently fell back to local Ollama. The
  fixture now re-binds `app.llm.get_settings` explicitly.
- Sixteen test files asserted hard-coded owner/bridge numbers that are no longer
  in source (correctly — they are personal data). They now skip with a named
  reason via `tests/support/private_data.py`, or set the number as configuration
  the way an operator would.

What did change is that a missing database no longer produces 700 cryptic
"Schema setup failed" errors: `tests/conftest.py` installs a no-database guard
that turns every PostgreSQL touch into a recognisable skip (`204 database test(s)
skipped`, everything else still runs), and `REQUIRE_TEST_DB=1` — set by
`make test-local` and by CI — turns a missing database into a hard failure so a
skipped suite can never look green.

Running the real thing on a workstation:

```bash
export TEST_DB_PASSWORD=$(openssl rand -hex 12)
make test-docker-up          # postgres-test on 127.0.0.1:5433 + mocks
make test-local              # REQUIRE_TEST_DB=1 pytest tests/unit tests/integration tests/db tests/workflows
make test-docker-down
```

### Known flaky test (documented, then fixed)

`tests/unit/test_llm_provider_order.py::TestCheckHealthConcurrencyAndTimeout::test_all_four_checks_actually_run_concurrently`
failed with *"check_health() took 1.4–1.6s for 3 checks each delayed 0.3s"*.

- **Cause** — the test stubbed only three of the four provider probes.
  `_reset_settings()` sets `GROQ_API_KEY` to a fake value, so the fourth probe
  (`_check_groq`) made a real HTTPS call to `api.groq.com`. Where egress is
  blocked (CI/sandbox), building the client and failing the connect blocks the
  event loop for ~1.3 s and the three `sleep(0.3)` probes serialize. The timing
  assertion was measuring the network, not `check_health()`.
- **Impact** — false red builds that hide real regressions; no runtime effect.
  `check_health()` is correctly concurrent (measured: 0.303 s for three 0.3 s
  probes in a clean interpreter).
- **Fix** — the test now stubs `_check_groq` as well, keeps the strict
  `< 2 × delay` assertion, and documents why ambient network must never be part
  of a timing assertion.

## Configuration and deployment

Application settings are defined in [`app/config.py`](app/config.py). Use
environment variables or a deployment secrets manager for credentials, provider
API keys, bridge addresses, and contact identifiers. The app should remain in a
non-sending mode until bridge configuration, authorization, and message-routing
tests have passed.

Schema changes are applied only through `db/migrate.py` (or `scripts/apply_schema.py`
for a full build); 92 files in `db/migrations` are applied in filename order. The two newest are
[`091_outbound_provenance_and_history_backfill.sql`](db/migrations/091_outbound_provenance_and_history_backfill.sql)
(outbound provenance columns + history-import state table, described above) and
[`092_admin_bootstrap_credentials.sql`](db/migrations/092_admin_bootstrap_credentials.sql)
(`must_change_password`, `credential_set_at`, and a case-insensitive unique index
on `fazle_admins.username`) and
[`093_seed_rbac_roles.sql`](db/migrations/093_seed_rbac_roles.sql) (the six RBAC
role rows a non-production database never had). Both writers are schema-tolerant: a database that
has not run them still accepts every message, and the bootstrap falls back to the
pre-092 column set instead of failing startup.

Operational switches introduced with that migration:

| Variable | Default | Effect |
|---|---|---|
| `BRIDGE_HISTORY_BACKFILL_ON_START` | `false` | Import each bridge store once, at startup |
| `BRIDGE_HISTORY_BACKFILL_SINCE` | *(whole store)* | ISO lower bound for that import |
| `BRIDGE_HISTORY_BACKFILL_INCLUDE_OUTBOUND` | `true` | Also import `is_from_me = 1` history |
| `BRIDGE_OUTBOUND_CAPTURE_ENABLED` | `true` | Persist messages typed on the phone itself |
| `BRIDGE_OUTBOUND_POLL_S` | `10` | How often that capture loop reads the store |
| `BOOTSTRAP_ADMIN_USERNAME` | `admin` | Username created on the first start (see [First login](#first-login-bootstrap-credentials)) |
| `BOOTSTRAP_ADMIN_PASSWORD` | *(generated)* | First password; empty ⇒ generated and logged once. Never overwrites an existing one |
| `BOOTSTRAP_ADMIN_CREDENTIALS` | `true` | `false` disables the first-boot account |
| `SCHEDULER_ENABLED` | `true` | Periodic jobs — turn off when another app on the host owns them |
| `OUTBOUND_ENABLED` | `false` | Actual sending through the bridge |

For the staged clean-install, migration, bridge, AI-provider, verification, and
rollback checklist, see [`MASTER_PRODUCTION_PLAN.md`](MASTER_PRODUCTION_PLAN.md).
