#!/usr/bin/env python3
"""
crime_alert.py — NOPD Calls for Service neighborhood monitor.

Polls the New Orleans Open Data Socrata API for new incidents, filters
by a configured list of streets and block numbers, and sends alerts via
email, SMS, or webhook.

Usage:
    python crime_alert.py                   # run once and exit
    python crime_alert.py --loop            # run continuously (Ctrl+C to stop)
    python crime_alert.py --backfill 7      # pull last 7 days, then exit
    python crime_alert.py --backfill 7 --loop  # pull last 7 days, then keep polling
"""

import argparse
import json
import logging
import os
import re
import signal
import smtplib
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
from twilio.rest import Client as TwilioClient

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maps common abbreviated street suffixes to their full forms.
# Only the LAST word in a street name is expanded, so "ST CHARLES AVE"
# correctly becomes "ST CHARLES AVENUE" (not "STREET CHARLES AVENUE").
SUFFIX_MAP = {
    "ST": "STREET",
    "AVE": "AVENUE",
    "AV": "AVENUE",       # NOPD data uses "Av" as well as "Ave"
    "PKWY": "PARKWAY",
    "BLVD": "BOULEVARD",
    "DR": "DRIVE",
    "RD": "ROAD",
    "CT": "COURT",
    "LN": "LANE",
    "PL": "PLACE",
    "HWY": "HIGHWAY",
    "CIR": "CIRCLE",
    "TER": "TERRACE",
    "EXPY": "EXPRESSWAY",
}

# NOPD masks addresses to the block level using "XX" (e.g., "24XX LARK ST").
# This regex captures the numeric prefix and the street name separately.
BLOCK_ADDRESS_PATTERN = re.compile(r"^(\d+)XX\s+(.+)$", re.IGNORECASE)

# How many records to request per Socrata API page.
# Kept at 200 to limit peak memory usage — important on low-RAM servers.
SOCRATA_PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class NotificationError(Exception):
    """Raised when a notification delivery attempt fails."""
    pass


# ---------------------------------------------------------------------------
# Config and state
# ---------------------------------------------------------------------------

# Maps each secret value (by its location in the config dict) to the
# corresponding environment variable name.
#
# On a cloud server where secrets.yaml is absent, set these env vars instead.
# Environment variables override secrets.yaml values when both are present,
# so you can also use them to patch a single value without editing the file.
#
# List-valued secrets (recipients, to_numbers) are read from a single env var
# as a comma-separated string:  "alice@example.com,bob@example.com"
_ENV_VAR_MAP = {
    # path into config dict          env var name
    ("socrata", "app_token"):        "SOCRATA_APP_TOKEN",
    ("email",   "smtp_host"):        "EMAIL_SMTP_HOST",
    ("email",   "smtp_port"):        "EMAIL_SMTP_PORT",      # int
    ("email",   "sender"):           "EMAIL_SENDER",
    ("email",   "password"):         "EMAIL_PASSWORD",
    ("email",   "recipients"):       "EMAIL_RECIPIENTS",     # comma-separated
    ("sms",     "twilio_account_sid"): "TWILIO_ACCOUNT_SID",
    ("sms",     "twilio_auth_token"): "TWILIO_AUTH_TOKEN",
    ("sms",     "from_number"):      "TWILIO_FROM_NUMBER",
    ("sms",     "to_numbers"):       "TWILIO_TO_NUMBERS",    # comma-separated
    ("webhook", "url"):              "WEBHOOK_URL",
    ("webhook", "headers"):          "WEBHOOK_HEADERS",      # JSON string
    ("sheets",  "spreadsheet_id"):   "GOOGLE_SPREADSHEET_ID",
    ("sheets",  "service_account_json"): "GOOGLE_SERVICE_ACCOUNT_JSON",
}

# Keys whose values are lists (read from env as comma-separated strings).
_LIST_KEYS = {("email", "recipients"), ("sms", "to_numbers")}

# Keys whose values should be cast to int.
_INT_KEYS = {("email", "smtp_port")}

# Keys whose values are JSON objects (dicts).
_JSON_KEYS = {("webhook", "headers")}


def _apply_env_overrides(config: dict) -> None:
    """
    Overlay environment variable values onto the config dict in place.

    For each entry in _ENV_VAR_MAP, if the corresponding env var is set
    and non-empty, write it into config — creating intermediate dicts as
    needed.  This runs after secrets.yaml is loaded (if present), so env
    vars act as overrides on top of the file.
    """
    for (section, key), env_name in _ENV_VAR_MAP.items():
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue  # env var not set — leave whatever came from the file

        # Cast to the correct Python type.
        if (section, key) in _LIST_KEYS:
            value = [item.strip() for item in raw.split(",") if item.strip()]
        elif (section, key) in _INT_KEYS:
            try:
                value = int(raw)
            except ValueError:
                logger.warning("Env var %s='%s' is not an integer — ignoring.", env_name, raw)
                continue
        elif (section, key) in _JSON_KEYS:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Env var %s is not valid JSON — ignoring.", env_name)
                continue
        else:
            value = raw

        # Write into config, creating the section dict if it doesn't exist yet.
        config.setdefault(section, {})[key] = value


def _is_placeholder(value) -> bool:
    """Return True if a config value is an unfilled template placeholder."""
    if not isinstance(value, str):
        return False
    return value.startswith("YOUR_")


def load_config(config_path: str = "config.yaml", secrets_path: str = "secrets.yaml") -> dict:
    """
    Load config.yaml and (optionally) secrets.yaml, then overlay env vars.

    Resolution order for each secret value, highest priority first:
        1. Environment variable  (e.g. EMAIL_PASSWORD)
        2. secrets.yaml
        3. Absent / placeholder  → will fail at send time with a clear error

    secrets.yaml is optional: if it doesn't exist the script logs a notice
    and relies entirely on environment variables.  This allows the same
    codebase to run locally (with secrets.yaml) and on a cloud server
    (with env vars set in a systemd EnvironmentFile or similar).
    """
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_file.open() as f:
        config = yaml.safe_load(f)

    # Load secrets.yaml if it exists; otherwise start with empty secret sections.
    secrets_file = Path(secrets_path)
    if secrets_file.exists():
        with secrets_file.open() as f:
            secrets = yaml.safe_load(f) or {}
        # Deep-merge secrets into config so nested dicts (e.g. sheets, email) are
        # combined rather than replaced. config.update() would overwrite the entire
        # sheets dict from config.yaml with the one from secrets.yaml, losing keys
        # like enabled/log_tab that only live in config.yaml.
        for key, val in secrets.items():
            if key in config and isinstance(config[key], dict) and isinstance(val, dict):
                config[key].update(val)
            else:
                config[key] = val
        logger.debug("Loaded secrets from %s.", secrets_path)
    else:
        logger.info(
            "No secrets.yaml found — relying on environment variables for credentials. "
            "(Set EMAIL_PASSWORD, SOCRATA_APP_TOKEN, etc.  See secrets.template.yaml for the full list.)"
        )

    # Overlay any env vars on top, so server deployments don't need a secrets file.
    _apply_env_overrides(config)

    # Scrub any values that are still unfilled placeholders so send functions
    # get None rather than the literal string "YOUR_APP_PASSWORD".
    for section in ("socrata", "email", "sms", "webhook", "sheets"):
        section_dict = config.get(section, {})
        if isinstance(section_dict, dict):
            for k, v in section_dict.items():
                if _is_placeholder(v):
                    section_dict[k] = None

    return config


def load_state(state_file: str) -> dict:
    """
    Read the last-polled timestamp from state_file.

    Returns {"last_polled": None} if the file doesn't exist or is malformed,
    so the caller can fall back to the backfill window or 24-hour default.
    """
    path = Path(state_file)
    if not path.exists():
        return {"last_polled": None}

    try:
        with path.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read state file %s (%s) — starting fresh.", state_file, e)
        return {"last_polled": None}


def save_state(state_file: str, state: dict) -> None:
    """
    Write state dict to state_file as JSON, atomically.

    Uses a write-to-temp-then-rename pattern so the file is never left
    half-written if the process crashes mid-save.
    """
    dir_path = Path(state_file).parent or Path(".")
    # Write to a temp file in the same directory, then atomically rename.
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        # os.replace is atomic on POSIX and Windows (Python 3.3+).
        os.replace(tmp_path, state_file)
    except Exception:
        # Clean up the temp file if something went wrong.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------

def normalize_street_name(raw: str) -> str:
    """
    Expand the last token of a street name using SUFFIX_MAP, then uppercase.

    Only the final word is expanded so "ST CHARLES AVE" becomes
    "ST CHARLES AVENUE" (not "STREET CHARLES AVENUE").

    Examples:
        "LARK ST"        → "LARK STREET"
        "LAKE OAKS PKWY" → "LAKE OAKS PARKWAY"
        "ST CHARLES AVE" → "ST CHARLES AVENUE"
    """
    # Collapse any extra whitespace and uppercase everything.
    parts = raw.upper().split()
    if not parts:
        return raw.upper()

    # Only expand the last token.
    last = parts[-1]
    if last in SUFFIX_MAP:
        parts[-1] = SUFFIX_MAP[last]

    return " ".join(parts)


def parse_address(address_str: str):
    """
    Parse an NOPD masked block address into (block_number, street_name).

    NOPD format:  "24XX LARK ST"  →  (2400, "LARK STREET")

    Steps:
        1. Match the pattern r'^(\\d+)XX\\s+(.+)$' (case-insensitive).
        2. block_number = int(prefix) * 100  (handles leading zeros)
        3. street_name  = normalize_street_name(street_part)

    Returns None if the address doesn't match the XX pattern (e.g., a fully
    written address or a null value).
    """
    if not address_str:
        return None

    # Collapse multiple spaces before matching.
    cleaned = re.sub(r"\s+", " ", address_str.strip())
    match = BLOCK_ADDRESS_PATTERN.match(cleaned)
    if not match:
        return None

    prefix_digits, street_part = match.group(1), match.group(2)
    block_number = int(prefix_digits) * 100
    street_name = normalize_street_name(street_part.strip())
    return block_number, street_name


def is_in_watch_area(address_str: str, watch_areas: list) -> bool:
    """
    Return True if address_str matches any entry in the watch_areas list.

    Matching is done after normalizing both the parsed address and the
    config street name, so "JAY ST" in config matches "22XX JAY ST" in data.
    """
    parsed = parse_address(address_str)
    if parsed is None:
        return False

    block_number, street_name = parsed

    for area in watch_areas:
        config_street = normalize_street_name(area["street"])
        if street_name == config_street and block_number in area["blocks"]:
            return True

    return False


# ---------------------------------------------------------------------------
# Socrata API
# ---------------------------------------------------------------------------

def build_soql_timestamp(dt: datetime) -> str:
    """
    Format a datetime for use in a Socrata SoQL WHERE clause.

    The NOPD dataset stores timecreate as a local New Orleans floating
    timestamp (no UTC offset). We compare naively in local time to avoid
    DST conversion errors.

    Format required by Socrata:  "YYYY-MM-DDTHH:MM:SS.000"
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000")


def _fetch_from_dataset(
    since_dt: datetime,
    dataset_id: str,
    base_url: str,
    headers: dict,
    until_dt: datetime = None,
) -> list:
    """
    Query a single Socrata dataset for all incidents with timecreate > since_dt.

    If until_dt is provided, also adds timecreate < until_dt to the filter,
    bounding the query to a closed time window (used by --backfill-month).

    Paginates automatically: if a page returns exactly SOCRATA_PAGE_SIZE
    rows, it fetches the next page using the last seen timecreate as the
    new lower bound, accumulating until a short page is returned.

    Returns a list of incident dicts (may be empty).
    Raises requests.HTTPError on non-2xx responses.
    """
    url = f"{base_url}/{dataset_id}.json"
    all_incidents = []
    current_since = since_dt

    while True:
        where = f"timecreate > '{build_soql_timestamp(current_since)}'"
        if until_dt is not None:
            where += f" AND timecreate < '{build_soql_timestamp(until_dt)}'"

        params = {
            "$where": where,
            "$order": "timecreate ASC",
            "$limit": SOCRATA_PAGE_SIZE,
        }

        logger.debug("Querying %s with $where: %s", url, params["$where"])
        response = requests.get(url, params=params, headers=headers, timeout=30)

        # 404 means the dataset ID doesn't exist (e.g., a prior-year dataset
        # that was removed or had its ID changed). Return empty instead of
        # crashing — the other configured datasets will still be queried.
        if response.status_code == 404:
            logger.warning("Dataset %s returned 404 — skipping (check the ID in config.yaml).", url)
            return []

        response.raise_for_status()

        page = response.json()
        all_incidents.extend(page)

        # If we got a full page, there may be more records — advance the window.
        if len(page) == SOCRATA_PAGE_SIZE:
            # Use the timecreate of the last record as the new lower bound.
            last_ts_str = page[-1].get("timecreate", "")
            if last_ts_str:
                # Parse the Socrata timestamp (format: "2026-03-20T14:30:00.000")
                try:
                    current_since = datetime.strptime(last_ts_str[:19], "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    logger.warning("Could not parse timecreate '%s' for pagination; stopping.", last_ts_str)
                    break
            else:
                break
        else:
            # Short page means we've reached the end.
            break

    return all_incidents


def fetch_incidents(since_dt: datetime, config: dict, until_dt: datetime = None) -> list:
    """
    Query all configured Socrata datasets and return a combined, deduplicated list.

    since_dt — lower bound (exclusive): only incidents with timecreate > since_dt
    until_dt — optional upper bound (exclusive): timecreate < until_dt.
               Used by --backfill-month to bound the query to one calendar month
               without loading the full year into memory.

    Supports two config formats:
        New:  api.datasets  — list of {id, year} dicts (query all datasets)
        Old:  api.dataset_id — single string (backward-compatible)

    All datasets are queried with the same window so incidents near year
    boundaries are never missed. Results are deduplicated by nopd_item.

    Raises requests.HTTPError on non-2xx responses from any dataset.
    """
    base_url = config["api"]["base_url"].rstrip("/")

    # Build the auth header once; reuse for every dataset request.
    headers = {}
    app_token = config.get("socrata", {}).get("app_token", "")
    if app_token:
        headers["X-App-Token"] = app_token

    # Resolve the dataset list — support both the new list format and the old
    # single-ID format so existing config files continue to work.
    api_cfg = config.get("api", {})
    if "datasets" in api_cfg:
        dataset_ids = [ds["id"] for ds in api_cfg["datasets"]]
    elif "dataset_id" in api_cfg:
        # Backward-compatible: wrap the single ID in a list.
        dataset_ids = [api_cfg["dataset_id"]]
    else:
        logger.error("No datasets configured under api.datasets or api.dataset_id.")
        return []

    # Query each dataset and merge results, deduplicating by nopd_item.
    # Errors from one dataset are logged but don't abort the others.
    seen_items: dict = {}  # nopd_item → incident dict
    for dataset_id in dataset_ids:
        logger.debug("Fetching from dataset %s …", dataset_id)
        try:
            incidents = _fetch_from_dataset(since_dt, dataset_id, base_url, headers, until_dt=until_dt)
        except requests.HTTPError as e:
            logger.error("HTTP error fetching dataset %s: %s — skipping.", dataset_id, e)
            continue
        for incident in incidents:
            key = incident.get("nopd_item") or id(incident)  # fall back to object id if key missing
            seen_items[key] = incident
        logger.debug("Dataset %s returned %d record(s).", dataset_id, len(incidents))

    return list(seen_items.values())


# ---------------------------------------------------------------------------
# Alert formatting and dispatch
# ---------------------------------------------------------------------------

def format_alert(incident: dict) -> str:
    """
    Build the human-readable alert message string for one incident.

    Uses .get(field, "N/A") so a missing field never causes a crash.
    """
    incident_type = incident.get("typetext", "N/A")
    address = incident.get("block_address", "N/A")
    priority = incident.get("priority", "N/A")
    timestamp = incident.get("timecreate", "N/A")
    item_number = incident.get("nopd_item", "N/A")

    return (
        f"NOPD ALERT — {incident_type}\n"
        f"Address:  {address}\n"
        f"Priority: {priority}\n"
        f"Time:     {timestamp}\n"
        f"Item #:   {item_number}"
    )


def send_email(message: str, config: dict) -> None:
    """
    Send an alert via Gmail SMTP using STARTTLS.

    Requires secrets.yaml to have: email.smtp_host, smtp_port, sender,
    password, and recipients (list).

    Raises NotificationError on any delivery failure so the caller can
    log it and continue processing other incidents.
    """
    email_cfg = config.get("email", {})
    host = email_cfg.get("smtp_host", "smtp.gmail.com")
    port = email_cfg.get("smtp_port", 587)
    sender = email_cfg.get("sender", "")
    password = email_cfg.get("password", "")
    recipients = email_cfg.get("recipients", [])

    if not recipients:
        raise NotificationError("No email recipients configured in secrets.yaml.")

    # Build the email. Subject = first line of the message.
    subject = message.splitlines()[0]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(message, "plain"))

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        logger.info("Email sent to %s", recipients)
    except smtplib.SMTPException as e:
        raise NotificationError(f"SMTP error: {e}") from e


def send_sms(message: str, config: dict) -> None:
    """
    Send an alert via Twilio SMS.

    Requires secrets.yaml to have: sms.twilio_account_sid, twilio_auth_token,
    from_number, and to_numbers (list).

    Trial accounts can only send to numbers verified in the Twilio console.
    Messages longer than 1600 characters are truncated to stay within MMS limits.

    Raises NotificationError on delivery failure.
    """
    sms_cfg = config.get("sms", {})
    account_sid = sms_cfg.get("twilio_account_sid", "")
    auth_token = sms_cfg.get("twilio_auth_token", "")
    from_number = sms_cfg.get("from_number", "")
    to_numbers = sms_cfg.get("to_numbers", [])

    if not to_numbers:
        raise NotificationError("No SMS recipients configured in secrets.yaml.")

    # Twilio MMS body limit is 1600 characters.
    body = message[:1600]

    try:
        client = TwilioClient(account_sid, auth_token)
        for to_number in to_numbers:
            client.messages.create(body=body, from_=from_number, to=to_number)
            logger.info("SMS sent to %s", to_number)
    except Exception as e:
        raise NotificationError(f"Twilio error: {e}") from e


def send_webhook(message: str, incident: dict, config: dict) -> None:
    """
    POST alert data as JSON to a configured webhook URL (e.g., n8n).

    Payload keys:
        message   — the formatted human-readable alert string
        incident  — the raw incident dict from the Socrata API
        timestamp — ISO UTC timestamp of when this alert was sent

    Raises NotificationError on any request or HTTP error.
    """
    webhook_cfg = config.get("webhook", {})
    url = webhook_cfg.get("url", "")
    extra_headers = webhook_cfg.get("headers", {}) or {}

    if not url or url.startswith("https://your-"):
        raise NotificationError("Webhook URL not configured in secrets.yaml.")

    headers = {"Content-Type": "application/json"}
    headers.update(extra_headers)

    payload = {
        "message": message,
        "incident": incident,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        logger.info("Webhook delivered to %s (HTTP %s)", url, response.status_code)
    except requests.RequestException as e:
        raise NotificationError(f"Webhook error: {e}") from e


def send_alert(message: str, incident: dict, config: dict) -> None:
    """
    Dispatch an alert using the method specified in config['notifications']['method'].

    Supported methods: email, sms, webhook.
    Logs a warning if the method is unrecognized.
    Propagates NotificationError from the underlying sender.
    """
    method = config.get("notifications", {}).get("method", "email").lower()

    if method == "email":
        send_email(message, config)
    elif method == "sms":
        send_sms(message, config)
    elif method == "webhook":
        send_webhook(message, incident, config)
    else:
        logger.warning("Unknown notification method '%s' — no alert sent.", method)


# ---------------------------------------------------------------------------
# Google Sheets logging
# ---------------------------------------------------------------------------

# Column headers written to the Log tab on first use.
SHEETS_LOG_HEADERS = [
    "Date", "Time", "Day of Week", "Block Address",
    "Crime Type", "Priority", "NOPD Item",
]


def init_sheets(config: dict):
    """
    Connect to Google Sheets and return the log worksheet, or None if disabled.

    Returns None (without raising) when:
        - sheets.enabled is False in config.yaml
        - spreadsheet_id or service_account_json is missing / placeholder
        - gspread / google-auth packages are not installed

    On first connection, writes the header row if the tab is empty so the
    spreadsheet is ready to receive data immediately.
    """
    sheets_cfg = config.get("sheets", {})

    # Check the kill-switch in config.yaml first.
    if not sheets_cfg.get("enabled", False):
        print("SHEETS INIT: reached return point 1 (sheets.enabled is false or missing)")
        return None

    spreadsheet_id = sheets_cfg.get("spreadsheet_id")
    sa_json_path = sheets_cfg.get("service_account_json")
    log_tab = sheets_cfg.get("log_tab", "Log")

    if not spreadsheet_id or not sa_json_path:
        print("SHEETS INIT: reached return point 2 (spreadsheet_id or service_account_json is missing)")
        logger.warning(
            "Sheets logging is enabled but spreadsheet_id or service_account_json "
            "is not configured — skipping Sheets setup."
        )
        return None

    # Lazy import so the script still works if the packages aren't installed.
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        print(f"SHEETS INIT: reached return point 3 (ImportError)")
        print(f"SHEETS INIT ERROR: {e}")
        logger.warning(
            "gspread / google-auth not installed. "
            "Run: pip install gspread google-auth    Sheets logging disabled."
        )
        return None

    # The Sheets API scope is the only permission the service account needs.
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
    ]

    try:
        creds = Credentials.from_service_account_file(sa_json_path, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(spreadsheet_id)
    except FileNotFoundError as e:
        print(f"SHEETS INIT: reached return point 4 (FileNotFoundError)")
        print(f"SHEETS INIT ERROR: {e}")
        logger.warning("Service account JSON not found at %s — Sheets logging disabled.", sa_json_path)
        return None
    except Exception as e:
        print(f"SHEETS INIT: reached return point 5 (Exception)")
        print(f"SHEETS INIT ERROR: {e}")
        logger.warning("Could not connect to Google Sheets: %s — logging disabled.", e)
        return None

    # Get or create the log tab.
    try:
        worksheet = spreadsheet.worksheet(log_tab)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=log_tab, rows=1000, cols=len(SHEETS_LOG_HEADERS))
        logger.info("Created new tab '%s' in spreadsheet.", log_tab)

    # Write headers if the sheet is empty (no rows at all).
    if worksheet.row_count == 0 or not worksheet.get_all_values():
        worksheet.append_row(SHEETS_LOG_HEADERS)
        logger.info("Wrote header row to Sheets log tab '%s'.", log_tab)

    logger.info("Google Sheets logging active — appending to tab '%s'.", log_tab)
    print("SHEETS INIT: reached return point 6 (success — returning worksheet)")
    return worksheet


def log_to_sheets(incident: dict, worksheet) -> None:
    """
    Append one incident row to the Google Sheets log tab.

    Columns: Date | Time | Day of Week | Block Address | Crime Type | Priority | NOPD Item

    Wrapped in try/except by the caller — a Sheets failure must never crash
    the main monitoring loop.
    """
    ts_raw = incident.get("timecreate", "")
    try:
        # Socrata timestamps: "2026-03-20T14:30:00.000000"
        dt = datetime.strptime(ts_raw[:19], "%Y-%m-%dT%H:%M:%S")
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M:%S")
        dow_str = dt.strftime("%A")          # e.g. "Monday"
    except (ValueError, TypeError):
        date_str = ts_raw
        time_str = ""
        dow_str = ""

    row = [
        date_str,
        time_str,
        dow_str,
        incident.get("block_address", ""),
        incident.get("typetext", ""),
        incident.get("priority", ""),
        incident.get("nopd_item", ""),
    ]
    logger.debug("Appending row to Sheets: %s", row)
    worksheet.append_row(row)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def get_default_since(backfill_days: int = None) -> datetime:
    """
    Compute the starting timestamp for the first run (no state file yet).

    If backfill_days is given, go back that many days.
    Otherwise, default to the last 24 hours.
    """
    if backfill_days is not None:
        return datetime.now() - timedelta(days=backfill_days)
    return datetime.now() - timedelta(hours=24)


def process_incidents(incidents: list, config: dict, state: dict, worksheet=None) -> tuple:
    """
    Filter incidents by watch area, send alerts, log to Sheets, and advance last_polled.

    Strategy for last_polled:
        After iterating all incidents, update last_polled to the maximum
        timecreate seen — regardless of whether alerts were successfully
        sent. This prevents an alert storm if the notification service
        is down for an extended period; it also means a failed alert
        will not be retried on the next cycle. This is intentional.

    worksheet — a gspread Worksheet object returned by init_sheets(), or None
        to skip Sheets logging.

    Returns:
        (matched_count, failure_count)
    """
    watch_areas = config.get("watch_areas", [])
    matched_count = 0
    failure_count = 0
    max_timecreate = None  # Track the latest timestamp we've seen this cycle.

    for incident in incidents:
        ts = incident.get("timecreate")
        if ts and (max_timecreate is None or ts > max_timecreate):
            max_timecreate = ts

        address = incident.get("block_address", "")
        if not is_in_watch_area(address, watch_areas):
            continue

        # This incident is in a watched area — send an alert.
        matched_count += 1
        message = format_alert(incident)
        logger.info(
            "MATCH: %s at %s (item %s)",
            incident.get("typetext", "?"),
            address,
            incident.get("nopd_item", "?"),
        )

        try:
            send_alert(message, incident, config)
        except NotificationError as e:
            logger.error("Notification failed for item %s: %s", incident.get("nopd_item", "?"), e)
            failure_count += 1

        # Append a row to the Sheets log. A Sheets failure is non-fatal.
        if worksheet is None:
            print("SHEETS DEBUG: worksheet is None")
        else:
            print("SHEETS DEBUG: about to log to sheets")
        if worksheet is not None:
            try:
                log_to_sheets(incident, worksheet)
            except Exception as e:
                logger.error(
                    "Sheets logging failed for item %s: %s",
                    incident.get("nopd_item", "?"),
                    e,
                    exc_info=True,
                )

    # Advance the polling window to the last timestamp we processed.
    if max_timecreate:
        state["last_polled"] = max_timecreate

    return matched_count, failure_count


def run_backfill_month(config: dict, year: int, month: int, worksheet=None) -> None:
    """
    Fetch, process, and log one calendar month of incidents, then return.

    Unlike run_once(), this does NOT read or write state.json — it is a
    self-contained historical query that never affects the continuous-poll
    window.  Use it to populate the Google Sheets log month-by-month without
    loading a full year into memory at once:

        python crime_alert.py --backfill-month 2025-01
        python crime_alert.py --backfill-month 2025-02
        ...

    The lower bound is one second before midnight on the first of the month
    (because the Socrata filter is strict ">"), so all records with
    timecreate >= YYYY-MM-01 00:00:00 are captured.
    The upper bound is the first moment of the following month (exclusive),
    so records from adjacent months are never included.
    """
    # Lower bound: one second before the month starts so timecreate > bound
    # captures everything from 00:00:00 on the 1st.
    since_dt = datetime(year, month, 1) - timedelta(seconds=1)

    # Upper bound: first moment of the next month (exclusive <).
    if month == 12:
        until_dt = datetime(year + 1, 1, 1)
    else:
        until_dt = datetime(year, month + 1, 1)

    month_label = f"{year}-{month:02d}"
    logger.info(
        "Backfilling %s (fetching timecreate >= %s and < %s) …",
        month_label,
        datetime(year, month, 1).strftime("%Y-%m-%d %H:%M:%S"),
        until_dt.strftime("%Y-%m-%d %H:%M:%S"),
    )

    try:
        incidents = fetch_incidents(since_dt, config, until_dt=until_dt)
    except requests.ConnectionError as e:
        logger.error("Connection error during backfill of %s: %s", month_label, e)
        return
    except requests.Timeout:
        logger.error("Request timed out during backfill of %s.", month_label)
        return
    except requests.HTTPError as e:
        logger.error("HTTP error during backfill of %s: %s", month_label, e)
        return

    logger.info("Fetched %d incident(s) for %s.", len(incidents), month_label)

    # Temporary state dict — discarded after this call so state.json is untouched.
    temp_state: dict = {}
    matched, failures = process_incidents(incidents, config, temp_state, worksheet=worksheet)
    logger.info(
        "Backfill %s complete: %d matched, %d alert(s) sent, %d failure(s).",
        month_label,
        matched,
        matched - failures,
        failures,
    )


def run_once(config: dict, state: dict, backfill_days: int = None, worksheet=None) -> None:
    """
    Execute a single poll cycle: fetch new incidents, process, save state.

    If the API is unreachable, we log the error and skip saving state
    so the same window is retried on the next cycle.

    worksheet — passed through to process_incidents for Sheets logging.
    """
    state_file = config.get("state_file", "state.json")

    # Determine the start of the polling window.
    if state.get("last_polled"):
        # Parse the stored timestamp string back into a datetime.
        ts_str = state["last_polled"]
        try:
            # Socrata returns microseconds in some records; truncate to seconds.
            since_dt = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            logger.warning("Could not parse last_polled '%s'; using default window.", ts_str)
            since_dt = get_default_since(backfill_days)
    else:
        since_dt = get_default_since(backfill_days)

    logger.info("Polling for incidents after %s …", since_dt.strftime("%Y-%m-%d %H:%M:%S"))

    try:
        incidents = fetch_incidents(since_dt, config)
    except requests.ConnectionError as e:
        logger.warning("Connection error — will retry next cycle: %s", e)
        return
    except requests.Timeout:
        logger.warning("API request timed out — will retry next cycle.")
        return
    except requests.HTTPError as e:
        logger.error("HTTP error from API: %s", e)
        return

    logger.info("Fetched %d incident(s).", len(incidents))

    matched, failures = process_incidents(incidents, config, state, worksheet=worksheet)
    logger.info(
        "Cycle complete: %d matched, %d alert(s) sent, %d failure(s).",
        matched,
        matched - failures,
        failures,
    )

    # Save state even if there were notification failures; see process_incidents docstring.
    save_state(state_file, state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="NOPD Calls for Service neighborhood alert monitor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python crime_alert.py                        # run once\n"
            "  python crime_alert.py --loop                 # poll continuously\n"
            "  python crime_alert.py --backfill 7           # pull last 7 days, then exit\n"
            "  python crime_alert.py --backfill 7 --loop    # backfill, then keep polling\n"
            "  python crime_alert.py --backfill-month 2025-12  # one calendar month, then exit\n"
        ),
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously, polling at the interval set in config.yaml.",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        metavar="N",
        default=None,
        help=(
            "On the first run (no state file), pull incidents from the last N days. "
            "Has no effect on subsequent runs once a state file exists."
        ),
    )
    parser.add_argument(
        "--backfill-month",
        metavar="YYYY-MM",
        default=None,
        help=(
            "Fetch and process exactly one calendar month of data, log matches to "
            "Google Sheets, then exit. Does not read or update state.json. "
            "Run once per month to populate the Sheets log without loading a full "
            "year into memory: --backfill-month 2025-01, then 2025-02, etc."
        ),
    )
    args = parser.parse_args()

    # Load config — exits with a clear error if files are missing.
    try:
        config = load_config()
    except FileNotFoundError as e:
        logger.error("%s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        sys.exit(1)

    state_file = config.get("state_file", "state.json")
    state = load_state(state_file)

    # Initialize Google Sheets logging once at startup (returns None if disabled).
    print("SHEETS DEBUG: config sheets section =", config.get("sheets"))
    worksheet = init_sheets(config)
    print("SHEETS DEBUG: worksheet after init_sheets =", worksheet)

    # --backfill-month: fetch one calendar month, log to Sheets, exit.
    # Handled before --loop so it never enters the continuous poll path.
    if args.backfill_month:
        try:
            year_str, month_str = args.backfill_month.split("-")
            year, month = int(year_str), int(month_str)
            if not (1 <= month <= 12):
                raise ValueError
        except ValueError:
            logger.error(
                "--backfill-month requires YYYY-MM format with a valid month (e.g. 2025-12)."
            )
            sys.exit(1)
        run_backfill_month(config, year, month, worksheet=worksheet)
        return

    if not args.loop:
        # Single-shot mode: run once and exit.
        run_once(config, state, backfill_days=args.backfill, worksheet=worksheet)
        return

    # --- Continuous loop mode ---
    # Use a threading.Event so SIGINT / SIGTERM trigger a clean shutdown
    # rather than a stack trace.
    shutdown_event = threading.Event()

    def _handle_signal(signum, frame):
        logger.info("Shutdown signal received — finishing current cycle and exiting.")
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    interval = config.get("polling", {}).get("interval_seconds", 300)
    logger.info("Starting continuous poll loop (interval: %ds). Press Ctrl+C to stop.", interval)

    while not shutdown_event.is_set():
        run_once(config, state, backfill_days=args.backfill, worksheet=worksheet)
        # Only apply --backfill on the very first cycle (while state is still empty).
        # After the first run_once, state['last_polled'] will be set, so get_default_since
        # won't be called again — but we clear backfill_days just to be explicit.
        args.backfill = None

        # Sleep in small increments so Ctrl+C is responsive.
        for _ in range(interval):
            if shutdown_event.is_set():
                break
            time.sleep(1)

    logger.info("Exited cleanly.")


if __name__ == "__main__":
    main()
