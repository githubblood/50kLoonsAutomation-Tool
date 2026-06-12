#!/usr/bin/env python3
"""
main.py — Lead Automation Entry Point
──────────────────────────────────────
Reads pending rows from Google Sheets (all 4 tabs) and classifies each
as Duplicate or Fresh on the live website via email / SSN (no full form).
"""
from __future__ import annotations

import importlib
import logging
import logging.handlers
import os
import signal
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

# Force IPv4 for all urllib3/requests connections (IPv6 unavailable on this network)
import urllib3.util.connection as _urllib3_cn
_urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

import requests

import structlog
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from tqdm import tqdm

from utils.sheet_handler import SheetHandler

# One tab per offer — website check via matching form filler
WORKSHEETS: list[tuple[str, str, str, str]] = [
    (
        "50k Loans",
        os.getenv("SHEET_WS_50K", "Sheet1"),
        "core.form_filler",
        "https://50kloans.com/",
    ),
    (
        "BorrowMoney",
        os.getenv("SHEET_WS_BORROW_MONEY", "Sheet2"),
        "core.form_filler_borrowmoney",
        "https://borrowmoney.us",
    ),
    (
        "Low Credit",
        os.getenv("SHEET_WS_LOW_CREDIT", "Sheet3"),
        "core.form_filler_lowcredit",
        "https://lowcreditfinance.com",
    ),
    (
        "Super Personal",
        os.getenv("SHEET_WS_SUPER_PERSONAL", "Sheet4"),
        "core.form_filler_superpersonal",
        "https://superpersonalfinder.com",
    ),
]

# ── Bootstrap ────────────────────────────────────────────────────────

load_dotenv()  # Load .env into os.environ

console = Console()

_stop_flag: list[bool] = [False]


def _on_sigterm(signum: int, frame: object) -> None:  # noqa: ARG001
    _stop_flag[0] = True


signal.signal(signal.SIGTERM, _on_sigterm)


def _get_outbound_ip(proxy_url: str | None) -> str:
    """Return the actual outbound IP address, routing through proxy if supplied."""
    try:
        kwargs: dict = {"timeout": 10}
        if proxy_url:
            kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        return requests.get("https://api.ipify.org?format=text", **kwargs).text.strip()
    except Exception:
        if proxy_url:
            return urlparse(proxy_url).hostname or "unknown"
        return "direct"


def load_config(path: str = "config.yaml") -> dict:
    """Load and return the YAML configuration file."""
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logging(config: dict) -> None:
    """Configure structlog → stdlib bridge writing to console + rotating file."""
    log_cfg = config.get("logging", {})
    level = log_cfg.get("level", os.getenv("LOG_LEVEL", "INFO"))
    log_file = log_cfg.get("log_file", "logs/automation.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    level_num = getattr(logging, level.upper(), logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(),
        ],
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level_num)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


# ── Main loop ────────────────────────────────────────────────────────

def main() -> None:
    config = load_config()
    setup_logging(config)
    log = structlog.get_logger("main")

    console.print("\n[bold cyan]╔══════════════════════════════════════╗[/]")
    console.print("[bold cyan]║   🚀  Lead Automation Engine  🚀    ║[/]")
    console.print("[bold cyan]╚══════════════════════════════════════╝[/]\n")

    sheet_url = os.getenv("GOOGLE_SHEET_URL", "")
    console.print("[cyan]ℹ  Website classify mode: Duplicate / Fresh (email + SSN on site)[/]")
    if sheet_url:
        short = sheet_url if len(sheet_url) <= 60 else sheet_url[:60] + "…"
        console.print(f"[cyan]ℹ  Workbook: {short}[/]\n")

    direct_ip = _get_outbound_ip(None)

    totals = {"fresh": 0, "duplicate": 0, "failed": 0, "processed": 0}
    per_sheet: list[tuple[str, str, dict[str, int]]] = []

    only_worksheet = os.getenv("ONLY_WORKSHEET", "").strip()

    for offer_name, worksheet, filler_mod, target_url in WORKSHEETS:
        if only_worksheet and worksheet != only_worksheet:
            continue
        if _stop_flag[0]:
            break

        console.print(f"[bold magenta]── {offer_name} ({worksheet}) ──[/]")

        try:
            mod = importlib.import_module(filler_mod)
        except ModuleNotFoundError as e:
            console.print(f"  [bold red]✗ Form filler not found: {filler_mod}[/]")
            log.error("filler.missing", module=filler_mod, error=str(e))
            continue

        from core.exceptions import FormFillerError

        FormFiller = mod.FormFiller

        offer_config = dict(config)
        offer_config["target"] = {**config.get("target", {}), "url": target_url}
        ss_dir = Path(f"screenshots/{worksheet}")
        ss_dir.mkdir(parents=True, exist_ok=True)
        offer_config.setdefault("screenshots", {})["directory"] = str(ss_dir)

        try:
            sheet = SheetHandler(config, worksheet_name=worksheet, sheet_url=sheet_url)
        except Exception as e:
            console.print(f"  [bold red]✗ Failed to open {worksheet}:[/] {e}")
            log.error("sheet.connect_failed", worksheet=worksheet, error=str(e))
            continue

        form_filler = FormFiller(offer_config)

        pending = sheet.get_pending_rows()
        if not pending:
            console.print(f"  [yellow]⚠  No pending rows on {worksheet}[/]")
            per_sheet.append((offer_name, worksheet, {"fresh": 0, "duplicate": 0, "failed": 0, "pending": 0}))
            continue

        console.print(f"  [green]✓ {len(pending)} pending row(s) — checking on website[/]")
        stats = {"fresh": 0, "duplicate": 0, "failed": 0}

        for row in tqdm(pending, desc=f"{worksheet}", unit="row"):
            if _stop_flag[0]:
                log.warning("main.stop_requested", msg="SIGTERM received — stopping cleanly")
                break

            row_num = row["_row_number"]
            log.info("row.start", worksheet=worksheet, row=row_num, offer=offer_name)

            try:
                result = form_filler.classify_lead_on_site(
                    row=row,
                    proxy_url=None,
                    row_number=row_num,
                )
                status = result.get("status", "Fresh")
                notes = result.get("notes", "")
                sheet.update_row(
                    row_num,
                    status=status,
                    notes=notes,
                    proxy_used="direct",
                    ip=direct_ip,
                )
                if status == "Duplicate":
                    stats["duplicate"] += 1
                    console.print(f"  [yellow]⚠ {worksheet} row {row_num} — Duplicate (website)[/]")
                else:
                    stats["fresh"] += 1
                    console.print(f"  [green]✓ {worksheet} row {row_num} — Fresh (website)[/]")

            except FormFillerError as e:
                sheet.update_row(
                    row_num,
                    status="Failed",
                    notes=f"[{e.error_type}] {e}",
                    proxy_used="direct",
                    ip=direct_ip,
                )
                stats["failed"] += 1
                console.print(f"  [red]✗ {worksheet} row {row_num} — Failed ({e.error_type})[/]")
                log.warning("row.classify_failed", row=row_num, error=str(e))

        totals["fresh"] += stats["fresh"]
        totals["duplicate"] += stats["duplicate"]
        totals["failed"] += stats["failed"]
        totals["processed"] += len(pending)
        per_sheet.append((offer_name, worksheet, {**stats, "pending": len(pending)}))
        console.print()

    if not per_sheet:
        console.print("[yellow]⚠  Could not process any worksheets.[/]")
        return

    console.print()
    table = Table(title="📊 Run Summary (website check, all sheets)", show_header=True, header_style="bold magenta")
    table.add_column("Offer", style="cyan")
    table.add_column("Tab", style="dim")
    table.add_column("Fresh", justify="right", style="green")
    table.add_column("Duplicate", justify="right", style="yellow")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Rows", justify="right", style="bold")
    for offer_name, worksheet, s in per_sheet:
        table.add_row(
            offer_name,
            worksheet,
            str(s.get("fresh", 0)),
            str(s.get("duplicate", 0)),
            str(s.get("failed", 0)),
            str(s.get("pending", 0)),
        )
    table.add_section()
    table.add_row(
        "TOTAL",
        "",
        str(totals["fresh"]),
        str(totals["duplicate"]),
        str(totals["failed"]),
        str(totals["processed"]),
    )
    console.print(table)
    console.print()


if __name__ == "__main__":
    main()
