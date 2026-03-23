# NOPD Crime Alert Monitor

Polls the New Orleans Police Department Calls for Service dataset on the [New Orleans Open Data portal](https://data.nola.gov) and sends you an alert whenever a new incident is reported on one of your watched streets.

Alerts can be delivered via **email** (Gmail App Password), **SMS** (Twilio), or **webhook** (e.g., n8n).

---

## How it works

1. Every 5 minutes (configurable), the script calls the Socrata API for any incidents created after the last run.
2. Each incident's masked block address (e.g., `24XX LARK ST`) is parsed and compared against your watch list in `config.yaml`.
3. If there's a match, an alert is dispatched through your chosen notification channel.
4. The timestamp of the last-seen incident is saved in `state.json` so the next poll only fetches new records — no duplicates.

---

## Prerequisites

- Python 3.10 or later
- A [Socrata app token](#3-get-a-socrata-app-token) (free, reduces rate-limit risk)
- One of:
  - A Gmail account with an App Password (for email)
  - A Twilio account (for SMS)
  - A webhook endpoint such as [n8n](https://n8n.io) (for automation)

---

## Installation

```bash
# 1. Navigate to the project folder
cd "path/to/crime-tracker"

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows PowerShell

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy the config template and fill in your watch areas
cp config.template.yaml config.yaml
# Open config.yaml and replace the placeholder streets/blocks with your own.
# config.yaml is gitignored — your neighborhood details stay private.

# 5. Copy the secrets template and fill it in
cp secrets.template.yaml secrets.yaml
# Then open secrets.yaml in your editor and replace the placeholder values
```

---

## Configuration

### `config.yaml` — your personal watch list (gitignored, never committed)

Copy `config.template.yaml` to `config.yaml`, then edit it:

```bash
cp config.template.yaml config.yaml
```

`config.yaml` is listed in `.gitignore` so your street names and block numbers
are never pushed to GitHub. `config.template.yaml` (with placeholder values)
is committed in its place so others can onboard easily.

Open `config.yaml` and review:

| Setting | What it does |
|---|---|
| `api.datasets` | List of `{id, year}` entries — all are queried every poll cycle |
| `polling.interval_seconds` | How often to check, in seconds. Default: 300 (5 min) |
| `watch_areas` | Streets and block numbers to watch (see below) |
| `notifications.method` | `email`, `sms`, or `webhook` |
| `sheets.enabled` | Set to `true` to log matches to Google Sheets (see *Google Sheets logging*) |

#### Adding or changing watch areas

NOPD masks house numbers to the block level: `24XX LARK ST` means somewhere in the 2400 block. The script parses this by extracting the numeric prefix and multiplying by 100.

To watch a new street, add an entry to `watch_areas`:

```yaml
watch_areas:
  - street: "CANAL ST"       # street name as it appears in NOPD data
    blocks: [3100, 3200]     # block numbers to watch (prefix × 100)
```

Street suffix abbreviations are handled automatically — `JAY ST` and `JAY STREET` both work.

### `secrets.yaml` — credentials (never committed)

Fill in only the sections you need. Unused sections can be left as-is.

---

## Credential Setup

### 1. Gmail App Password (for `method: email`)

You need a **16-character App Password** — this is *not* your normal Gmail login password.

1. Sign in to your Google Account at [myaccount.google.com](https://myaccount.google.com).
2. Go to **Security** → **2-Step Verification** and make sure it's turned on. (App Passwords require 2SV to be active.)
3. Still under Security, scroll to **App passwords** (if you don't see it, search "App passwords" in the top search bar).
4. In the drop-downs, select **Mail** for the app and **Other (custom name)** for the device. Type `crime-alert` and click **Generate**.
5. Copy the 16-character password shown (spaces don't matter — copy with or without them).
6. Paste it into `secrets.yaml` under `email.password`.

```yaml
email:
  sender: "your@gmail.com"
  password: "abcd efgh ijkl mnop"   # ← your 16-char App Password
  recipients:
    - "you@example.com"
```

> **Note:** If you see "App passwords" greyed out or missing, your organization may have disabled it. In that case, use the webhook method and route alerts through n8n → Gmail.

---

### 2. Twilio Trial Account (for `method: sms`)

1. Sign up at [twilio.com/try-twilio](https://www.twilio.com/try-twilio). Verify your phone number during signup.
2. After signup, your **Account SID** and **Auth Token** are shown on the Twilio Console dashboard.
3. Click **Get a Trial Number** to claim a free Twilio phone number — this is your `from_number`.
4. **Verify recipient numbers**: Trial accounts can only send SMS to verified numbers. Go to **Phone Numbers → Verified Caller IDs** in the console and add each number you want to text.
5. Fill in `secrets.yaml`:

```yaml
sms:
  twilio_account_sid: "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  twilio_auth_token: "your_auth_token"
  from_number: "+15005550006"     # your Twilio number
  to_numbers:
    - "+15045551234"              # must be verified for trial accounts
```

---

### 3. Get a Socrata App Token

Unauthenticated Socrata requests are capped at ~1,000/day. An app token raises this significantly and is free.

1. Register or log in at [data.nola.gov](https://data.nola.gov).
2. Click your name (top right) → **Edit Profile** → **Developer Settings** → **App Tokens**.
3. Click **Create New App Token**, give it a name (e.g., `crime-alert`), and save.
4. Copy the **App Token** value into `secrets.yaml`:

```yaml
socrata:
  app_token: "your_token_here"
```

---

### 4. Webhook (for `method: webhook`)

Any service that accepts a POST request works. The script sends JSON with three keys:

```json
{
  "message": "NOPD ALERT — DISTURBANCE\nAddress: 24XX LARK ST\n...",
  "incident": { ...raw API fields... },
  "timestamp": "2026-03-20T20:30:00Z"
}
```

In n8n, create a **Webhook** trigger node and copy the URL into `secrets.yaml`:

```yaml
webhook:
  url: "https://your-n8n.example.com/webhook/abc123"
  headers:
    Authorization: "Bearer your_secret"   # optional
```

---

### 5. Google Sheets logging (optional)

When enabled, every matched incident is appended as a row to a Google Sheet, and `setup_dashboard.py` can build a Dashboard tab with summary charts.

**Step 1 — Create a GCP project and enable the Sheets API**

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project.
2. In the left menu go to **APIs & Services** → **Enable APIs** → search for **Google Sheets API** → **Enable**.

**Step 2 — Create a service account and download a key**

1. Go to **IAM & Admin** → **Service Accounts** → **Create Service Account**.
2. Give it a name (e.g., `crime-tracker`), click **Create and Continue**, skip the optional role fields, click **Done**.
3. Click the service account email → **Keys** tab → **Add Key** → **Create new key** → **JSON** → **Create**.
4. Save the downloaded JSON file somewhere safe (e.g., `~/crime-tracker-sa.json`). This is your `service_account_json` path.

**Step 3 — Share the spreadsheet with the service account**

1. Create a new Google Sheet (or use an existing one).
2. Copy the spreadsheet ID from the URL: `https://docs.google.com/spreadsheets/d/`**`SPREADSHEET_ID`**`/edit`
3. Click **Share**, paste the service account email (looks like `crime-tracker@your-project.iam.gserviceaccount.com`), set role to **Editor**, click **Send**.

**Step 4 — Fill in `secrets.yaml` and enable in `config.yaml`**

```yaml
# secrets.yaml
sheets:
  spreadsheet_id: "your_spreadsheet_id_here"
  service_account_json: "/path/to/crime-tracker-sa.json"
```

```yaml
# config.yaml
sheets:
  enabled: true
```

---

## Usage

```bash
# Activate the virtualenv first
source .venv/bin/activate

# Run once and exit (good for testing)
python crime_alert.py

# Pull the last 3 days of data, then exit
python crime_alert.py --backfill 3

# Run continuously, polling every 5 minutes
python crime_alert.py --loop

# Backfill 7 days on first run, then keep polling
python crime_alert.py --backfill 7 --loop
```

Press **Ctrl+C** to stop the loop cleanly.

### Dashboard (Google Sheets only)

After incidents have been logged to the Sheet, run this once to build or refresh the Dashboard tab:

```bash
python setup_dashboard.py
```

It creates a **Dashboard** tab with:
- Incident counts for Last 30 Days / Last Quarter / Year to Date
- Bar chart of incidents by crime type
- Breakdown by day of week and time of day

Re-run it any time to refresh the dashboard with the latest data.

---

## Running as a Background Service

### macOS (launchd)

Create the file `~/Library/LaunchAgents/com.crimetracker.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.crimetracker</string>

  <key>ProgramArguments</key>
  <array>
    <string>/path/to/crime-tracker/.venv/bin/python</string>
    <string>/path/to/crime-tracker/crime_alert.py</string>
    <string>--loop</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/path/to/crime-tracker</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/tmp/crime-tracker.log</string>

  <key>StandardErrorPath</key>
  <string>/tmp/crime-tracker.err</string>
</dict>
</plist>
```

Replace `/path/to/crime-tracker` with the actual absolute path.

```bash
# Load the service (starts now and on every login)
launchctl load ~/Library/LaunchAgents/com.crimetracker.plist

# Check logs
tail -f /tmp/crime-tracker.log

# Stop the service
launchctl unload ~/Library/LaunchAgents/com.crimetracker.plist
```

---

### Linux (systemd user unit)

Create `~/.config/systemd/user/crime-tracker.service`:

```ini
[Unit]
Description=NOPD Crime Alert Monitor
After=network-online.target

[Service]
WorkingDirectory=/path/to/crime-tracker
ExecStart=/path/to/crime-tracker/.venv/bin/python crime_alert.py --loop
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
```

```bash
# Reload systemd, enable, and start
systemctl --user daemon-reload
systemctl --user enable --now crime-tracker

# Follow logs
journalctl --user -fu crime-tracker

# Stop
systemctl --user stop crime-tracker
```

---

### Simple background process (any Unix)

```bash
nohup python crime_alert.py --loop >> crime-tracker.log 2>&1 &
echo $! > crime-tracker.pid   # save PID so you can kill it later

# Stop it
kill $(cat crime-tracker.pid)
```

---

## Updating for a New Year

NOPD publishes a new Calls for Service dataset each year with a new Socrata dataset ID. Add the new year's entry to the `datasets` list in `config.yaml` (keep prior years in the list — they're still queried until they stop returning new data):

```yaml
api:
  datasets:
    - id: "2hk3-u8jp"
      year: 2025
    - id: "es9j-6y5d"
      year: 2026
    - id: "NEW_DATASET_ID"   # ← add new year here
      year: 2027
```

Known dataset IDs:

| Year | Dataset ID |
|---|---|
| 2024 | `wgrp-d3ma` |
| 2025 | `2hk3-u8jp` |
| 2026 | `es9j-6y5d` |

After adding a new year, delete `state.json` if you want to backfill from the start of the new year, or use `--backfill 365` on the first run. Datasets that return 404 (e.g., if an old ID changes) are automatically skipped with a warning.

---

## Troubleshooting

**No incidents returned after `--backfill`**
- Check that `config.yaml` has the correct dataset IDs under `api.datasets` for the current year.
- Delete `state.json` and re-run with `--backfill N` — a future-dated `last_polled` will return zero results.

**Gmail authentication error**
- Confirm you're using the 16-character **App Password** in `secrets.yaml`, not your regular Gmail password.
- Make sure 2-Step Verification is still enabled on the sending account.

**Twilio: "is not a verified number"**
- Trial accounts require recipients to be verified. Add the recipient number at **Twilio Console → Phone Numbers → Verified Caller IDs**.

**Rate limit errors from Socrata**
- Make sure your `socrata.app_token` in `secrets.yaml` is a real value (not a placeholder).

**Address never matches**
- The script only matches `XX`-masked addresses (`24XX LARK ST`). If NOPD logs a fully written address, it won't be caught. This is rare for residential streets.
- Double-check the street name spelling in `config.yaml` matches the NOPD data. You can browse the dataset directly at `https://data.nola.gov/resource/es9j-6y5d.json?$limit=10` to see real address formats.
