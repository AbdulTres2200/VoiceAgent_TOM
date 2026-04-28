#!/usr/bin/env python3
"""
Bulk-populate custom field values for technicians in ServiceTitan.

Usage:
    python populate_techs.py --dry-run   # Preview payloads (default)
    python populate_techs.py --execute   # Actually update ServiceTitan

Requirements:
    pip install requests python-dotenv
"""

import argparse
import os
import sys
import time
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Technician data: name, ServiceTitan ID, and skill ratings (1-5)
# ---------------------------------------------------------------------------
TECHNICIANS = [
    {"name": "Tim Boyle",            "id": 1532411211, "sewers": 1, "water": 4, "misc": 4, "gas": 4, "well": 5},
    {"name": "Cody Luzer",           "id": 1445682694, "sewers": 1, "water": 1, "misc": 2, "gas": 5, "well": 2},
    {"name": "Brandon Morris",       "id": 51356417,   "sewers": 4, "water": 1, "misc": 1, "gas": 2, "well": 5},
    {"name": "Brian Vasquez",        "id": 55245951,   "sewers": 1, "water": 5, "misc": 5, "gas": 5, "well": 5},
    {"name": "Gabe Vasquez",         "id": 1443826842, "sewers": 1, "water": 5, "misc": 5, "gas": 5, "well": 5},
    {"name": "Anthony Hall",         "id": 1444608645, "sewers": 1, "water": 5, "misc": 5, "gas": 5, "well": 5},
    {"name": "Tyler Smith",          "id": 1499771410, "sewers": 3, "water": 1, "misc": 2, "gas": 5, "well": 5},
    {"name": "Ryan Francis",         "id": 1500661655, "sewers": 3, "water": 2, "misc": 1, "gas": 2, "well": 4},
    {"name": "Bryan Bell",           "id": 1531259220, "sewers": 3, "water": 1, "misc": 1, "gas": 1, "well": 1},
    {"name": "Will Copney",          "id": 1544770891, "sewers": 1, "water": 1, "misc": 1, "gas": 3, "well": 1},
    {"name": "Travis Brenot",        "id": 1549496523, "sewers": 1, "water": 1, "misc": 1, "gas": 1, "well": 1},
    {"name": "Brandon Higley",       "id": 1688829622, "sewers": 1, "water": 1, "misc": 1, "gas": 1, "well": 1},
    {"name": "Brad Fortner",         "id": 1773983037, "sewers": 1, "water": 2, "misc": 3, "gas": 3, "well": 3},
    {"name": "Jose Velez",           "id": 1791984235, "sewers": 3, "water": 1, "misc": 1, "gas": 1, "well": 5},
    {"name": "Quinn Borchert",       "id": 1791949179, "sewers": 3, "water": 2, "misc": 2, "gas": 5, "well": 5},
    {"name": "Joe Hilton",           "id": 1795414241, "sewers": 2, "water": 2, "misc": 2, "gas": 3, "well": 4},
    {"name": "Cameron Miller",       "id": 1796013726, "sewers": 3, "water": 3, "misc": 3, "gas": 3, "well": 3},
    {"name": "Mason Donathan",       "id": 1796628850, "sewers": 3, "water": 1, "misc": 5, "gas": 5, "well": 5},
    {"name": "Jason Ludwig",         "id": 1808108006, "sewers": 2, "water": 1, "misc": 1, "gas": 1, "well": 1},
    {"name": "Caleb Josey",          "id": 1793829879, "sewers": 3, "water": 3, "misc": 3, "gas": 3, "well": 3},
    {"name": "Scott Ferguson",       "id": 1808108383, "sewers": 2, "water": 1, "misc": 1, "gas": 1, "well": 1},
    {"name": "Christopher Billings", "id": 1417373641, "sewers": 1, "water": 5, "misc": 5, "gas": 5, "well": 5},
    # Mark Clemz omitted — not present in ServiceTitan
]

# ---------------------------------------------------------------------------
# Custom field type IDs from ServiceTitan
# ---------------------------------------------------------------------------
FIELD_IDS = {
    "Dispatchable":          1812958436,
    "Skill_Sewers_Mainline": 1812962532,
    "Skill_Water_Heaters":   1812948478,
    "Skill_Misc_Plumbing":   1812962533,
    "Skill_Gas_Lines":       1812961639,
    "Skill_Well_Pump":       1812943717,
}

# ---------------------------------------------------------------------------
# Globals for auth token caching
# ---------------------------------------------------------------------------
_cached_token = None


def load_env():
    """Load and validate environment variables from .env file."""
    load_dotenv()

    required = ["ST_CLIENT_ID", "ST_CLIENT_SECRET", "ST_APP_KEY", "ST_TENANT_ID"]
    missing = [var for var in required if not os.getenv(var)]

    if missing:
        print("ERROR: Missing required environment variables in .env file:")
        for var in missing:
            print(f"  - {var}")
        print("\nCreate a .env file with these keys:")
        print("  ST_CLIENT_ID=your_client_id")
        print("  ST_CLIENT_SECRET=your_client_secret")
        print("  ST_APP_KEY=your_app_key")
        print("  ST_TENANT_ID=your_tenant_id")
        sys.exit(1)

    return {
        "client_id": os.getenv("ST_CLIENT_ID"),
        "client_secret": os.getenv("ST_CLIENT_SECRET"),
        "app_key": os.getenv("ST_APP_KEY"),
        "tenant_id": os.getenv("ST_TENANT_ID"),
    }


def get_access_token(config):
    """Get OAuth2 access token from ServiceTitan. Cached for the run."""
    global _cached_token

    if _cached_token:
        return _cached_token

    url = "https://auth.servicetitan.io/connect/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    resp = requests.post(url, data=data, headers=headers)

    if resp.status_code != 200:
        print(f"ERROR: Authentication failed ({resp.status_code})")
        print(f"Response: {resp.text}")
        sys.exit(1)

    _cached_token = resp.json().get("access_token")
    return _cached_token


def build_custom_fields_payload(tech):
    """Build the customFields array for a technician."""
    return [
        {"typeId": FIELD_IDS["Dispatchable"], "value": "YES"},
        {"typeId": FIELD_IDS["Skill_Sewers_Mainline"], "value": str(tech["sewers"])},
        {"typeId": FIELD_IDS["Skill_Water_Heaters"], "value": str(tech["water"])},
        {"typeId": FIELD_IDS["Skill_Misc_Plumbing"], "value": str(tech["misc"])},
        {"typeId": FIELD_IDS["Skill_Gas_Lines"], "value": str(tech["gas"])},
        {"typeId": FIELD_IDS["Skill_Well_Pump"], "value": str(tech["well"])},
    ]


def update_technician(config, tech, dry_run=True):
    """
    Update a single technician's custom fields.
    Returns (success: bool, error_msg: str or None)
    """
    tech_id = tech["id"]
    payload = {"customFields": build_custom_fields_payload(tech)}

    if dry_run:
        import json
        print(f"  Payload: {json.dumps(payload, indent=2)}")
        return True, None

    token = get_access_token(config)
    url = f"https://api.servicetitan.io/settings/v2/tenant/{config['tenant_id']}/technicians/{tech_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": config["app_key"],
        "Content-Type": "application/json",
    }

    # Try PATCH first
    resp = requests.patch(url, json=payload, headers=headers)

    # Handle rate limiting with retry
    if resp.status_code == 429:
        print("  Rate limited, backing off 5s...")
        time.sleep(5)
        resp = requests.patch(url, json=payload, headers=headers)

    # If PATCH not supported, try PUT with full record
    if resp.status_code == 405:
        # Fetch current record first
        get_resp = requests.get(url, headers=headers)
        if get_resp.status_code != 200:
            return False, f"GET failed ({get_resp.status_code}): {get_resp.text[:200]}"

        full_record = get_resp.json()
        full_record["customFields"] = build_custom_fields_payload(tech)
        resp = requests.put(url, json=full_record, headers=headers)

        if resp.status_code == 429:
            print("  Rate limited, backing off 5s...")
            time.sleep(5)
            resp = requests.put(url, json=full_record, headers=headers)

    if resp.status_code in (200, 204):
        return True, None
    else:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def main():
    parser = argparse.ArgumentParser(
        description="Bulk-populate custom fields for technicians in ServiceTitan"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Preview payloads without calling the API (default)"
    )
    group.add_argument(
        "--execute",
        action="store_true",
        help="Actually update technicians in ServiceTitan"
    )
    args = parser.parse_args()

    dry_run = not args.execute

    config = load_env()

    if dry_run:
        print("=" * 60)
        print("DRY RUN MODE - No changes will be made")
        print("=" * 60)
    else:
        print("=" * 60)
        print("EXECUTE MODE - Updating ServiceTitan")
        print("=" * 60)
        # Pre-fetch token to fail fast if auth is broken
        get_access_token(config)
        print("Authentication successful.\n")

    total = len(TECHNICIANS)
    success_count = 0
    failed = []

    for i, tech in enumerate(TECHNICIANS, start=1):
        name = tech["name"]
        tech_id = tech["id"]

        print(f"[{i}/{total}] {name} ({tech_id})")

        ok, error = update_technician(config, tech, dry_run=dry_run)

        if ok:
            if not dry_run:
                print(f"[{i}/{total}] {name} ({tech_id}) — OK")
            success_count += 1
        else:
            print(f"[{i}/{total}] {name} ({tech_id}) — FAILED: {error}")
            failed.append(name)

        # Sleep between calls (skip on dry run)
        if not dry_run and i < total:
            time.sleep(0.25)

    # Summary
    print("\n" + "=" * 60)
    print(f"Updated: {success_count} / {total}. Failed: {len(failed)}.")
    if failed:
        print("\nFailed technicians:")
        for name in failed:
            print(f"  - {name}")
    print("=" * 60)


if __name__ == "__main__":
    main()
