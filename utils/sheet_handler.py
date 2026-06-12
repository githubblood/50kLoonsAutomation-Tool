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
import gspread.utils
from google.oauth2.service_account import Credentials
import structlog

from utils.lead_identity import duplicate_reason, normalize_email, ssn_match_keys

log = structlog.get_logger(__name__)

# Google API scopes required for Sheets + Drive (read/write)
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetHandler:
    """Manages all interactions with a single Google Sheet worksheet."""

    def __init__(
        self,
        config: dict,
        *,
        worksheet_name: str | None = None,
        sheet_url: str | None = None,
    ) -> None:
        """
        Args:
            config: Parsed application config dict (from config.yaml + .env).
            worksheet_name: Tab name override (default: GOOGLE_SHEET_WORKSHEET).
            sheet_url: Spreadsheet URL override (default: GOOGLE_SHEET_URL).
        """
        self._config = config
        self._col_map: dict[str, str] = config.get("sheet_columns", {})

        # Authenticate
        creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/service_account.json")
        credentials = Credentials.from_service_account_file(creds_path, scopes=_SCOPES)
        client = gspread.authorize(credentials)

        # Open the sheet
        sheet_url = (sheet_url or os.getenv("GOOGLE_SHEET_URL", "")).strip()
        worksheet_name = (
            worksheet_name or os.getenv("GOOGLE_SHEET_WORKSHEET", "Sheet1")
        ).strip()
        self.worksheet_name = worksheet_name

        if sheet_url.startswith("http"):
            spreadsheet = client.open_by_url(sheet_url)
        else:
            # Treat as spreadsheet ID
            spreadsheet = client.open_by_key(sheet_url)

        self._worksheet = spreadsheet.worksheet(worksheet_name)
        self._headers: list[str] = self._worksheet.row_values(1)
        self._ensure_result_columns()
        log.info("sheet.connected", worksheet=worksheet_name, columns=len(self._headers))

    # ── helpers ──────────────────────────────────────────────────────

    def _ensure_result_columns(self) -> None:
        """Add any missing result columns to the sheet header row."""
        result_fields = ["status", "notes", "proxy_used", "ip", "last_attempt",
                         "submission_id", "retry_count"]
        missing = [
            self._col_map.get(f, f)
            for f in result_fields
            if self._col_map.get(f, f) not in self._headers
        ]
        if not missing:
            return

        # Expand the grid if the sheet doesn't have enough columns.
        needed_cols = len(self._headers) + len(missing)
        if needed_cols > self._worksheet.col_count:
            self._worksheet.resize(
                rows=self._worksheet.row_count,
                cols=needed_cols,
            )

        for col_name in missing:
            next_col = len(self._headers) + 1
            self._worksheet.update_cell(1, next_col, col_name)
            self._headers.append(col_name)

        log.info("sheet.columns_added", columns=missing)

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
            val = str(record.get(status_col, "")).strip().lower()
            if val in ("pending", ""):
                record["_row_number"] = idx
                pending.append(record)

        log.info("sheet.pending_rows", count=len(pending))
        return pending

    def check_sheet_duplicate(
        self, row_number: int, row: dict[str, Any]
    ) -> tuple[bool, str]:
        """
        Return (True, reason) if another row in this worksheet shares the same
        email or SSN as *row*; otherwise (False, "").
        """
        email = normalize_email(row)
        ssn_keys = ssn_match_keys(row)
        if not email and not ssn_keys:
            return False, ""

        all_records = self._worksheet.get_all_records()
        for idx, other in enumerate(all_records, start=2):
            if idx == row_number:
                continue
            reason = duplicate_reason(row_number, row, idx, other)
            if reason:
                log.info(
                    "sheet.duplicate_found",
                    row=row_number,
                    other_row=idx,
                    reason=reason,
                )
                return True, reason
        return False, ""

    def classify_lead(
        self, row_number: int, row: dict[str, Any], *, ip: str = ""
    ) -> str:
        """
        Classify a pending row as Duplicate or Fresh (email / SSN vs other rows).
        Writes Status + Notes to the sheet. Does not run any form automation.

        Returns:
            ``"Duplicate"`` or ``"Fresh"``.
        """
        is_dup, dup_reason = self.check_sheet_duplicate(row_number, row)
        if is_dup:
            self.update_row(
                row_number,
                status="Duplicate",
                notes=dup_reason,
                proxy_used="direct",
                ip=ip,
            )
            return "Duplicate"

        self.update_row(
            row_number,
            status="Fresh",
            notes="New lead — email/SSN not found on other rows",
            proxy_used="direct",
            ip=ip,
        )
        return "Fresh"

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
        """Write result metadata back to the sheet for one row (single batch API call)."""
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

        data = []
        for field, value in updates:
            try:
                col = self._col_index(field)
                cell = gspread.utils.rowcol_to_a1(row_number, col)
                data.append({"range": cell, "values": [[value]]})
            except KeyError:
                log.warning("sheet.column_missing", field=field)

        if data:
            self._worksheet.batch_update(data, value_input_option="RAW")
        log.info("sheet.row_updated", row=row_number, status=status)
