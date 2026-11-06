"""Deterministic intent classifier for social auto-reply."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Classification:
    intent: str
    confidence: float
    reason: str = ""


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(term in lower for term in terms)


def classify_message(text: str, *, media_flag: bool = False, platform: str = "") -> Classification:
    raw = (text or "").strip()
    lower = raw.lower()
    if media_flag and not raw:
        return Classification("media_only", 0.99, "media without text")
    if not raw:
        return Classification("unclear", 0.9, "blank message")
    if _has_any(lower, ("accountant", "হিসাব", "cash entry", "admin cash", "ক্যাশ", "ledger")):
        return Classification("accountant", 0.94, "accountant/internal finance keyword")
    if _has_any(lower, ("employee id", "employee no", "emp id", "কর্মচারী আইডি", "আইডি নম্বর")):
        return Classification("employee_id", 0.94, "employee id keyword")
    if _has_any(lower, ("report", "রিপোর্ট", "dashboard", "payroll", "বেতন তালিকা")):
        return Classification("reports_issue", 0.9, "report/payroll keyword")
    if _has_any(lower, ("escort", "mother vessel", "lighter", "roster", "program", "programme", "guard dibo", "guard a dibo", "গার্ড", "ডিউটি", "জাহাজের ফর্ম")):
        return Classification("escort_order", 0.94, "escort/order keyword")
    if _has_any(lower, ("transaction", "trxid", "trx id", "bkash", "b-kash", "nagad", "rocket", "ট্রানজেকশন", "লেনদেন")):
        return Classification("transaction", 0.94, "transaction keyword")
    # Employee demanding their own salary/payment — step-by-step flow (must be before payment_issue)
    if _has_any(lower, (
        "বেতন দেন", "salary দেন", "টাকা দেন", "payment দেন",
        "বেতন পাই নাই", "টাকা পাই নাই", "salary হয়নি", "বেতন হয়নি",
        "টাকা লাগবে", "বেতন কবে", "টাকা কবে দিবেন", "টাকা কবে পাব",
        "বেতন পাইনি", "টাকা পাইনি", "আমার বেতন", "আমার টাকা",
        "my salary", "salary দাও", "টাকা চাই", "বেতন চাই",
    )):
        return Classification("employee_salary_complaint", 0.93, "employee salary demand keyword")
    # Salary objection — recruitment context (trainee low pay pushback)
    if _has_any(lower, ("বেতন কম", "salary কম", "কম ভাই", "এত কম কেন", "টাকা কম", "কম বেতন", "এত কম")):
        return Classification("salary_objection", 0.88, "salary objection keyword")
    if _has_any(lower, ("টাকা পাইনি", "payment", "salary issue", "বেতন পাইনি", "salary correction", "advance paid", "paid", "পেমেন্ট")):
        return Classification("payment_issue", 0.92, "payment issue keyword")
    if _has_any(lower, ("ভুয়া", "ফেক", "fake", "scam", "ধান্দা", "মিথ্যা", "fraud")):
        return Classification("scam_allegation", 0.95, "scam/fake allegation")
    if _has_any(lower, ("মামলা", "আইন", "legal", "court", "police", "পুলিশ")):
        return Classification("legal_issue", 0.9, "legal keyword")
    if _has_any(lower, ("শালা", "চুদ", "হারাম", "abuse", "threat", "মারবো", "মেরে ফেল", "ধমকি")):
        return Classification("abuse", 0.9, "abusive keyword")
    if _has_any(lower, ("অভিযোগ", "complaint", "ভুক্তভোগী", "কথাই কাজে মিল নাই")):
        return Classification("complaint", 0.9, "complaint keyword")
    if _has_any(lower, ("জামানত", "ঘুষ", "টাকা লাগে", "fee", "processing", "joining fee", "খরচ লাগবে")):
        return Classification("fees", 0.92, "fee keyword")
    if _has_any(lower, ("salary", "বেতন", "চেলারি", "sallery")):
        return Classification("salary", 0.88, "salary keyword")
    if _has_any(lower, ("address", "location", "office", "কোথায়", "কোথায়", "ঠিকানা", "koi", "kothay", "আসবো")):
        return Classification("location", 0.9, "location keyword")
    if _has_any(lower, ("document", "কাগজ", "nid", "জন্ম নিবন্ধন", "ছবি", "required documents")):
        return Classification("documents", 0.88, "documents keyword")
    if _has_any(lower, ("বয়স", "বয়স", "age", "৫০", "55", "৫৫", "চাকরি পাবো")):
        return Classification("age_issue", 0.82, "age keyword")
    if _has_any(lower, ("জাহাজে নাকি", "ship e naki", "পানিতে", "মাটিতে", "ল্যান্ড", "সাগরে", "ship না office", "ship e", "office e")):
        return Classification("job_details", 0.93, "ship/office clarification")
    if _has_any(lower, ("job details", "কাজ কী", "কি কাজ", "job ta", "survey scout", "details", "বিস্তারিত")):
        return Classification("job_details", 0.86, "job details keyword")
    if _has_any(lower, ("training", "ট্রেনিং", "শিখ", "experience nai", "অভিজ্ঞতা নাই", "new")):
        return Classification("training", 0.86, "training keyword")
    if _has_any(lower, ("join", "joining", "কিভাবে জয়েন", "যোগদান", "কবে আসবো", "process", "প্রক্রিয়া")):
        return Classification("join_process", 0.86, "joining process keyword")
    if _has_any(lower, ("follow up", "again", "আবার", "reply দেন", "update", "যোগাযোগ")):
        return Classification("recruitment_follow_up", 0.78, "recruitment follow-up keyword")
    if _looks_like_applicant_info(raw):
        return Classification("applicant_info_complete", 0.82, "name/age/education/address pattern")
    if _has_any(lower, ("interested", "apply", "আমি করতে চাই", "job চাই", "আবেদন", "আমি করবো", "করতে চাই", "কাজ করিতে চাই", "কাজ করতে চাই", "চাকরি করতে চাই")):
        return Classification("interested", 0.88, "interest keyword")
    if lower in {"hi", "hello", "assalamualaikum", "আসসালামু আলাইকুম", "হ্যালো", "start"} or _has_any(lower, ("সালাম", "hello", "hi")):
        return Classification("greeting", 0.8, "greeting keyword")
    return Classification("unclear", 0.45, "no deterministic match")


def classify_comment(text: str) -> Classification:
    cls = classify_message(text, platform="facebook_comment")
    if cls.intent in {"scam_allegation", "complaint", "abuse"}:
        return Classification("negative_comment", cls.confidence, cls.reason)
    # Salary/payment complaints in comments → redirect to inbox (not a public reply)
    if cls.intent in {"employee_salary_complaint", "payment_issue"}:
        return Classification("comment_salary_redirect", cls.confidence, "salary in comment — redirect to inbox")
    return cls


def _looks_like_applicant_info(text: str) -> bool:
    lower = text.lower()
    has_age = bool(re.search(r"\b(age|বয়স|বয়স)\b|\b[1-6][0-9]\b", lower))
    has_education = any(x in lower for x in ("ssc", "hsc", "s.s.c", "class", "ক্লাস", "শিক্ষা", "যোগ্যতা", "অনার্স"))
    has_address = any(x in lower for x in ("address", "ঠিকানা", "জেলা", "থানা", "chatt", "dhaka", "noakhali", "চট্টগ্রাম"))
    has_name = any(x in lower for x in ("name", "নাম")) or bool(re.match(r"^[a-zA-Z .]{5,}", text.strip()))
    return sum((has_age, has_education, has_address, has_name)) >= 3
