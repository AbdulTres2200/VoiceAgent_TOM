import os
import time
import requests
import usaddress
from dotenv import load_dotenv

load_dotenv()

# ServiceTitan credentials (shared with servicetitan.py)
TENANT_ID = os.getenv("TENANT_ID")
APP_KEY = os.getenv("APP_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

# Cache for service area zips (24 hour TTL for speed)
_service_area_cache = {
    "zips": set(),
    "zip_to_zone": {},
    "expires_at": 0,
    "loaded": False
}

# Google Maps API timeout (seconds)
GOOGLE_API_TIMEOUT = 3

# Token cache (shared pattern with servicetitan.py)
_token_cache = {
    "access_token": None,
    "expires_at": 0
}


def get_access_token():
    """
    Get access token from ServiceTitan OAuth endpoint.
    Caches token and reuses it for 900 seconds.
    """
    current_time = time.time()

    if _token_cache["access_token"] and current_time < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    url = "https://auth.servicetitan.io/connect/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    response = requests.post(url, data=data, headers=headers)
    response.raise_for_status()

    token_data = response.json()
    access_token = token_data.get("access_token")

    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = current_time + 900

    return access_token


def get_service_area_zips(force_refresh=False):
    """
    Fetch all zip codes from active ServiceTitan zones.
    Caches result for 24 hours for speed.
    Returns tuple of (set of zips, dict mapping zip -> zone_name)
    """
    current_time = time.time()

    # Return cached data if still valid (and not forcing refresh)
    if not force_refresh and _service_area_cache["loaded"] and current_time < _service_area_cache["expires_at"]:
        # Silent return for cached data (no logging for speed)
        return _service_area_cache["zips"], _service_area_cache["zip_to_zone"]

    print("[ServiceArea] Fetching service area zips from ServiceTitan...")

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    url = f"https://api.servicetitan.io/dispatch/v2/tenant/{TENANT_ID}/zones"
    print(f"  GET {url}")

    try:
        resp = requests.get(url, headers=headers)
        print(f"  Status: {resp.status_code}")

        if resp.status_code == 200:
            zones_data = resp.json().get("data", [])
            print(f"  Found {len(zones_data)} zones")

            all_zips = set()
            zip_to_zone = {}

            for zone in zones_data:
                zone_name = zone.get("name", "Unknown Zone")
                is_active = zone.get("active", True)

                if not is_active:
                    continue

                # Extract zips from zone
                zips = zone.get("zips", [])
                if isinstance(zips, list):
                    for zip_code in zips:
                        if zip_code:
                            clean_zip = str(zip_code).strip()[:5]  # Take first 5 digits
                            all_zips.add(clean_zip)
                            zip_to_zone[clean_zip] = zone_name

            print(f"  Extracted {len(all_zips)} unique zip codes from active zones")

            # Cache for 24 hours (86400 seconds)
            _service_area_cache["zips"] = all_zips
            _service_area_cache["zip_to_zone"] = zip_to_zone
            _service_area_cache["expires_at"] = current_time + 86400
            _service_area_cache["loaded"] = True

            return all_zips, zip_to_zone
        else:
            print(f"  Error: {resp.text}")
            return set(), {}

    except Exception as e:
        print(f"  Error fetching zones: {e}")
        return set(), {}


def parse_address_google(address_string: str, google_api_key: str = None):
    """
    Parse address using Google Geocoding API.
    Falls back to usaddress if Google fails or key not provided.
    Returns dict with street, city, state, zip, formatted_address, lat, lng
    """
    result = {
        "street": "",
        "city": "",
        "state": "",
        "zip": "",
        "formatted_address": "",
        "lat": None,
        "lng": None,
        "method": "unknown"
    }

    if not address_string:
        return result

    # Try Google Geocoding API first (with timeout for speed)
    if google_api_key:
        print(f"[ServiceArea] Parsing address with Google: {address_string}")
        try:
            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {
                "address": address_string,
                "key": google_api_key
            }

            resp = requests.get(url, params=params, timeout=GOOGLE_API_TIMEOUT)
            data = resp.json()

            if data.get("status") == "OK" and data.get("results"):
                google_result = data["results"][0]

                # Extract lat/lng
                location = google_result.get("geometry", {}).get("location", {})
                result["lat"] = location.get("lat")
                result["lng"] = location.get("lng")
                result["formatted_address"] = google_result.get("formatted_address", "")

                # Parse address components
                components = google_result.get("address_components", [])
                street_number = ""
                route = ""

                for comp in components:
                    types = comp.get("types", [])

                    if "street_number" in types:
                        street_number = comp.get("long_name", "")
                    elif "route" in types:
                        route = comp.get("long_name", "")
                    elif "locality" in types:
                        result["city"] = comp.get("long_name", "")
                    elif "administrative_area_level_1" in types:
                        result["state"] = comp.get("short_name", "")
                    elif "postal_code" in types:
                        result["zip"] = comp.get("long_name", "")[:5]

                # Combine street number and route
                result["street"] = f"{street_number} {route}".strip()
                result["method"] = "google"

                # Fallback: if Google didn't return zip, try to extract from original input
                if not result["zip"]:
                    import re
                    zip_match = re.search(r'\b(\d{5})(?:-\d{4})?\b', address_string)
                    if zip_match:
                        result["zip"] = zip_match.group(1)
                        print(f"  Google missing zip, extracted from input: {result['zip']}")

                print(f"  Google result: {result['street']}, {result['city']}, {result['state']} {result['zip']}")
                return result

            else:
                print(f"  Google API error: {data.get('status')} - {data.get('error_message', '')}")

        except requests.exceptions.Timeout:
            print(f"  Google API timeout (>{GOOGLE_API_TIMEOUT}s) - falling back to usaddress")
        except Exception as e:
            print(f"  Google API exception: {e}")

    # Fallback to usaddress
    print(f"[ServiceArea] Falling back to usaddress: {address_string}")
    try:
        parsed, addr_type = usaddress.tag(address_string)

        street_parts = []
        for key in ['AddressNumber', 'StreetNamePreDirectional', 'StreetName',
                    'StreetNamePostType', 'StreetNamePostDirectional']:
            if key in parsed:
                street_parts.append(parsed[key])

        result["street"] = " ".join(street_parts)
        result["city"] = parsed.get("PlaceName", "")
        result["state"] = parsed.get("StateName", "")
        result["zip"] = parsed.get("ZipCode", "")[:5] if parsed.get("ZipCode") else ""
        result["formatted_address"] = address_string
        result["method"] = "usaddress"

        print(f"  usaddress result: {result['street']}, {result['city']}, {result['state']} {result['zip']}")

    except Exception as e:
        print(f"  usaddress error: {e}")
        result["formatted_address"] = address_string
        result["method"] = "failed"

    return result


def check_service_area(address_string: str, google_api_key: str = None):
    """
    Check if an address is within the service area.
    Returns dict with in_service_area, zip_code, zone_name, business_unit info, message, and parsed address info.
    """
    from app.services.servicetitan import get_business_unit_by_zone

    print(f"\n[ServiceArea] Checking service area for: {address_string}")

    # Parse the address
    parsed = parse_address_google(address_string, google_api_key)
    zip_code = parsed.get("zip", "")

    # Get service area zips
    valid_zips, zip_to_zone = get_service_area_zips()

    # Check if zip is in service area
    in_service_area = zip_code in valid_zips if zip_code else False
    zone_name = zip_to_zone.get(zip_code, "") if in_service_area else ""

    # Get business unit for this zone
    business_unit_id = None
    business_unit_name = None
    if in_service_area and zone_name:
        bu_info = get_business_unit_by_zone(zone_name)
        business_unit_id = bu_info.get("business_unit_id")
        business_unit_name = bu_info.get("business_unit_name")

    # Generate appropriate message
    if not zip_code:
        message = "I couldn't determine the zip code from that address. Could you please provide the full address including the zip code?"
    elif in_service_area:
        message = "Great news, we service your area!"
    else:
        message = f"I'm sorry, but we don't currently service the {zip_code} zip code area. We only service specific areas in Western Pennsylvania, Erie, and the Ohio Valley region."

    result = {
        "in_service_area": in_service_area,
        "zip_code": zip_code,
        "zone_name": zone_name,
        "business_unit_id": business_unit_id,
        "business_unit_name": business_unit_name,
        "message": message,
        "formatted_address": parsed.get("formatted_address", ""),
        "street": parsed.get("street", ""),
        "city": parsed.get("city", ""),
        "state": parsed.get("state", ""),
        "lat": parsed.get("lat"),
        "lng": parsed.get("lng"),
        "parse_method": parsed.get("method", "")
    }

    print(f"  Result: in_service_area={in_service_area}, zip={zip_code}, zone={zone_name}")
    print(f"  Business Unit: {business_unit_name} (ID: {business_unit_id})")
    print(f"  Message: {message}")

    return result


def preload_service_area_cache():
    """
    Pre-load the service area zones cache at startup.
    Call this when the FastAPI app starts to ensure first request is fast.
    """
    print("[ServiceArea] Pre-loading service area zones cache...")
    zips, zip_to_zone = get_service_area_zips(force_refresh=True)
    print(f"[ServiceArea] Cache loaded with {len(zips)} zip codes")
    return len(zips)
