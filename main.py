#!/usr/bin/env python3
"""
main.py — Lead Automation Entry Point
──────────────────────────────────────
Reads pending rows from Google Sheets, rotates proxy + device
fingerprint per row, fills a web form, and writes results back.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import socket
import sys
import time
import traceback
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
from utils.proxy_manager import ProxyManager
from utils.device_manager import DeviceManager
from core.form_filler import FormFiller, FormFillerError

# ── Bootstrap ────────────────────────────────────────────────────────

load_dotenv()  # Load .env into os.environ

console = Console()

# ── Graceful shutdown ────────────────────────────────────────────────
# Set by SIGTERM handler; main loop checks this flag between rows so the
# current row always completes and its status is written before we exit.
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

    # Initialise components
    try:
        sheet = SheetHandler(config)
    except Exception as e:
        console.print(f"[bold red]✗ Google Sheets connection failed:[/] {e}")
        sys.exit(1)

    proxy_mgr = ProxyManager()
    device_mgr = DeviceManager(config)
    form_filler = FormFiller(config)

    retry_cfg = config.get("retry", {})
    max_retries = retry_cfg.get("max_retries", 3)
    backoff_base = retry_cfg.get("backoff_base", 2)
    backoff_max = retry_cfg.get("backoff_max", 30)

    # Fetch pending rows
    pending = sheet.get_pending_rows()
    if not pending:
        console.print("[yellow]⚠  No pending rows found. Nothing to do.[/]")
        return

    console.print(f"[green]✓ Found {len(pending)} pending row(s)[/]")
    if proxy_mgr.has_proxies:
        console.print(f"[green]✓ Loaded {proxy_mgr.total} proxy/proxies[/]")
    else:
        console.print("[yellow]⚠ No proxies loaded — running direct[/]")

    # ── Process each row ─────────────────────────────────────────
    stats = {"success": 0, "failed": 0, "retry": 0, "duplicate": 0}

    for row in tqdm(pending, desc="Processing rows", unit="row"):
        if _stop_flag[0]:
            log.warning("main.stop_requested", msg="SIGTERM received — stopping cleanly")
            break

        row_num = row["_row_number"]
        log.info("row.start", row=row_num)

        # Lock the row
        sheet.mark_in_progress(row_num)

        # Get current retry count from sheet
        rc_col = config.get("sheet_columns", {}).get("retry_count", "Retry_Count")
        retry_count = int(row.get(rc_col, 0) or 0)

        attempt = 0
        success = False

        while attempt <= max_retries and not success:
            # Rotate proxy + fingerprint per attempt
            proxy_url = proxy_mgr.next_proxy()
            fingerprint = device_mgr.build_fingerprint(row)
            proxy_display = proxy_url or "direct"
            proxy_type = ProxyManager.proxy_type(proxy_url)

            # Get the actual outbound IP (through proxy if set)
            proxy_ip = _get_outbound_ip(proxy_url)

            log.info("row.attempt", row=row_num, attempt=attempt + 1,
                     proxy_type=proxy_type,
                     proxy=ProxyManager._mask(proxy_url) if proxy_url else "direct",
                     proxy_ip=proxy_ip or "direct")

            try:
                result = form_filler.process_row(
                    row=row,
                    fingerprint=fingerprint,
                    proxy_url=proxy_url,
                    row_number=row_num,
                )
                # Success!
                sheet.update_row(
                    row_num,
                    status="Success",
                    notes=result.get("notes", ""),
                    proxy_used=proxy_display,
                    ip=proxy_ip,
                    submission_id=result.get("submission_id", ""),
                    retry_count=retry_count + attempt,
                )
                stats["success"] += 1
                success = True
                console.print(f"  [green]✓ Row {row_num} — Success[/]")

            except FormFillerError as e:
                if e.error_type == "duplicate":
                    sheet.update_row(
                        row_num,
                        status="Duplicate",
                        notes=f"[duplicate] {e}",
                        proxy_used=proxy_display,
                        ip=proxy_ip,
                        retry_count=retry_count + attempt,
                    )
                    stats["duplicate"] += 1
                    success = True
                    console.print(f"  [yellow]⚠ Row {row_num} — Duplicate[/]")
                    break

                if e.error_type == "missing_data":
                    sheet.update_row(
                        row_num,
                        status="Failed",
                        notes=f"[missing_data] {e}",
                        proxy_used=proxy_display,
                        ip=proxy_ip,
                        retry_count=retry_count + attempt,
                    )
                    stats["failed"] += 1
                    success = True
                    console.print(f"  [red]✗ Row {row_num} — Failed (missing_data)[/]")
                    break

                attempt += 1
                retry_count_now = retry_count + attempt
                log.warning("row.error", row=row_num, attempt=attempt,
                            error_type=e.error_type, msg=str(e))

                if attempt > max_retries:
                    # Exhausted retries
                    sheet.update_row(
                        row_num,
                        status="Failed",
                        notes=f"[{e.error_type}] {e} (after {attempt} attempts)",
                        proxy_used=proxy_display,
                        ip=proxy_ip,
                        retry_count=retry_count_now,
                    )
                    stats["failed"] += 1
                    console.print(f"  [red]✗ Row {row_num} — Failed ({e.error_type})[/]")
                else:
                    # Mark for retry and backoff
                    delay = min(backoff_base ** attempt, backoff_max)
                    sheet.update_row(
                        row_num,
                        status="Retry",
                        notes=f"[{e.error_type}] {e} — retrying in {delay}s",
                        proxy_used=proxy_display,
                        ip=proxy_ip,
                        retry_count=retry_count_now,
                    )
                    stats["retry"] += 1
                    log.info("row.backoff", row=row_num, delay=delay)
                    time.sleep(delay)

            except Exception as e:
                # Unexpected error — mark failed, move on
                attempt += 1
                log.error("row.unexpected_error", row=row_num, error=str(e),
                          traceback=traceback.format_exc())
                sheet.update_row(
                    row_num,
                    status="Failed",
                    notes=f"[unexpected] {e}",
                    proxy_used=proxy_display,
                    ip=proxy_ip,
                    retry_count=retry_count + attempt,
                )
                stats["failed"] += 1
                console.print(f"  [red]✗ Row {row_num} — Unexpected error[/]")
                break  # Don't retry unexpected errors

    # ── Summary ──────────────────────────────────────────────────
    console.print()
    table = Table(title="📊 Run Summary", show_header=True, header_style="bold magenta")
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="bold")
    table.add_row("✅ Success", str(stats["success"]))
    table.add_row("🟨 Duplicate", str(stats["duplicate"]))
    table.add_row("❌ Failed", str(stats["failed"]))
    table.add_row("🔄 Retried (intermediate)", str(stats["retry"]))
    table.add_row("📋 Total Processed", str(len(pending)))
    console.print(table)
    console.print()


if __name__ == "__main__":
    main()
