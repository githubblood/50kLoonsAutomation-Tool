"""
Read-only verification harness for Low Credit duplicate detection.

Usage:
    python test_classify_row.py <ROW_NUMBER>

Fetches one row from the Low Credit worksheet (Sheet3), runs the live
website classification, and prints Fresh / Duplicate. Does NOT write
anything back to the sheet.
"""
import os
import sys

import yaml
from dotenv import load_dotenv

load_dotenv()

from utils.sheet_handler import SheetHandler
from core.exceptions import FormFillerError
import core.form_filler_lowcredit as filler_mod


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python test_classify_row.py <ROW_NUMBER>")
        sys.exit(1)
    target_row = int(sys.argv[1])

    with open("config.yaml") as fh:
        config = yaml.safe_load(fh)

    sheet_url      = os.getenv("SHEET_URL_LOW_CREDIT") or os.getenv("GOOGLE_SHEET_URL", "")
    worksheet_name = os.getenv("SHEET_WS_LOW_CREDIT")  or os.getenv("GOOGLE_SHEET_WORKSHEET", "Sheet1")
    sheet = SheetHandler(config, worksheet_name=worksheet_name, sheet_url=sheet_url)

    rows = sheet._worksheet.get_all_records()
    row = None
    for idx, rec in enumerate(rows, start=2):
        if idx == target_row:
            rec["_row_number"] = idx
            row = rec
            break
    if not row:
        print(f"Row {target_row} not found on {worksheet_name}")
        sys.exit(1)

    offer_config = dict(config)
    offer_config["target"] = {**config.get("target", {}), "url": "https://lowcreditfinance.com"}
    offer_config.setdefault("screenshots", {})["directory"] = "screenshots/low_credit"

    ff = filler_mod.FormFiller(offer_config)
    print(f"Classifying row {target_row} (email={row.get('Email Address') or row.get('Email')}) ...")
    try:
        result = ff.classify_lead_on_site(row=row, proxy_url=None, row_number=target_row, stop_event=None)
        print("RESULT:", result["status"], "—", result.get("notes", ""))
    except FormFillerError as exc:
        if exc.error_type == "duplicate":
            print("RESULT: Duplicate —", exc)
        else:
            print(f"RESULT: Failed [{exc.error_type}] — {exc}")


if __name__ == "__main__":
    main()
