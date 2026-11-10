# বাকি ১৯টি test — কোনটি আপনার সিদ্ধান্ত, কোনটি টেস্টের ভুল

তারিখ: ২০২৬-১১-১০ · শাখা: `arena/a91f5fb5-friendly-sniffle` · ভিত্তি: `master@8707417`

পুরো `tests/unit` এখন **১৫ failed, ০ error** দেয়, আর প্রতিটি ফাইল আলাদা চালালেও
ঠিক সেই ১৫টিই আসে (order-independent)। ২০২৬-১১-১০-এ ৭ নম্বর গ্রুপের ৪টি
(টেস্টের পুরোনো ভুল) ঠিক করা হয়েছে — ১৯ থেকে ১৫। এই ডকুমেন্টে প্রতিটির (ক) আসলে
কী জানতে চাওয়া হচ্ছে, (খ) কোন ফিচারের কোন ধাপে আটকায় — সেটি ওয়ার্কফ্লো সহ,
(গ) কোন অপশনে কী পরিণতি।

---

## ০. এক নজরে

| # | গ্রুপ | টেস্ট | কে ঠিক করবে |
|---|---|---|---|
| ১ | Private corpus (`knowledge_base/**`, `resources/ops/**`) | ৬ | **আপনার সিদ্ধান্ত** (ডেটা কোথায় থাকবে) |
| ২ | K3.2 reconciliation scripts | ২ | **আপনার সিদ্ধান্ত** (আর একবার চালাবেন কি?) |
| ৩ | `[media:<type>]` আচরণ | ৩ | **আপনার সিদ্ধান্ত** (জমা রাখা vs ফেলে দেওয়া) |
| ৪ | `.gitignore`-এ authority register | ১ | **আপনার সিদ্ধান্ত** (public repo-তে ব্যবসায়িক সংখ্যা?) |
| ৫ | Deployment `.env`-নির্ভর assertion | ২ | আপনার উত্তর দরকার (production-এ সত্যিই ON?) |
| ৬ | পুরোনো টেস্ট, কোড বদলেছে | ~~৪~~ **০** | **সম্পন্ন ২০২৬-১১-১০** |
| ৭ | বাহ্যিক ইনস্টল | ১ | পরিবেশ (`hermes-runner`) |

বাকি মোট: **১৫** = ৬ (corpus) + ২ (K3.2 scripts) + ৩ (media) + ১ (`.gitignore`) +
২ (flags) + ১ (hermes-runner)।

---

## ১. Private corpus — ৬টি টেস্ট

**প্রশ্ন:** কোম্পানির জ্ঞানভাণ্ডার — `knowledge_base/**` (recruitment policy,
workflow, fact authority) আর `resources/ops/**` — কোথায় থাকবে, এবং টেস্ট কি সেটি
দেখতে পাবে?

**আক্রান্ত ফিচার:** (ক) ক্যান্ডিডেটকে recruitment উত্তর, (খ) RAG সার্চ ও উদ্ধৃতি,
(গ) K3.1/K3.2 fact authority।

### ওয়ার্কফ্লো — ক্যান্ডিডেটের প্রশ্ন → উত্তর

```
ক্যান্ডিডেটের WhatsApp/Meta মেসেজ
  └─ app/main.py:_handle_meta_message / modules/bridge_poller.process_bridge_inbound
       └─ modules/message_router  (intent = recruitment)
            └─ modules/hermes_dispatch.generate_recruitment_reply
                 ├─ shared/reply_policy.build_whatsapp_recruitment_policy(kb_context)
                 │    ├─ build_canonical_recruitment_context()
                 │    │     ├─ knowledge_base/01_employee_knowledge/recruitment_policy.md   ← নেই
                 │    │     └─ knowledge_base/05_workflows/recruitment_workflow.md          ← নেই
                 │    └─ modules/recruitment_authority.render_authority_markers()
                 │          └─ knowledge_base/00_governance/recruitment_fact_authority.json ← নেই
                 └─ LLM (Ollama / 9router / Groq …)
       └─ modules/outbound.enqueue → bridge → WhatsApp
```

**ঠিক কোথায় আটকায়:** তিনটি ফাইলই `.gitignore`-এ বাদ দেওয়া, তাই loader-গুলো
`OSError` ধরে `("", "")` ফেরায়। ফল: prompt-এ canonical context খালি → উত্তর
"fail-closed" হয় (ভুল তথ্য যায় না, কিন্তু উত্তর দুর্বল/সাধারণ)। ব্যবহারকারীর
চোখে এটি *নীরব মান-হ্রাস*, error নয় — এটাই সবচেয়ে বিপজ্জনক দিক।

**আক্রান্ত ফিচার:** RAG সার্চ

```
modules/rag.build_index()
  ├─ _load_resource_files()        → resources/ops/**          ← নেই
  ├─ _load_knowledge_base_files()  → knowledge_base/**/*.md     ← নেই
  └─ _load_kb_rows()               → DB: fazle_knowledge_base   (আছে)
        ↓ BM25 + Qdrant
modules/rag.search(q, role="candidate" | "admin")
  role gate: knowledge_base → {"admin", "developer"}   (modules/rag/__init__.py:198)
modules/rag.answer(q, role=...)  → citations
```

`_load_knowledge_base_files()` খালি লিস্ট দেয় ⇒ `test_loads_real_files_admin_only`
(`len(sources) > 0`) আর `test_admin_role_can_see_kb_file_content` (`kb_file:` hit)
লাল।

**অপশন**

| | কী | পরিণতি |
|---|---|---|
| A | corpus আলাদা private repo/storage-এ; টেস্ট skip (এখন যা আছে) | নিরাপদ, কিন্তু ৬টি টেস্ট কখনোই আসল কনটেন্ট যাচাই করে না |
| B | **sanitized fixture corpus** repo-তে (গঠন/শিরোনাম একই, নাম-ফোন-ফি বাদ) | structure + role-permission টেস্ট সত্যিই চলে; আসল সংখ্যার টেস্ট তখনো private corpus ছাড়া চলবে না |
| C | পুরো corpus commit | সুপারিশ নয় — ব্যক্তিগত তথ্য ও ব্যবসায়িক শর্ত public হবে |

---

## ২. K3.2 reconciliation scripts — ২টি টেস্ট

**প্রশ্ন:** K3.2-এর স্ক্রিপ্ট দুটি (`scripts/seed_recruitment_kb.sql`,
`scripts/reconcile_recorded_k32.py`) repo-তে ফিরিয়ে আনব, নাকি K3.2 বাতিল ধরব?

**আক্রান্ত ফিচার:** পুরোনো recruitment FAQ (৬৬টি row) আর governance row-দের
canonical JSON authority-র অধীনে আনা।

### ওয়ার্কফ্লো

```
db/migrations/086_k32_recruitment_reconciliation.sql   (আছে, কিন্তু চালানো হয়নি)
  BEGIN;
  temp table _k32_recruitment_faq_expected (66 id/key, retain = শুধু 117)
  DO $$ precondition $$   ← কোনো row আধা-রিকনসাইল হলে RAISE EXCEPTION + abort
  UPDATE fazle_knowledge_base     SET is_active = FALSE   (retain=false → 65টি)
  UPDATE fazle_knowledge_governance SET status='deprecated', deprecated_at=NOW()  (9টি)
  COMMIT;

scripts/seed_recruitment_kb.sql        ← নেই (idempotent seed, DELETE করে না)
scripts/reconcile_recorded_k32.py      ← নেই (production/data-তে একই কাজ, scoped, idempotent)
```

**ঠিক কোথায় আটকায়:** টেস্ট `SEED.read_text()` আর
`spec_from_file_location("reconcile_recorded_k32", …)` করে ⇒ `FileNotFoundError`।
আর `.gitignore`-এ `/scripts/*` থাকায় কেউ লিখলেও commit হবে না — `apply_schema.py`
একই ফাঁদে পড়েছিল আগে।

**কেন এটা জরুরি:** এখন DB-তে ৬৬টি পুরোনো FAQ row *active* থাকলে উত্তর দুটি
উৎস থেকে আসতে পারে (পুরোনো DB row + canonical JSON) ⇒ দ্বিতীয় policy engine,
যা K3.1 ইচ্ছাকৃতভাবে বন্ধ করেছে।

**অপশন**

| | কী | পরিণতি |
|---|---|---|
| A | স্ক্রিপ্ট দুটি পুনর্গঠন + `.gitignore`-এ un-ignore | disposable DB-তে যাচাই করে চালানো যাবে; ২টি টেস্ট সবুজ |
| B | K3.2 বাতিল; migration 086 + টেস্ট retire | কম কাজ, কিন্তু পুরোনো row-গুলো active থেকে যাবে — recruitment উত্তরে পুরোনো ফি/শর্ত ফিরে আসার ঝুঁকি |

---

## ৩. `[media:<type>]` — ৩টি টেস্ট

**প্রশ্ন:** location / sticker / contacts / reaction / অচেনা (ভবিষ্যতের) Meta
মেসেজ **সংরক্ষণ** করবেন (কিন্তু প্রসেস নয়), নাকি আগের মতো পুরোপুরি **ফেলে**
দেবেন?

**আক্রান্ত ফিচার:** Meta (Messenger/Instagram) inbound pipeline।

### ওয়ার্কফ্লো — এখন

```
Meta webhook → app/main.py:_handle_meta_message
  msg_type ∉ {text, image, audio, document, video}
      → text = "" → text = "[media:location]"        (app/main.py:2985)
  → duplicate check (source_message_ref)
  → _save_message(...)                                ← ★ DB-তে জমা হয়
  → modules/message_router.inbound_execution(...)      ← provenance
  → if msg_type != "text" and not extracted_text:
          outcome = "skipped"; reason = "unsupported_media"; return   ← ★ এখানেই শেষ
     (কোনো reply নেই, admin draft নেই, outbound queue নেই)
```

আগের আচরণ ছিল `if not text: return` ⇒ কিছুই সেভ হতো না।
টেস্টটি পুরোনো আচরণ ধরে (`save.assert_not_awaited()`), তাই লাল।

| | কী | পরিণতি |
|---|---|---|
| A | টেস্ট নতুন আচরণে লিখা: **persist + suppress** | কাস্টমার কী পাঠিয়েছে তার অডিট trail থাকে ("আমি তো পাঠিয়েছিলাম" — প্রমাণ থাকে), ভবিষ্যতে media সাপোর্ট যোগ করলে ডেটা হাতে থাকবে |
| B | পুরোনো drop আচরণ ফেরানো | DB পরিষ্কার থাকে, কিন্তু ওই মেসেজের কোনো রেকর্ডই থাকে না |

সুপারিশ **A** — তবে `[media:…]` টেক্সট যেন LLM/reply-policy-তে না যায় (এখন
যায় না; `_finish_meta_inbound`-এ পৌঁছায়ই না) সেটি একই PR-এ টেস্টে পিন করা।

---

## ৪. `.gitignore`-এ authority register — ১টি টেস্ট

**প্রশ্ন:** `knowledge_base/00_governance/recruitment_fact_authority.json`
(canonical authority) আর `04_business_rules/recruitment_lifecycle_authority.md`
repo-তে **tracked** থাকবে?

**আক্রান্ত ফিচার:** recruitment উত্তরের fact grounding (K3.1)।

```
modules/knowledge_base.get_reply(text, intent)
  → _authority_reply_or_safe_fallback()      (modules/knowledge_base/__init__.py:118)
       → modules/recruitment_authority.facts_for_recruitment_question()
            → knowledge_base/00_governance/recruitment_fact_authority.json
       → answerable?  না হলে  →  "এই বিষয়ে নিশ্চিত তথ্যের জন্য অফিসে যোগাযোগ করুন।"
  → [FAZLE_EVIDENCE] marker → reply_policy prompt → LLM
```

**ঠিক কোথায় আটকায়:** `.gitignore`-এ দুটি নিয়ম বিরোধ করছে —
লাইন ৯১ `/knowledge_base/` (পুরো গাছ ignore) আর লাইন ১৫৯
`…recruitment_fact_authority.json.lock` (শুধু lock)।
`git check-ignore --no-index` এখন register-কেও ignored বলে (rc=0), টেস্ট চায় rc≠0।

| | কী | পরিণতি |
|---|---|---|
| A | register + reviewed md-কে `.gitignore`-এ ব্যতিক্রম | authority সংস্করণ-নিয়ন্ত্রিত, পরিবর্তনের ইতিহাস থাকে — কিন্তু **বয়স/ফি/বেতন public repo-তে** যাবে |
| B | পুরো `knowledge_base/` private রাখা, টেস্ট rewrite | ব্যবসায়িক সংখ্যা গোপন থাকে; authority-র ইতিহাস তখন আলাদা private স্টোরে রাখতে হবে |

এটি ব্যবসায়িক/প্রকাশ-নীতি সিদ্ধান্ত — কোড নয়।

---

## ৫. Deployment `.env`-নির্ভর ২টি assertion

`test_conversation_memory::test_stage1_flags_enabled_stage2_stage3_flags_still_off`
আর `test_phase_c_wireins::test_existing_recruitment_flags_unchanged` দাবি করে
কিছু flag **True** (`CONVERSATION_MEMORY_ENABLED`, `WHATSAPP_THREAD_RECONSTRUCTION`,
`RECRUITMENT_ANSWER_FIRST_ENABLED`, `RECRUITMENT_FACTCHECK_NORMALIZED`) — docstring
বলে "production .env-এ Owner approval-এর পর ON"। কিন্তু কোড ডিফল্ট ও
`.env.example` দুটোই `False`।

**আপনার উত্তর দরকার:** লাইভ production-এ কি এই flag গুলো সত্যিই ON?
- **হ্যাঁ** ⇒ টেস্টের জন্য একটি `tests/fixtures/production_flags.env` যোগ করব (নাম
  ছাড়া, শুধু flag) — টেস্ট তখন সত্যিই deployment চুক্তি যাচাই করবে।
- **না** ⇒ দুটি টেস্ট কোড-ডিফল্ট (False) অনুযায়ী ঠিক করব।

---

## ৬. পুরোনো টেস্ট — কোড নয়, টেস্ট ঠিক করা (৪টি) — ✅ সম্পন্ন (২০২৬-১১-১০)

চারটিই ঠিক করা হয়েছে, কোনো ব্যবসায়িক সিদ্ধান্ত ছাড়াই। `tests/unit` ফল: **১৫ failed**।

| টেস্ট | আসল কারণ | যা করা হলো |
|---|---|---|
| `test_scheduler_phase5b…::test_bridge3_probes…` | টেস্ট hardcoded `/home/azim/whatsapp3/store/messages.db`; প্রোব এখন `settings.bridge3_db_path` পড়ে | sibling টেস্টের মতো `get_settings().bridge3_db_path`-এর সাথে তুলনা |
| `test_escort_source_of_truth_refactor::test_no_match_queues…` | `_is_authoritative_bridge_confirmation()` recipient ছাড়া শুধু **admin** নম্বর দেখে; টেস্ট `BRIDGE2_NUMBER` সেট করত, `ADMIN_BRIDGE2_NUMBER` নয় ⇒ `""` ফিরত | helper-এ `ADMIN_BRIDGE2_NUMBER` যোগ; negative টেস্ট দুটি (ভুল sender / ভুল recipient) এখনো rejection-ই দাবি করে |
| `test_recruitment_ai_restricted::test_meta_recruitment_safe_mode…` | টেস্ট `message_router.inbound_execution`-এর আগের যুগের; mock করা `_save_message` provenance claim-এ None ⇒ enqueue-এর আগেই return | `_save_message` আসল id দেয়, provenance scope stub করা |
| `test_fpe_rules::test_allowlisted_sync_chat…` | gate `FPE_SYNC_CHAT_JIDS` পড়ে, টেস্টে সেটি খালি ⇒ accountant-এর chat-ও blocked | টেস্টে `FPE_SYNC_CHAT_JIDS` সেট + settings cache clear |

## ৭. বাহ্যিক ইনস্টল (১টি)

`test_stage_h_financial_operations::test_customer_mode_toolset…` —
`/home/user/hermes-runner/server.py` দরকার। কোডে সমস্যা নেই; এই স্যান্ডবক্সে
hermes-runner ইনস্টল করা নেই, তাই টেস্টটি environment-gated।

---

## প্রত্যাশিত পরিণতি (সিদ্ধান্ত নিলে)

| সিদ্ধান্ত | সাথে সাথে যা ঠিক হবে | ব্যবহারকারীর উপর প্রভাব |
|---|---|---|
| private corpus (B) | ৪/৬ টেস্ট | RAG উদ্ধৃতি ও role-গেট আসল ডেটায় যাচাই হবে |
| K3.2 scripts (A) | ২ | recruitment উত্তরে পুরোনো FAQ ফিরে আসার ঝুঁকি বন্ধ |
| media (A) | ৩ | কাস্টমার media মেসেজের অডিট trail |
| `.gitignore` (A) | ১ | authority পরিবর্তনের ইতিহাস tracked |
| `.env` flags (উত্তর) | ২ | deployment চুক্তি বনাম কোড ডিফল্ট স্পষ্ট হবে |
| ৬ নম্বর গ্রুপ | ~~৪~~ **৪ ✅** | সম্পন্ন — কোনো ব্যবসায়িক সিদ্ধান্ত ছাড়াই |
