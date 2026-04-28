"""
Technician dispatch service for ServiceTitan.

Selects the best available technician based on:
- Availability (active, dispatchable, idle)
- Zone coverage (tech's zones include customer's zone)
- Skill rating for the job category
- GPS distance to customer
"""

import os
import time
import math
import requests
from dotenv import load_dotenv

load_dotenv()

# ServiceTitan credentials
TENANT_ID = os.getenv("TENANT_ID") or os.getenv("ST_TENANT_ID")
APP_KEY = os.getenv("APP_KEY") or os.getenv("ST_APP_KEY")
CLIENT_ID = os.getenv("CLIENT_ID") or os.getenv("ST_CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET") or os.getenv("ST_CLIENT_SECRET")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# Custom field type IDs
FIELD_IDS = {
    "Dispatchable": 1812958436,
    "Skill_Sewers_Mainline": 1812962532,
    "Skill_Water_Heaters": 1812948478,
    "Skill_Misc_Plumbing": 1812962533,
    "Skill_Gas_Lines": 1812961639,
    "Skill_Well_Pump": 1812943717,
}

# Map job categories to custom field names
JOB_CATEGORY_TO_FIELD = {
    "Sewers/Mainline": "Skill_Sewers_Mainline",
    "Water Heaters": "Skill_Water_Heaters",
    "Misc Plumbing": "Skill_Misc_Plumbing",
    "Gas Lines": "Skill_Gas_Lines",
    "Well Pump": "Skill_Well_Pump",
}

# Token cache
_token_cache = {"access_token": None, "expires_at": 0}

# Zone cache (zone_id -> zone data, zip -> zone_id)
_zone_cache = {"data": {}, "zip_to_zone": {}, "expires_at": 0}


def get_access_token():
    """Get OAuth access token, cached for 900 seconds."""
    current_time = time.time()
    if _token_cache["access_token"] and current_time < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    url = "https://auth.servicetitan.io/connect/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    resp = requests.post(url, data=data, headers=headers)
    resp.raise_for_status()

    token = resp.json().get("access_token")
    _token_cache["access_token"] = token
    _token_cache["expires_at"] = current_time + 900
    return token


def get_api_headers():
    """Get headers for ServiceTitan API calls."""
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json",
    }


def haversine_miles(lat1, lon1, lat2, lon2):
    """Calculate distance between two points in miles using haversine formula."""
    R = 3958.8  # Earth's radius in miles

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = math.sin(delta_lat / 2) ** 2 + \
        math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


def geocode_address(address):
    """
    Geocode an address using Google Maps API.
    Returns (lat, lng, zip_code) or (None, None, None) on failure.
    """
    if not GOOGLE_MAPS_API_KEY:
        print("[Dispatch] No GOOGLE_MAPS_API_KEY configured")
        return None, None, None

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": GOOGLE_MAPS_API_KEY}

    try:
        resp = requests.get(url, params=params, timeout=5)
        data = resp.json()

        if data.get("status") != "OK" or not data.get("results"):
            print(f"[Dispatch] Geocoding failed: {data.get('status')}")
            return None, None, None

        result = data["results"][0]
        location = result["geometry"]["location"]
        lat = location["lat"]
        lng = location["lng"]

        # Extract zip code from address components
        zip_code = None
        for component in result.get("address_components", []):
            if "postal_code" in component.get("types", []):
                zip_code = component["short_name"][:5]
                break

        return lat, lng, zip_code

    except Exception as e:
        print(f"[Dispatch] Geocoding error: {e}")
        return None, None, None


def get_zones():
    """
    Fetch all zones from ServiceTitan, cached for 24 hours.
    Returns dict: {zone_id: zone_data} and {zip: zone_id}
    """
    current_time = time.time()
    if _zone_cache["data"] and current_time < _zone_cache["expires_at"]:
        return _zone_cache["data"], _zone_cache["zip_to_zone"]

    url = f"https://api.servicetitan.io/dispatch/v2/tenant/{TENANT_ID}/zones"
    headers = get_api_headers()

    all_zones = {}
    zip_to_zone = {}
    page = 1

    while True:
        resp = requests.get(url, headers=headers, params={"page": page, "pageSize": 100})
        if resp.status_code != 200:
            print(f"[Dispatch] Failed to fetch zones: {resp.status_code}")
            break

        zones = resp.json().get("data", [])
        if not zones:
            break

        for zone in zones:
            if not zone.get("active"):
                continue
            zone_id = zone["id"]
            all_zones[zone_id] = zone
            for zip_code in zone.get("zips", []):
                clean_zip = str(zip_code).strip()[:5]
                zip_to_zone[clean_zip] = zone_id

        if len(zones) < 100:
            break
        page += 1

    _zone_cache["data"] = all_zones
    _zone_cache["zip_to_zone"] = zip_to_zone
    _zone_cache["expires_at"] = current_time + 86400  # 24 hours

    print(f"[Dispatch] Loaded {len(all_zones)} zones, {len(zip_to_zone)} zip codes")
    return all_zones, zip_to_zone


def get_all_technicians():
    """Fetch all technicians from ServiceTitan."""
    url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/technicians"
    headers = get_api_headers()

    all_techs = []
    page = 1

    while True:
        resp = requests.get(url, headers=headers, params={"page": page, "pageSize": 100})
        if resp.status_code != 200:
            print(f"[Dispatch] Failed to fetch technicians: {resp.status_code}")
            break

        techs = resp.json().get("data", [])
        if not techs:
            break

        all_techs.extend(techs)
        if len(techs) < 100:
            break
        page += 1

    return all_techs


def get_tech_custom_field(tech, field_name):
    """Get a custom field value from a technician."""
    type_id = FIELD_IDS.get(field_name)
    if not type_id:
        return None

    for cf in tech.get("customFields", []):
        if cf.get("typeId") == type_id:
            return cf.get("value")
    return None


def get_tech_skill_rating(tech, job_category):
    """
    Get the skill rating for a technician for a given job category.
    Returns int 1-5, or None if not set.
    """
    field_name = JOB_CATEGORY_TO_FIELD.get(job_category)
    if not field_name:
        return None

    value = get_tech_custom_field(tech, field_name)
    if value is None:
        return None

    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def dispatch_technician(job_category, customer_address, is_emergency=False):
    """
    Find the best available technician for a job.

    Args:
        job_category: One of "Sewers/Mainline", "Water Heaters", "Misc Plumbing",
                      "Gas Lines", "Well Pump"
        customer_address: Full address string or dict with lat/lng/zip
        is_emergency: If True, may affect prioritization (reserved for future use)

    Returns:
        dict with tech_id, tech_name, skill_rating, distance_miles,
        auto_dispatch, requires_approval
        OR {"error": "no_match"} if no technician available
    """
    print(f"[Dispatch] Finding tech for {job_category} at {customer_address}")

    # Validate job category
    if job_category not in JOB_CATEGORY_TO_FIELD:
        return {"error": f"invalid_category: {job_category}"}

    # Get customer location
    if isinstance(customer_address, dict):
        customer_lat = customer_address.get("lat")
        customer_lng = customer_address.get("lng")
        customer_zip = customer_address.get("zip")
    else:
        customer_lat, customer_lng, customer_zip = geocode_address(customer_address)

    if not customer_zip:
        print("[Dispatch] Could not determine customer zip code")
        return {"error": "no_match", "reason": "could_not_geocode"}

    print(f"[Dispatch] Customer location: {customer_lat}, {customer_lng}, zip={customer_zip}")

    # Get zones and find customer's zone
    zones, zip_to_zone = get_zones()
    customer_zone_id = zip_to_zone.get(customer_zip)

    if not customer_zone_id:
        print(f"[Dispatch] Zip {customer_zip} not in any service zone")
        return {"error": "no_match", "reason": "outside_service_area"}

    print(f"[Dispatch] Customer zone: {customer_zone_id} ({zones.get(customer_zone_id, {}).get('name', 'Unknown')})")

    # Get all technicians
    technicians = get_all_technicians()
    print(f"[Dispatch] Fetched {len(technicians)} technicians")

    # Filter technicians
    candidates = []
    for tech in technicians:
        tech_id = tech.get("id")
        tech_name = tech.get("name", "Unknown")

        # Must be active
        if not tech.get("active"):
            continue

        # Must have Dispatchable=YES
        dispatchable = get_tech_custom_field(tech, "Dispatchable")
        if dispatchable != "YES":
            continue

        # Must be Idle
        if tech.get("status") != "Idle":
            continue

        # Must cover customer's zone
        tech_zones = tech.get("zoneIds", [])
        if customer_zone_id not in tech_zones:
            continue

        # Must have skill rating for this category
        skill = get_tech_skill_rating(tech, job_category)
        if skill is None:
            continue

        # Get tech's current location for distance calculation
        location = tech.get("location", {})
        tech_lat = location.get("latitude")
        tech_lng = location.get("longitude")

        # Calculate distance if we have both coordinates
        distance = None
        if customer_lat and customer_lng and tech_lat and tech_lng:
            distance = haversine_miles(customer_lat, customer_lng, tech_lat, tech_lng)

        candidates.append({
            "id": tech_id,
            "name": tech_name,
            "skill": skill,
            "distance": distance,
            "lat": tech_lat,
            "lng": tech_lng,
        })

    print(f"[Dispatch] Found {len(candidates)} eligible technicians")

    if not candidates:
        return {"error": "no_match", "reason": "no_eligible_techs"}

    # Sort by skill (1 = best, ascending) then by distance (ascending)
    # Techs without distance go to the end
    def sort_key(c):
        dist = c["distance"] if c["distance"] is not None else float("inf")
        return (c["skill"], dist)

    candidates.sort(key=sort_key)

    # Find the best skill rating among candidates
    best_skill = candidates[0]["skill"]

    # Filter to only top-skilled technicians
    top_skilled = [c for c in candidates if c["skill"] == best_skill]

    # Among top-skilled, pick the closest one
    if len(top_skilled) > 1:
        top_skilled.sort(key=lambda c: c["distance"] if c["distance"] is not None else float("inf"))

    chosen = top_skilled[0]

    # Determine dispatch type based on skill rating
    # Skill 1-3 = auto dispatch, 4-5 = requires approval
    auto_dispatch = chosen["skill"] <= 3
    requires_approval = chosen["skill"] >= 4

    result = {
        "tech_id": chosen["id"],
        "tech_name": chosen["name"],
        "skill_rating": chosen["skill"],
        "distance_miles": round(chosen["distance"], 2) if chosen["distance"] else None,
        "auto_dispatch": auto_dispatch,
        "requires_approval": requires_approval,
    }

    print(f"[Dispatch] Selected: {chosen['name']} (skill={chosen['skill']}, distance={result['distance_miles']} mi)")
    return result


# CLI for testing
if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 3:
        print("Usage: python -m app.services.dispatch <job_category> <address>")
        print("       python -m app.services.dispatch 'Water Heaters' '123 Main St, Pittsburgh, PA 15213'")
        sys.exit(1)

    category = sys.argv[1]
    address = sys.argv[2]
    emergency = "--emergency" in sys.argv

    result = dispatch_technician(category, address, is_emergency=emergency)
    print("\nResult:")
    print(json.dumps(result, indent=2))
