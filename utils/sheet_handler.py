"""
utils/sheet_handler.py
──────────────────────
Google Sheets integration via Service Account.

Responsibilities:
  • Authenticate with Google using a service-account JSON key.
  • Fetch all rows where Status == "Pending".
  • Lock a row by setting Status → "In Progress".
  • Write back results (Success / Failed / Retry) with metadata.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
import structlog

log = structlog.get_logger(__name__)

# Google API scopes required for Sheets + Drive (read/write)
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetHandler:
    """Manages all interactions with a single Google Sheet worksheet."""

    def __init__(self, config: dict) -> None:
        """
        Args:
            config: Parsed application config dict (from config.yaml + .env).
        """
        self._config = config
        self._col_map: dict[str, str] = config.get("sheet_columns", {})

        # Authenticate
        creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/service_account.json")
        credentials = Credentials.from_service_account_file(creds_path, scopes=_SCOPES)
        client = gspread.authorize(credentials)

        # Open the sheet
        sheet_url = os.getenv("GOOGLE_SHEET_URL", "")
        worksheet_name = os.getenv("GOOGLE_SHEET_WORKSHEET", "Sheet1")

        if sheet_url.startswith("http"):
            spreadsheet = client.open_by_url(sheet_url)
        else:
            # Treat as spreadsheet ID
            spreadsheet = client.open_by_key(sheet_url)

        self._worksheet = spreadsheet.worksheet(worksheet_name)
        self._headers: list[str] = self._worksheet.row_values(1)
        log.info("sheet.connected", worksheet=worksheet_name, columns=len(self._headers))

    # ── helpers ──────────────────────────────────────────────────────

    def _col_index(self, internal_name: str) -> int:
        """Return 1-based column index for an internal field name."""
        header = self._col_map.get(internal_name, internal_name)
        try:
            return self._headers.index(header) + 1
        except ValueError:
            raise KeyError(f"Column '{header}' not found in sheet headers: {self._headers}")

    def _now_iso(self) -> str:
        """Current UTC timestamp in ISO-8601."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── public API ───────────────────────────────────────────────────

    def get_pending_rows(self) -> list[dict[str, Any]]:
        """
        Return all rows where the Status column == 'Pending'.

        Each dict contains:
          • ``_row_number``  – the 1-based sheet row (for updates)
          • every column header → cell value
        """
        all_records = self._worksheet.get_all_records()
        status_col = self._col_map.get("status", "Status")
        pending: list[dict[str, Any]] = []

        for idx, record in enumerate(all_records, start=2):  # row 1 = header
            if str(record.get(status_col, "")).strip().lower() == "pending":
                record["_row_number"] = idx
                pending.append(record)

        log.info("sheet.pending_rows", count=len(pending))
        return pending

    def mark_in_progress(self, row_number: int) -> None:
        """Set Status → 'In Progress' for a given row."""
        col = self._col_index("status")
        self._worksheet.update_cell(row_number, col, "In Progress")
        log.debug("sheet.status_update", row=row_number, status="In Progress")

    def update_row(
        self,
        row_number: int,
        *,
        status: str,
        notes: str = "",
        proxy_used: str = "",
        ip: str = "",
        submission_id: str = "",
        retry_count: int | None = None,
    ) -> None:
        """
        Write result metadata back to the sheet for one row.

        Args:
            row_number: 1-based row in the worksheet.
            status:     "Success", "Failed", or "Retry".
            notes:      Human-readable description of what happened.
            proxy_used: The proxy address used for this attempt.
            ip:         The proxy IP address (stored in 'ip' column).
            submission_id: Any ID returned by the target site.
            retry_count: Current retry counter value.
        """
        updates: list[tuple[str, str | int]] = [
            ("status", status),
            ("notes", notes),
            ("proxy_used", proxy_used),
            ("ip", ip),
            ("last_attempt", self._now_iso()),
            ("submission_id", submission_id),
        ]
        if retry_count is not None:
            updates.append(("retry_count", retry_count))

        for field, value in updates:
            try:
                col = self._col_index(field)
                self._worksheet.update_cell(row_number, col, value)
            except KeyError:
                # Column doesn't exist in the sheet — skip silently
                log.warning("sheet.column_missing", field=field)

        log.info("sheet.row_updated", row=row_number, status=status)
