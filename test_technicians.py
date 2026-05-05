"""
Test script to fetch technicians from ServiceTitan Settings API.
"""
import os
import time
import requests
from dotenv import load_dotenv
import json

load_dotenv()

# ServiceTitan credentials
TENANT_ID = os.getenv("TENANT_ID")
APP_KEY = os.getenv("APP_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

def get_access_token():
    """Get access token from ServiceTitan OAuth endpoint."""
    print("[ServiceTitan] Requesting access token...")

    url = "https://auth.servicetitan.io/connect/token"

    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }

    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }

    response = requests.post(url, data=data, headers=headers)
    response.raise_for_status()

    token_data = response.json()
    access_token = token_data.get("access_token")

    print("[ServiceTitan] Access token obtained successfully")
    return access_token


def get_technicians():
    """Fetch all technicians from ServiceTitan Settings API."""
    print(f"\n[ServiceTitan] Fetching technicians for tenant {TENANT_ID}...")

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }

    # Settings API endpoint for technicians
    url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/technicians"

    all_technicians = []
    page = 1
    page_size = 100

    while True:
        params = {
            "page": page,
            "pageSize": page_size
        }

        print(f"[ServiceTitan] Fetching page {page}...")
        response = requests.get(url, headers=headers, params=params)

        print(f"[ServiceTitan] Response status: {response.status_code}")

        if response.status_code != 200:
            print(f"[ServiceTitan] Error response: {response.text}")
            break

        data = response.json()
        technicians = data.get("data", [])

        if not technicians:
            break

        all_technicians.extend(technicians)
        print(f"[ServiceTitan] Got {len(technicians)} technicians on page {page}")

        # Check if there are more pages
        if len(technicians) < page_size:
            break

        page += 1

    return all_technicians


def main():
    print("=" * 60)
    print("ServiceTitan Technicians Test")
    print("=" * 60)

    technicians = get_technicians()

    print(f"\n{'=' * 60}")
    print(f"Total technicians found: {len(technicians)}")
    print("=" * 60)

    if technicians:
        # Print field names from first technician
        print("\nAvailable fields:")
        print("-" * 40)
        first_tech = technicians[0]
        for key in sorted(first_tech.keys()):
            value = first_tech[key]
            value_type = type(value).__name__
            print(f"  - {key}: {value_type}")

        # Print all technicians with details
        print(f"\n{'=' * 60}")
        print("Technician Details:")
        print("=" * 60)

        for tech in technicians:
            print(f"\nID: {tech.get('id')}")
            print(f"  Name: {tech.get('name')}")
            print(f"  Active: {tech.get('active')}")
            print(f"  Business Unit ID: {tech.get('businessUnitId')}")

            # Print all other fields
            for key, value in tech.items():
                if key not in ['id', 'name', 'active', 'businessUnitId']:
                    print(f"  {key}: {value}")
            print("-" * 40)

    # Save full response to JSON file for inspection
    output_file = "technicians_output.json"
    with open(output_file, "w") as f:
        json.dump(technicians, f, indent=2)
    print(f"\nFull data saved to: {output_file}")


if __name__ == "__main__":
    main()
