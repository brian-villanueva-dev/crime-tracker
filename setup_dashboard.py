#!/usr/bin/env python3
"""
setup_dashboard.py — Build or refresh the Dashboard tab in the Google Sheet.

Run this manually whenever you want to update the summary view:

    python setup_dashboard.py

It reads all rows from the Log tab (written by crime_alert.py) and writes
a Dashboard tab containing:

  - Summary table   — incident counts for Last 30 Days / Last Quarter / YTD
  - Crime Type table — count per incident type, sorted by frequency
  - Day of Week table — count per weekday (Monday … Sunday)
  - Time of Day table — count per period (Morning / Afternoon / Evening / Night)
  - Bar chart        — incidents by crime type (Sheets API batchUpdate)
  - Bar chart        — incidents by day of week

The Dashboard tab is completely rebuilt on each run, so it is always
in sync with the Log tab.

Requirements:
    pip install gspread google-auth google-api-python-client

Authentication:
    Fill in sheets.spreadsheet_id  in secrets.yaml  (or config.yaml)
    Fill in sheets.service_account_json in secrets.yaml

    The spreadsheet must be shared with the service account email address
    (Editor access).
"""

import sys
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# Reuse the load_config() function from the main script to avoid duplicating
# YAML loading and env-var overlay logic.
from crime_alert import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_date(date_str: str):
    """Parse a 'YYYY-MM-DD' string into a date object, or return None."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None


def _compute_date_ranges():
    """Return (cutoff_30d, cutoff_quarter, cutoff_ytd) as date objects."""
    today = datetime.now().date()
    cutoff_30d      = today - timedelta(days=30)
    cutoff_quarter  = today - timedelta(days=90)
    cutoff_ytd      = today.replace(month=1, day=1)
    return cutoff_30d, cutoff_quarter, cutoff_ytd


# ---------------------------------------------------------------------------
# Data aggregation
# ---------------------------------------------------------------------------

def aggregate(log_rows: list) -> dict:
    """
    Aggregate log rows (list of dicts with keys matching SHEETS_LOG_HEADERS)
    into counts by date range, crime type, day of week, and time of day.

    Returns a dict with keys:
        last_30d, last_quarter, ytd  — int counts
        by_type    — Counter {crime_type: count}
        by_dow     — Counter {weekday_name: count}
        by_tod     — Counter {period_name: count}
    """
    cutoff_30d, cutoff_quarter, cutoff_ytd = _compute_date_ranges()

    count_30d = count_quarter = count_ytd = 0
    by_type   = Counter()
    by_dow    = Counter()
    by_tod    = Counter()

    for row in log_rows:
        date  = _parse_date(row.get("Date", ""))
        ctype = row.get("Crime Type", "UNKNOWN") or "UNKNOWN"
        dow   = row.get("Day of Week", "") or ""
        time_str = row.get("Time", "") or ""

        # Date range counts.
        if date:
            if date >= cutoff_30d:
                count_30d += 1
            if date >= cutoff_quarter:
                count_quarter += 1
            if date >= cutoff_ytd:
                count_ytd += 1

        # Crime type.
        by_type[ctype] += 1

        # Day of week — preserve Monday-first order later.
        if dow:
            by_dow[dow] += 1

        # Time of day buckets based on the hour.
        if time_str:
            try:
                hour = int(time_str.split(":")[0])
            except (ValueError, IndexError):
                hour = -1
            if 6 <= hour < 12:
                period = "Morning (6am–noon)"
            elif 12 <= hour < 18:
                period = "Afternoon (noon–6pm)"
            elif 18 <= hour < 24:
                period = "Evening (6pm–midnight)"
            else:
                period = "Night (midnight–6am)"
            by_tod[period] += 1

    return {
        "last_30d":    count_30d,
        "last_quarter": count_quarter,
        "ytd":         count_ytd,
        "by_type":     by_type,
        "by_dow":      by_dow,
        "by_tod":      by_tod,
    }


# ---------------------------------------------------------------------------
# Dashboard layout helpers
# ---------------------------------------------------------------------------

# Days in calendar order (Monday first) for consistent display.
DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Time of day periods in order (early to late).
TOD_ORDER = [
    "Morning (6am–noon)",
    "Afternoon (noon–6pm)",
    "Evening (6pm–midnight)",
    "Night (midnight–6am)",
]


def build_dashboard_rows(stats: dict, generated_at: datetime) -> list:
    """
    Build the full list of rows to write to the Dashboard tab.

    Each element is a list (a spreadsheet row).  Empty lists become blank rows.

    Returns (rows, metadata) where metadata contains row indices needed for
    chart creation:
        crime_type_header_row  — 1-based row of the Crime Type table header
        crime_type_data_start  — 1-based row of the first data row
        crime_type_data_end    — 1-based row of the last data row
        dow_header_row         — 1-based row of the Day of Week table header
        dow_data_start / end   — same for Day of Week table
    """
    rows = []

    def _add(row):
        rows.append(row)

    def _blank():
        rows.append([])

    # ---- Title ----
    _add([f"NOPD Crime Alert Dashboard — Generated {generated_at.strftime('%Y-%m-%d %H:%M')}"])
    _blank()

    # ---- Summary table ----
    _add(["Summary", "Incident Count"])
    _add(["Last 30 Days",   stats["last_30d"]])
    _add(["Last Quarter",   stats["last_quarter"]])
    _add(["Year to Date",   stats["ytd"]])
    _blank()

    # ---- Crime Type table ----
    crime_type_header_row = len(rows) + 1   # 1-based index
    _add(["Crime Type", "Count"])
    crime_type_data_start = len(rows) + 1

    # Sort by count descending, then alphabetically for ties.
    for ctype, count in sorted(stats["by_type"].items(), key=lambda x: (-x[1], x[0])):
        _add([ctype, count])

    crime_type_data_end = len(rows)
    _blank()

    # ---- Day of Week table ----
    dow_header_row = len(rows) + 1
    _add(["Day of Week", "Count"])
    dow_data_start = len(rows) + 1

    for day in DAY_ORDER:
        _add([day, stats["by_dow"].get(day, 0)])

    dow_data_end = len(rows)
    _blank()

    # ---- Time of Day table ----
    _add(["Time of Day", "Count"])
    for period in TOD_ORDER:
        _add([period, stats["by_tod"].get(period, 0)])

    metadata = {
        "crime_type_header_row": crime_type_header_row,
        "crime_type_data_start": crime_type_data_start,
        "crime_type_data_end":   crime_type_data_end,
        "dow_header_row":        dow_header_row,
        "dow_data_start":        dow_data_start,
        "dow_data_end":          dow_data_end,
    }

    return rows, metadata


# ---------------------------------------------------------------------------
# Google Sheets chart creation (requires raw Sheets API — gspread can't do charts)
# ---------------------------------------------------------------------------

def _build_bar_chart_request(
    sheet_id: int,
    chart_title: str,
    header_row: int,
    data_start_row: int,
    data_end_row: int,
    anchor_row: int,
    anchor_col: int,
) -> dict:
    """
    Build a Sheets API AddChartRequest for a bar chart reading from column A (labels)
    and column B (values) in the specified row range.

    All row/column indices are 0-based (Sheets API convention).
    anchor_row / anchor_col control where the chart appears on the sheet.
    """
    # Convert 1-based spreadsheet rows to 0-based Sheets API indices.
    header_idx     = header_row - 1      # row containing the header (used as series label)
    data_start_idx = data_start_row - 1
    data_end_idx   = data_end_row        # end is exclusive in Sheets API

    def _grid_range(start_row, end_row, start_col, end_col):
        return {
            "sheetId":          sheet_id,
            "startRowIndex":    start_row,
            "endRowIndex":      end_row,
            "startColumnIndex": start_col,
            "endColumnIndex":   end_col,
        }

    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": chart_title,
                    "basicChart": {
                        "chartType": "BAR",
                        "legendPosition": "BOTTOM_LEGEND",
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": "Count"},
                            {"position": "LEFT_AXIS",   "title": ""},
                        ],
                        # Column A = category labels (rows data_start…data_end, col 0)
                        "domains": [
                            {
                                "domain": {
                                    "sourceRange": {
                                        "sources": [_grid_range(data_start_idx, data_end_idx, 0, 1)]
                                    }
                                }
                            }
                        ],
                        # Column B = bar values (same rows, col 1)
                        "series": [
                            {
                                "series": {
                                    "sourceRange": {
                                        "sources": [_grid_range(data_start_idx, data_end_idx, 1, 2)]
                                    }
                                },
                                "targetAxis": "BOTTOM_AXIS",
                            }
                        ],
                        "headerCount": 0,   # header row is handled separately via domain labels
                    },
                },
                # Where to place the chart on the sheet.
                "position": {
                    "overlayPosition": {
                        "anchorCell": {
                            "sheetId":     sheet_id,
                            "rowIndex":    anchor_row,
                            "columnIndex": anchor_col,
                        },
                        "offsetXPixels": 0,
                        "offsetYPixels": 0,
                        "widthPixels":   600,
                        "heightPixels":  400,
                    }
                },
            }
        }
    }


def add_charts(sheets_service, spreadsheet_id: str, dashboard_sheet_id: int, metadata: dict):
    """
    Add two bar charts to the Dashboard tab via the Sheets API v4 batchUpdate.

    Charts are placed to the right of the data tables (starting at column D).
    Existing charts are not removed first — call this only once after clearing
    the sheet, or delete charts manually before re-running.
    """
    requests_body = []

    # Chart 1: Incidents by Crime Type (anchored top-right of page).
    requests_body.append(
        _build_bar_chart_request(
            sheet_id=dashboard_sheet_id,
            chart_title="Incidents by Crime Type",
            header_row=metadata["crime_type_header_row"],
            data_start_row=metadata["crime_type_data_start"],
            data_end_row=metadata["crime_type_data_end"],
            anchor_row=1,    # row 2 (0-based)
            anchor_col=3,    # column D (0-based)
        )
    )

    # Chart 2: Incidents by Day of Week (below the first chart).
    requests_body.append(
        _build_bar_chart_request(
            sheet_id=dashboard_sheet_id,
            chart_title="Incidents by Day of Week",
            header_row=metadata["dow_header_row"],
            data_start_row=metadata["dow_data_start"],
            data_end_row=metadata["dow_data_end"],
            anchor_row=24,   # roughly below the crime type chart
            anchor_col=3,
        )
    )

    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests_body},
    ).execute()

    logger.info("Added %d chart(s) to Dashboard tab.", len(requests_body))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Load config (reuses crime_alert.load_config so env vars and secrets.yaml
    # are resolved with the same precedence logic).
    try:
        config = load_config()
    except FileNotFoundError as e:
        logger.error("%s", e)
        sys.exit(1)

    sheets_cfg = config.get("sheets", {})

    # Validate required settings.
    spreadsheet_id  = sheets_cfg.get("spreadsheet_id")
    sa_json_path    = sheets_cfg.get("service_account_json")
    log_tab_name    = sheets_cfg.get("log_tab", "Log")
    dashboard_tab   = sheets_cfg.get("dashboard_tab", "Dashboard")

    if not spreadsheet_id or not sa_json_path:
        logger.error(
            "sheets.spreadsheet_id and sheets.service_account_json must be set "
            "in secrets.yaml (or as GOOGLE_SPREADSHEET_ID / GOOGLE_SERVICE_ACCOUNT_JSON env vars)."
        )
        sys.exit(1)

    # Import Google libraries (already checked by requirements.txt, but give a
    # friendly error in case the user hasn't run pip install yet).
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build as gapi_build
    except ImportError as e:
        logger.error(
            "Missing dependency: %s\n"
            "Run: pip install gspread google-auth google-api-python-client", e
        )
        sys.exit(1)

    # Authenticate — the service account needs both Sheets (read/write) scope.
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    try:
        creds = Credentials.from_service_account_file(sa_json_path, scopes=scopes)
    except FileNotFoundError:
        logger.error("Service account JSON not found: %s", sa_json_path)
        sys.exit(1)

    # gspread client for reading/writing cell data.
    gc = gspread.authorize(creds)
    # Raw Sheets API client for chart creation (not supported by gspread).
    sheets_service = gapi_build("sheets", "v4", credentials=creds)

    try:
        spreadsheet = gc.open_by_key(spreadsheet_id)
    except Exception as e:
        logger.error("Could not open spreadsheet %s: %s", spreadsheet_id, e)
        sys.exit(1)

    # ---- Read the Log tab ----
    try:
        log_ws = spreadsheet.worksheet(log_tab_name)
    except gspread.WorksheetNotFound:
        logger.error(
            "Log tab '%s' not found. Run crime_alert.py with a watch-area match first "
            "to create it, or ensure sheets.log_tab in config.yaml matches.", log_tab_name
        )
        sys.exit(1)

    log_rows = log_ws.get_all_records()   # list of dicts keyed by header row
    logger.info("Read %d row(s) from Log tab.", len(log_rows))

    if not log_rows:
        logger.warning("Log tab is empty — no incidents to summarize. Dashboard will show zeros.")

    # ---- Compute statistics ----
    stats = aggregate(log_rows)
    logger.info(
        "Stats: %d last-30d, %d last-quarter, %d YTD, %d crime types.",
        stats["last_30d"], stats["last_quarter"], stats["ytd"], len(stats["by_type"])
    )

    # ---- Get or create the Dashboard tab ----
    try:
        dash_ws = spreadsheet.worksheet(dashboard_tab)
        # Delete all existing charts before clearing — otherwise re-runs stack up charts.
        sheet_meta = sheets_service.spreadsheets().get(
            spreadsheetId=spreadsheet_id
        ).execute()
        dash_sheet_id = next(
            s["properties"]["sheetId"]
            for s in sheet_meta["sheets"]
            if s["properties"]["title"] == dashboard_tab
        )
        existing_charts = [
            c["chartId"]
            for s in sheet_meta["sheets"]
            if s["properties"]["title"] == dashboard_tab
            for c in s.get("charts", [])
        ]
        if existing_charts:
            delete_requests = [{"deleteEmbeddedObject": {"objectId": cid}} for cid in existing_charts]
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": delete_requests},
            ).execute()
            logger.info("Deleted %d existing chart(s) from Dashboard.", len(existing_charts))

        # Clear all cell content so we start fresh.
        dash_ws.clear()

    except gspread.WorksheetNotFound:
        # Create a new Dashboard tab.
        dash_ws = spreadsheet.add_worksheet(title=dashboard_tab, rows=200, cols=20)
        sheet_meta = sheets_service.spreadsheets().get(
            spreadsheetId=spreadsheet_id
        ).execute()
        dash_sheet_id = next(
            s["properties"]["sheetId"]
            for s in sheet_meta["sheets"]
            if s["properties"]["title"] == dashboard_tab
        )
        logger.info("Created new Dashboard tab.")

    # ---- Write data rows ----
    dashboard_rows, metadata = build_dashboard_rows(stats, generated_at=datetime.now())

    # batch_update_values expects rows as lists; convert any empty rows to [""].
    values = [row if row else [""] for row in dashboard_rows]
    dash_ws.update("A1", values)
    logger.info("Wrote %d row(s) to Dashboard tab.", len(values))

    # ---- Add charts (only if there is crime type data to chart) ----
    if stats["by_type"]:
        try:
            add_charts(sheets_service, spreadsheet_id, dash_sheet_id, metadata)
        except Exception as e:
            logger.warning("Chart creation failed (non-fatal): %s", e)
    else:
        logger.info("No incident data — skipping chart creation.")

    logger.info("Dashboard updated successfully.")


if __name__ == "__main__":
    main()
