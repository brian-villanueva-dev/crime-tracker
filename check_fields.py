"""
Diagnostic — searches the last 3 days for any incident on your watched streets
(ignoring block number) to confirm the address format and matching logic.
"""
import re
import requests
import yaml
from datetime import datetime, timedelta

cfg = yaml.safe_load(open("config.yaml"))
url = f"{cfg['api']['base_url']}/{cfg['api']['dataset_id']}.json"

# Collect the bare street names from config (strip leading numbers/blocks).
watch_streets = [area["street"].upper() for area in cfg["watch_areas"]]
print("Watching for streets:", watch_streets)
print()

since = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000")
rows = requests.get(
    url,
    params={"$where": f"timecreate > '{since}'", "$limit": 5000, "$order": "timecreate DESC"},
    timeout=30,
).json()

print(f"Fetched {len(rows)} incidents from the last 3 days.")
print()

# Look for any incident whose block_address contains one of the watch street names.
SUFFIX_MAP = {"ST": "STREET", "AV": "AVENUE", "AVE": "AVENUE", "PKWY": "PARKWAY",
              "BLVD": "BOULEVARD", "DR": "DRIVE", "RD": "ROAD"}

def normalize(s):
    parts = s.upper().split()
    if parts and parts[-1] in SUFFIX_MAP:
        parts[-1] = SUFFIX_MAP[parts[-1]]
    return " ".join(parts)

found = []
for r in rows:
    addr = r.get("block_address", "")
    m = re.match(r"^(\d+)XX\s+(.+)$", addr, re.IGNORECASE)
    if not m:
        continue
    block = int(m.group(1)) * 100
    street = normalize(m.group(2).strip())
    for area in cfg["watch_areas"]:
        if normalize(area["street"]) == street:
            found.append((addr, block, r.get("typetext"), r.get("timecreate")))

if found:
    print(f"Found {len(found)} incident(s) on watched streets:")
    for addr, block, typ, ts in found:
        print(f"  {ts}  {addr}  ({typ})")
else:
    print("No incidents found on watched streets in the last 3 days.")
    print()
    print("Sample of all unique street names seen in the data:")
    streets = set()
    for r in rows[:500]:
        addr = r.get("block_address", "")
        m = re.match(r"^(\d+)XX\s+(.+)$", addr, re.IGNORECASE)
        if m:
            streets.add(normalize(m.group(2).strip()))
    for s in sorted(streets)[:30]:
        print(" ", s)
