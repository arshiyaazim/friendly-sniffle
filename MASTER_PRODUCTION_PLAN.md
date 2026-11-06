# Fazle Core — GitHub থেকে Production-এ নেওয়ার মাস্টার প্ল্যান

এই নথি নতুন GitHub repository-তে নিরাপদে source প্রকাশ, খালি PostgreSQL
database-এ clean install, WhatsApp bridge সংযোগ, AI provider নির্বাচন, staging
পরীক্ষা, production deployment এবং rollback-এর ক্রমানুসারী checklist।

`[ ]` কাজ বাকি, `[x]` প্রমাণসহ যাচাই সম্পন্ন, `[!]` বাধা বা সিদ্ধান্ত প্রয়োজন।
Production database, live messaging, Git commit/push বা service পরিবর্তনের আগে
সেই নির্দিষ্ট কাজের জন্য আলাদা অনুমোদন নিতে হবে।

## বর্তমান অবস্থা

- Target remote: `https://github.com/arshiyaazim/friendly-sniffle.git`
- Local branch: `master`; initial public-release commit
  `65731f6a5677d16e4f5818f164348afcadb53da8` target repository-তে push হয়েছে।
- Target repository public; `master` branch remote-এর সাথে synchronized।
- `.gitignore` internal docs/knowledge base, operational data এবং
  `tests/e2e/screenshots/`, `tests/fixtures/phase3/` ও generated
  `tests/coverage-unit.xml`-কে public commit থেকে বাদ দেয়।
- Credential scan এবং নির্বাচিত Python ফাইলের syntax compilation পাস করেছে।
  Full unit suite যাচাই হয়নি: Windows-এ `fcntl` অনুপস্থিত, কয়েকটি test
  dependency নেই, একটি ignored corpus নেই এবং test-schema connection refused;
  collection-এ 24টি error হয়েছে।
- Staged whitespace check-এ একাধিক trailing whitespace/blank-line warning
  পাওয়া গেছে; unrelated formatting পরিবর্তন করা হয়নি।
- নতুন production database-এ migration, clean-install verification, VPS বা
  production service পরিবর্তন করা হয়নি।

## 1. Public release প্রস্তুতি

- [ ] `.gitignore` এবং প্রকাশযোগ্য ফাইলের তালিকা পর্যালোচনা করুন।
- [ ] source-এ ব্যক্তিগত phone number, নাম, customer/employee record, production
  endpoint, database dump, session store, `.env`, API key বা password আছে কি না
  পরীক্ষা করুন।
- [ ] runtime defaults-এ ব্যক্তিগত contact বা production endpoint না রেখে
  environment/configuration-নির্ভর করুন।
- [ ] `.env.example`-এ প্রয়োজনীয় non-secret settings ও optional integration-এর
  উদাহরণ রাখুন; শুধু নাম দেখে প্রতিটি setting বাস্তবে ব্যবহৃত হয় কি না যাচাই করুন।
- [ ] public docs-এ company-specific তথ্য, server path, private business
  instructions ও internal links সরান।
- [ ] tests-এ real data নয়, synthetic fixture ব্যবহার নিশ্চিত করুন।
- [ ] static UI-তে production-derived employee/customer fixture নেই নিশ্চিত করুন।
- [ ] dependency, test, privacy ও secret scan চালান; প্রতিটি finding নিষ্পত্তি
  করুন।
- [ ] exact file list এবং diff মালিককে review-এর জন্য দেখান।
- [ ] review ও আলাদা অনুমোদনের পরে নির্দিষ্ট ফাইল stage/commit করুন এবং
  `scripts/secret_scan.sh`-এর সমতুল্য secret scan পাস করান।
- [ ] commit-এর পরে অনুমতি নিয়ে target remote-এ push করুন; force-push নয়।

## 2. Clean-install database তৈরি ও migration

### 2.1 Database ও migration gate

- [ ] নতুন, খালি PostgreSQL database এবং পৃথক least-privilege app account
  তৈরি করুন; production credentials source control-এ রাখবেন না।
- [ ] runtime-এ ব্যবহৃত migration set নিশ্চিত করুন: `db/migrate.py` শুধু
  `db/migrations/*.sql` চালায়; legacy `migrations/` বা module-local migration
  directories-কে runner চালাবে ধরে নেবেন না।
- [ ] migration inventory তৈরি করে dependency order, duplicate/overlapping
  changes, non-idempotent DDL, production-specific values, seed rows, foreign
  keys/indexes, transaction behavior এবং migration history পরীক্ষা করুন।
- [ ] clean database-এ সব migration বাস্তবে চালিয়ে schema smoke test করুন।
  `--dry-run` কেবল pending file দেখায়; SQL validity বা install success প্রমাণ
  করে না।
- [ ] migration শেষে app-এর প্রত্যেক module-এর প্রয়োজনীয় table, index,
  constraint, view, function ও baseline configuration যাচাই করুন।
- [ ] upgrade test-ও চালান: আগের supported schema snapshot থেকে বর্তমান schema-তে
  migrations ধারাবাহিকভাবে চলে কি না।
- [ ] database install পুনরাবৃত্তিযোগ্য ও versioned না হওয়া পর্যন্ত production
  migration চালাবেন না।

### 2.2 Production migration run

- [ ] সঠিক production target, backup freshness, restore procedure এবং নির্দিষ্ট
  migration filenames লিখিতভাবে নিশ্চিত করুন।
- [ ] আলাদা অনুমোদন নিয়ে migration চালান; command output এবং applied migration
  তালিকা সংরক্ষণ করুন।
- [ ] schema validation ও read-only health checks পাস হলে তবেই app চালু করুন।
- [ ] migration ব্যর্থ হলে পরের migration জোর করে চালাবেন না; error ও database
  state সংরক্ষণ করে থামুন।

**Rollback:** schema পরিবর্তনের স্বয়ংক্রিয় reverse ধরে নেবেন না। আগে থেকে
পরীক্ষিত backup/PITR restore পদ্ধতি ব্যবহার করুন; restore নিজেই data বদলায়, তাই
target, restore point ও প্রভাব যাচাই করে আলাদা অনুমোদন নিতে হবে। অন্যথায়
forward-fix-কে অগ্রাধিকার দিন।

## 3. Application configuration ও নিরাপত্তা

- [ ] runtime configuration `app/config.py`-এর সাথে সামঞ্জস্যপূর্ণ কি না
  যাচাই করুন এবং প্রয়োজনীয় settings-এর deployment checklist তৈরি করুন।
- [ ] API key, database password, AI-provider key ও bridge credentials secret
  manager বা server-side environment-এ রাখুন; browser/localStorage-এ provider
  secret বা database credential রাখবেন না।
- [ ] admin settings UI থেকে secret গ্রহণ করলে server-side encrypted storage,
  masked display, permission check, audit trail ও rotation আচরণ বাস্তবায়ন ও
  পরীক্ষা করুন; নিরাপদ storage না থাকলে secret UI field চালু করবেন না।
- [ ] admin, accountant, escort-client ও অন্যান্য contact-কে configurable
  identity হিসেবে রাখুন; code-এ ব্যক্তিগত number/allowlist রাখবেন না।
- [ ] office contact ও payroll social links-কে public configuration থেকে
  দেখান; unconfigured contact link UI-তে লুকানো থাকে কি না যাচাই করুন।
- [ ] RBAC data ও shared authorization dependency ব্যবহার নিশ্চিত করুন।
- [ ] outbound message, auto-reply ও payment relay শুরুতে বন্ধ রাখুন।

## 4. WhatsApp bridge সংযোগ

- [ ] MCP/bridge API contract, auth, database/table schema, message ID, sender,
  recipient, timestamp, direction, attachment, pagination ও rate-limit support
  নথিভুক্ত করুন।
- [ ] বর্তমান bridge pairings অক্ষত রেখে read-only health check ও connectivity
  test করুন; pairing reset/re-pair করবেন না।
- [ ] bridge number-to-label mapping, accountant contact, escort-client contact
  এবং অন্যান্য monitored contact admin configuration দিয়ে স্থাপন করুন।
- [ ] duplicate delivery, retry, out-of-order messages, timezone, deleted media,
  group chat এবং sender identity mapping-এর test fixture বানান।
- [ ] message ingest-এ idempotency, audit/provenance, access control ও
  fail-closed behavior যাচাই করুন।
- [ ] প্রথমে inbound-only/non-sending mode-এ staging test করুন।

## 5. পুরোনো conversation backfill

- [ ] পুরোনো bridge থেকে data read/export-এর অনুমতি, source table mapping,
  retention, PII handling ও নির্বাচিত date range নিশ্চিত করুন।
- [ ] ছোট synthetic/redacted sample-এ importer চালিয়ে field mapping ও timestamp
  যাচাই করুন।
- [ ] importer-কে read-only source, idempotent destination write এবং
  dry-run/preview সমর্থন করান।
- [ ] backfill চলাকালে outbound sending, AI auto-reply ও payment side effect
  নিষ্ক্রিয় রাখুন।
- [ ] duplicate count, skipped/invalid rows, message order, attachments ও
  source/destination totals মিলিয়ে লিখিত reconciliation করুন।
- [ ] পূর্ণ import-এর আগে target ও data scope-এর জন্য আলাদা অনুমোদন নিন।

## 6. AI provider নির্বাচন ও যাচাই

সমর্থনযোগ্য adapter হিসেবে local Ollama, OpenRouter ও Google Gemini বিবেচনা
করুন; provider-কে interchangeable configuration হিসেবে ধরুন।

- [ ] provider, base URL, model, timeout, token limit, temperature, retry,
  fallback ও rate limit-এর configuration contract নির্ধারণ করুন।
- [ ] provider API key server-side secret storage-এ রাখুন; logs, UI response,
  database plain text বা Git-এ প্রকাশ করবেন না।
- [ ] Ollama-এর local endpoint/model availability পরীক্ষা করুন।
- [ ] OpenRouter ও Gemini-র key, endpoint, model access, billing limit এবং
  retention/privacy terms যাচাই করুন।
- [ ] mock/test provider দিয়ে timeout, malformed response, rate limit, outage,
  fallback, retry এবং secret-redaction পরীক্ষা করুন।
- [ ] employee, candidate ও escort-client workflow-এ আলাদা prompt, data access,
  permission ও human-review boundary যাচাই করুন।
- [ ] staging-এ read-only/draft mode-এ বাস্তবসম্মত test করুন; real contact-এ
  message পাঠাবেন না।

## 7. Staging rehearsal ও release gate

- [ ] production-এর মতো configuration-সহ isolated staging app ও disposable DB
  তৈরি করুন; production data কপি করবেন না।
- [ ] clean migration, app startup, `/health` বা নির্ধারিত health endpoint,
  login/API key, RBAC, frontend route এবং module smoke test চালান।
- [ ] attendance, payroll, escort, recruitment, bridge ingest, AI provider,
  conversation history ও admin settings-এর acceptance test চালান।
- [ ] database backup/restore rehearsal, log redaction ও alerting যাচাই করুন।
- [ ] unit suite চালান; প্রয়োজনীয় integration/e2e suite-ও সংশ্লিষ্ট service
  চালু রেখে সম্পন্ন করুন।
- [ ] release commit, migration set, deployment window, service target ও rollback
  নির্দেশনা review করুন।

## 8. Production deployment

প্রতিটি item-এর আগে নির্দিষ্ট production target ও action-এর আলাদা অনুমোদন
নিতে হবে।

- [ ] pre-deployment backup/restore point যাচাই করুন।
- [ ] approved commit ও artifact-ই deploy হচ্ছে তা যাচাই করুন।
- [ ] approved migration set চালিয়ে schema ও health check পাস করান।
- [ ] deployment-এর পরে application service controlled procedure দিয়ে restart
  করুন; তারপর API health, logs, database connectivity ও bridge read-only check
  নিশ্চিত করুন।
- [ ] bridge inbound চালু করুন; outbound এবং AI auto-reply বন্ধ রেখেই প্রথমে
  monitoring করুন।
- [ ] সব module, authorization, duplicate prevention ও data reconciliation
  পাস হলে আলাদা অনুমোদনে draft/outbound behavior ধাপে ধাপে চালু করুন।
- [ ] real customer-কে automated message পাঠানোর আগে আলাদা explicit approval
  নিন এবং monitored canary test করুন।

## 9. Rollback ও post-deploy monitoring

- [ ] deployment ব্যর্থ হলে app-কে আগের verified artifact-এ ফেরান; migration
  rollback/DB restore কেবল পরীক্ষিত runbook ও explicit approval-এ করুন।
- [ ] bridge message send বন্ধ করুন, কিন্তু pairing/session store মুছবেন না।
- [ ] API errors, DB pool, migration state, queue depth, retries, duplicate
  messages, AI-provider errors, cost/rate limits এবং outbound delivery পর্যবেক্ষণ
  করুন।
- [ ] অনাকাঙ্ক্ষিত data write বা message দেখা গেলে outbound gate বন্ধ করে
  incident record করুন এবং পরবর্তী পরিবর্তনের আগে থামুন।
- [ ] backup retention, access review, secret rotation ও restore drill নির্ধারিত
  cadence-এ করুন।

## সম্পন্নের প্রমাণ

| ধাপ | প্রমাণ | অবস্থা |
|---|---|---|
| Public source review | Sanitized file list, diff, test ও secret-scan report | `[ ]` |
| GitHub release | Approved commit SHA ও target remote push result | `[ ]` |
| Clean install | Empty-DB migration log ও schema smoke-test result | `[ ]` |
| Bridge | Pairing অক্ষত, read-only health ও inbound test | `[ ]` |
| Backfill | Approved scope, reconciliation ও duplicate report | `[ ]` |
| AI providers | Provider-specific connectivity ও failure-mode tests | `[ ]` |
| Staging | Acceptance tests, backup/restore rehearsal, health evidence | `[ ]` |
| Production | Approved commit/migrations, post-deploy health ও monitoring | `[ ]` |
