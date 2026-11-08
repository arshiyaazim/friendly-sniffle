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
| Modules | 81 domain packages under `modules/` |
| Migrations | 89 ordered SQL files under `db/migrations/` |

---

## Contents

- [Requirements](#requirements) · [Local setup](#local-setup) · [Docker, the VPS installer and the restart gate](#docker-the-vps-installer-and-the-restart-gate) · [Tests](#tests)
- [Folder structure](#folder-structure) · [Ports and services](#ports-and-services)
- [Pages and URLs](#pages-and-urls) — every screen, its tabs, tables, buttons, forms and modals
- [API surface](#api-surface)
- [Inbound message pipeline](#inbound-message-pipeline) — identity → intent → routing → safety gates
- [Identity brain and identity-based domains](#identity-brain-and-identity-based-domains)
- [Payroll and accounts](#payroll-and-accounts)
- [Escort duty roster and dispatch](#escort-duty-roster-and-dispatch)
- [Recruitment funnel](#recruitment-funnel)
- [Scheduled jobs](#scheduled-jobs) · [Reports](#reports)
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

## Docker, the VPS installer and the restart gate

```bash
docker build -t fazle-core .
docker run --env-file .env -p 8200:8200 fazle-core
```

`install.sh` is the VPS path. It creates the venv, installs `requirements.txt`,
renders `fazle-core.service` for wherever the checkout actually lives
(`@APP_DIR@` / `@RUN_USER@` are substituted, never shipped raw), installs and
starts the unit, and then waits for the restart gate before declaring success:

```bash
bash install.sh                       # APP_DIR / RUN_USER / PYTHON_BIN may be overridden
```

The unit writes uvicorn's stderr to `logs/fazle-core.error.log` and reads
`<repo>/.env` (plus the app's own `.env` loading). For a real deployment, follow
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
│   ├── main.py                  FastAPI app: 143 routes, lifespan, mounts every router
│   ├── config.py                Settings (163 fields) — the single source of configuration
│   ├── database.py              asyncpg pool: fetch_all / fetch_val / fetch_one / execute
│   ├── llm.py, ollama.py        reply generation + provider fallback chain
│   ├── *_provider.py            groq / minimax / nine-router / omniroute / openai-compatible
│   ├── unified_routing.py       workload → provider routing
│   ├── bridge.py, outbound client, logging_setup.py, error_log.py
│   └── static/                  12 served SPAs (+2 previews), no bundler, no node_modules
├── modules/                     81 domain packages — one package per feature area
├── shared/                      cross-cutting helpers: auth, drafts, phone, events, queue arbiter
├── db/
│   ├── migrate.py               migration runner (see Local setup)
│   └── migrations/              89 ordered .sql files — the only schema authority
├── migrations/                  older standalone SQL, NOT run by db/migrate.py
├── scripts/                     restart_gate_readiness.py, secret_scan.sh
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
| Ops & platform | `scheduler`, `backup`, `health_monitor`, `operations_health`, `observability`, `reports`, `preflight_guard`, `rbac`, `outbound`, `bridge_poller`, `kb_upload`, `knowledge_base`, `ai_console`, `ai_readonly_tools`, `business_read_service`, `admin_chat_lab`, `admin_commands`, `admin_directives`, `hermes_kernel`, `hermes_tasks`, `social_auto_reply`, `media_normalization`, `ocr_processor`, `voice_processor`, `image_hash`, `location_adapter`, `wa_chat_frontend` |

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

Login gate: **Login** with the internal API key; **Logout** clears it.

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

FastAPI mounts 143 routes in `app/main.py` plus the domain routers. Grouped by
prefix:

| Prefix | Purpose |
|---|---|
| `/api/fpe/*` (51 endpoints) | Payroll engine: `employees`, `transactions`, `cash`, `income`, `operator`, `unmatched`, `review`, `sync`, `normalization`, `staging-payments`, `ledger`, `stats`, `admin` |
| `/api/admin/*`, `/admin/users/*` | Admin employees/transactions/users, login, knowledge upload |
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

## Configuration and deployment

Application settings are defined in [`app/config.py`](app/config.py). Use
environment variables or a deployment secrets manager for credentials, provider
API keys, bridge addresses, and contact identifiers. The app should remain in a
non-sending mode until bridge configuration, authorization, and message-routing
tests have passed.

For the staged clean-install, migration, bridge, AI-provider, verification, and
rollback checklist, see [`MASTER_PRODUCTION_PLAN.md`](MASTER_PRODUCTION_PLAN.md).
