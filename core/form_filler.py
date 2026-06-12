"""
core/form_filler.py — 50kloans.com multi-step form automation.

The form lives entirely inside an iframe.global iframe.
All Playwright interactions use the Frame object, not the Page object.

Confirmed step map (discovered by diagnose_50k.py):
  00 (1%)   How Much Do You Need?          → loan amount chip
  01 (3%)   Enter Your Email Address       → email + CONTINUE
  02 (7%)   Last 4 Digits of Your SSN      → last_ssn text + CONTINUE
  03 (14%)  What Is Your Credit Score?     → credit chip
  04 (17%)  Your Legal Name                → first_name + last_name + CONTINUE
  05 (21%)  Date Of Birth                  → dob text MM/DD/YYYY + CONTINUE
  06 (24%)  What Is Your ZIP Code?         → zip + CONTINUE
  07 (28%)  What Is Your Street Address?   → street_address + city + state select + CONTINUE
  08 (31%)  Source of Income               → chip: Employed
  09 (34%)  Active in Military?            → chip: No
  10 (38%)  How Often Are You Paid?        → pay frequency chip
  11 (41%)  Monthly Gross Income           → monthly_income + CONTINUE
  12 (45%)  Next Pay Date                  → chip "Next scheduled date" + CONTINUE
  13 (48%)  Employer Information           → employer_name + job_title + employer_phone + CONTINUE
  14 (52%)  How Is Your Paycheck Received? → chip: Direct Deposit
  15 (55%)  ABA Routing Number             → routing_number + CONTINUE
  16 (59%)  Bank Name                      → bank_name text + CONTINUE
  17 (62%)  Type of Bank Account           → chip: Checking / Savings
  18 (66%)  Length of Bank Account         → chip: More than 2 years
  19+       Phone, DL, loan purpose, account number, etc.
"""
from __future__ import annotations

from datetime import datetime
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import structlog
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Frame, Page

from core.exceptions import FormFillerError
from core.site_classify import (
    click_page_ctas,
    ensure_iframe_global,
    finish_if_classified_on_site,
    has_iframe_global,
    maybe_fresh_at_step_start,
    validate_classify_fields,
)
from utils.proxy_manager import ProxyManager

log = structlog.get_logger(__name__)


class FormFiller:
    """Fills and submits the 50kloans.com multi-step form."""

    _STATE_CODES = {
        "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
        "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
        "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
        "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
        "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
        "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
        "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
        "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
        "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
        "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
        "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
        "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
        "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
    }

    _LOAN_AMOUNT_CHIPS = [1000, 1500, 2000, 2500, 5000, 7500, 10000, 20000, 25000]

    _PAY_FREQ_MAP = {
        "monthly": "Monthly",
        "twice monthly": "Twice Monthly",
        "semi-monthly": "Twice Monthly",
        "semimonthly": "Twice Monthly",
        "bimonthly": "Twice Monthly",
        "twice a month": "Twice Monthly",
        "weekly": "Weekly",
        "biweekly": "Biweekly",
        "bi-weekly": "Biweekly",
        "every 2 weeks": "Biweekly",
        "every two weeks": "Biweekly",
        "2 weeks": "Biweekly",
    }

    def __init__(self, config: dict) -> None:
        self._config = config
        self._target = config.get("target", {})
        self._ss_dir = Path(config.get("screenshots", {}).get("directory", "screenshots"))
        self._ss_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ public

    def process_row(
        self,
        row: dict[str, Any],
        proxy_url: str | None,
        row_number: int,
        stop_event=None,
    ) -> dict[str, Any]:
        """Fill and submit the form for one sheet row."""
        headless = os.getenv("HEADLESS", "true").lower() == "true"
        fields = self._parse_fields(row)
        self._validate_required_fields(fields)

        with sync_playwright() as pw:
            launch_args: dict[str, Any] = {"headless": headless}
            if proxy_url:
                launch_args["proxy"] = ProxyManager.to_playwright_proxy(proxy_url)

            browser: Browser = pw.chromium.launch(**launch_args)
            try:
                context: BrowserContext = browser.new_context()
                page: Page = context.new_page()

                url = self._target.get("url", "https://50kloans.com")
                log.info("form.navigating", url=url, row=row_number)
                try:
                    page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception:
                    pass  # networkidle may never fire on SPAs; continue once DOM loaded
                if (page.url or "").startswith("chrome-error://"):
                    raise FormFillerError(
                        f"Page failed to load ({page.url}) — proxy unreachable or blocked",
                        error_type="proxy_error",
                    )
                time.sleep(5)
                try:
                    page.screenshot(path=str(self._ss_dir / "live_view.png"))
                except Exception:
                    pass

                self._fill_form(page, fields, row_number, stop_event=stop_event)

                self._screenshot(page, row_number, "success")
                submission_id = str(uuid.uuid4())[:8].upper()
                log.info("form.success", row=row_number, submission_id=submission_id)
                context.close()
                return {
                    "status": "Success",
                    "notes": "Form submitted successfully",
                    "submission_id": submission_id,
                }

            except FormFillerError:
                try:
                    self._screenshot(page, row_number, "error")
                except Exception:
                    pass
                raise
            except Exception as exc:
                error_type = self._classify_error(exc)
                try:
                    self._screenshot(page, row_number, error_type)
                except Exception:
                    pass
                raise FormFillerError(str(exc), error_type=error_type) from exc
            finally:
                browser.close()

    def classify_lead_on_site(
        self,
        row: dict[str, Any],
        proxy_url: str | None,
        row_number: int,
        stop_event=None,
    ) -> dict[str, str]:
        """Check duplicate vs fresh on the live website (email + SSN steps only)."""
        headless = os.getenv("HEADLESS", "true").lower() == "true"
        fields = self._parse_fields(row)
        validate_classify_fields(fields, row_number=row_number)

        with sync_playwright() as pw:
            launch_args: dict[str, Any] = {"headless": headless}
            if proxy_url:
                launch_args["proxy"] = ProxyManager.to_playwright_proxy(proxy_url)

            browser: Browser = pw.chromium.launch(**launch_args)
            try:
                context: BrowserContext = browser.new_context()
                page: Page = context.new_page()

                self._ensure_iframe_for_classify(page, row_number, fields)

                self._fill_form(
                    page, fields, row_number, stop_event=stop_event, classify_only=True
                )
                context.close()
                return {
                    "status": "Fresh",
                    "notes": "Website: new lead (passed email/SSN check)",
                }
            except FormFillerError as exc:
                if exc.error_type == "duplicate":
                    return {"status": "Duplicate", "notes": str(exc)}
                raise
            finally:
                browser.close()

    # --------------------------------------------------------------- form flow

    def _fill_form(
        self,
        page: Page,
        f: dict,
        row_number: int,
        stop_event=None,
        *,
        classify_only: bool = False,
    ) -> None:
        """Main form-fill loop — iterates through all iframe.global steps."""
        self._classify_only = classify_only
        if classify_only and not has_iframe_global(page):
            self._ensure_iframe_for_classify(page, row_number, f)
        frame = self._get_frame(
            page, wait_seconds=15 if classify_only else 30, require_iframe=classify_only
        )
        log.info("form.frame", url=frame.url[:80], row=row_number)
        if not classify_only:
            time.sleep(5)  # let the SPA render before polling

        prev_title = ""
        blank_steps = 0
        max_steps = 25 if classify_only else 60
        for step_num in range(0, max_steps):
            if not classify_only:
                time.sleep(1)
            if stop_event and stop_event.is_set():
                raise FormFillerError("Stopped by user", error_type="stopped")
            try:
                title = self._get_title(frame).lower().strip()
            except Exception as _te:
                _te_msg = str(_te).lower()
                if "closed" in _te_msg or "target page" in _te_msg or "browser has been" in _te_msg:
                    raise FormFillerError(
                        f"Browser closed unexpectedly at step {step_num}",
                        error_type="browser_closed",
                    ) from _te
                title = ""

            # Reached a completion / offer page
            if any(kw in title for kw in [
                "thank you", "congratulation", "review your", "your offers",
                "matched", "we found", "processing", "submitted",
            ]):
                log.info("form.completed", step=step_num, title=title, row=row_number)
                return

            if not title:
                blank_steps += 1
                if classify_only and blank_steps >= 12:
                    raise FormFillerError(
                        "Website form did not load (no step title)",
                        error_type="stuck",
                    )
                frame = self._get_frame(page, wait_seconds=3 if classify_only else 30)
                # The iframe is lazy-loaded; its first step renders a moment after
                # the frame appears. Wait briefly each blank iteration (even in
                # classify mode) so we don't burn the budget before the title shows.
                time.sleep(0.7 if classify_only else 3)
                if step_num != 0:
                    log.warning("form.no_title", step=step_num, row=row_number)
                continue
            blank_steps = 0

            # ── Duplicate detection ─────────────────────────────────────────
            # Signal 1: "welcome back" greeting — form recognised the email as
            # an existing applicant.
            if "welcome back" in title:
                self._screenshot(page, row_number, "duplicate_welcome_back")
                log.warning(
                    "form.duplicate_detected",
                    reason="welcome_back",
                    step=step_num,
                    title=title[:80],
                    row=row_number,
                )
                raise FormFillerError(
                    f"Duplicate: form greeted with 'welcome back' at step {step_num}",
                    error_type="duplicate",
                )

            # Signal 2: form jumped straight to the 93% submit/request-cash
            # step after only a few early steps — means the lead was already
            # submitted and the site skipped the full flow.
            _is_submit_step = (
                "submit" in title or "loan request" in title or "request cash" in title
            )
            if _is_submit_step and step_num < 10:
                self._screenshot(page, row_number, "duplicate_auto_jump")
                log.warning(
                    "form.duplicate_detected",
                    reason="auto_jump_to_submit",
                    step=step_num,
                    title=title[:80],
                    row=row_number,
                )
                raise FormFillerError(
                    f"Duplicate: form auto-jumped to submit step at step {step_num}",
                    error_type="duplicate",
                )
            # ── End duplicate detection ─────────────────────────────────────

            if maybe_fresh_at_step_start(
                classify_only=classify_only,
                title=title,
                step_num=step_num,
                row_number=row_number,
                page=page,
                screenshot=self._screenshot,
                first_name=str(f.get("first_name") or ""),
            ):
                return

            # Transient verification spinner after SSN ("verifying your
            # information..."). Not a real step (no handler) — wait for it to
            # resolve instead of falling through to _handle_step → "stuck".
            if step_num >= 2 and any(kw in title for kw in (
                "verifying", "checking your", "please wait", "one moment", "loading",
            )):
                log.info("form.verifying_wait", step=step_num, title=title[:60], row=row_number)
                time.sleep(1.5)
                continue

            log.info("form.step", step=step_num, title=title[:60], row=row_number)
            if not classify_only:
                try:
                    page.screenshot(path=str(self._ss_dir / "live_view.png"))
                except Exception:
                    pass

            result = self._handle_step(frame, title, f)
            if not result:
                self._screenshot(page, row_number, f"stuck_{step_num}")
                raise FormFillerError(
                    f"No handler matched step {step_num}: {title!r}",
                    error_type="stuck",
                )

            if finish_if_classified_on_site(
                classify_only=classify_only,
                frame=frame,
                page=page,
                title=title,
                step_num=step_num,
                row_number=row_number,
                get_title=self._get_title,
                screenshot=self._screenshot,
                wait_after_step=lambda: (None if classify_only else time.sleep(4)),
                first_name=str(f.get("first_name") or ""),
            ):
                return

            # Final submission — clicking "Request Cash" submits the form.
            # The iframe title doesn't change after submit, so handle the
            # post-submit offer page, then return.
            if "submit" in title or "loan request" in title or "request cash" in title:
                log.info("form.submitted", step=step_num, title=title, row=row_number)
                self._handle_post_submit(page, row_number)
                return

            # Wait for the form to advance — poll in classify mode, fixed sleep otherwise
            if classify_only:
                self._wait_title_change(frame, avoid=[], timeout=3)
            else:
                time.sleep(4)

            # Check if stuck on the same step
            try:
                new_title = self._get_title(frame).lower().strip()
            except Exception:
                new_title = ""

            if new_title and new_title == prev_title and step_num > 0:
                if not classify_only:
                    time.sleep(3)
                try:
                    new_title = self._get_title(frame).lower().strip()
                except Exception:
                    new_title = ""
                if new_title == prev_title:
                    self._screenshot(page, row_number, f"stuck_{step_num}")
                    raise FormFillerError(
                        f"Form did not advance after step {step_num}: {title!r}",
                        error_type="stuck",
                    )

            prev_title = title
            if not classify_only:
                try:
                    page.screenshot(path=str(self._ss_dir / "live_view.png"))
                except Exception:
                    pass

        msg = (
            "Website classify did not pass email/SSN within 60 steps"
            if classify_only
            else "Form did not complete within 60 steps"
        )
        raise FormFillerError(msg, error_type="timeout")

    def _handle_post_submit(self, page: Page, row_number: int) -> None:
        """After 'Request Cash' is clicked, the iframe shows a processing screen
        (94% spinner with checklist: Request started → Validating → Checking
        eligibility → Matching lenders → Offer received).

        The orange 'Get Your FREE Credit Score' button is rendered inside that
        same iframe immediately and is already visible at ~94%.  We click it as
        soon as it appears (no need to wait for 100%).  The button opens a new
        tab; we screenshot it and close it.
        """
        log.info("form.waiting_offers", row=row_number)

        # Selectors matching the actual button text seen on the offers screen.
        # Ordered from most-specific to least-specific.
        btn_selectors = [
            "text=Get Your FREE Credit Score",
            "button:has-text('FREE Credit Score')",
            "a:has-text('FREE Credit Score')",
            "text=Get Your Free Credit Score",
            "button:has-text('Free Credit Score')",
            "a:has-text('Free Credit Score')",
            "text=Get Your Credit Score Now",
            "button:has-text('Credit Score')",
            "a:has-text('Credit Score')",
        ]

        def _find_btn():
            """Return the first visible credit-score button across all frames."""
            # Search every attached frame first (button lives in the iframe)
            for f in page.frames:
                if f.is_detached():
                    continue
                for sel in btn_selectors:
                    try:
                        el = f.locator(sel).first
                        if el.is_visible(timeout=300):
                            return el
                    except Exception:
                        pass
            # Fallback: main page
            for sel in btn_selectors:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=300):
                        return el
                except Exception:
                    pass
            return None

        def _offer_received() -> bool:
            """True when the 'Offer received' checklist item turns green."""
            for f in page.frames:
                if f.is_detached():
                    continue
                try:
                    # The item text exists AND the parent has a green/active class
                    el = f.locator("text=Offer received").first
                    if el.is_visible(timeout=200):
                        # Check if it has an active/checked sibling icon
                        parent = f.evaluate(
                            """() => {
                                var els = Array.from(document.querySelectorAll('*'));
                                for (var e of els) {
                                    if (e.textContent.trim() === 'Offer received') {
                                        var p = e.closest('li,div,[class]');
                                        return p ? p.className : '';
                                    }
                                }
                                return '';
                            }"""
                        )
                        if parent and any(k in parent.lower() for k in ["active", "complete", "check", "done", "green", "success"]):
                            return True
                except Exception:
                    pass
            return False

        btn = None
        deadline = time.time() + 150  # generous cap — click as soon as visible
        elapsed_log = 0
        _last_ss = 0.0
        while time.time() < deadline:
            btn = _find_btn()
            if btn:
                break
            # Also check if "Offer received" ticked — means processing done
            if _offer_received():
                log.info("form.offer_received_ticked", row=row_number)
                btn = _find_btn()
                break
            now_t = time.time()
            # Keep live preview updated every 4 s during the post-submit wait
            if now_t - _last_ss >= 4:
                try:
                    page.screenshot(path=str(self._ss_dir / "live_view.png"))
                    _last_ss = now_t
                except Exception:
                    pass
            now = int(now_t - (deadline - 150))
            if now - elapsed_log >= 15:
                log.info("form.offers_processing", elapsed_s=now, row=row_number)
                elapsed_log = now
            time.sleep(2)

        # Screenshot the offers page regardless of outcome
        try:
            page.screenshot(
                path=str(self._ss_dir / f"row_{row_number}_offers_page.png"),
                full_page=True,
            )
        except Exception:
            pass

        if not btn:
            log.warning("form.credit_btn_not_found", row=row_number)
            return

        log.info("form.clicking_credit_btn", row=row_number)
        # Button is target="_blank" — catch the new tab with expect_page
        try:
            with page.context.expect_page(timeout=15000) as new_page_info:
                btn.click()
            new_tab = new_page_info.value
            new_tab.wait_for_load_state("domcontentloaded", timeout=30000)
            log.info("form.credit_tab_opened", url=new_tab.url[:80], row=row_number)
            try:
                new_tab.screenshot(
                    path=str(self._ss_dir / f"row_{row_number}_credit_tab.png")
                )
            except Exception:
                pass
            new_tab.close()
        except Exception:
            # Fallback: button navigates in the current page (no new tab)
            try:
                btn.click()
                log.info("form.credit_btn_clicked_inline", row=row_number)
            except Exception as e:
                log.warning("form.credit_btn_click_failed", error=str(e), row=row_number)

    # ---------------------------------------------------------------- step handlers

    def _handle_step(self, frame: Frame, title: str, f: dict) -> str | None:
        """Dispatch the current form step to the correct action."""

        # ── Step 0: Loan amount ──────────────────────────────────────────────
        if "how much" in title or ("amount" in title and "loan" in title):
            return self._handle_loan_amount_step(frame, f)

        # ── Step 1: Email ────────────────────────────────────────────────────
        if "email" in title:
            email = f["email"]
            ok = self._react_fill_any(
                frame,
                [
                    'input[name="email"]',
                    "input[type=email]",
                    "input[autocomplete=email]",
                    "input[placeholder*='email' i]",
                    "input[name*=email i]",
                    "input[id*=email i]",
                ],
                email,
            )
            if not ok:
                ok = self._fill_nth_visible_input(frame, 0, email)
            if not ok:
                ok = self._type_email_fallback(frame, email)
            self._dispatch_input_blur(frame, 'input[name="email"], input[type=email]')
            self._ensure_visible_checkboxes_checked(frame)
            time.sleep(0.2 if self._classify_only else 0.8)
            if self._wait_and_click_continue(frame, timeout_s=12 if self._classify_only else 25):
                return "CONTINUE"
            result = self._continue(frame)
            return result or "EMAIL"

        # ── Step 2 / 22: SSN (last-4 or full) ──────────────────────────────
        if "ssn" in title or ("social" in title and "secur" in title):
            if "last" in title or "4" in title or "digit" in title:
                self._react_fill_any(
                    frame, ['input[name="last_ssn"]', "input:visible"], f["last_ssn"]
                ) or self._fill(frame, 'input[name="last_ssn"]', f["last_ssn"])
            else:
                self._react_fill_any(
                    frame,
                    ['input[name="ssn"]', 'input[name*="ssn" i]', "input:visible"],
                    f["ssn"],
                ) or self._fill(
                    frame,
                    'input[name="ssn"], input[name*="ssn" i], input:visible',
                    f["ssn"],
                )
            time.sleep(0.2 if self._classify_only else 0.8)
            if self._wait_and_click_continue(frame, timeout_s=10 if self._classify_only else 18):
                return "CONTINUE"
            result = self._continue(frame)
            return result or "SSN"

        # ── Step 3: Credit score ─────────────────────────────────────────────
        if "credit" in title and ("score" in title or "rating" in title) and "trial" not in title:
            return self._chip(frame, f["credit_chip"])

        # ── Step 4: Legal name ───────────────────────────────────────────────
        if "legal name" in title or (
            "name" in title and "your" in title
            and "bank" not in title and "employer" not in title
        ):
            self._fill(frame, 'input[name="first_name"]', f["first_name"])
            self._fill(frame, 'input[name="last_name"]', f["last_name"])
            time.sleep(1)
            return self._continue(frame)

        # ── Step 5: Date of birth ────────────────────────────────────────────
        if "birth" in title or "date of birth" in title or title.startswith("hi "):
            self._fill(frame, 'input[name="dob"]', f["dob"])
            time.sleep(1)
            return self._continue(frame)

        # ── Step 6: ZIP ──────────────────────────────────────────────────────
        if "zip" in title:
            self._fill(frame, 'input[name="zip"]', f["zip"])
            time.sleep(1)
            return self._continue(frame)

        # ── Step 7: Street address ───────────────────────────────────────────
        if "street" in title or "address" in title:
            self._fill(frame, 'input[name="street_address"]', f["street_address"])
            self._fill(frame, 'input[name="city"]', f["city"])
            try:
                frame.locator('select[name="state"]').select_option(
                    value=f["state"], timeout=5000
                )
            except Exception:
                try:
                    frame.locator("select:visible").first.select_option(
                        value=f["state"], timeout=3000
                    )
                except Exception:
                    pass
            time.sleep(1)
            return self._continue(frame)

        # ── Step 8: Source of income ─────────────────────────────────────────
        if "source" in title and "income" in title:
            return self._chip(frame, "Employed")

        # ── Step 9: Military ─────────────────────────────────────────────────
        if "military" in title or "veteran" in title:
            return self._chip(frame, "No")

        # ── Step 10: Pay frequency ───────────────────────────────────────────
        if "often" in title or "paid" in title or "frequen" in title:
            return self._chip(frame, f["pay_freq_chip"])

        # ── Step 11: Monthly income (before employer handler) ────────────────
        if "monthly" in title or ("gross" in title and "income" in title):
            self._fill(frame, 'input[name="monthly_income"], input:visible', f["monthly_income"])
            time.sleep(1)
            return self._continue(frame)

        # ── Step 12: Next pay date ───────────────────────────────────────────
        if ("next" in title and "pay" in title) or "next pay" in title:
            self._chip(frame, "Next scheduled")
            time.sleep(1)
            return self._continue(frame)

        # ── Step 13: Employer information ────────────────────────────────────
        if "employer" in title or ("employ" in title and "info" in title):
            self._fill(frame, 'input[name="employer_name"]', f["employer_name"])
            self._fill(frame, 'input[name="job_title"]', f.get("job_title", "Employee"))
            self._fill(frame, 'input[name="employer_phone"]', f.get("employer_phone") or f["phone"])
            time.sleep(1)
            return self._continue(frame)

        # ── Step 14: Paycheck received ───────────────────────────────────────
        if "paycheck" in title or ("received" in title and "pay" in title):
            return self._chip(frame, "Direct Deposit")

        # ── Step 15: ABA Routing number ──────────────────────────────────────
        if "routing" in title or "aba" in title:
            routing_val = f["routing_number"]
            # Log all inputs for diagnostics
            try:
                inp_info = frame.evaluate("() => Array.from(document.querySelectorAll('input')).map(function(i){return{name:i.name,id:i.id,ph:i.placeholder,type:i.type,vis:!!i.offsetParent};})")
                log.info("form.routing_inputs", inputs=inp_info)
            except Exception:
                pass
            # Try targeted selector first, then any visible input
            filled = self._fill(
                frame,
                'input[name*="routing" i], input[id*="routing" i], input[placeholder*="routing" i]',
                routing_val,
            )
            if not filled:
                filled = self._fill(frame, 'input:visible', routing_val)
            # React-compatible native setter as extra insurance
            try:
                frame.evaluate(
                    "(v) => { var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set; var inp=document.querySelector('input:not([type=hidden])'); if(inp){s.call(inp,v);inp.dispatchEvent(new Event('input',{bubbles:true}));inp.dispatchEvent(new Event('change',{bubbles:true}));} }",
                    routing_val,
                )
            except Exception:
                pass
            time.sleep(2)
            result = self._continue(frame)
            if not result:
                # Last resort: force-click the continue button
                try:
                    for btn in frame.locator("button").all():
                        t = (btn.text_content() or "").strip().upper()
                        if any(kw in t for kw in ["CONTINUE", "NEXT", "SUBMIT"]):
                            btn.click(force=True, timeout=3000)
                            result = "FORCED"
                            break
                except Exception:
                    pass
            return result

        # ── Step 16: Bank name ───────────────────────────────────────────────
        if title == "bank name" or "bank name" in title:
            self._fill(frame, 'input[name="bank_name"], input[name*="bank" i]', f["bank_name"])
            time.sleep(1)
            return self._continue(frame)

        # ── Step 17: Type of bank account ───────────────────────────────────
        if "type" in title and ("bank" in title or "account" in title):
            chip_val = "Checking" if f["account_type"].lower().startswith("check") else "Savings"
            return self._chip(frame, chip_val)

        # ── Step 18: Length of bank account ─────────────────────────────────
        if "length" in title and ("bank" in title or "account" in title):
            return (
                self._chip(frame, "More than 2")
                or self._chip(frame, "1-2")
                or self._chip(frame, "2 year")
            )

        # ── Bank account number ──────────────────────────────────────────────
        if (
            ("account" in title and ("number" in title or "add" in title))
            or title == "account number"
        ) and "type" not in title and "length" not in title:
            self._fill(
                frame,
                'input[name="bank_account_number"], input[name="account_number"], '
                'input[name*="account" i]',
                f["account_number"],
            )
            time.sleep(1)
            return self._continue(frame)

        # ── Phone number (personal) ──────────────────────────────────────────
        if "phone" in title or "mobile" in title or "cell" in title or "contact" in title:
            self._fill(
                frame,
                'input[name="phone"], input[type=tel], input[name*="phone" i]',
                f["phone"],
            )
            time.sleep(1)
            return self._continue(frame)

        # ── Loan purpose ─────────────────────────────────────────────────────
        if "purpose" in title or "reason" in title or (
            "use" in title and ("loan" in title or "fund" in title)
        ):
            result = self._chip(frame, f["loan_purpose_chip"])
            if result:
                return result
            try:
                frame.locator("select:visible").first.select_option(
                    label=f["loan_purpose_chip"], timeout=3000
                )
                return self._continue(frame)
            except Exception:
                pass
            return self._continue(frame)

        # ── Driver's license ─────────────────────────────────────────────────
        if "license" in title or "driver" in title or "id number" in title:
            self._fill(
                frame,
                'input[name*="license" i], input[name*="dl_" i], '
                'input[name*="driver" i], input:visible',
                f["dl_number"],
            )
            try:
                frame.locator('select[name*="state" i]').first.select_option(
                    value=f["dl_state"], timeout=3000
                )
            except Exception:
                pass
            time.sleep(1)
            result = self._continue(frame)
            if not result:
                try:
                    for btn in frame.locator("button").all():
                        t = (btn.text_content() or "").strip().upper()
                        if any(kw in t for kw in ["CONTINUE", "NEXT", "SUBMIT"]):
                            btn.click(force=True, timeout=3000)
                            result = "FORCED"
                            break
                except Exception:
                    pass
            return result or "DL_FILLED"
        # ── Unsecured debt question ────────────────────────────────────────────
        if "debt" in title or "unsecured" in title:
            return self._chip(frame, "No")

        # ── Free trial upsell (step 24) ────────────────────────────────────────
        if "trial" in title or ("free" in title and "day" in title):
            # Click Yes - clean advance with no modal; No triggers a blocking overlay
            result = self._chip(frame, "Yes")
            if result:
                return result
            return self._continue(frame)
        # ── Submit / Request Cash (final step) ───────────────────────────────
        if "submit" in title or "loan request" in title or "request cash" in title:
            return self._continue(frame)

        # ── Generic chip-only step (no visible text inputs) ──────────────────
        try:
            visible_inputs = frame.locator("input:visible, select:visible").count()
        except Exception:
            visible_inputs = 0

        chips = frame.evaluate("""
            () => Array.from(document.querySelectorAll(
                    'button,[class*="chip"],[class*="option"],[class*="choice"]'))
                .filter(e => e.offsetParent !== null)
                .map(e => e.textContent.trim())
                .filter(t => t && t.toUpperCase() !== 'BACK' && t.length < 60)
        """)
        if chips and not visible_inputs:
            result = self._chip(frame, chips[0])
            if result:
                return result

        # ── Generic fallback: try CONTINUE ───────────────────────────────────
        return self._continue(frame)

    # ---------------------------------------------------------------- helpers

    def _wait_for_50k_landing(self, page: Page, *, timeout_s: float = 25) -> None:
        """Wait for trackier redirect or iframe after navigation."""
        deadline = time.time() + timeout_s
        poll = 0.2 if getattr(self, "_classify_only", False) else 1
        while time.time() < deadline:
            if has_iframe_global(page):
                return
            low = (page.url or "").lower()
            if "50kloans" in low or "iframe.global" in low:
                return
            if "gotrackier" not in low and "trackier" not in low and low.startswith("http"):
                return
            time.sleep(poll)

    def _ensure_iframe_for_classify(
        self, page: Page, row_number: int, fields: dict[str, Any]
    ) -> None:
        """Navigate through trackier/50k entry points until iframe.global loads."""
        loan = str(fields.get("loan_amount_value") or fields.get("loan_amount_chip") or "5000")
        urls: list[str] = []
        for u in (
            "https://50kloans.com/",
            "https://www.50kloans.com/",
            self._target.get("url", ""),
        ):
            u = (u or "").strip()
            if u and u not in urls:
                urls.append(u)

        last_err: FormFillerError | None = None
        for url in urls:
            log.info("form.classify_navigating", url=url, row=row_number)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            if (page.url or "").startswith("chrome-error://"):
                continue
            self._wait_for_50k_landing(page)
            click_page_ctas(page, loan_amount=loan)
            # The form iframe (#application-form, style=nova) is lazy-loaded on
            # the homepage and only loads once scrolled into view — clicking the
            # "Request Funds" anchor alone is not enough in headless.
            for _ in range(25):
                self._scroll_iframe_into_view(page)
                if has_iframe_global(page):
                    log.info("form.classify_iframe_ready", url=url, row=row_number)
                    return
                time.sleep(0.4)
            try:
                ensure_iframe_global(page, timeout_s=15, loan_amount=loan)
                return
            except FormFillerError as exc:
                last_err = exc
                log.warning("form.classify_iframe_retry", url=url, row=row_number)

        raise last_err or FormFillerError(
            "Loan form iframe did not load on website",
            error_type="stuck",
        )

    def _wait_for_iframe_ready(self, page: Page, timeout_s: int = 30) -> None:
        ensure_iframe_global(page, timeout_s=float(timeout_s))

    def _get_frame(
        self, page: Page, *, wait_seconds: int = 30, require_iframe: bool = False
    ) -> Frame:
        if (page.url or "").startswith("chrome-error://"):
            raise FormFillerError(
                f"Page failed to load ({page.url}) — proxy unreachable or blocked",
                error_type="proxy_error",
            )
        poll_interval = 0.2 if getattr(self, "_classify_only", False) else 1
        checks = int(wait_seconds / poll_interval)
        for _ in range(checks):
            frames = [
                f for f in page.frames
                if "iframe.global" in (f.url or "") and f != page.main_frame
            ]
            if frames:
                prefer = [
                    f for f in frames
                    if "50K" in (f.url or "").upper()
                    or "50k" in (f.url or "").lower()
                    or "style=nova" in (f.url or "").lower()
                ]
                return prefer[0] if prefer else frames[0]
            # The form iframe (#application-form, src=iframe.global) is lazy-loaded
            # on scroll; actively nudge it into view each poll so it loads even
            # under concurrent CPU load.
            self._scroll_iframe_into_view(page)
            time.sleep(poll_interval)
        if require_iframe or getattr(self, "_classify_only", False):
            raise FormFillerError(
                "Loan form iframe did not load on website",
                error_type="stuck",
            )
        log.warning("form.iframe_not_found", fallback="main_frame")
        return page.main_frame

    def _scroll_iframe_into_view(self, page: Page) -> None:
        """Trigger the lazy-loaded #application-form iframe by scrolling it into view."""
        try:
            page.evaluate(
                """() => {
                    const f = document.querySelector(
                        '#application-form, #loan-form, iframe[src*="iframe.global"]'
                    );
                    if (f) f.scrollIntoView({block: 'center'});
                }"""
            )
            page.mouse.wheel(0, 500)
        except Exception:
            pass

    def _get_title(self, frame: Frame) -> str:
        try:
            result = frame.evaluate(
                "() => { var els = document.querySelectorAll('h1,h2,h3,[class*=\"title\"],[class*=\"Title\"],[class*=\"heading\"],[class*=\"question\"],[class*=\"Question\"]'); for (var i = 0; i < els.length; i++) { var t = els[i].textContent.trim(); if (t.length > 3 && t.length < 150) return t; } return ''; }"
            )
            return result
        except Exception as e:
            log.warning("form.get_title_error", error=str(e)[:120])
            raise

    def _fill(self, frame: Frame, selector: str, value: str) -> bool:
        try:
            loc = frame.locator(selector).first
            loc.wait_for(state="visible", timeout=2000 if getattr(self, "_classify_only", False) else 5000)
            loc.fill(str(value))
            return True
        except Exception as e:
            log.warning("form.fill_error", selector=selector[:60], error=str(e)[:80])
            return False

    def _handle_loan_amount_step(self, frame: Frame, f: dict) -> str | None:
        """Click loan amount chip (exact digit match — avoids $5,000 vs $50,000)."""
        chip = str(f.get("loan_amount_chip") or "").strip()
        val = str(f.get("loan_amount_value") or "").strip()
        clicked = self._click_loan_chip_js(frame, val or chip)
        if not clicked:
            for c in (chip, "$5,000", "$2,500", "$1,000", "$10,000", "$500", "$250"):
                clicked = self._click_loan_chip_js(frame, c)
                if clicked:
                    break
        if clicked:
            time.sleep(0.2)
            if not self._wait_title_change(
                frame, avoid=["how much", "amount", "loan"], timeout=8
            ):
                self._wait_and_click_continue(frame, timeout_s=10)
                self._wait_title_change(
                    frame, avoid=["how much", "amount", "loan"], timeout=6
                )
            return clicked
        return self._chip(frame, chip, exact_digits=True)

    def _click_loan_chip_js(self, frame: Frame, amount: str) -> str | None:
        """Click a loan amount chip by matching numeric value."""
        digits = re.sub(r"\D", "", str(amount or ""))
        if not digits:
            return None
        label = str(amount or "").strip()
        if not label.startswith("$") and digits:
            try:
                label = f"${int(digits):,}"
            except ValueError:
                pass
        for sel in (
            "button.lcf-option",
            "button[class*='chip']",
            "button[class*='option']",
            "button",
            '[class*="chip"]',
            '[class*="option"]',
        ):
            try:
                loc = frame.locator(sel).filter(has_text=label).first
                if loc.is_visible(timeout=1000):
                    loc.click(timeout=5000)
                    return label
            except Exception:
                pass
        try:
            return frame.evaluate(
                """(digits) => {
                    const pools = [
                        ...document.querySelectorAll('button.lcf-option'),
                        ...document.querySelectorAll('button[class*="chip"]'),
                        ...document.querySelectorAll('button[class*="option"]'),
                        ...document.querySelectorAll('button'),
                        ...document.querySelectorAll('[class*="chip"]'),
                        ...document.querySelectorAll('[class*="option"]'),
                    ];
                    const seen = new Set();
                    for (const b of pools) {
                        if (!b || seen.has(b)) continue;
                        seen.add(b);
                        if (!b.offsetParent) continue;
                        const d = (b.textContent || '').replace(/\\D/g, '');
                        if (d === digits) {
                            b.click();
                            return (b.textContent || '').trim();
                        }
                    }
                    return null;
                }""",
                digits,
            )
        except Exception:
            return None

    def _wait_title_change(
        self, frame: Frame, avoid: list[str] | None = None, timeout: float = 12
    ) -> bool:
        """Wait until the step title changes (chip steps often auto-advance)."""
        avoid = [a.lower() for a in (avoid or [])]
        try:
            start = self._get_title(frame).lower().strip()
        except Exception:
            start = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.2)
            try:
                cur = self._get_title(frame).lower().strip()
            except Exception:
                continue
            if not cur:
                continue
            if cur != start and not any(a in cur for a in avoid):
                return True
            if start and any(a in start for a in avoid) and not any(a in cur for a in avoid):
                return True
        return False

    def _chip(
        self, frame: Frame, text_fragment: str, *, exact_digits: bool = False
    ) -> str | None:
        frag = text_fragment.strip().upper()
        try:
            for sel in ["button", '[class*="chip"]', '[class*="option"]', '[class*="choice"]']:
                for el in frame.locator(sel).all():
                    t = (el.text_content() or "").strip().upper()
                    if exact_digits:
                        if not self._chip_label_matches(text_fragment, t):
                            continue
                    elif frag not in t:
                        continue
                    if el.is_visible():
                        el.click(timeout=5000)
                        return t
        except Exception as e:
            log.warning("form.chip_error", fragment=text_fragment, error=str(e)[:80])
        return None

    def _chip_label_matches(self, fragment: str, label: str) -> bool:
        """Match chip labels without false positives (e.g. 5000 vs 50000)."""
        frag_digits = re.sub(r"\D", "", fragment)
        label_digits = re.sub(r"\D", "", label)
        if frag_digits and label_digits:
            return frag_digits == label_digits
        return fragment.strip().upper() in label.strip().upper()

    def _react_set_value(self, frame: Frame, selector: str, value: str) -> bool:
        """Set input value using native setter + input/change/blur events."""
        try:
            loc = frame.locator(selector).first
            if loc.count() == 0 or not loc.is_visible(timeout=500 if getattr(self, "_classify_only", False) else 1500):
                return False
            return bool(
                loc.evaluate(
                    """(el, val) => {
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype,
                            "value"
                        )?.set;
                        if (setter) setter.call(el, String(val));
                        else el.value = String(val);
                        el.dispatchEvent(new Event("input", { bubbles: true }));
                        el.dispatchEvent(new Event("change", { bubbles: true }));
                        el.dispatchEvent(new Event("blur", { bubbles: true }));
                        return true;
                    }""",
                    str(value),
                )
            )
        except Exception:
            return False

    def _react_fill_any(self, frame: Frame, selectors: list[str], value: str) -> bool:
        for sel in selectors:
            if self._react_set_value(frame, sel, value):
                return True
        return False

    def _type_email_fallback(self, frame: Frame, email: str) -> bool:
        """Type email character-by-character when React ignores programmatic fills."""
        selectors = [
            'input[name="email"]',
            "input[type=email]",
            "input[autocomplete=email]",
            "input[placeholder*='email' i]",
            "input[id*=email i]",
            "input[name*=email i]",
            "input:visible",
        ]
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if loc.count() == 0 or not loc.is_visible(timeout=300 if self._classify_only else 800):
                    continue
                loc.click(timeout=2000)
                try:
                    loc.press("Control+a")
                    loc.press("Delete")
                except Exception:
                    pass
                loc.press_sequentially(email, delay=20)
                try:
                    loc.press("Tab")
                except Exception:
                    pass
                return True
            except Exception:
                continue
        return False

    def _dispatch_input_blur(self, frame: Frame, selector: str) -> None:
        try:
            frame.locator(selector).first.evaluate(
                """(el) => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                }"""
            )
        except Exception:
            pass

    def _fill_nth_visible_input(self, frame: Frame, n: int, value: str) -> bool:
        try:
            loc = frame.locator("input:visible").nth(n)
            loc.wait_for(state="visible", timeout=1000 if getattr(self, "_classify_only", False) else 3000)
            loc.fill(str(value))
            return True
        except Exception:
            return False

    def _ensure_visible_checkboxes_checked(self, frame: Frame) -> None:
        try:
            frame.evaluate(
                """() => {
                    const boxes = Array.from(document.querySelectorAll('input[type="checkbox"]'))
                      .filter(b => b && b.offsetParent !== null);
                    for (const b of boxes) {
                      try { if (!b.checked) b.click(); } catch(e) {}
                    }
                }"""
            )
        except Exception:
            pass

    def _wait_and_click_continue(self, frame: Frame, timeout_s: float = 18) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                clicked = bool(
                    frame.evaluate(
                        """() => {
                            const candidates = [
                              document.querySelector('button.lcf-btn-primary'),
                              document.querySelector('#continue-button'),
                              document.querySelector('button#continue-button'),
                              ...Array.from(document.querySelectorAll('button')).filter(
                                b => /continue|next|apply|submit|request/i.test((b.textContent||'').trim())
                              ),
                            ];
                            for (const b of candidates) {
                              if (!b || b.offsetParent === null) continue;
                              if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
                              b.click();
                              return true;
                            }
                            return false;
                        }"""
                    )
                )
                if clicked:
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def _continue(self, frame: Frame) -> str | None:
        for kw in ["CONTINUE", "NEXT", "SUBMIT", "APPLY NOW", "APPLY", "GET STARTED", "REQUEST CASH"]:
            for attempt in range(2):
                try:
                    for btn in frame.locator("button").all():
                        t = (btn.text_content() or "").strip().upper()
                        if kw in t and btn.is_visible() and btn.is_enabled():
                            btn.click(timeout=5000)
                            return kw
                except Exception:
                    pass
                if attempt < 1:
                    time.sleep(0.3)
        return None

    # ---------------------------------------------------------------- parsing

    def _parse_fields(self, row: dict) -> dict:
        def g(*keys: str) -> str:
            """Return the first non-empty value from the given column keys."""
            for k in keys:
                v = str(row.get(k) or "").strip()
                if v:
                    return v
            return ""

        # Personal
        first_name = g("First Name", "First_Name")
        last_name = g("Last Name", "Last_Name")
        email = g("Email Address", "Email")
        phone = re.sub(r"\D", "", g("Phone Number", "Phone"))

        # SSN — new sheet has separate Full / Last-4 columns
        full_ssn_raw = re.sub(r"\D", "", g("SSN Full", "SSN"))
        last_ssn_raw = re.sub(r"\D", "", g("SSN Last 4"))
        if full_ssn_raw:
            full_ssn = full_ssn_raw
            last_ssn = full_ssn_raw[-4:]
        elif last_ssn_raw:
            full_ssn = last_ssn_raw
            last_ssn = last_ssn_raw[-4:]
        else:
            full_ssn = ""
            last_ssn = ""

        # DOB
        dob = self._normalize_dob(g("Date of Birth (DOB)", "dob"))

        # Address
        _zip_raw = re.sub(r"\D", "", g("ZIP Code", "Zip"))
        zip_code = _zip_raw.zfill(5) if _zip_raw else ""
        street = g("Street Address", "Address")
        city = g("City")
        state = self._normalize_state(g("State"))

        # Loan amount → closest chip
        loan_raw = re.sub(r"[,$\s]", "", g("Requested Loan Amount ($)", "Loan_Amount"))
        try:
            loan_int = int(float(loan_raw))
        except (ValueError, TypeError):
            loan_int = 5000
        loan_amount_chip = self._closest_loan_chip(loan_int)

        # Credit
        credit_chip = self._map_credit_chip(g("Credit Score Rating", "Credit_Score"))

        # Pay frequency
        pay_freq_raw = g("Pay Frequency", "Pay_Frequency").lower().strip()
        pay_freq_chip = self._PAY_FREQ_MAP.get(pay_freq_raw, "Biweekly")

        # Income
        monthly_income = re.sub(r"[,$\s]", "", g("Monthly Net Income ($)", "Monthly_Income")) or "3000"

        # Employer
        employer_name = g("Employer Name", "Employer_Name") or "Employer"
        job_title = g("Job Title") or "Employee"
        employer_phone = re.sub(r"\D", "", g("Employer Work Phone", "Phone Number", "Phone")) or phone

        # Bank
        _routing_raw = g("ABA Routing Number", "routingNumber")
        # ABA routing numbers are 9 digits; Google Sheets drops leading zeros
        routing_number = _routing_raw.zfill(9) if _routing_raw else ""
        account_number = g("Account Number", "accountNumber")
        account_type = g("Account Type", "bankAccountType") or "Checking"
        bank_name = g("Bank Name", "bankName") or "Chase"

        # Driver's license — new sheet has a dedicated DL state column
        dl_number = g("Driver License / ID Number", "driversLicenseNumber")
        dl_state_raw = g("Driver License State", "bankState")  # fall back to bankState
        dl_state = self._normalize_state(dl_state_raw) if dl_state_raw else state

        # Loan purpose
        loan_purpose_chip = self._map_loan_purpose(g("Loan Purpose", "Loan_Purpose"))

        return {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone": phone,
            "last_ssn": last_ssn,
            "ssn": full_ssn,
            "dob": dob,
            "zip": zip_code,
            "street_address": street,
            "city": city,
            "state": state,
            "loan_amount_chip": loan_amount_chip,
            "loan_amount_value": str(loan_int),
            "credit_chip": credit_chip,
            "pay_freq_chip": pay_freq_chip,
            "monthly_income": monthly_income,
            "employer_name": employer_name,
            "job_title": job_title,
            "employer_phone": employer_phone,
            "routing_number": routing_number,
            "account_number": account_number,
            "account_type": account_type,
            "bank_name": bank_name,
            "dl_number": dl_number,
            "dl_state": dl_state,
            "loan_purpose_chip": loan_purpose_chip,
        }

    def _validate_required_fields(self, f: dict) -> None:
        required = [
            "first_name", "last_name", "email", "phone",
            "last_ssn", "dob", "zip", "street_address", "city", "state",
        ]
        missing = [k for k in required if not f.get(k)]
        if missing:
            raise FormFillerError(
                f"Missing required fields: {missing}",
                error_type="missing_data",
            )

    # ---------------------------------------------------------------- normalise

    def _normalize_dob(self, raw: str) -> str:
        raw = raw.strip()
        if not raw:
            return ""
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
            except ValueError:
                pass
        if re.match(r"^\d{2}/\d{2}/\d{4}$", raw):
            return raw
        return raw

    def _normalize_state(self, raw: str) -> str:
        raw = raw.strip()
        if len(raw) == 2:
            return raw.upper()
        return self._STATE_CODES.get(raw.lower(), raw.upper()[:2])

    def _closest_loan_chip(self, amount: int) -> str:
        closest = min(self._LOAN_AMOUNT_CHIPS, key=lambda x: abs(x - amount))
        return f"${closest:,}"

    def _map_credit_chip(self, raw: str) -> str:
        raw = raw.lower().strip()
        direct = {"poor": "Poor", "fair": "Fair", "good": "Good", "excellent": "Excellent"}
        if raw in direct:
            return direct[raw]
        for key, val in direct.items():
            if raw.startswith(key):
                return val
        try:
            score = int(re.sub(r"[^\d]", "", raw)[:3])
            if score < 580:
                return "Poor"
            if score < 670:
                return "Fair"
            if score < 740:
                return "Good"
            return "Excellent"
        except (ValueError, TypeError):
            return "Fair"

    def _map_loan_purpose(self, raw: str) -> str:
        raw = raw.lower().strip()
        if not raw:
            return "Debt Consolidation"
        if "debt" in raw or "consol" in raw:
            return "Debt Consolidation"
        if "home" in raw or "house" in raw or "improv" in raw:
            return "Home Improvement"
        if "auto" in raw or "car" in raw or "vehicle" in raw:
            return "Auto"
        if "medical" in raw or "health" in raw:
            return "Medical"
        if "business" in raw:
            return "Business"
        if "vacation" in raw or "travel" in raw:
            return "Vacation"
        return "Personal"

    # ---------------------------------------------------------------- utilities

    def _screenshot(self, page: Page, row: int, label: str) -> None:
        try:
            path = self._ss_dir / f"row_{row:04d}_{label}.png"
            page.screenshot(path=str(path), full_page=False)
            log.debug("screenshot.saved", path=str(path))
        except Exception as e:
            log.warning("screenshot.failed", error=str(e)[:80])

    def _classify_error(self, exc: Exception) -> str:
        msg = str(exc).lower()
        if "duplicate" in msg or "already" in msg or "exist" in msg:
            return "duplicate"
        if "proxy" in msg or "net::err" in msg or "tunnel" in msg:
            return "proxy_error"
        if "timeout" in msg:
            return "timeout"
        return "unknown"

