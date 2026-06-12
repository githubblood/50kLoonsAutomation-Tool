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
    finish_if_classified_on_site,
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
    # BorrowMoney.us iframe (style=2, owner=BORROWMONEY) — $100–$5,000
    # Confirmed on borrowmoney.us iframe (owner=BORROWMONEY)
    _LOAN_AMOUNT_CHIPS_BORROWMONEY = [100, 250, 500, 1000, 2500, 5000]

    _PAY_FREQ_MAP = {
        "monthly": "Monthly",
        # LowCreditFinance chip label
        "twice monthly": "Twice Monthly (1st & 15th)",
        "semi-monthly": "Twice Monthly (1st & 15th)",
        "semimonthly": "Twice Monthly (1st & 15th)",
        "bimonthly": "Twice Monthly (1st & 15th)",
        "twice a month": "Twice Monthly (1st & 15th)",
        "weekly": "Weekly",
        # LowCreditFinance chip label
        "biweekly": "Bi-Weekly (Every 2 Weeks)",
        "bi-weekly": "Bi-Weekly (Every 2 Weeks)",
        "every 2 weeks": "Bi-Weekly (Every 2 Weeks)",
        "every two weeks": "Bi-Weekly (Every 2 Weeks)",
        "2 weeks": "Bi-Weekly (Every 2 Weeks)",
    }

    def __init__(self, config: dict) -> None:
        self._config = config
        self._target = config.get("target", {})
        self._strict_sheet = bool(config.get("form", {}).get("strict_sheet_data", True))
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
        entry_url = self._resolve_entry_url(self._target.get("url", "https://50kloans.com"))
        site = self._site_profile(entry_url)
        fields = self._parse_fields(row, site=site)
        self._validate_required_fields(fields, row_number=row.get("_row_number"))
        log.info(
            "form.sheet_data",
            row=row.get("_row_number"),
            email=fields.get("email", "")[:40],
            loan=fields.get("loan_amount_value"),
            strict=self._strict_sheet,
        )

        with sync_playwright() as pw:
            launch_args: dict[str, Any] = {"headless": headless}
            if proxy_url:
                launch_args["proxy"] = ProxyManager.to_playwright_proxy(proxy_url)

            browser: Browser = pw.chromium.launch(**launch_args)
            try:
                context: BrowserContext = browser.new_context()
                page: Page = context.new_page()

                log.info("form.navigating", url=entry_url, site=site, row=row_number)
                try:
                    page.goto(entry_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as nav_err:
                    if proxy_url:
                        raise FormFillerError(
                            f"Navigation failed through proxy: {nav_err}",
                            error_type="proxy_error",
                        ) from nav_err
                if (page.url or "").startswith("chrome-error://"):
                    raise FormFillerError(
                        f"Page failed to load ({page.url}) — proxy unreachable or blocked",
                        error_type="proxy_error",
                    )
                time.sleep(3)
                self._prepare_entry_page(page, entry_url, fields, row_number)
                time.sleep(2)
                try:
                    page.screenshot(path=str(self._ss_dir / "live_view.png"))
                except Exception:
                    pass

                self._fill_form(page, fields, row_number, stop_event=stop_event)

                self._screenshot(page, row_number, "success")
                submission_id = str(uuid.uuid4())[:8].upper()
                is_dry_run = os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes", "y"}
                log.info("form.success", row=row_number, submission_id=submission_id, dry_run=is_dry_run)
                context.close()
                return {
                    "status": "Success",
                    "notes": "Dry-run completed (stopped before final submit)" if is_dry_run else "Form submitted successfully",
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
        entry_url = self._resolve_entry_url(self._target.get("url", "https://superpersonalfinder.com"))
        site = self._site_profile(entry_url)
        fields = self._parse_fields(row, site=site)
        validate_classify_fields(fields, row_number=row_number)

        with sync_playwright() as pw:
            launch_args: dict[str, Any] = {"headless": headless}
            if proxy_url:
                launch_args["proxy"] = ProxyManager.to_playwright_proxy(proxy_url)

            browser: Browser = pw.chromium.launch(**launch_args)
            try:
                context: BrowserContext = browser.new_context()
                page: Page = context.new_page()

                log.info("form.classify_navigating", url=entry_url, site=site, row=row_number)
                try:
                    page.goto(entry_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
                if (page.url or "").startswith("chrome-error://"):
                    raise FormFillerError(
                        f"Page failed to load ({page.url})",
                        error_type="stuck",
                    )
                self._prepare_entry_page(page, entry_url, fields, row_number)

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
        frame = self._get_frame(page, classify_only=classify_only)
        log.info("form.frame", url=frame.url[:80], row=row_number)
        if not classify_only:
            time.sleep(5)

        debug_steps = os.getenv("DEBUG_STEPS", "").strip().lower() in {"1", "true", "yes", "y"}
        debug_dir = self._ss_dir / "debug_steps"
        if debug_steps:
            debug_dir.mkdir(parents=True, exist_ok=True)

        prev_title = ""
        empty_titles = 0
        for step_num in range(0, 60):
            if not classify_only:
                time.sleep(1)
            if stop_event and stop_event.is_set():
                raise FormFillerError("Stopped by user", error_type="stopped")
            try:
                title = self._get_title(frame).lower().strip()
            except Exception:
                title = ""

            # Reached a completion / offer page
            if any(kw in title for kw in [
                "thank you", "congratulation", "review your", "your offers",
                "matched", "we found", "processing", "submitted",
            ]):
                log.info("form.completed", step=step_num, title=title, row=row_number)
                return

            # Self-heal: if the form iframe never loaded, _get_frame fell back to
            # the host page and we're reading its marketing headline (e.g.
            # "borrow up to $50,000 with good & bad credit."). That is NOT a form
            # step — re-trigger the lazy iframe load and re-acquire the frame
            # instead of mis-firing _handle_step → "stuck".
            iframe_live = any(
                "iframe.global" in (fr.url or "") and fr != page.main_frame
                for fr in page.frames
            )
            if not iframe_live:
                empty_titles += 1
                if empty_titles > 30:
                    raise FormFillerError(
                        "Form iframe did not load on superpersonalfinder.com",
                        error_type="stuck",
                    )
                self._prepare_entry_page(
                    page, self._target.get("url", ""), f, row_number
                )
                frame = self._get_frame(page, classify_only=classify_only)
                time.sleep(0.5 if classify_only else 1)
                continue

            if not title:
                # Re-fetch the frame — it may have detached/reloaded.
                frame = self._get_frame(page, classify_only=classify_only)
                # SPF's iframe is lazy-loaded: the frame URL is set but the form
                # content can take a couple seconds to render. Wait briefly so we
                # don't spin through all 60 steps before the first title appears.
                empty_titles += 1
                if empty_titles > 30:
                    raise FormFillerError(
                        "Form iframe loaded but no step content rendered",
                        error_type="stuck",
                    )
                time.sleep(3 if not classify_only else 0.7)
                if step_num != 0:
                    log.warning("form.no_title", step=step_num, row=row_number)
                continue
            empty_titles = 0

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

            if debug_steps:
                try:
                    page.screenshot(
                        path=str(debug_dir / f"row_{row_number:04d}_step_{step_num:02d}.png"),
                        full_page=True,
                    )
                except Exception:
                    pass
                try:
                    html = frame.content()
                    (debug_dir / f"row_{row_number:04d}_step_{step_num:02d}.html").write_text(
                        html, encoding="utf-8"
                    )
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

            if "submit" in title or "loan request" in title or "request cash" in title:
                log.info("form.submitted", step=step_num, title=title, row=row_number)
                self._handle_post_submit(page, row_number)
                return

            # Wait for the form to advance
            new_title = prev_title
            deadline = time.time() + (6 if classify_only else 18)
            poll = 0.1 if classify_only else 2
            while time.time() < deadline:
                time.sleep(poll)
                try:
                    new_title = self._get_title(frame).lower().strip()
                except Exception:
                    new_title = ""
                if new_title and new_title != prev_title:
                    break

            if new_title and new_title == prev_title and step_num > 0:
                self._continue(frame)
                if not classify_only:
                    time.sleep(4)
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
        while time.time() < deadline:
            btn = _find_btn()
            if btn:
                break
            # Also check if "Offer received" ticked — means processing done
            if _offer_received():
                log.info("form.offer_received_ticked", row=row_number)
                btn = _find_btn()
                break
            now = int(time.time() - (deadline - 150))
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
        if (
            "how much" in title
            or "do you need" in title
            or "would you like to borrow" in title
            or ("amount" in title and "loan" in title)
            or ("borrow" in title and "amount" in title)
        ):
            val = str(f.get("loan_amount_value") or "").strip()
            chip = str(f.get("loan_amount_chip") or "").strip()
            clicked = self._click_loan_chip_js(frame, val or chip)
            if not clicked:
                clicked = self._click_loan_chip_js(frame, chip)
            if not clicked:
                for c in (chip, "$2,500", "$1,000", "$5,000", "$500", "$250"):
                    clicked = self._click_loan_chip_js(frame, c)
                    if clicked:
                        break
            if clicked:
                time.sleep(0.6)
                if not self._click_primary_continue(frame):
                    self._continue(frame)
                self._wait_title_change(
                    frame, avoid=["how much", "borrow", "do you need"], timeout=20
                )
                return clicked
            # BorrowMoney: custom amount field loan_amount + Continue
            if val and self._react_fill_any(
                frame,
                ['input[name="loan_amount"]', 'input[placeholder*="custom" i]'],
                val,
            ):
                time.sleep(0.5)
                if self._click_primary_continue(frame):
                    self._wait_title_change(frame, avoid=["how much", "borrow"], timeout=20)
                    return "CUSTOM_AMOUNT"
            try:
                pill = frame.locator("button.lcf-trigger-pill").first
                if pill.is_visible(timeout=2000):
                    pill.click(timeout=5000)
                    time.sleep(1)
                    if val:
                        self._react_fill_any(frame, ["input:visible"], val)
                        time.sleep(0.5)
                    if self._click_primary_continue(frame):
                        self._wait_title_change(frame, avoid=["how much"], timeout=20)
                        return "CUSTOM_AMOUNT"
            except Exception:
                pass
            return None

        # ── Enter amount (variant step) ──────────────────────────────────────
        if ("enter" in title and "amount" in title) or title.strip() == "enter your amount":
            amt = f.get("loan_amount_value") or ""
            if amt:
                self._fill_any(frame, ["input:visible", "input[type=text]:visible"], amt)
                time.sleep(1)
            return self._continue(frame)

        # ── Step 1: Email ────────────────────────────────────────────────────
        if "email" in title:
            if not self._react_fill_any(
                frame,
                [
                    'input[name="email"]',
                    "input[type=email]",
                    "input[name*=email i]",
                    "input[id*=email i]",
                ],
                f["email"],
            ):
                self._fill_nth_visible_input(frame, 0, f["email"])
            try:
                frame.evaluate(
                    """() => {
                        const el = document.querySelector('input[name="email"]');
                        if (!el) return;
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    }"""
                )
            except Exception:
                pass
            time.sleep(0.8)
            if self._click_primary_continue(frame):
                return "CONTINUE"
            return self._continue(frame)

        # ── Step 2 / 23+: SSN (last-4, single field, or XXX-XX-XXXX split) ─
        if "ssn" in title or ("social" in title and "secur" in title):
            if self._has_named_input(frame, "ssn_1"):
                self._fill_ssn_parts(frame, f["ssn"])
            elif "last" in title or "4" in title or "digit" in title:
                self._fill_any(
                    frame,
                    ['input[name="last_ssn"]', "input:visible"],
                    f["last_ssn"],
                )
            else:
                self._fill_any(
                    frame,
                    [
                        'input[name="ssn"]',
                        'input[name*="ssn" i]',
                        "input:visible",
                    ],
                    f["ssn"],
                )
            time.sleep(1)
            return self._continue(frame)

        # ── Verify identity (variant SSN step) ───────────────────────────────
        # Some variants title the last-4 SSN step as "Verify your identity".
        if "verify" in title and "identity" in title:
            self._react_fill_any(frame, ['input[name="last_ssn"]', "input:visible"], f["last_ssn"])
            time.sleep(0.8)
            if self._click_primary_continue(frame):
                return "CONTINUE"
            return self._continue(frame)

        # ── Post-verify confirmation (auto-advance screen) ───────────────────
        if "verification complete" in title or "verified successfully" in title:
            time.sleep(2)
            self._click_primary_continue(frame)
            self._continue(frame)
            self._wait_title_change(
                frame,
                avoid=["verification complete", "verified successfully"],
                timeout=20,
            )
            return "VERIFIED"

        # ── Step 3: Credit score ─────────────────────────────────────────────
        if "credit" in title and ("score" in title or "rating" in title) and "trial" not in title:
            chip = f["credit_chip"]
            clicked = None
            try:
                frame.get_by_text(chip, exact=True).first.click(timeout=8000)
                clicked = chip
            except Exception:
                pass
            if not clicked:
                clicked = self._click_chip_by_label_js(frame, chip) or self._chip(frame, chip)
            if not clicked:
                try:
                    frame.locator("button.lcf-option").first.click(timeout=5000)
                    clicked = "AUTO"
                except Exception:
                    pass
            if clicked:
                self._wait_title_change(frame, avoid=["credit score"], timeout=15)
                return clicked
            return None

        # ── Step 4: Legal name ───────────────────────────────────────────────
        if (
            "legal name" in title
            or "full name" in title
            or (
                "name" in title
                and "your" in title
                and "bank" not in title
                and "employer" not in title
            )
        ):
            # LowCreditFinance uses fname/lname; other variants may use first_name/last_name.
            if not self._react_fill_any(
                frame,
                [
                    'input[name="first_name"]',
                    'input[name="fname"]',
                    'input[name="firstName"]',
                ],
                f["first_name"],
            ):
                self._fill_nth_visible_input(frame, 0, f["first_name"])
            if not self._react_fill_any(
                frame,
                [
                    'input[name="last_name"]',
                    'input[name="lname"]',
                    'input[name="lastName"]',
                ],
                f["last_name"],
            ):
                self._fill_nth_visible_input(frame, 1, f["last_name"])
            time.sleep(1)
            return self._continue(frame)

        # ── Step 5: Date of birth ────────────────────────────────────────────
        if "birth" in title or "date of birth" in title or title.startswith("hi "):
            # BorrowMoney.us (style=2): single date_of_birth MM/DD/YYYY
            # LowCreditFinance: dob_month / dob_day / dob_year
            mm, dd, yyyy = self._split_dob(f["dob"])
            filled = False
            if self._react_fill_any(
                frame,
                ['input[name="date_of_birth"]', 'input[placeholder*="MM/DD" i]'],
                f["dob"],
            ):
                filled = True
            if mm and dd and yyyy:
                filled = (
                    self._react_fill_any(frame, ['input[name="dob_month"]'], mm) or filled
                )
                filled = (
                    self._react_fill_any(frame, ['input[name="dob_day"]'], dd) or filled
                )
                filled = (
                    self._react_fill_any(frame, ['input[name="dob_year"]'], yyyy) or filled
                )
            if not filled:
                if not self._fill(frame, 'input[name="dob"]', f["dob"]):
                    if mm and dd and yyyy and self._count_visible_inputs(frame) >= 3:
                        self._fill_nth_visible_input(frame, 0, mm)
                        self._fill_nth_visible_input(frame, 1, dd)
                        self._fill_nth_visible_input(frame, 2, yyyy)
                    else:
                        self._fill_any(
                            frame,
                            ['input[name="date_of_birth"]', "input:visible"],
                            f["dob"],
                        )
            time.sleep(1)
            if self._click_primary_continue(frame):
                return "CONTINUE"
            return self._continue(frame)

        # ── Step 6: ZIP ──────────────────────────────────────────────────────
        if "zip" in title:
            self._react_fill_any(
                frame,
                ['input[name="zip_code"]', 'input[name="zip"]', 'input[placeholder*="90210" i]'],
                f["zip"],
            )
            try:
                frame.evaluate(
                    """() => {
                        const el = document.querySelector('input[name="zip_code"], input[name="zip"]');
                        if (!el) return;
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    }"""
                )
            except Exception:
                pass
            time.sleep(1)
            if self._click_primary_continue(frame):
                return "CONTINUE"
            return self._continue(frame)

        # ── Step 7: Street address ───────────────────────────────────────────
        if "street" in title or "address" in title:
            # Some variants don't use stable name= attributes; fill by best effort order.
            if not self._fill(frame, 'input[name="street_address"]', f["street_address"]):
                self._fill_nth_visible_input(frame, 0, f["street_address"])
            if not self._fill(frame, 'input[name="city"]', f["city"]):
                self._fill_nth_visible_input(frame, 1, f["city"])
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
            raw_src = f.get("income_source_raw") or ""
            clicked = self._click_borrowmoney_income_chip(frame, raw_src)
            if clicked:
                time.sleep(1)
                if self._click_primary_continue(frame):
                    return clicked
                return self._continue(frame) or clicked
            chip = f.get("income_source_chip") or ""
            for label in (chip, "Employed", "Job income", "Employment", "Benefits"):
                if not label:
                    continue
                if self._click_chip_by_label_js(frame, label) or self._chip(frame, label):
                    time.sleep(1)
                    if self._click_primary_continue(frame):
                        return label
                    return self._continue(frame) or label
            if not self._strict_sheet:
                return self._chip(frame, "Employed")
            return None

        # ── Step 9: Military ─────────────────────────────────────────────────
        if "military" in title or "veteran" in title:
            yn = (f.get("active_military_chip") or "No").strip().lower()
            el_id = "military-status-yes" if yn.startswith("y") else "military-status-no"
            try:
                frame.locator(f"#{el_id}").click(timeout=5000)
                time.sleep(0.8)
                if self._click_primary_continue(frame):
                    return el_id
                return self._continue(frame) or el_id
            except Exception:
                return self._chip(frame, f.get("active_military_chip") or "No")

        # ── Step 10: Pay frequency ───────────────────────────────────────────
        if (
            "often" in title
            or "paid" in title
            or "frequen" in title
            or "times a month" in title
        ):
            raw_pf = f.get("pay_freq_raw") or ""
            clicked = self._click_borrowmoney_pay_freq(frame, raw_pf)
            if clicked:
                time.sleep(0.8)
                if self._click_primary_continue(frame):
                    return clicked
                return self._continue(frame) or clicked
            chip = f.get("pay_freq_chip") or ""
            for label in (
                chip,
                "Biweekly",
                "Bi-Weekly",
                "Every 2 weeks",
                "Twice a month",
                "Monthly",
                "Weekly",
            ):
                if label and (
                    self._click_chip_by_label_js(frame, label) or self._chip(frame, label)
                ):
                    time.sleep(0.8)
                    if self._click_primary_continue(frame):
                        return label
                    return self._continue(frame) or label
            if not self._strict_sheet:
                return self._chip(frame, "Biweekly")
            return None

        # ── Step 11: Monthly income (before employer handler) ────────────────
        if "monthly" in title or ("gross" in title and "income" in title):
            self._fill(frame, 'input[name="monthly_income"], input:visible', f["monthly_income"])
            time.sleep(1)
            return self._continue(frame)

        # ── Step 12: Next pay date ───────────────────────────────────────────
        if ("next" in title and "pay" in title) or "next pay" in title:
            chip = f.get("next_payday_choice") or "Next scheduled date"
            clicked = (
                self._chip(frame, chip)
                or self._chip(frame, "Next scheduled date")
                or self._chip(frame, "Next scheduled")
                or self._chip(frame, "In two weeks")
                or self._chip(frame, "Two weeks")
            )
            time.sleep(1)
            self._click_primary_continue(frame) or self._continue(frame)
            return clicked or "NEXT_PAY"

        # ── Step 13: Employer information ────────────────────────────────────
        if (
            "employer" in title
            or ("employ" in title and "info" in title)
            or "working right now" in title
            or ("where" in title and "working" in title)
        ):
            emp_phone = f.get("employer_phone") or f["phone"]
            if not self._react_fill_any(
                frame, ['input[name="employer_name"]'], f["employer_name"]
            ):
                self._fill_labeled_input(frame, "Employer Name", f["employer_name"])
            if not self._react_fill_any(
                frame, ['input[name="job_title"]'], f.get("job_title", "")
            ):
                self._fill_labeled_input(frame, "Job Title", f.get("job_title", ""))
            if not self._react_fill_any(
                frame, ['input[name="employer_phone"]'], emp_phone
            ):
                self._fill_labeled_input(frame, "Employer Phone", emp_phone)
            time.sleep(1)
            if self._click_primary_continue(frame):
                return "CONTINUE"
            return self._continue(frame)

        # ── Step 14: Paycheck received ───────────────────────────────────────
        if "paycheck" in title or ("received" in title and "pay" in title):
            return self._chip(frame, f.get("paycheck_method_chip") or "Direct Deposit")

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
            filled = self._fill_any(
                frame,
                [
                    'input[name="routing_number"]',
                    'input[name*="routing" i]',
                    'input[id*="routing" i]',
                    'input[placeholder*="routing" i]',
                ],
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
            self._fill_any(
                frame,
                ['input[name="bank_name"]', 'input[name*="bank" i]', "input:visible"],
                f["bank_name"],
            )
            time.sleep(1)
            return self._continue(frame)

        # ── Step 17: Type of bank account ───────────────────────────────────
        if "type" in title and ("bank" in title or "account" in title):
            chip_val = "Checking" if f["account_type"].lower().startswith("check") else "Savings"
            return self._chip(frame, chip_val)

        # ── Step 18: Length of bank account ─────────────────────────────────
        if ("length" in title or "how long" in title) and ("bank" in title or "account" in title):
            desired = f.get("account_age_chip") or "More than 2 Years"
            # Try exact, then common partials, then a JS-assisted click.
            result = (
                # LowCreditFinance: this option is present on every variant and is usually safe.
                self._chip(frame, "More than 2 Years")
                or self._chip(frame, desired)
                or self._chip(frame, "More than 2")
                or self._chip(frame, "More than 2 Years")
                or self._chip(frame, "1-2 Years")
                or self._chip(frame, "1-2")
                or self._chip(frame, "6-12")
                or self._chip(frame, "3-6")
                or self._chip(frame, "1-3")
            )
            if result:
                return result
            try:
                clicked = frame.evaluate(
                    """(desired) => {
                        const want = String(desired || '').toLowerCase();
                        const btns = Array.from(document.querySelectorAll('button'))
                          .filter(b => b && b.offsetParent !== null);
                        const pick = btns.find(b => (b.textContent||'').toLowerCase().includes(want))
                          || btns.find(b => (b.textContent||'').toLowerCase().includes('more than 2'));
                        if (!pick) return false;
                        pick.click();
                        return true;
                    }""",
                    desired,
                )
                if clicked:
                    return "JS_CLICK"
            except Exception:
                pass
            return None

        # ── Bank account number ──────────────────────────────────────────────
        if (
            ("account" in title and ("number" in title or "add" in title))
            or title == "account number"
        ) and "type" not in title and "length" not in title:
            self._fill_any(
                frame,
                [
                    'input[name="bank_account_number"]',
                    'input[name="account_number"]',
                    'input[name*="account" i]',
                    "input:visible",
                ],
                f["account_number"],
            )
            time.sleep(1)
            return self._continue(frame)

        # ── Phone number (personal) ──────────────────────────────────────────
        if "phone" in title or "mobile" in title or "cell" in title or "contact" in title:
            phone = f["phone"]
            if not self._react_fill_any(
                frame,
                [
                    'input[type="tel"].lcf-input',
                    'input[type="tel"]',
                    'input[name="phone"]',
                    'input[name*="phone" i]',
                ],
                phone,
            ):
                self._fill_nth_visible_input(frame, 0, phone)
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
            self._fill_any(
                frame,
                [
                    'input[name="drivers_license_number"]',
                    'input[name*="license" i]',
                    'input[name*="dl_" i]',
                    'input[name*="driver" i]',
                    "input:visible",
                ],
                f["dl_number"],
            )
            try:
                frame.locator('select[name*="state" i]').first.select_option(
                    value=f["dl_state"], timeout=3000
                )
            except Exception:
                pass
            time.sleep(1)
            return self._continue(frame)
        # ── Unsecured debt question ────────────────────────────────────────────
        if "debt" in title or "unsecured" in title:
            chip = f.get("unsecured_debt_chip") or "No"
            clicked = None
            try:
                frame.get_by_text(chip, exact=True).first.click(timeout=8000)
                clicked = chip
            except Exception:
                pass
            if not clicked:
                clicked = self._click_chip_by_label_js(frame, chip) or self._chip(frame, chip)
            if clicked:
                self._wait_title_change(frame, avoid=["debt", "unsecured", "10,000"], timeout=15)
                return clicked
            return None

        # ── Free trial upsell (step 24) ────────────────────────────────────────
        if "trial" in title or ("free" in title and "day" in title):
            desired = f.get("free_trial_choice_chip") or "Yes"
            for choice in (desired, "Yes", "No"):
                clicked = None
                try:
                    frame.get_by_text(choice, exact=True).first.click(timeout=8000)
                    clicked = choice
                except Exception:
                    clicked = self._click_chip_by_label_js(frame, choice) or self._chip(
                        frame, choice
                    )
                if clicked:
                    self._wait_title_change(frame, avoid=["trial", "free 7-day"], timeout=15)
                    return clicked
            return self._continue(frame)

        # ── Terms & conditions consent (checkbox) ─────────────────────────────
        if "terms" in title or "conditions" in title or "consent" in title:
            if f.get("terms_consent"):
                try:
                    frame.evaluate(
                        """() => {
                            const boxes = Array.from(document.querySelectorAll('input[type="checkbox"]'))
                              .filter(b => b && b.offsetParent !== null);
                            for (const b of boxes) { if (!b.checked) b.click(); }
                        }"""
                    )
                except Exception:
                    pass
            return self._continue(frame)
        # ── Submit / Request Cash (final step) ───────────────────────────────
        if "submit" in title or "loan request" in title or "request cash" in title:
            if os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes", "y"}:
                # Safety: do not actually submit in dry-run mode.
                try:
                    frame.page.screenshot(path=str(self._ss_dir / f"dry_run_before_submit.png"))
                except Exception:
                    pass
                return "DRY_RUN_STOP"
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

    def _site_profile(self, url: str) -> str:
        explicit = (self._target.get("site") or "").strip().lower()
        if explicit in {"borrowmoney", "borrow_money", "bm"}:
            return "borrowmoney"
        if explicit in {"superpersonalfinder", "super_personal", "spf", "superpersonal"}:
            return "superpersonalfinder"
        u = (url or "").lower()
        if "borrowmoney" in u:
            return "borrowmoney"
        if "superpersonalfinder" in u:
            return "superpersonalfinder"
        return "default"

    def _resolve_entry_url(self, url: str) -> str:
        """Normalize entry URL so automation lands on the iframe apply page."""
        u = (url or "").strip()
        low = u.lower()
        if "borrowmoney.us" in low:
            if "/apply" in low:
                return u if u.endswith("/") else u.rstrip("/") + "/"
            return "https://borrowmoney.us/apply/"
        if "superpersonalfinder" in low:
            return "https://superpersonalfinder.com/"
        return u

    def _prepare_entry_page(
        self,
        page: Page,
        url: str,
        fields: dict,
        row_number: int,
    ) -> None:
        """Ensure iframe form is loaded (site-specific landing CTAs)."""
        site = self._site_profile(url)

        if site == "superpersonalfinder":
            if any("iframe.global" in (f.url or "") for f in page.frames):
                log.info("form.spf_iframe_ready", row=row_number)
                return
            # The form iframe (#application-form, src=iframe.global) is present
            # in the host HTML but LAZY-LOADED: its frame URL stays empty until
            # it is scrolled into view. Clicking "Request Funds" only jumps the
            # anchor (#request); we must scroll the iframe into the viewport to
            # trigger the intersection-observer load (esp. in headless).
            for sel in (
                "a:has-text('Request Funds')",
                "text=Request Funds",
                "button:has-text('Request Funds')",
            ):
                try:
                    page.locator(sel).first.click(timeout=8000)
                    break
                except Exception:
                    continue
            for _ in range(25):
                # Scroll the iframe into view so the lazy-load observer fires
                # even if the anchor jump didn't.
                self._scroll_iframe_into_view(page)
                if any("iframe.global" in (f.url or "") for f in page.frames):
                    log.info("form.spf_iframe_ready", row=row_number)
                    return
                time.sleep(1)
            log.warning("form.spf_iframe_slow", row=row_number)
            return

        if site != "borrowmoney":
            return

        low = (page.url or "").lower()
        frame_urls = " ".join((f.url or "") for f in page.frames)
        if "/apply" in low and "iframe.global" in frame_urls:
            log.info("form.borrowmoney_apply_ready", row=row_number)
            return

        if "/apply" not in low:
            loan = fields.get("loan_amount_value") or "2000"
            try:
                slider = page.locator('input[type="range"], input#amount, input[name="amount"]').first
                if slider.is_visible(timeout=3000):
                    slider.fill(str(loan))
                    time.sleep(0.5)
            except Exception:
                pass
            for sel in (
                'a[href*="/apply"]',
                "a.btn-primary:has-text('Get Started')",
                "button:has-text('Get Started')",
            ):
                try:
                    page.locator(sel).first.click(timeout=8000)
                    time.sleep(4)
                    break
                except Exception:
                    continue
            if "/apply" not in (page.url or "").lower():
                page.goto("https://borrowmoney.us/apply/", wait_until="domcontentloaded", timeout=60000)
                time.sleep(4)

        for _ in range(25):
            if any("iframe.global" in (f.url or "") for f in page.frames):
                log.info("form.borrowmoney_iframe_ready", row=row_number)
                return
            time.sleep(1)
        log.warning("form.borrowmoney_iframe_slow", row=row_number)

    def _get_frame(self, page: Page, *, classify_only: bool = False) -> Frame:
        if (page.url or "").startswith("chrome-error://"):
            raise FormFillerError(
                f"Page failed to load ({page.url})",
                error_type="stuck",
            )
        poll = 0.2 if classify_only else 1
        checks = int(15 / poll) if classify_only else 30
        for _ in range(checks):
            frames = [
                f for f in page.frames
                if "iframe.global" in (f.url or "")
                and f != page.main_frame
            ]
            if frames:
                prefer = [
                    f for f in frames
                    if "BORROWMONEY" in (f.url or "").upper()
                    or "LOWCREDIT" in (f.url or "").upper()
                    or "style=lcf" in (f.url or "").lower()
                ]
                return prefer[0] if prefer else frames[0]
            # SPF's iframe is lazy-loaded on scroll. Actively nudge it into view
            # on every poll so the load fires even under concurrent CPU load
            # (passive waiting alone can time out with many parallel browsers).
            self._scroll_iframe_into_view(page)
            time.sleep(poll)
        log.warning("form.iframe_not_found", fallback="main_frame")
        return page.main_frame

    def _scroll_iframe_into_view(self, page: Page) -> None:
        """Trigger the lazy-loaded form iframe by scrolling it into the viewport."""
        try:
            page.evaluate(
                """() => {
                    const f = document.querySelector(
                        '#application-form, iframe[src*="iframe.global"]'
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

    def _wait_title_change(
        self, frame: Frame, avoid: list[str] | None = None, timeout: float = 12
    ) -> bool:
        """Wait until the step title changes (chip steps auto-advance)."""
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

    def _fill(self, frame: Frame, selector: str, value: str) -> bool:
        try:
            loc = frame.locator(selector).first
            loc.wait_for(state="visible", timeout=5000)
            loc.fill(str(value))
            return True
        except Exception as e:
            log.warning("form.fill_error", selector=selector[:60], error=str(e)[:80])
            return False

    def _react_set_value(self, frame: Frame, selector: str, value: str) -> bool:
        """Set input value using native setter + input/change/blur events."""
        try:
            return bool(
                frame.evaluate(
                    """([sel, val]) => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
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
                    [selector, str(value)],
                )
            )
        except Exception:
            return False

    def _has_named_input(self, frame: Frame, name: str) -> bool:
        try:
            return frame.locator(f'input[name="{name}"]').count() > 0
        except Exception:
            return False

    def _fill_ssn_parts(self, frame: Frame, ssn: str) -> bool:
        """Fill LowCredit split SSN fields: ssn_1 (3), ssn_2 (2), ssn_3 (4)."""
        digits = re.sub(r"\D", "", ssn or "")
        if len(digits) < 9:
            digits = (digits + "000000000")[:9]
        p1, p2, p3 = digits[:3], digits[3:5], digits[5:9]
        parts = [("ssn_1", p1), ("ssn_2", p2), ("ssn_3", p3)]
        ok = False
        for name, part in parts:
            if self._react_fill_any(frame, [f'input[name="{name}"]'], part):
                ok = True
            else:
                idx = int(name.split("_")[1]) - 1
                self._fill_nth_visible_input(frame, idx, part)
                ok = True
            try:
                frame.evaluate(
                    """(n) => {
                        const el = document.querySelector('input[name="' + n + '"]');
                        if (!el) return;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    }""",
                    name,
                )
            except Exception:
                pass
            time.sleep(0.3)
        return ok

    def _fill_any(self, frame: Frame, selectors: list[str], value: str) -> bool:
        """Try multiple selectors until one fills successfully."""
        for sel in selectors:
            if self._fill(frame, sel, value):
                return True
        return False

    def _react_fill_any(self, frame: Frame, selectors: list[str], value: str) -> bool:
        """Try React-safe setValue for the first selector that exists."""
        for sel in selectors:
            if self._react_set_value(frame, sel, value):
                return True
        return False

    def _count_visible_inputs(self, frame: Frame) -> int:
        try:
            return frame.locator("input:visible").count()
        except Exception:
            return 0

    def _fill_nth_visible_input(self, frame: Frame, n: int, value: str) -> bool:
        try:
            loc = frame.locator("input:visible").nth(n)
            loc.wait_for(state="visible", timeout=3000)
            loc.fill(str(value))
            return True
        except Exception:
            return False

    def _fill_labeled_input(self, frame: Frame, label_text: str, value: str) -> bool:
        """Fill an input that is visually identified by a floating label."""
        # This matches the current LCF iframe markup:
        # <div class="lcf-input-wrapper ..."><label>Employer Name</label><input ...></div>
        candidates = [
            f".lcf-input-wrapper:has(label:has-text('{label_text}')) input",
            f"div:has(label:has-text('{label_text}')) input",
        ]
        if self._fill_any(frame, candidates, value):
            return True
        # Fallback: if labels fail (e.g. different component), just fill any visible input.
        return self._fill_any(frame, ["input:visible"], value)

    def _split_dob(self, dob_mmddyyyy: str) -> tuple[str, str, str]:
        m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", dob_mmddyyyy or "")
        if not m:
            return "", "", ""
        mm, dd, yyyy = m.group(1), m.group(2), m.group(3)
        if len(yyyy) == 2:
            yyyy = "19" + yyyy
        return mm.zfill(2), dd.zfill(2), yyyy

    def _click_chip_by_label_js(self, frame: Frame, label: str) -> str | None:
        """Click an lcf-option chip by exact label text (e.g. Fair, Good)."""
        want = (label or "").strip()
        if not want:
            return None
        try:
            loc = frame.locator(
                f'button.lcf-option:has(.lcf-option-label:text-is("{want}"))'
            ).first
            if loc.count() > 0 and loc.is_visible(timeout=2000):
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click(timeout=8000)
                return want
        except Exception:
            pass
        try:
            return frame.evaluate(
                """(label) => {
                    const want = String(label || '').trim().toUpperCase();
                    const labels = Array.from(document.querySelectorAll('.lcf-option-label'));
                    for (const span of labels) {
                        const t = (span.textContent || '').trim().toUpperCase();
                        if (t !== want) continue;
                        const btn = span.closest('button');
                        if (btn) { btn.click(); return (span.textContent || '').trim(); }
                    }
                    return null;
                }""",
                want,
            )
        except Exception:
            return None

    def _click_loan_chip_js(self, frame: Frame, amount: str) -> str | None:
        """Click a loan amount chip by matching numeric value (avoids 2000 vs 20000)."""
        digits = re.sub(r"\D", "", str(amount or ""))
        if not digits:
            return None
        # Playwright fallback for LCF option buttons (Super Personal Finder)
        label = str(amount or "").strip()
        if not label.startswith("$") and digits:
            try:
                n = int(digits)
                label = f"${n:,}"
            except ValueError:
                pass
        try:
            loc = frame.locator("button.lcf-option").filter(has_text=label).first
            if loc.is_visible(timeout=1500):
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
                        ...document.querySelectorAll('button'),
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

    def _click_borrowmoney_pay_freq(self, frame: Frame, raw: str) -> str | None:
        """BorrowMoney style=2 pay-frequency-* button ids."""
        v = (raw or "").strip().lower()
        id_map = [
            (("biweek", "every 2", "2 week"), "pay-frequency-biweekly"),
            (("twice", "semi", "two time"), "pay-frequency-twice_monthly"),
            (("month",), "pay-frequency-monthly"),
            (("week",), "pay-frequency-weekly"),
        ]
        for keys, el_id in id_map:
            if any(k in v for k in keys):
                try:
                    frame.locator(f"#{el_id}").click(timeout=5000)
                    return el_id
                except Exception:
                    pass
        return None

    def _click_borrowmoney_income_chip(self, frame: Frame, raw: str) -> str | None:
        """BorrowMoney style=2 income-source-* button ids."""
        v = (raw or "").strip().lower()
        id_map = [
            (("employ", "job", "work"), "income-source-employed"),
            (("unemploy",), "income-source-unemployed"),
            (("benefit", "ssi", "disab"), "income-source-benefits"),
        ]
        for keys, el_id in id_map:
            if any(k in v for k in keys):
                try:
                    frame.locator(f"#{el_id}").click(timeout=5000)
                    return el_id
                except Exception:
                    pass
        return None

    def _click_primary_continue(self, frame: Frame) -> bool:
        try:
            loc = frame.locator("button.lcf-btn-primary").first
            if loc.is_visible(timeout=1500) and loc.is_enabled():
                loc.click(timeout=5000)
                return True
        except Exception:
            pass
        try:
            return bool(
                frame.evaluate(
                    """() => {
                        const candidates = [
                            document.querySelector('button.lcf-btn-primary'),
                            document.querySelector('#continue-button'),
                            document.querySelector('button#continue-button'),
                            ...Array.from(document.querySelectorAll('button')).filter(
                                b => /^continue$/i.test((b.textContent || '').trim())
                            ),
                        ];
                        for (const b of candidates) {
                            if (!b || b.offsetParent === null) continue;
                            if (b.disabled) continue;
                            b.click();
                            return true;
                        }
                        return false;
                    }"""
                )
            )
        except Exception:
            return False

    def _chip_label_matches(self, fragment: str, label: str) -> bool:
        """Match chip labels without false positives (e.g. 2000 vs $20,000)."""
        frag_norm = fragment.strip().upper()
        label_norm = label.strip().upper()
        if frag_norm == label_norm:
            return True
        frag_digits = re.sub(r"\D", "", fragment)
        label_digits = re.sub(r"\D", "", label)
        if frag_digits and label_digits:
            return frag_digits == label_digits
        return frag_norm in label_norm

    def _chip(self, frame: Frame, text_fragment: str) -> str | None:
        """Click a chip/option button by matching visible label text."""
        frag = text_fragment.strip().upper()
        if not frag:
            return None

        # Prefer LCF option buttons (most chip steps on LowCreditFinance).
        selectors = [
            "button.lcf-option",
            '[class*="lcf-option"]',
            "button",
            '[class*="chip"]',
            '[class*="option"]',
            '[class*="choice"]',
        ]

        def _label_text(el) -> str:
            try:
                inner = el.locator(".lcf-option-label").first
                if inner.count() > 0:
                    t = (inner.text_content() or "").strip()
                    if t:
                        return t
            except Exception:
                pass
            return (el.text_content() or "").strip()

        try:
            for sel in selectors:
                for el in frame.locator(sel).all():
                    try:
                        if not el.is_visible():
                            continue
                        t_raw = _label_text(el)
                        if not self._chip_label_matches(text_fragment, t_raw):
                            continue
                        try:
                            el.scroll_into_view_if_needed(timeout=2000)
                        except Exception:
                            pass
                        try:
                            el.click(timeout=8000)
                            return t_raw or frag
                        except Exception:
                            # JS click fallback (React sometimes blocks normal clicks)
                            clicked = frame.evaluate(
                                """(el) => { try { el.click(); return true; } catch(e) { return false; } }""",
                                el,
                            )
                            if clicked:
                                return t_raw or frag
                    except Exception:
                        continue
        except Exception as e:
            log.warning("form.chip_error", fragment=text_fragment, error=str(e)[:80])

        # Last resort: find any visible button containing the fragment
        try:
            clicked = frame.evaluate(
                """(frag) => {
                    const wantDigits = String(frag || '').replace(/\\D/g, '');
                    const wantText = String(frag || '').trim().toUpperCase();
                    const btns = Array.from(document.querySelectorAll('button'))
                      .filter(b => b && b.offsetParent !== null);
                    const pick = btns.find(b => {
                      const t = (b.textContent || '').trim();
                      const tUp = t.toUpperCase();
                      const tDigits = t.replace(/\\D/g, '');
                      if (wantDigits && tDigits) return wantDigits === tDigits;
                      if (wantText) return tUp === wantText || tUp.includes(wantText);
                      return false;
                    });
                    if (!pick) return false;
                    pick.click();
                    return true;
                }""",
                text_fragment,
            )
            if clicked:
                return text_fragment
        except Exception:
            pass
        return None

    def _continue(self, frame: Frame) -> str | None:
        if self._click_primary_continue(frame):
            return "CONTINUE"

        classify = getattr(self, "_classify_only", False)
        for kw in ["CONTINUE", "NEXT", "SUBMIT", "APPLY NOW", "APPLY", "GET STARTED", "REQUEST CASH"]:
            for attempt in range(2 if classify else 3):
                try:
                    for btn in frame.locator("button").all():
                        t = (btn.text_content() or "").strip().upper()
                        if kw in t and btn.is_visible() and btn.is_enabled():
                            btn.click(timeout=5000)
                            return kw
                except Exception:
                    pass
                if attempt < (1 if classify else 2):
                    time.sleep(0.3 if classify else 0.8)
        # Fallback: try to click the primary button even if disabled
        try:
            clicked = frame.evaluate(
                """() => {
                    const btn =
                      document.querySelector('button.lcf-btn-primary') ||
                      Array.from(document.querySelectorAll('button')).find(b => /continue|next|submit|apply|request/i.test(b.textContent||''));
                    if (!btn) return false;
                    try { btn.disabled = false; btn.removeAttribute('disabled'); } catch(e) {}
                    try { btn.click(); return true; } catch(e) { return false; }
                }"""
            )
            if clicked:
                return "FORCED_CONTINUE"
        except Exception:
            pass
        return None

    # ---------------------------------------------------------------- parsing

    def _parse_fields(self, row: dict, site: str = "default") -> dict:
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
            loan_int = 2000 if site == "borrowmoney" else 5000
        if site == "borrowmoney":
            loan_int = max(100, min(loan_int, 5000))
        loan_amount_chip = self._closest_loan_chip(loan_int, site=site)
        loan_amount_value = str(loan_int)

        # Credit
        credit_raw = g("Credit Score Rating", "Credit_Score")
        credit_chip = self._map_credit_chip(credit_raw) if credit_raw else ""
        if not credit_chip and credit_raw and not self._strict_sheet:
            credit_chip = "Fair"

        # Pay frequency
        pay_freq_raw = g("Pay Frequency", "Pay_Frequency")
        pay_freq_chip = self._PAY_FREQ_MAP.get(pay_freq_raw.lower().strip(), "")
        if not pay_freq_chip and pay_freq_raw and not self._strict_sheet:
            pay_freq_chip = "Biweekly"

        # Income source (chip step)
        income_source_raw = g("Income Source", "Source of Income")
        income_source_chip = self._map_income_source(income_source_raw)
        if not income_source_chip and not self._strict_sheet:
            income_source_chip = "Employed"

        # Military (chip step)
        active_military_chip = self._map_yes_no_chip(
            g("Active Military Status", "Active in Military?")
        )
        if not active_military_chip and not self._strict_sheet:
            active_military_chip = "No"

        # Next payday (usually a chip choice, not a date input on LCF)
        next_payday_choice = self._map_next_payday_choice(g("Next Payday", "Next Pay Date"))
        if not next_payday_choice and not self._strict_sheet:
            next_payday_choice = "Next scheduled"

        # Income
        monthly_income = re.sub(r"[,$\s]", "", g("Monthly Net Income ($)", "Monthly_Income"))
        if not monthly_income and not self._strict_sheet:
            monthly_income = "3000"

        # Employer
        employer_name = g("Employer Name", "Employer_Name")
        if not employer_name and not self._strict_sheet:
            employer_name = "Employer"
        job_title = g("Job Title")
        if not job_title and not self._strict_sheet:
            job_title = "Employee"
        employer_phone = re.sub(r"\D", "", g("Employer Work Phone", "Employer Phone"))
        if len(employer_phone) < 10:
            employer_phone = phone

        # Paycheck payment method
        paycheck_method_chip = self._map_paycheck_method(
            g("Paycheck Payment Method", "How Is Your Paycheck Received?")
        )
        if not paycheck_method_chip and not self._strict_sheet:
            paycheck_method_chip = "Direct Deposit"

        # Bank
        _routing_raw = g("ABA Routing Number", "routingNumber")
        # ABA routing numbers are 9 digits; Google Sheets drops leading zeros
        routing_number = _routing_raw.zfill(9) if _routing_raw else ""
        account_number = g("Account Number", "accountNumber")
        account_type = g("Account Type", "bankAccountType")
        if not account_type and not self._strict_sheet:
            account_type = "Checking"
        bank_name = g("Bank Name", "bankName")
        if not bank_name and not self._strict_sheet:
            bank_name = "Chase"

        # Bank account age (chip step)
        account_age_chip = self._map_account_age(g("Account Age", "Length of Bank Account"))
        if not account_age_chip and not self._strict_sheet:
            account_age_chip = "More than 2 Years"

        # Driver's license — new sheet has a dedicated DL state column
        dl_number = g("Driver License / ID Number", "driversLicenseNumber")
        dl_state_raw = g("Driver License State", "bankState")  # fall back to bankState
        dl_state = self._normalize_state(dl_state_raw) if dl_state_raw else state

        # Loan purpose
        loan_purpose_chip = self._map_loan_purpose(g("Loan Purpose", "Loan_Purpose"))

        # Free credit score trial choice (Yes/No)
        free_trial_choice_chip = self._map_yes_no_chip(
            g("FREE Credit Score Trial Choice", "Free Trial")
        )
        if not free_trial_choice_chip:
            free_trial_choice_chip = "Yes"

        # Unsecured debt amount (Yes/No question on form; decide based on amount)
        unsecured_debt_chip = self._map_unsecured_debt(g("Unsecured Debt Amount", "Debt Amount"))
        if not unsecured_debt_chip and not self._strict_sheet:
            unsecured_debt_chip = "No"

        # Terms consent (checkbox step)
        terms_consent = self._truthy(g("Terms and Conditions Consent", "Terms Consent", "Consent"))

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
            "loan_amount_value": loan_amount_value,
            "credit_chip": credit_chip,
            "pay_freq_raw": pay_freq_raw,
            "pay_freq_chip": pay_freq_chip,
            "income_source_raw": income_source_raw,
            "income_source_chip": income_source_chip,
            "active_military_chip": active_military_chip,
            "next_payday_choice": next_payday_choice,
            "monthly_income": monthly_income,
            "employer_name": employer_name,
            "job_title": job_title,
            "employer_phone": employer_phone,
            "paycheck_method_chip": paycheck_method_chip,
            "routing_number": routing_number,
            "account_number": account_number,
            "account_type": account_type,
            "bank_name": bank_name,
            "account_age_chip": account_age_chip,
            "dl_number": dl_number,
            "dl_state": dl_state,
            "loan_purpose_chip": loan_purpose_chip,
            "free_trial_choice_chip": free_trial_choice_chip,
            "unsecured_debt_chip": unsecured_debt_chip,
            "terms_consent": terms_consent,
        }

    def _validate_required_fields(self, f: dict, row_number: int | None = None) -> None:
        required = [
            "first_name", "last_name", "email", "phone",
            "last_ssn", "dob", "zip", "street_address", "city", "state",
            "loan_amount_value", "credit_chip", "monthly_income",
            "employer_name", "routing_number", "account_number", "bank_name",
            "pay_freq_chip", "income_source_chip", "active_military_chip",
            "paycheck_method_chip", "account_type", "account_age_chip",
        ]
        if not self._strict_sheet:
            required = required[:11]
        missing = [k for k in required if not f.get(k)]
        if missing:
            prefix = f"Sheet row {row_number}: " if row_number else ""
            raise FormFillerError(
                f"{prefix}missing required column data: {missing}. "
                "Fill these cells in the Google Sheet.",
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

    def _truthy(self, raw: str) -> bool:
        v = (raw or "").strip().lower()
        return v in {"1", "true", "yes", "y", "checked", "on", "agree", "i agree"}

    def _map_yes_no_chip(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if not v:
            return ""
        if v in {"yes", "y", "true", "1"}:
            return "Yes"
        if v in {"no", "n", "false", "0"}:
            return "No"
        if "yes" in v:
            return "Yes"
        if "no" in v:
            return "No"
        return ""

    def _map_income_source(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if not v:
            return ""
        # LowCreditFinance chip labels
        if "employ" in v or "job" in v or "work" in v:
            return "Employed"
        if "self" in v:
            return "Self employment"
        if "benefit" in v or "ssi" in v or "disab" in v:
            return "Benefits"
        if "unemploy" in v:
            return "Unemployed"
        if "retir" in v:
            return "Retired"
        return raw.strip()

    def _map_paycheck_method(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if not v:
            return ""
        if "direct" in v:
            return "Direct Deposit"
        if "check" in v or "cheque" in v:
            return "Paper Check"
        if "cash" in v:
            return "Cash"
        if "debit" in v or "card" in v:
            return "Debit Card"
        return raw.strip()

    def _map_account_age(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if not v:
            return ""
        if "less" in v and "month" in v:
            return "Less than 1 Month"
        if v.startswith("1-3") or ("1" in v and "3" in v and "month" in v):
            return "1-3 Months"
        if v.startswith("3-6") or ("3" in v and "6" in v and "month" in v):
            return "3-6 Months"
        if "6-12" in v or ("6" in v and "12" in v and "month" in v):
            return "6-12 Months"
        if "1-2" in v and "year" in v:
            return "1-2 Years"
        if "more" in v and "2" in v and "year" in v:
            return "More than 2 Years"
        if "2" in v and "year" in v:
            return "More than 2 Years"
        return raw.strip()

    def _map_next_payday_choice(self, raw: str) -> str:
        v = (raw or "").strip().lower()
        if not v:
            return ""
        if "next" in v and ("sched" in v or "scheduled" in v):
            return "Next scheduled"
        if re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", raw.strip()):
            return "Next scheduled"
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw.strip()):
            return "Next scheduled"
        return raw.strip()

    def _map_unsecured_debt(self, raw: str) -> str:
        v = (raw or "").strip()
        if not v:
            return ""
        yn = self._map_yes_no_chip(v)
        if yn:
            return yn
        try:
            amt = float(re.sub(r"[^\d.]", "", v) or "0")
            return "Yes" if amt > 0 else "No"
        except Exception:
            return "No"

    def _closest_loan_chip(self, amount: int, site: str = "default") -> str:
        chips = (
            self._LOAN_AMOUNT_CHIPS_BORROWMONEY
            if site == "borrowmoney"
            else self._LOAN_AMOUNT_CHIPS
        )
        closest = min(chips, key=lambda x: abs(x - amount))
        return f"${closest:,}"

    def _map_credit_chip(self, raw: str) -> str:
        raw = raw.lower().strip()
        direct = {
            "poor": "Poor",
            "fair": "Fair",
            "good": "Good",
            "excellent": "Excellent",
            "not sure": "Not Sure",
        }
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
            return "Personal"
        # LowCreditFinance purpose chips:
        # Personal | Business | Home | Vehicle | Education | Other
        if "business" in raw:
            return "Business"
        if "home" in raw or "house" in raw or "improv" in raw:
            return "Home"
        if "auto" in raw or "car" in raw or "vehicle" in raw:
            return "Vehicle"
        if "educ" in raw or "school" in raw or "college" in raw:
            return "Education"
        # Debt consolidation and medical typically fall under "Personal"
        if "debt" in raw or "consol" in raw:
            return "Personal"
        if "medical" in raw or "health" in raw:
            return "Personal"
        return "Other"

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

