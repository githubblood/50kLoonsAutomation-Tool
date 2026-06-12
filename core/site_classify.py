"""
core/site_classify.py — Website duplicate / fresh detection helpers.

Used by form fillers in classify-only mode: fill through email + SSN,
then stop based on how the live form responds (not Google Sheet rows).
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

import structlog

from core.exceptions import FormFillerError

log = structlog.get_logger(__name__)

_PAGE_CTA_SELECTORS = (
    "button:has-text('Get Started')",
    "a:has-text('Get Started')",
    "button:has-text('Continue')",
    "a:has-text('Continue')",
    "button:has-text('Apply Now')",
    "button:has-text('Apply')",
    "button:has-text('Request Funds')",
    "button:has-text('Request')",
    "button.btn-primary",
    "a.btn-primary",
    "button.lcf-btn-primary",
    'input[type="range"]',
)


def click_page_ctas(page: Any, *, loan_amount: str = "") -> None:
    """Click common landing CTAs and optionally set a loan slider on the host page."""
    if loan_amount:
        for sel in ('input[type="range"]', "input#amount", 'input[name="amount"]'):
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    loc.fill(str(loan_amount))
                    time.sleep(0.1)
                    break
            except Exception:
                pass
    for sel in _PAGE_CTA_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=400):
                loc.click(timeout=2000)
                time.sleep(0.3)
        except Exception:
            pass
    try:
        page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('button,a')) {
                    const t = (el.textContent || '').toLowerCase();
                    if (/get started|continue|apply now|apply|request funds|request|start now/.test(t)) {
                        try { el.click(); } catch (e) {}
                    }
                }
            }"""
        )
    except Exception:
        pass


def has_iframe_global(page: Any) -> bool:
    return any("iframe.global" in (f.url or "") for f in page.frames)


def ensure_iframe_global(
    page: Any,
    *,
    timeout_s: float = 45,
    loan_amount: str = "",
) -> None:
    """Wait for iframe.global, clicking host-page CTAs until it appears."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if has_iframe_global(page):
            log.info("form.iframe_ready")
            return
        click_page_ctas(page, loan_amount=loan_amount)
        try:
            page.locator(
                'iframe[src*="iframe.global"], iframe[src*="global"]'
            ).first.wait_for(state="attached", timeout=1000)
        except Exception:
            pass
        if has_iframe_global(page):
            log.info("form.iframe_ready")
            return
        time.sleep(0.3)
    raise FormFillerError(
        "Loan form iframe did not load on website",
        error_type="stuck",
    )


def _is_ssn_title(title: str) -> bool:
    t = title.lower()
    return (
        "ssn" in t
        or ("social" in t and "secur" in t)
        or "verify your identity" in t
        or ("last" in t and "4" in t and "digit" in t)
        or "last 4" in t
    )


def validate_classify_fields(fields: dict, *, row_number: int | None = None) -> None:
    """Only email + SSN are required to check duplicate/fresh on the website."""
    missing = [k for k in ("email", "last_ssn") if not fields.get(k)]
    if missing:
        prefix = f"Sheet row {row_number}: " if row_number else ""
        raise FormFillerError(
            f"{prefix}missing email or SSN for website check: {missing}",
            error_type="missing_data",
        )


def _is_pre_classify_title(title: str) -> bool:
    """Steps before we consider the lead 'fresh' on the website.

    Includes transient verification / loading spinners (e.g. "verifying your
    information") shown right after the SSN step. These are NOT a real next
    question — deciding 'fresh' on them is premature, because a returning lead
    only jumps to the end (offers / ~93%) *after* this screen resolves.
    """
    t = title.lower()
    if _is_ssn_title(t):
        return True
    return any(
        kw in t
        for kw in (
            "email",
            "how much",
            "amount",
            "loan",
            "verify your identity",
            "verifying",
            "checking your",
            "please wait",
            "one moment",
            "loading",
            "borrow up to",      # superpersonalfinder.com intro splash
            "bad credit",        # same splash
        )
    )


def raise_if_site_duplicate(
    title: str,
    *,
    page: Any,
    row_number: int,
    step_num: int,
    screenshot: Callable[[Any, int, str], None],
    first_name: str = "",
) -> None:
    """Raise FormFillerError(duplicate) when the website signals a returning lead."""
    t = title.lower().strip()

    # Personalised greeting after SSN (e.g. "Jennifer!"). In classify mode we
    # only enter email + SSN — never the name — so the site can only display the
    # lead's first name if it pulled it from existing records → already a lead →
    # Duplicate. Guard with a short, non-question title to avoid matching real
    # questions that happen to contain a common name.
    fname = (first_name or "").strip().lower()
    if (
        fname
        and len(fname) >= 3
        and step_num >= 2
        and len(t) <= 30
        and "?" not in t
        and re.search(r"\b" + re.escape(fname) + r"\b", t)
    ):
        screenshot(page, row_number, "duplicate_welcome_back")
        log.warning(
            "form.duplicate_detected",
            reason="name_greeting",
            step=step_num,
            title=t[:80],
            row=row_number,
        )
        raise FormFillerError(
            f"Duplicate: site greeted lead by name ('{title}') at step {step_num}",
            error_type="duplicate",
        )

    if "welcome back" in t:
        screenshot(page, row_number, "duplicate_welcome_back")
        log.warning(
            "form.duplicate_detected",
            reason="welcome_back",
            step=step_num,
            title=t[:80],
            row=row_number,
        )
        raise FormFillerError(
            f"Website duplicate: 'welcome back' at step {step_num}",
            error_type="duplicate",
        )

    is_submit = "submit" in t or "loan request" in t or "request cash" in t
    if is_submit and step_num < 10:
        screenshot(page, row_number, "duplicate_auto_jump")
        log.warning(
            "form.duplicate_detected",
            reason="auto_jump_to_submit",
            step=step_num,
            title=t[:80],
            row=row_number,
        )
        raise FormFillerError(
            f"Website duplicate: auto-jumped to submit at step {step_num}",
            error_type="duplicate",
        )


def maybe_fresh_at_step_start(
    *,
    classify_only: bool,
    title: str,
    step_num: int,
    row_number: int,
    page: Any,
    screenshot: Callable[[Any, int, str], None],
    first_name: str = "",
) -> bool:
    """If classify-only and the form already advanced past SSN, mark Fresh."""
    if not classify_only or step_num < 2:
        return False
    t = title.lower().strip()
    if not t or _is_pre_classify_title(t):
        return False
    raise_if_site_duplicate(
        t,
        page=page,
        row_number=row_number,
        step_num=step_num,
        screenshot=screenshot,
        first_name=first_name,
    )
    log.info("form.fresh_on_site", row=row_number, step=t[:80])
    return True


def finish_if_classified_on_site(
    *,
    classify_only: bool,
    frame: Any,
    page: Any,
    title: str,
    step_num: int,
    row_number: int,
    get_title: Callable[[Any], str],
    screenshot: Callable[[Any, int, str], None],
    wait_after_step: Callable[[], None],
    first_name: str = "",
) -> bool:
    """
    After the SSN step is submitted, wait for the next screen.

    Returns True when the website treated the lead as fresh (advanced past SSN).
    Raises FormFillerError(duplicate) when the website signals a returning lead.
    """
    if not classify_only or not _is_ssn_title(title):
        return False

    wait_after_step()
    try:
        nxt = get_title(frame).lower().strip()
    except Exception:
        nxt = ""

    if nxt:
        raise_if_site_duplicate(
            nxt,
            page=page,
            row_number=row_number,
            step_num=step_num,
            screenshot=screenshot,
            first_name=first_name,
        )

    cur = title.lower().strip()
    if nxt and nxt != cur and not _is_pre_classify_title(nxt):
        log.info("form.fresh_on_site", row=row_number, next_step=nxt[:80])
        return True
    return False
