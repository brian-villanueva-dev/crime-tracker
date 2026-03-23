# Deploying to Google Cloud Compute Engine (free e2-micro)

This guide walks you through running the crime alert monitor 24/7 on a free
Google Cloud VM. The e2-micro instance is part of GCP's **Always Free** tier —
no charges as long as you stay within the limits (1 VM in us-east1/us-west1/
us-central1, 30 GB disk, 1 GB egress/month).

---

## 1. Create a GCP account and project

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign
   in with your Google account.
2. Accept the terms of service. You'll get a $300 free trial credit on top of
   the Always Free tier — don't worry, the e2-micro stays free even after the
   trial expires.
3. Click the project selector (top bar) → **New Project** → give it a name
   (e.g., `crime-tracker`) → **Create**.
4. Make sure the new project is selected in the top bar.

---

## 2. Create the VM instance

1. In the left menu go to **Compute Engine** → **VM instances**.
   (If prompted, click **Enable** to enable the Compute Engine API — takes ~1 min.)
2. Click **Create Instance**.
3. Fill in the form:

   | Field | Value |
   |---|---|
   | Name | `crime-tracker` |
   | Region | `us-east1` (or `us-west1` / `us-central1` — must be one of these for free tier) |
   | Zone | any zone in that region |
   | Machine type | `e2-micro` (under **General purpose → E2**) |
   | Boot disk | Click **Change** → Debian 12 (Bookworm) → 30 GB standard persistent disk |
   | Firewall | Leave both boxes unchecked (no inbound traffic needed) |

4. Click **Create**. The VM will appear in the list within ~30 seconds.

---

## 3. Connect via SSH

Click the **SSH** button next to your VM in the instances list. A browser-based
terminal opens. All commands below are run inside this terminal.

> Alternatively, install the [gcloud CLI](https://cloud.google.com/sdk/docs/install)
> locally and run `gcloud compute ssh crime-tracker --zone=YOUR_ZONE`.

---

## 4. Install Python and git

Debian 12 ships with Python 3.11. Just install pip and git:

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
```

---

## 5. Upload the project files

**Option A — clone from a private GitHub repo** (recommended if you have the
code in git):

```bash
git clone https://github.com/YOUR_USERNAME/crime-tracker.git
cd crime-tracker
```

**Option B — upload files directly** using the GCP console's built-in upload:

In the SSH browser window, click the gear icon (⚙) in the top-right corner →
**Upload file**. Upload `crime_alert.py`, `config.yaml`, and `requirements.txt`.
Then:

```bash
mkdir -p ~/crime-tracker
mv crime_alert.py config.yaml requirements.txt ~/crime-tracker/
cd ~/crime-tracker
```

---

## 6. Set up the Python virtual environment

```bash
cd ~/crime-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
deactivate
```

---

## 7. Store your credentials as environment variables

On the server, **do not create secrets.yaml**. Instead, create a
`/etc/crime-tracker.env` file that systemd will load. This file lives outside
the project directory and is readable only by root and the service.

```bash
sudo nano /etc/crime-tracker.env
```

Paste and fill in the variables for whichever notification method you're using
(`email`, `sms`, or `webhook`). Delete or leave blank the sections you don't need.

```bash
# Which notification method to use (must match config.yaml)
# No variable needed — this comes from config.yaml

# Socrata (reduces rate-limit risk — get a free token at data.nola.gov)
SOCRATA_APP_TOKEN=your_token_here

# --- Email ---
EMAIL_SENDER=your@gmail.com
EMAIL_PASSWORD=your_16_char_app_password
EMAIL_RECIPIENTS=recipient@example.com,second@example.com
# Optional — defaults to smtp.gmail.com:587 if omitted
# EMAIL_SMTP_HOST=smtp.gmail.com
# EMAIL_SMTP_PORT=587

# --- SMS (Twilio) ---
# TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# TWILIO_AUTH_TOKEN=your_auth_token
# TWILIO_FROM_NUMBER=+15005550006
# TWILIO_TO_NUMBERS=+15045551234,+15045555678

# --- Webhook ---
# WEBHOOK_URL=https://your-n8n.example.com/webhook/abc123
# WEBHOOK_HEADERS={"Authorization": "Bearer your_secret"}
```

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X`), then lock down the file:

```bash
sudo chmod 600 /etc/crime-tracker.env
```

---

## 8. Create the systemd service

```bash
sudo nano /etc/systemd/system/crime-tracker.service
```

Paste this — **replace `YOUR_USERNAME`** with your Linux username (run `whoami`
if unsure):

```ini
[Unit]
Description=NOPD Crime Alert Monitor
# Wait for the network to be up before starting.
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/crime-tracker

# Full path to the venv Python binary.
ExecStart=/home/YOUR_USERNAME/crime-tracker/.venv/bin/python crime_alert.py --loop

# Load credentials from the env file created in step 7.
EnvironmentFile=/etc/crime-tracker.env

# Restart automatically on any failure, with a 30-second cooldown.
Restart=on-failure
RestartSec=30

# Write stdout/stderr to the systemd journal (view with journalctl).
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Save and exit.

---

## 9. Enable and start the service

```bash
# Tell systemd to pick up the new service file.
sudo systemctl daemon-reload

# Start the service now and enable it to start on every boot.
sudo systemctl enable --now crime-tracker
```

Check that it's running:

```bash
sudo systemctl status crime-tracker
```

You should see `Active: active (running)`. If it shows `failed`, jump to the
troubleshooting section below.

---

## 10. View logs

```bash
# Live log stream (Ctrl+C to stop)
sudo journalctl -fu crime-tracker

# Last 50 lines
sudo journalctl -u crime-tracker -n 50

# Logs since a specific time
sudo journalctl -u crime-tracker --since "2026-03-20 09:00:00"
```

The script logs every poll cycle, so you'll see lines like:

```
2026-03-23 09:01:45 [INFO] Polling for incidents after 2026-03-23 09:01:45 …
2026-03-23 09:01:46 [INFO] Fetched 3 incident(s).
2026-03-23 09:01:46 [INFO] Cycle complete: 0 matched, 0 alert(s) sent, 0 failure(s).
```

---

## 11. Managing the service

```bash
# Stop the service
sudo systemctl stop crime-tracker

# Restart after changing config.yaml or the script
sudo systemctl restart crime-tracker

# Disable autostart (won't start on next boot)
sudo systemctl disable crime-tracker

# Check whether it starts on boot
sudo systemctl is-enabled crime-tracker
```

---

## Updating the script

```bash
cd ~/crime-tracker

# If using git:
git pull

# If uploading a new file manually, upload it via the GCP console gear icon.

# Either way, restart the service to pick up changes:
sudo systemctl restart crime-tracker
```

---

## Updating credentials

Edit the env file and restart:

```bash
sudo nano /etc/crime-tracker.env
sudo systemctl restart crime-tracker
```

---

## Updating watch areas

Edit `config.yaml` directly on the server:

```bash
nano ~/crime-tracker/config.yaml
sudo systemctl restart crime-tracker
```

---

## Troubleshooting

**Service fails to start**
```bash
sudo journalctl -u crime-tracker -n 30
```
Common causes:
- Wrong `WorkingDirectory` or `ExecStart` path — double-check your username and venv location
- Missing dependency — re-run `source .venv/bin/activate && pip install -r requirements.txt`

**`ModuleNotFoundError: No module named 'yaml'`**
The service is using the system Python instead of the venv. Make sure `ExecStart`
points to `.venv/bin/python`, not `/usr/bin/python3`.

**No alerts being sent / credential error in logs**
```bash
sudo journalctl -u crime-tracker | grep ERROR
```
Check that `/etc/crime-tracker.env` has the correct values and that the file
is being loaded (`EnvironmentFile=` line in the service file).

**Free tier billing alert**
Set up a budget alert so you're notified if something unexpected incurs a charge:
GCP Console → **Billing** → **Budgets & alerts** → **Create budget** → set
amount to $1 and notify at 100%.
