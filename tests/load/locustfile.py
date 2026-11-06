"""
Locust load test file for fazle-core API.

Run:
  locust -f tests/load/locustfile.py --host http://localhost:8200 \
         --headless -u 50 -r 10 --run-time 60s

Environment:
  TEST_API_KEY   — X-Internal-Key header value (default: test-internal-key)
"""
from __future__ import annotations

import os
import json
import random
from locust import HttpUser, task, between, events

# ─── Config ──────────────────────────────────────────────────────────────────
API_KEY = os.getenv("TEST_API_KEY", "test-internal-key")
HEADERS = {"X-Internal-Key": API_KEY, "Content-Type": "application/json"}

# Phones seeded in the load test DB (or replace with real numbers)
GUARD_PHONES = [
    "8801811111111",
    "8801811111112",
    "8801811111113",
]
CLIENT_PHONES = [
    "8801955555551",
    "8801955555552",
]
ADMIN_PHONE = "8801700000001"

VESSELS = ["MV ATLAS", "MV POSEIDON", "MV EAGLE", "MV FALCON"]
LIGHTERS = ["AMENA-1", "AMENA-2", "SAKINA-1", "SAKINA-2"]
SHIFTS = ["D", "N"]  # Day / Night


def _escort_order_payload(client_phone: str) -> dict:
    vessel = random.choice(VESSELS)
    lighter = random.choice(LIGHTERS)
    shift = random.choice(SHIFTS)
    return {
        "from": client_phone,
        "text": (
            f"{vessel} lighter vessel {lighter} "
            f"master mobile 01933333333 wheat 5000MT "
            f"escort lagbe 10/05/2026 {'Day' if shift=='D' else 'Night'}"
        ),
        "source": "bridge1",
    }


def _attendance_payload(guard_phone: str) -> dict:
    return {
        "from": guard_phone,
        "text": f"হাজির আছি MV {random.choice(VESSELS)}",
        "source": "bridge1",
    }


def _advance_payload(guard_phone: str) -> dict:
    amount = random.choice([500, 1000, 1500, 2000])
    return {
        "from": guard_phone,
        "text": f"অগ্রিম লাগবে {amount} টাকা",
        "source": "bridge1",
    }


# ─── User classes ────────────────────────────────────────────────────────────

class ReadOnlyUser(HttpUser):
    """Simulates monitoring dashboard / admin read queries."""
    wait_time = between(0.5, 2.0)

    @task(10)
    def health(self):
        self.client.get("/health", headers=HEADERS, name="/health")

    @task(5)
    def list_employees(self):
        self.client.get("/employees?page=1&limit=20", headers=HEADERS, name="/employees")

    @task(5)
    def list_drafts(self):
        self.client.get("/drafts", headers=HEADERS, name="/drafts")

    @task(4)
    def list_transactions(self):
        self.client.get("/transactions?limit=20", headers=HEADERS, name="/transactions")

    @task(3)
    def list_escort_programs(self):
        self.client.get("/escort-programs?status=Running", headers=HEADERS, name="/escort-programs")

    @task(3)
    def list_attendance(self):
        self.client.get("/attendance", headers=HEADERS, name="/attendance")

    @task(2)
    def list_payroll_runs(self):
        self.client.get("/payroll/runs", headers=HEADERS, name="/payroll/runs")

    @task(2)
    def list_payment_drafts(self):
        self.client.get("/payment-drafts", headers=HEADERS, name="/payment-drafts")


class WebhookUser(HttpUser):
    """Simulates WhatsApp messages arriving via bridges."""
    wait_time = between(1.0, 5.0)

    @task(3)
    def escort_order(self):
        phone = random.choice(CLIENT_PHONES)
        payload = _escort_order_payload(phone)
        self.client.post(
            "/webhook/mcp1",
            headers=HEADERS,
            json=payload,
            name="/webhook/mcp1 [escort]",
        )

    @task(3)
    def attendance_report(self):
        phone = random.choice(GUARD_PHONES)
        payload = _attendance_payload(phone)
        self.client.post(
            "/webhook/mcp1",
            headers=HEADERS,
            json=payload,
            name="/webhook/mcp1 [attendance]",
        )

    @task(2)
    def advance_request(self):
        phone = random.choice(GUARD_PHONES)
        payload = _advance_payload(phone)
        self.client.post(
            "/webhook/mcp1",
            headers=HEADERS,
            json=payload,
            name="/webhook/mcp1 [advance]",
        )

    @task(1)
    def unknown_message(self):
        """Unknown sender — should be handled gracefully (no crash)."""
        self.client.post(
            "/webhook/mcp1",
            headers=HEADERS,
            json={"from": "8801999000000", "text": "হ্যালো", "source": "bridge1"},
            name="/webhook/mcp1 [unknown]",
        )


class MetaWebhookUser(HttpUser):
    """Simulates Meta / WhatsApp Cloud API webhook messages."""
    wait_time = between(2.0, 8.0)

    def _meta_payload(self, phone: str, text: str) -> dict:
        return {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "ENTRY_ID",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "16505551234", "phone_number_id": "PH_ID"},
                        "contacts": [{"profile": {"name": "Test User"}, "wa_id": phone}],
                        "messages": [{
                            "from": phone,
                            "id": f"wamid.{random.randint(100000, 999999)}",
                            "timestamp": "1714900000",
                            "text": {"body": text},
                            "type": "text",
                        }],
                    },
                    "field": "messages",
                }],
            }],
        }

    @task(1)
    def text_message(self):
        phone = random.choice(GUARD_PHONES + CLIENT_PHONES)
        payload = self._meta_payload(phone, "হ্যালো")
        self.client.post(
            "/webhook/meta",
            headers=HEADERS,
            json=payload,
            name="/webhook/meta",
        )


# ─── Threshold assertions (used in CI smoke load test) ───────────────────────

@events.quitting.add_listener
def on_quitting(environment, **kwargs):
    """Fail the load test if p99 response time > 2000ms or error rate > 1%."""
    stats = environment.runner.stats

    max_p99_ms = 2000
    max_fail_ratio = 0.01

    failed = False
    for name, stat in stats.entries.items():
        p99 = stat.get_response_time_percentile(0.99)
        if p99 is not None and p99 > max_p99_ms:
            print(f"[FAIL] {name}: p99 {p99}ms > threshold {max_p99_ms}ms")
            failed = True

    total = stats.total
    if total.num_requests > 0:
        fail_ratio = total.num_failures / total.num_requests
        if fail_ratio > max_fail_ratio:
            print(f"[FAIL] Error rate {fail_ratio:.2%} > threshold {max_fail_ratio:.2%}")
            failed = True

    if failed:
        environment.process_exit_code = 1
