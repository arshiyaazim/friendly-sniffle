"""
Playwright E2E test suite for the Fazle Core admin dashboard.

Uses pytest-playwright's sync API to avoid asyncio event-loop conflicts with
pytest-asyncio (asyncio_mode=auto).

Prerequisites
-------------
  playwright install chromium
  export TEST_APP_URL=http://localhost:8200
  export TEST_API_KEY=<your INTERNAL_API_KEY>

Run
---
  make test-e2e
  # or directly:
  TEST_API_KEY=fk_xxx pytest tests/e2e/ -m e2e -v --html=tests/e2e/report.html
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Generator

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Request, Response, expect

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_URL  = os.getenv("TEST_APP_URL", "http://localhost:8200")
API_KEY  = os.getenv("TEST_API_KEY", "test-internal-key")
BAD_KEY  = "WRONG-BAD-KEY-000"
LS_KEY   = "fazle_api_key"
DASH     = "/dashboard"

SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

ALL_TABS: list[tuple[str, str]] = [
    ("overview",      "Overview"),
    ("drafts",        "Drafts"),
    ("gaps",          "Gaps"),
    ("conversations", "Conversations"),
    ("recruitment",   "Recruitment"),
    ("payroll",       "Payroll"),
    ("escort",        "Escort Duty"),
    ("transactions",  "Transactions"),
    ("attendance",    "Attendance"),
    ("reports",       "Reports"),
    ("users",         "Users (B19)"),
    ("audit",         "Audit"),
    ("backups",       "Backups"),
    ("scheduler",     "Scheduler"),
    ("rag",           "RAG (B21)"),
    ("obs",           "Observability (B22)"),
    ("chat",          "Chat"),
]

AUTOLOAD_TABS: list[str] = [
    "overview", "drafts", "gaps", "recruitment", "escort",
    "transactions", "attendance", "users", "audit", "backups",
    "scheduler", "rag", "obs",
]

TAB_BOX: dict[str, str] = {
    "overview":    "overviewStats",
    "drafts":      "draftsBox",
    "gaps":        "gapsSummary",
    "recruitment": "recruitmentBox",
    "escort":      "escortBox",
    "transactions":"txnBox",
    "attendance":  "attendBox",
    "users":       "usersBox",
    "audit":       "auditBox",
    "backups":     "backupsBox",
    "scheduler":   "schedulerBox",
    "rag":         "ragStatsBox",
    "obs":         "obsSummaryBox",
}

CRITICAL_API_PATHS = [
    "/admin/overview", "/admin/drafts", "/admin/payment-drafts",
    "/admin/reviewed-replies", "/admin/gaps", "/admin/recruitment",
    "/admin/cash-transactions", "/admin/attendance", "/admin/users",
    "/admin/audit", "/backup/list", "/backup/status",
    "/scheduler/status", "/rag/stats",
    "/observability/summary", "/observability/errors", "/reports",
]

LOADING = "Loading\u2026"

pytestmark = pytest.mark.e2e

# ---------------------------------------------------------------------------
# pytest-playwright hooks (applied before any test)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict) -> dict:
    browser_type_launch_args.update({
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    })
    return browser_type_launch_args


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    browser_context_args.update({
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-US",
    })
    return browser_context_args

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inject_key(page: Page, key: str = API_KEY) -> None:
    """Load dashboard, inject key, reload until overlay gone."""
    page.goto(f"{APP_URL}{DASH}")
    page.evaluate("([k,v]) => localStorage.setItem(k,v)", [LS_KEY, key])
    page.reload()
    page.wait_for_selector("#loginOverlay", state="hidden", timeout=12_000)


def _tab(page: Page, tab_key: str) -> None:
    page.click(f'nav button[data-tab="{tab_key}"]')
    page.wait_for_load_state("networkidle", timeout=15_000)


def _shot(page: Page, name: str) -> None:
    page.screenshot(path=str(SCREENSHOT_DIR / f"{name}.png"), full_page=True)


def _row_count(page: Page, box_id: str) -> int:
    return page.locator(f"#{box_id} tbody tr:visible, #{box_id} tr:visible").count()

# ---------------------------------------------------------------------------
# Shared auth fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_page(page: Page) -> Generator[Page, None, None]:
    """Page pre-loaded with valid API key; attaches error/failure lists."""
    js_errors: list[str] = []
    net_failures: list[str] = []

    page.on("pageerror", lambda e: js_errors.append(f"[exception] {e}"))
    page.on("console",   lambda m: js_errors.append(f"[console.error] {m.text}")
                         if m.type == "error" else None)
    page.on("response",      lambda r: net_failures.append(f"HTTP {r.status} {r.url}")
                             if r.status >= 400 else None)
    page.on("requestfailed", lambda r: net_failures.append(f"FAILED {r.failure} {r.url}"))

    page._js_errors    = js_errors     # type: ignore[attr-defined]
    page._net_failures = net_failures  # type: ignore[attr-defined]

    _inject_key(page)
    yield page


@pytest.fixture
def bare_page(page: Page) -> Generator[Page, None, None]:
    """Page with no localStorage key set."""
    page.goto(f"{APP_URL}{DASH}")
    page.evaluate("(k) => localStorage.removeItem(k)", LS_KEY)
    page.reload()
    yield page

# ===========================================================================
# Suite 1 — Auth flow
# ===========================================================================

class TestAuthFlow:

    def test_no_key_shows_overlay(self, bare_page: Page) -> None:
        expect(bare_page.locator("#loginOverlay")).to_be_visible(timeout=5_000)

    def test_overlay_has_key_input(self, bare_page: Page) -> None:
        expect(bare_page.locator("#keyInput")).to_be_visible(timeout=5_000)

    def test_wrong_key_shows_error(self, bare_page: Page) -> None:
        bare_page.fill("#keyInput", BAD_KEY)
        bare_page.click("button:has-text('Login')")
        expect(bare_page.locator("#loginErr")).not_to_be_empty(timeout=12_000)

    def test_correct_key_hides_overlay(self, page: Page) -> None:
        page.goto(f"{APP_URL}{DASH}")
        page.evaluate("(k) => localStorage.removeItem(k)", LS_KEY)
        page.reload()
        page.fill("#keyInput", API_KEY)
        page.click("button:has-text('Login')")
        expect(page.locator("#loginOverlay")).to_be_hidden(timeout=15_000)

    def test_key_persisted_after_login(self, page: Page) -> None:
        _inject_key(page)
        stored = page.evaluate("(k) => localStorage.getItem(k)", LS_KEY)
        assert stored == API_KEY, f"Key not persisted: {stored!r}"

    def test_reload_with_stored_key_skips_overlay(self, page: Page) -> None:
        _inject_key(page)
        page.reload()
        expect(page.locator("#loginOverlay")).to_be_hidden(timeout=12_000)

    def test_logout_clears_key_and_shows_overlay(self, page: Page) -> None:
        _inject_key(page)
        page.click("button:has-text('Logout')")
        expect(page.locator("#loginOverlay")).to_be_visible(timeout=5_000)
        stored = page.evaluate("(k) => localStorage.getItem(k)", LS_KEY)
        assert stored is None, f"Key still present after logout: {stored!r}"

    def test_x_internal_key_header_sent(self, auth_page: Page) -> None:
        captured: list[str] = []

        def _check(req: Request) -> None:
            if "/admin/overview" in req.url:
                captured.append(req.headers.get("x-internal-key", ""))

        auth_page.on("request", _check)
        auth_page.evaluate("loadOverview()")
        auth_page.wait_for_load_state("networkidle", timeout=12_000)
        assert any(k == API_KEY for k in captured), (
            f"X-Internal-Key missing or wrong; captured={captured}"
        )

# ===========================================================================
# Suite 2 — Navigation
# ===========================================================================

class TestNavigation:

    def test_all_tab_buttons_present(self, auth_page: Page) -> None:
        for tab_key, _ in ALL_TABS:
            n = auth_page.locator(f'nav button[data-tab="{tab_key}"]').count()
            assert n == 1, f"Nav button missing for tab '{tab_key}'"

    def test_default_active_is_overview(self, auth_page: Page) -> None:
        attr = auth_page.locator("nav button.active").first.get_attribute("data-tab")
        assert attr == "overview", f"Expected overview active, got '{attr}'"

    @pytest.mark.parametrize("tab_key,label", ALL_TABS)
    def test_click_sets_active(self, auth_page: Page, tab_key: str, label: str) -> None:
        _tab(auth_page, tab_key)
        active = auth_page.locator("nav button.active").get_attribute("data-tab")
        assert active == tab_key, f"Expected '{tab_key}' active, got '{active}'"

    @pytest.mark.parametrize("tab_key,label", ALL_TABS)
    def test_section_visible_after_click(self, auth_page: Page, tab_key: str, label: str) -> None:
        _tab(auth_page, tab_key)
        expect(auth_page.locator(f"#tab-{tab_key}")).to_be_visible(timeout=5_000)

    def test_browser_back_restores_tab(self, auth_page: Page) -> None:
        _tab(auth_page, "drafts")
        _tab(auth_page, "users")
        auth_page.go_back()
        auth_page.wait_for_load_state("networkidle", timeout=10_000)
        # any recognised tab should be active
        active = auth_page.locator("nav button.active").first.get_attribute("data-tab")
        assert active in ("drafts", "overview"), f"Unexpected tab after back: {active}"

# ===========================================================================
# Suite 3 — No errors, no 404s, no failed fetches
# ===========================================================================

class TestNoErrors:

    def test_no_js_exceptions_across_all_tabs(self, browser: Browser) -> None:
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg  = ctx.new_page()
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        _inject_key(pg)
        for tab_key, _ in ALL_TABS:
            _tab(pg, tab_key)
            _shot(pg, f"noerr_{tab_key}")
        ctx.close()
        assert errors == [], "JS exceptions:\n" + "\n".join(errors)

    def test_no_404_on_critical_api_paths(self, browser: Browser) -> None:
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg  = ctx.new_page()
        api_404s: list[str] = []

        def _chk(r: Response) -> None:
            if r.status == 404 and any(p in r.url for p in CRITICAL_API_PATHS):
                api_404s.append(f"404 {r.url}")

        pg.on("response", _chk)
        _inject_key(pg)
        for tab_key, _ in ALL_TABS:
            _tab(pg, tab_key)
        ctx.close()
        assert api_404s == [], "404 responses:\n" + "\n".join(api_404s)

    def test_no_failed_network_requests(self, browser: Browser) -> None:
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg  = ctx.new_page()
        fails: list[str] = []
        pg.on("requestfailed", lambda r: fails.append(f"{r.failure} {r.url}"))
        _inject_key(pg)
        for tab_key, _ in ALL_TABS:
            _tab(pg, tab_key)
        ctx.close()
        real = [f for f in fails if "/favicon.ico" not in f]
        assert real == [], "Network failures:\n" + "\n".join(real)

    def test_no_console_errors_on_initial_load(self, browser: Browser) -> None:
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg  = ctx.new_page()
        console_errors: list[str] = []
        pg.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        _inject_key(pg)
        pg.wait_for_load_state("networkidle", timeout=15_000)
        ctx.close()
        assert console_errors == [], "console.error on load:\n" + "\n".join(console_errors)

    def test_no_5xx_on_critical_paths(self, browser: Browser) -> None:
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg  = ctx.new_page()
        errs: list[str] = []

        def _chk(r: Response) -> None:
            if r.status >= 500 and any(p in r.url for p in CRITICAL_API_PATHS):
                errs.append(f"{r.status} {r.url}")

        pg.on("response", _chk)
        _inject_key(pg)
        for tab_key, _ in ALL_TABS:
            _tab(pg, tab_key)
        ctx.close()
        assert errs == [], "5xx responses:\n" + "\n".join(errs)

# ===========================================================================
# Suite 4 — Non-empty responses
# ===========================================================================

class TestNonEmptyResponses:

    @pytest.mark.parametrize("tab_key", AUTOLOAD_TABS)
    def test_box_exits_loading(self, auth_page: Page, tab_key: str) -> None:
        _tab(auth_page, tab_key)
        box_id = TAB_BOX.get(tab_key)
        if not box_id:
            pytest.skip(f"No box for '{tab_key}'")
        expect(auth_page.locator(f"#{box_id}")).not_to_have_text(LOADING, timeout=15_000)

    @pytest.mark.parametrize("tab_key", AUTOLOAD_TABS)
    def test_box_not_blank(self, auth_page: Page, tab_key: str) -> None:
        _tab(auth_page, tab_key)
        box_id = TAB_BOX.get(tab_key)
        if not box_id:
            pytest.skip(f"No box for '{tab_key}'")
        expect(auth_page.locator(f"#{box_id}")).not_to_be_empty(timeout=15_000)

    def test_overview_stats_has_digits(self, auth_page: Page) -> None:
        _tab(auth_page, "overview")
        text = auth_page.locator("#overviewStats").inner_text()
        assert re.search(r"\d", text), f"No digits in overviewStats: {text!r}"

    def test_mode_badge_populated(self, auth_page: Page) -> None:
        text = auth_page.locator("#modeBadge").inner_text().strip()
        assert text not in ("", "\u2026"), f"modeBadge blank: {text!r}"

    def test_reports_dropdown_has_options(self, auth_page: Page) -> None:
        _tab(auth_page, "reports")
        sel = auth_page.locator("#reportName")
        expect(sel).not_to_have_text("loading\u2026", timeout=12_000)
        assert sel.locator("option").count() >= 1

    def test_users_box_has_rows(self, auth_page: Page) -> None:
        _tab(auth_page, "users")
        box = auth_page.locator("#usersBox")
        expect(box).not_to_have_text(LOADING, timeout=12_000)
        assert box.locator("tr").count() >= 1

    def test_scheduler_box_has_content(self, auth_page: Page) -> None:
        _tab(auth_page, "scheduler")
        box = auth_page.locator("#schedulerBox")
        expect(box).not_to_have_text(LOADING, timeout=12_000)
        assert box.locator("tr, li").count() >= 1

    def test_backups_box_has_rows(self, auth_page: Page) -> None:
        _tab(auth_page, "backups")
        box = auth_page.locator("#backupsBox")
        expect(box).not_to_have_text(LOADING, timeout=12_000)
        assert box.locator("tr").count() >= 1

    def test_rag_stats_not_empty(self, auth_page: Page) -> None:
        _tab(auth_page, "rag")
        box = auth_page.locator("#ragStatsBox")
        expect(box).not_to_have_text(LOADING, timeout=12_000)
        text = box.inner_text().strip()
        assert text not in ("", "\u2014"), f"ragStatsBox empty: {text!r}"

    def test_obs_errors_box_renders(self, auth_page: Page) -> None:
        _tab(auth_page, "obs")
        expect(auth_page.locator("#obsErrorsBox")).not_to_be_empty(timeout=12_000)

    def test_gaps_alerts_box_renders(self, auth_page: Page) -> None:
        _tab(auth_page, "gaps")
        expect(auth_page.locator("#gapsAlertsBox")).not_to_be_empty(timeout=12_000)

# ===========================================================================
# Suite 5 — Tables render
# ===========================================================================

class TestTablesRender:

    def _check(self, page: Page, tab_key: str, box_id: str) -> None:
        _tab(page, tab_key)
        box = page.locator(f"#{box_id}")
        expect(box).not_to_have_text(LOADING, timeout=12_000)
        n = box.locator("table, tr, .chip, .summary-chip, .stat, .empty-state").count()
        assert n >= 1, f"#{box_id} empty after loading '{tab_key}'"

    def test_overview_stats_grid(self, auth_page: Page) -> None:
        self._check(auth_page, "overview", "overviewStats")

    def test_drafts_box(self, auth_page: Page) -> None:
        self._check(auth_page, "drafts", "draftsBox")

    def test_gaps_summary(self, auth_page: Page) -> None:
        self._check(auth_page, "gaps", "gapsSummary")

    def test_recruitment_table(self, auth_page: Page) -> None:
        self._check(auth_page, "recruitment", "recruitmentBox")

    def test_escort_table(self, auth_page: Page) -> None:
        self._check(auth_page, "escort", "escortBox")

    def test_transactions_table(self, auth_page: Page) -> None:
        self._check(auth_page, "transactions", "txnBox")

    def test_attendance_table(self, auth_page: Page) -> None:
        self._check(auth_page, "attendance", "attendBox")

    def test_users_table(self, auth_page: Page) -> None:
        self._check(auth_page, "users", "usersBox")

    def test_audit_table(self, auth_page: Page) -> None:
        self._check(auth_page, "audit", "auditBox")

    def test_backups_table(self, auth_page: Page) -> None:
        self._check(auth_page, "backups", "backupsBox")

    def test_scheduler_table(self, auth_page: Page) -> None:
        self._check(auth_page, "scheduler", "schedulerBox")

    def test_obs_summary(self, auth_page: Page) -> None:
        self._check(auth_page, "obs", "obsSummaryBox")

    def test_pay_drafts_adj_rev_buttons(self, auth_page: Page) -> None:
        _tab(auth_page, "drafts")
        box = auth_page.locator("#payDraftsBox")
        expect(box).not_to_have_text(LOADING, timeout=12_000)
        if box.locator("tr").count() <= 1:
            pytest.skip("No payment draft rows to inspect")
        assert box.locator("button:has-text('Adj')").count() >= 1, "Adj buttons missing"
        assert box.locator("button:has-text('Rev')").count() >= 1, "Rev buttons missing"

# ===========================================================================
# Suite 6 — Filters
# ===========================================================================

class TestFilters:

    def _filter_reduces_rows(self, page: Page, tab_key: str, box_id: str, input_id: str) -> None:
        _tab(page, tab_key)
        expect(page.locator(f"#{box_id}")).not_to_have_text(LOADING, timeout=12_000)
        before = _row_count(page, box_id)
        page.fill(f"#{input_id}", "ZZZZ_NO_MATCH_XYZ")
        page.wait_for_timeout(500)
        after = _row_count(page, box_id)
        assert after == 0 or after < before, f"Filter had no effect: before={before} after={after}"

    def test_recruitment_search(self, auth_page: Page) -> None:
        self._filter_reduces_rows(auth_page, "recruitment", "recruitmentBox", "recruitmentSearch")

    def test_escort_search(self, auth_page: Page) -> None:
        self._filter_reduces_rows(auth_page, "escort", "escortBox", "escortSearch")

    def test_escort_status_dropdown(self, auth_page: Page) -> None:
        _tab(auth_page, "escort")
        sel = auth_page.locator("#escortStatusFilter")
        expect(sel).to_be_visible(timeout=8_000)
        assert sel.locator("option").count() >= 2

    def test_transactions_search(self, auth_page: Page) -> None:
        self._filter_reduces_rows(auth_page, "transactions", "txnBox", "txnSearch")

    def test_transactions_method_dropdown(self, auth_page: Page) -> None:
        _tab(auth_page, "transactions")
        expect(auth_page.locator("#txnMethodFilter")).to_be_visible(timeout=8_000)
        assert auth_page.locator("#txnMethodFilter option").count() >= 2

    def test_transactions_type_dropdown(self, auth_page: Page) -> None:
        _tab(auth_page, "transactions")
        expect(auth_page.locator("#txnTypeFilter")).to_be_visible(timeout=8_000)
        assert auth_page.locator("#txnTypeFilter option").count() >= 2

    def test_attendance_search(self, auth_page: Page) -> None:
        self._filter_reduces_rows(auth_page, "attendance", "attendBox", "attendSearch")

    def test_conversation_direction_dropdown(self, auth_page: Page) -> None:
        _tab(auth_page, "conversations")
        sel = auth_page.locator("#conversationDirectionFilter")
        expect(sel).to_be_visible(timeout=8_000)
        assert sel.locator("option").count() >= 2

    def test_conversation_limit_dropdown(self, auth_page: Page) -> None:
        _tab(auth_page, "conversations")
        expect(auth_page.locator("#conversationLimitSelect")).to_be_visible(timeout=8_000)

    def test_conversation_issue_filter(self, auth_page: Page) -> None:
        _tab(auth_page, "conversations")
        expect(auth_page.locator("#conversationIssueFilter")).to_be_visible(timeout=8_000)

    def test_audit_filter_survives_no_match(self, auth_page: Page) -> None:
        _tab(auth_page, "audit")
        expect(auth_page.locator("#auditBox")).not_to_have_text(LOADING, timeout=12_000)
        auth_page.fill("#auditCmd", "ZZZZ_NO_MATCH_XYZ")
        auth_page.fill("#auditLimit", "5")
        auth_page.click('nav button[data-tab="audit"]')
        auth_page.wait_for_load_state("networkidle", timeout=12_000)
        expect(auth_page.locator("#auditBox")).not_to_have_text(LOADING, timeout=12_000)

# ===========================================================================
# Suite 7 — Broken navigation guards
# ===========================================================================

class TestBrokenNavigation:

    def test_health_returns_200(self, page: Page) -> None:
        resp = page.goto(f"{APP_URL}/health")
        assert resp and resp.status == 200, f"/health returned {resp.status if resp else 'none'}"

    def test_dashboard_root_returns_200(self, page: Page) -> None:
        resp = page.goto(f"{APP_URL}{DASH}")
        assert resp and resp.status == 200

    def test_admin_api_without_key_returns_401_or_403(self, page: Page) -> None:
        resp = page.goto(f"{APP_URL}/admin/overview")
        assert resp and resp.status in (401, 403), (
            f"/admin/overview without key returned {resp.status}"
        )

    @pytest.mark.parametrize("tab_key", ["drafts", "users", "rag", "audit"])
    def test_direct_slug_url_activates_correct_tab(self, page: Page, tab_key: str) -> None:
        _inject_key(page)
        page.goto(f"{APP_URL}{DASH}/{tab_key}")
        page.wait_for_load_state("networkidle", timeout=10_000)
        active = page.locator("nav button.active").get_attribute("data-tab")
        assert active == tab_key, f"/dashboard/{tab_key} activated '{active}'"

    def test_unknown_slug_falls_back_to_overview(self, page: Page) -> None:
        _inject_key(page)
        page.goto(f"{APP_URL}{DASH}/unknown-slug-xyz-999")
        page.wait_for_load_state("networkidle", timeout=10_000)
        active = page.locator("nav button.active").get_attribute("data-tab")
        assert active in ("overview", None), f"Unknown slug activated '{active}'"

    def test_rapid_tab_switch_leaves_section_visible(self, auth_page: Page) -> None:
        for tab_key, _ in ALL_TABS:
            auth_page.click(f'nav button[data-tab="{tab_key}"]')
        auth_page.wait_for_load_state("networkidle", timeout=15_000)
        visible = auth_page.locator("section.tab:visible").count()
        assert visible >= 1, "No tab section visible after rapid tab switching"

# ===========================================================================
# Suite 8 — Per-tab screenshot tour
# ===========================================================================

class TestPerTabScreenshots:

    def test_screenshot_tour_no_crashes(self, browser: Browser) -> None:
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg  = ctx.new_page()
        crashes: list[str] = []
        pg.on("pageerror", lambda e: crashes.append(str(e)))
        _inject_key(pg)
        _shot(pg, "00_overview_initial")
        for i, (tab_key, _) in enumerate(ALL_TABS, start=1):
            _tab(pg, tab_key)
            _shot(pg, f"{i:02d}_{tab_key}")
        ctx.close()
        assert crashes == [], "Crashes during screenshot tour:\n" + "\n".join(crashes)
