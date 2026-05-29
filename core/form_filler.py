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

from utils.stealth import inject_stealth
from utils.proxy_manager import ProxyManager

log = structlog.get_logger(__name__)


class FormFillerError(Exception):
    """Base exception for form-filling errors."""

    def __init__(self, message: str, error_type: str = "unknown"):
        super().__init__(message)
        self.error_type = error_type


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
        fingerprint: dict[str, Any],
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
                ctx_args = self._clean_fingerprint(fingerprint)
                context: BrowserContext = browser.new_context(**ctx_args)
                page: Page = context.new_page()
                inject_stealth(page, fingerprint)

                url = self._target.get("url", "https://50kloans.com")
                log.info("form.navigating", url=url, row=row_number)
                try:
                    page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception:
                    pass  # networkidle may never fire on SPAs; continue once DOM loaded
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

    # --------------------------------------------------------------- form flow

    def _fill_form(self, page: Page, f: dict, row_number: int, stop_event=None) -> None:
        """Main form-fill loop — iterates through all iframe.global steps."""
        frame = self._get_frame(page)
        log.info("form.frame", url=frame.url[:80], row=row_number)
        time.sleep(5)  # let the SPA fully render before polling

        prev_title = ""
        for step_num in range(0, 60):
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
                # Re-fetch the frame — it may have detached/reloaded.
                frame = self._get_frame(page)
                if step_num == 0:
                    time.sleep(3)
                    continue
                log.warning("form.no_title", step=step_num, row=row_number)
                time.sleep(3)
                continue

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

            log.info("form.step", step=step_num, title=title[:60], row=row_number)
            # Update live preview BEFORE filling the step so the UI shows the active step
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

            # Final submission — clicking "Request Cash" submits the form.
            # The iframe title doesn't change after submit, so handle the
            # post-submit offer page, then return.
            if "submit" in title or "loan request" in title or "request cash" in title:
                log.info("form.submitted", step=step_num, title=title, row=row_number)
                self._handle_post_submit(page, row_number)
                return

            # Wait for the form to advance
            time.sleep(4)

            # Check if stuck on the same step
            try:
                new_title = self._get_title(frame).lower().strip()
            except Exception:
                new_title = ""

            if new_title and new_title == prev_title and step_num > 0:
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
            try:
                page.screenshot(path=str(self._ss_dir / "live_view.png"))
            except Exception:
                pass

        raise FormFillerError("Form did not complete within 60 steps", error_type="timeout")

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
            return self._chip(frame, f["loan_amount_chip"])

        # ── Step 1: Email ────────────────────────────────────────────────────
        if "email" in title:
            self._fill(frame, "input[type=email], input[name*=email i]", f["email"])
            time.sleep(1)
            return self._continue(frame)

        # ── Step 2 / 22: SSN (last-4 or full) ──────────────────────────────
        if "ssn" in title or ("social" in title and "secur" in title):
            if "last" in title or "4" in title or "digit" in title:
                self._fill(frame, 'input[name="last_ssn"]', f["last_ssn"])
            else:
                self._fill(
                    frame,
                    'input[name="ssn"], input[name*="ssn" i], input:visible',
                    f["ssn"],
                )
            time.sleep(1)
            return self._continue(frame)

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

    def _get_frame(self, page: Page) -> Frame:
        for _ in range(30):
            frames = [f for f in page.frames if "iframe.global" in f.url]
            if frames:
                return frames[0]
            time.sleep(1)
        log.warning("form.iframe_not_found", fallback="main_frame")
        return page.main_frame

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
            loc.wait_for(state="visible", timeout=5000)
            loc.fill(str(value))
            return True
        except Exception as e:
            log.warning("form.fill_error", selector=selector[:60], error=str(e)[:80])
            return False

    def _chip(self, frame: Frame, text_fragment: str) -> str | None:
        frag = text_fragment.strip().upper()
        try:
            for sel in ["button", '[class*="chip"]', '[class*="option"]', '[class*="choice"]']:
                for el in frame.locator(sel).all():
                    t = (el.text_content() or "").strip().upper()
                    if frag in t and el.is_visible():
                        el.click(timeout=5000)
                        return t
        except Exception as e:
            log.warning("form.chip_error", fragment=text_fragment, error=str(e)[:80])
        return None

    def _continue(self, frame: Frame) -> str | None:
        for kw in ["CONTINUE", "NEXT", "SUBMIT", "APPLY NOW", "APPLY", "GET STARTED", "REQUEST CASH"]:
            for attempt in range(3):
                try:
                    for btn in frame.locator("button").all():
                        t = (btn.text_content() or "").strip().upper()
                        if kw in t and btn.is_visible() and btn.is_enabled():
                            btn.click(timeout=5000)
                            return kw
                except Exception:
                    pass
                if attempt < 2:
                    time.sleep(0.8)
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
        zip_code = g("ZIP Code", "Zip")
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

    def _clean_fingerprint(self, fp: dict) -> dict:
        allowed = {
            "user_agent", "viewport", "locale", "timezone_id",
            "geolocation", "color_scheme", "device_scale_factor",
            "is_mobile", "has_touch", "java_script_enabled",
            "extra_http_headers",
        }
        return {k: v for k, v in fp.items() if k in allowed and v is not None}
