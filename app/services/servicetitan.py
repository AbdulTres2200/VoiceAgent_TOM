import os
import time
import requests
import usaddress
from datetime import datetime, timedelta
from dotenv import load_dotenv
import json

load_dotenv()


def get_dispatch_category(job_type_name):
    """
    Map ServiceTitan job type names to dispatch categories.
    Returns one of: "Sewers/Mainline", "Water Heaters", "Misc Plumbing", "Gas Lines", "Well Pump"
    """
    if not job_type_name:
        return "Misc Plumbing"

    name_upper = job_type_name.upper()

    # Gas line jobs - check first to avoid GAS1 matching S1
    if "GAS" in name_upper:
        return "Gas Lines"

    # Water heater jobs
    if "WH" in name_upper or "WATER HEATER" in name_upper:
        return "Water Heaters"

    # Well pump jobs
    if "PUMP2" in name_upper or "WELL" in name_upper:
        return "Well Pump"

    # Main sewer/drain jobs - use word boundary to avoid matching GAS1
    if name_upper.startswith("S1") or "MAIN LINE" in name_upper or "MAINLINE" in name_upper:
        return "Sewers/Mainline"

    # Default to misc plumbing (covers P1, P2, P3, S2, Pump1, etc.)
    return "Misc Plumbing"


def parse_address(address_string):
    """
    Parse a full address string into components using usaddress library.
    Returns dict with street, city, state, zip.
    Uses defaults for zip and state if not found/invalid (required by ServiceTitan).
    """
    import re

    if not address_string:
        return {"street": "", "city": "", "state": DEFAULT_STATE, "zip": DEFAULT_ZIP}

    # First try to extract zip code (5 digits or 5+4 format)
    zip_match = re.search(r'\b(\d{5}(?:-\d{4})?)\b', address_string)
    zipcode = zip_match.group(1) if zip_match else DEFAULT_ZIP

    # Try to find state code (2-letter abbreviation)
    # Search from the END of the address to avoid matching street suffixes like "Ct" (Court)
    # The state typically appears after the city and before the zip code
    state = DEFAULT_STATE
    upper_address = address_string.upper()

    # Find all 2-letter sequences that are valid states
    all_state_matches = re.finditer(r'\b([A-Z]{2})\b', upper_address)
    valid_state_matches = [(m.group(1), m.start()) for m in all_state_matches if m.group(1) in VALID_STATES]

    if valid_state_matches:
        # If we found a zip code, prefer the state that appears closest before it
        if zip_match:
            zip_pos = upper_address.find(zipcode)
            # Filter to states that appear before the zip code
            states_before_zip = [(s, pos) for s, pos in valid_state_matches if pos < zip_pos]
            if states_before_zip:
                # Take the last state before the zip (closest to zip = most likely the actual state)
                state = states_before_zip[-1][0]
            else:
                # Fallback to the last valid state found
                state = valid_state_matches[-1][0]
        else:
            # No zip found, take the last valid state (most likely the actual state, not a street suffix)
            state = valid_state_matches[-1][0]

    try:
        parsed, addr_type = usaddress.tag(address_string)

        # Build street from address components
        street_parts = []
        for key in ['AddressNumber', 'StreetNamePreDirectional', 'StreetName',
                    'StreetNamePostType', 'StreetNamePostDirectional', 'OccupancyType', 'OccupancyIdentifier']:
            if key in parsed:
                street_parts.append(parsed[key])

        street = " ".join(street_parts) if street_parts else ""

        # Get city - combine PlaceName parts if usaddress split them wrong
        city = parsed.get('PlaceName', "")

        # If usaddress put something in StateName that's not a valid state, it's probably part of city
        parsed_state = parsed.get('StateName', "")
        if parsed_state and parsed_state.upper() not in VALID_STATES:
            # It's probably part of the city name (e.g., "New Brighton" -> city="New", state="Brighton")
            if city:
                city = f"{city} {parsed_state}"
            else:
                city = parsed_state

        # If no street parsed, try to extract it manually
        if not street:
            # Take everything before the city/state/zip
            street = address_string
            for remove in [city, parsed_state, zipcode, state]:
                if remove:
                    street = street.replace(remove, "")
            street = re.sub(r'[,\s]+$', '', street).strip()
            street = re.sub(r'^[,\s]+', '', street).strip()

        return {
            "street": street.strip(),
            "city": city.strip(),
            "state": state,
            "zip": zipcode
        }
    except Exception as e:
        print(f"[ServiceTitan] Address parsing failed: {e}")
        return {"street": address_string, "city": "", "state": DEFAULT_STATE, "zip": DEFAULT_ZIP}

# ServiceTitan credentials
TENANT_ID = os.getenv("TENANT_ID")
APP_KEY = os.getenv("APP_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

# OpenAI API Key for job type detection
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ServiceTitan Job Configuration (from your account)
JOB_TYPE_ID = int(os.getenv("JOB_TYPE_ID"))
BUSINESS_UNIT_ID = int(os.getenv("BUSINESS_UNIT_ID"))
CAMPAIGN_ID = int(os.getenv("CAMPAIGN_ID"))
JOB_PRIORITY = os.getenv("JOB_PRIORITY")
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY")
DEFAULT_ZIP = os.getenv("DEFAULT_ZIP")
DEFAULT_STATE = os.getenv("DEFAULT_STATE")

# Custom Field TypeIds for Job booking questions
CUSTOM_FIELD_HOMEOWNER = 1520488656  # "1 - Do you own the home?"
CUSTOM_FIELD_EMAIL_PROMO = 1583742649  # "2 - Do we have your permission to send promotional emails?"

# Valid US state codes
VALID_STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
                "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
                "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
                "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
                "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"}

# Token cache
_token_cache = {
    "access_token": None,
    "expires_at": 0
}

# Job types cache (24 hour TTL)
_job_types_cache = None
_job_types_cache_time = None
_job_type_detection_cache = {}

# Live call cache - stores to_number from inbound webhook
# Keyed by cleaned from_number, expires after 1 hour
_live_call_cache = {}

# Service area cache - stores business_unit info from check_service_area
# Keyed by cleaned phone number, expires after 1 hour
_service_area_bu_cache = {}

# Service area cache by address - stores business_unit info keyed by zip+street_number
# Used when phone number is not available (e.g., Retell custom functions)
_service_area_bu_by_address_cache = {}

# Service area address cache - stores parsed address from check_service_area
# Keyed by cleaned phone number, expires after 1 hour
_service_area_address_cache = {}

# Business unit cache (24 hour TTL)
_business_unit_cache = {
    "units": [],
    "name_to_id": {},
    "expires_at": 0
}

# County to Business Unit mapping (confirmed by client)
COUNTY_BUSINESS_UNIT_MAP = {
    # WESTERN PA (ID: 1239)
    "allegheny": "Western PA",
    "armstrong": "Western PA",
    "beaver": "Western PA",
    "butler": "Western PA",
    "fayette": "Morgantown",
    "indiana": "Western PA",
    "lawrence": "Western PA",
    "mercer": "Erie",
    "washington": "Western PA",
    "westmoreland": "Western PA",
    "green": "Morgantown",
    "greene": "Morgantown",

    # OHIO VALLEY
    "belmont": "Ohio Valley",
    "brooke": "Ohio Valley",
    "hancock": "Ohio Valley",
    "harrison": "Ohio Valley",
    "jefferson": "Ohio Valley",
    "marshall": "Ohio Valley",
    "ohio": "Ohio Valley",
    "wetzel": "Ohio Valley",

    # ERIE
    "erie": "Erie",
    "venango": "Erie",

    # MORGANTOWN
    "monongalia": "Morgantown",
    "monongahela": "Morgantown",
}


def store_live_call_info(from_number: str, to_number: str):
    """Store to_number from inbound webhook for use during booking."""
    cleaned_from = clean_phone(from_number)
    cleaned_to = clean_phone(to_number)
    if cleaned_from and cleaned_to:
        _live_call_cache[cleaned_from] = {
            "to_number": cleaned_to,
            "timestamp": time.time()
        }
        print(f"[LiveCallCache] Stored to_number for {cleaned_from}: {cleaned_to}")


def get_live_call_to_number(from_number: str):
    """Retrieve cached to_number for a phone number (within 1 hour)."""
    cleaned = clean_phone(from_number)
    if cleaned in _live_call_cache:
        cached = _live_call_cache[cleaned]
        age = time.time() - cached["timestamp"]
        if age < 3600:  # 1 hour
            print(f"[LiveCallCache] Found to_number for {cleaned}: {cached['to_number']} ({int(age)}s old)")
            return cached["to_number"]
        else:
            # Expired, remove it
            del _live_call_cache[cleaned]
    return None


def store_service_area_business_unit(phone: str, business_unit_id: int, business_unit_name: str, zone_name: str = None):
    """Store business unit info from service area check for use during booking."""
    cleaned = clean_phone(phone)
    if cleaned and business_unit_id:
        _service_area_bu_cache[cleaned] = {
            "business_unit_id": business_unit_id,
            "business_unit_name": business_unit_name,
            "zone_name": zone_name,
            "timestamp": time.time()
        }
        print(f"[ServiceAreaCache] Stored BU for {cleaned}: {business_unit_name} (ID: {business_unit_id})")


def get_service_area_business_unit(phone: str):
    """Retrieve cached business unit from service area check (within 1 hour)."""
    cleaned = clean_phone(phone)
    if cleaned in _service_area_bu_cache:
        cached = _service_area_bu_cache[cleaned]
        age = time.time() - cached["timestamp"]
        if age < 3600:  # 1 hour
            print(f"[ServiceAreaCache] Found BU for {cleaned}: {cached['business_unit_name']} (ID: {cached['business_unit_id']}) ({int(age)}s old)")
            return cached
        else:
            # Expired, remove it
            del _service_area_bu_cache[cleaned]
    return None


def store_service_area_address(phone: str, street: str, city: str, state: str, zip_code: str):
    """Store parsed address from service area check for use when creating leads/locations."""
    cleaned = clean_phone(phone)
    if cleaned and street:
        _service_area_address_cache[cleaned] = {
            "street": street,
            "city": city,
            "state": state,
            "zip": zip_code,
            "country": DEFAULT_COUNTRY,
            "timestamp": time.time()
        }
        print(f"[ServiceAreaCache] Stored address for {cleaned}: {street}, {city}, {state} {zip_code}")


def get_service_area_address(phone: str):
    """Retrieve cached address from service area check (within 1 hour)."""
    cleaned = clean_phone(phone)
    if cleaned in _service_area_address_cache:
        cached = _service_area_address_cache[cleaned]
        age = time.time() - cached["timestamp"]
        if age < 3600:  # 1 hour
            print(f"[ServiceAreaCache] Found address for {cleaned}: {cached['street']}, {cached['city']} ({int(age)}s old)")
            return cached
        else:
            # Expired, remove it
            del _service_area_address_cache[cleaned]
    return None


def _make_address_key(street: str, zip_code: str) -> str:
    """Create a normalized cache key from street and zip code.
    Uses zip + first number from street (e.g., '16511:541' for '541 Parkside Dr, 16511')
    """
    if not zip_code:
        return None
    # Extract first number from street address
    import re
    match = re.search(r'(\d+)', street or '')
    street_num = match.group(1) if match else ''
    if not street_num:
        return None
    return f"{zip_code}:{street_num}"


def store_service_area_bu_by_address(street: str, zip_code: str, business_unit_id: int, business_unit_name: str, zone_name: str = None):
    """Store business unit info by address for cases where phone is not available."""
    key = _make_address_key(street, zip_code)
    if key and business_unit_id:
        _service_area_bu_by_address_cache[key] = {
            "business_unit_id": business_unit_id,
            "business_unit_name": business_unit_name,
            "zone_name": zone_name,
            "timestamp": time.time()
        }
        print(f"[ServiceAreaCache] Stored BU by address key '{key}': {business_unit_name} (ID: {business_unit_id})")


def get_service_area_bu_by_address(street: str, zip_code: str):
    """Retrieve cached business unit by address (within 1 hour)."""
    key = _make_address_key(street, zip_code)
    if key and key in _service_area_bu_by_address_cache:
        cached = _service_area_bu_by_address_cache[key]
        age = time.time() - cached["timestamp"]
        if age < 3600:  # 1 hour
            print(f"[ServiceAreaCache] Found BU by address key '{key}': {cached['business_unit_name']} (ID: {cached['business_unit_id']}) ({int(age)}s old)")
            return cached
        else:
            del _service_area_bu_by_address_cache[key]
    return None


def get_business_units_from_st(force_refresh=False):
    """
    Fetch all active business units from ServiceTitan.
    Caches result for 24 hours.
    Returns list of business units and name->id mapping.
    """
    current_time = time.time()

    # Return cached data if still valid
    if not force_refresh and _business_unit_cache["units"] and current_time < _business_unit_cache["expires_at"]:
        return _business_unit_cache["units"], _business_unit_cache["name_to_id"]

    print("\n[Business Units] Fetching from ServiceTitan API...")

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/business-units"
    params = {"pageSize": 50, "active": "true"}

    print(f"  GET {url}")

    try:
        resp = requests.get(url, headers=headers, params=params)
        print(f"  Status: {resp.status_code}")

        if resp.status_code == 200:
            units_data = resp.json().get("data", [])
            print(f"  Found {len(units_data)} business units:")

            name_to_id = {}
            for unit in units_data:
                unit_id = unit.get("id")
                unit_name = unit.get("name", "")
                print(f"    - {unit_name} (ID: {unit_id})")
                # Map by exact name and lowercase name
                name_to_id[unit_name] = unit_id
                name_to_id[unit_name.lower()] = unit_id

            # Cache for 24 hours
            _business_unit_cache["units"] = units_data
            _business_unit_cache["name_to_id"] = name_to_id
            _business_unit_cache["expires_at"] = current_time + 86400

            return units_data, name_to_id
        else:
            print(f"  Error: {resp.text}")
            return [], {}

    except Exception as e:
        print(f"  Error fetching business units: {e}")
        return [], {}


def get_business_unit_by_zone(zone_name, city=None, state=None):
    """
    Get business unit ID and name for a zone (county) name.
    Uses COUNTY_BUSINESS_UNIT_MAP to determine correct business unit.
    Falls back to Western PA (ID: 1239) if no match.

    Args:
        zone_name: County name from ST zones API (e.g., "Allegheny County")
        city: Optional city name (not used currently, for future enhancement)
        state: Optional state code (not used currently, for future enhancement)

    Returns:
        dict with business_unit_id and business_unit_name
    """
    print(f"\n[Business Unit] Looking up zone: {zone_name}")

    # Get business units from cache/API
    units, name_to_id = get_business_units_from_st()

    # Default fallback
    default_result = {
        "business_unit_id": RETELL_BUSINESS_UNIT_ID,
        "business_unit_name": RETELL_BUSINESS_UNIT_NAME
    }

    if not zone_name:
        print(f"[Business Unit] No zone provided, using default: {RETELL_BUSINESS_UNIT_NAME} (ID: {RETELL_BUSINESS_UNIT_ID})")
        return default_result

    # Clean the zone name: lowercase, strip whitespace, remove "county" suffix
    cleaned_zone = zone_name.lower().strip()
    cleaned_zone = cleaned_zone.replace(" county", "").replace("county", "").strip()

    print(f"[Business Unit] Cleaned zone: '{cleaned_zone}'")

    # Look up in county map
    bu_name = COUNTY_BUSINESS_UNIT_MAP.get(cleaned_zone)

    if bu_name:
        # Find the business unit ID from fetched ST data
        bu_id = name_to_id.get(bu_name) or name_to_id.get(bu_name.lower())

        if bu_id:
            print(f"[Business Unit] Zone: {zone_name} -> {bu_name} (ID: {bu_id})")
            return {
                "business_unit_id": bu_id,
                "business_unit_name": bu_name
            }
        else:
            print(f"[Business Unit] Warning: Mapped to '{bu_name}' but ID not found in ST data")
            # Try common ID fallbacks
            if bu_name == "Western PA":
                return {"business_unit_id": 1239, "business_unit_name": "Western PA"}
            elif bu_name == "Erie":
                return {"business_unit_id": name_to_id.get("Erie", 1239), "business_unit_name": "Erie"}
            elif bu_name == "Morgantown":
                return {"business_unit_id": name_to_id.get("Morgantown", 1239), "business_unit_name": "Morgantown"}
            elif bu_name == "Ohio Valley":
                return {"business_unit_id": name_to_id.get("Ohio Valley", 1239), "business_unit_name": "Ohio Valley"}
    else:
        print(f"[Business Unit] Zone '{cleaned_zone}' not found in county map")

    print(f"[Business Unit] Using default: {RETELL_BUSINESS_UNIT_NAME} (ID: {RETELL_BUSINESS_UNIT_ID})")
    return default_result


# Keywords to filter out from job types
# Note: "Commercial" removed - we now support commercial customers and use AI to select appropriate job type
JOB_TYPE_FILTER_KEYWORDS = [
    "Excavation", "Opportunity", "Pre Sold", "Bio-Choice",
    "EX2", "Final Payment", "Trade", "Perma Liner", "ReEval", "Clean up", "Pipe Burst"
]


def clean_phone(phone_string, for_display=False):
    """
    Clean phone number for use with ServiceTitan API.
    - US numbers (+1): strips +1, returns 10 digits
    - Pakistani numbers: normalizes to 92XXXXXXXXXX format (12 digits)
    - Other international numbers: keeps full number with country code (digits only)
    - Returns empty string only if input is empty/None
    """
    import re
    if not phone_string:
        return ""

    original = phone_string

    # Extract only digits
    digits = re.sub(r'\D', '', phone_string)

    # Pakistani number handling - normalize to 92XXXXXXXXXX format
    # Local format: 03XX-XXXXXXX (11 digits starting with 0)
    # International: +923XX-XXXXXXX or 923XX-XXXXXXX (12 digits starting with 92)
    if len(digits) == 11 and digits.startswith('0') and digits[1] == '3':
        # Local Pakistani format (03XX...) - convert to international (923XX...)
        result = '92' + digits[1:]  # Replace leading 0 with 92
        print(f"[Phone] Pakistani local: {original} -> {result}")
        return result
    elif len(digits) == 12 and digits.startswith('92') and digits[2] == '3':
        # Already in international Pakistani format
        print(f"[Phone] Pakistani international: {original} -> {digits}")
        return digits

    # US number: 10 digits, or 11 starting with 1
    if len(digits) == 10:
        result = digits
        print(f"[Phone] US number: {original} -> {result}")
        return result
    elif len(digits) == 11 and digits.startswith('1'):
        result = digits[1:]  # Remove leading 1 for US
        print(f"[Phone] US number (+1): {original} -> {result}")
        return result

    # Other international number: keep full digits with country code
    if len(digits) >= 10:
        print(f"[Phone] International number: {original} -> {digits}")
        return digits

    # Short/invalid number - return as-is for logging purposes
    print(f"[Phone] Short/invalid number: {original} -> {digits}")
    return digits


def get_job_types_from_st():
    """
    Fetch active job types from ServiceTitan with 24-hour caching.
    Filters out commercial/excavation/specialty job types.
    Returns list of {id, name, priority, summary}.
    """
    global _job_types_cache, _job_types_cache_time

    # Check cache validity (24 hours)
    if _job_types_cache and _job_types_cache_time:
        cache_age = time.time() - _job_types_cache_time
        if cache_age < 86400:  # 24 hours
            print(f"[Job Types] Using cached job types ({len(_job_types_cache)} types, {int(cache_age/3600)}h old)")
            return _job_types_cache

    print("[Job Types] Fetching job types from ServiceTitan...")

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/job-types"
    params = {"pageSize": 100, "active": "true"}

    try:
        resp = requests.get(url, headers=headers, params=params)
        print(f"[Job Types] API Status: {resp.status_code}")


        if resp.status_code == 200:
            data = resp.json()
            all_job_types = data.get("data", [])
            print(f"[Job Types] Total from API: {len(all_job_types)}")

            # Filter and extract only needed fields
            filtered_types = []
            for jt in all_job_types:
                name = jt.get("name", "")

                # Check if name contains any filter keywords
                should_filter = any(keyword.lower() in name.lower() for keyword in JOB_TYPE_FILTER_KEYWORDS)

                if not should_filter:
                    filtered_types.append({
                        "id": jt.get("id"),
                        "name": name,
                        "priority": jt.get("priority", "Normal"),
                        "summary": jt.get("summary", "")
                    })

            # Cache the result
            _job_types_cache = filtered_types
            _job_types_cache_time = time.time()

            print(f"[Job Types] Filtered to {len(filtered_types)} residential job types")
            return filtered_types
        else:
            print(f"[Job Types] API Error: {resp.text}")
            return []

    except Exception as e:
        print(f"[Job Types] Exception: {e}")
        return []


def detect_job_type(issue_description: str, customer_type: str = "Residential"):
    """
    Use OpenAI to detect the best job type for a given issue description.
    Returns {job_type_id, job_type_name, priority, job_category}.

    Args:
        issue_description: The customer's issue description
        customer_type: "Residential" or "Commercial" - affects job type selection
    """
    global _job_type_detection_cache
    import re

    # Normalize customer_type
    is_commercial = customer_type.lower() == "commercial" if customer_type else False

    # Default fallback - use commercial fallback if commercial customer
    if is_commercial:
        fallback = {
            "job_type_id": 1447573448,  # Update this if you have a commercial default
            "job_type_name": "CP2 Commercial Minor Plumbing",
            "priority": "High",
            "job_category": "Misc Plumbing"
        }
    else:
        fallback = {
            "job_type_id": 1447573448,
            "job_type_name": "P2 Minor Plumbing",
            "priority": "High",
            "job_category": "Misc Plumbing"
        }

    if not issue_description:
        print("[Job Type] No issue description provided, using fallback")
        return fallback

    # Check cache first (include customer_type in cache key)
    cache_key = f"{customer_type.lower()}:{issue_description.lower().strip()}"
    if cache_key in _job_type_detection_cache:
        cached = _job_type_detection_cache[cache_key]
        print(f"[Job Type] Issue: {issue_description}")
        print(f"[Job Type] Customer Type: {customer_type}")
        print(f"[Job Type] Detected: {cached['job_type_name']} (ID: {cached['job_type_id']}) Priority: {cached['priority']}")
        print("[Job Type] Method: cache hit")
        return cached

    # Get job types
    all_job_types = get_job_types_from_st()
    if not all_job_types:
        print("[Job Type] No job types available, using fallback")
        return fallback

    # Filter job types by customer type to avoid Commercial/Residential mismatch
    # Commercial job types typically start with 'C' (CS1, CS2, CP1, CP2, CCL, CWH, etc.)
    if is_commercial:
        # For commercial customers, prefer commercial job types but include all as fallback
        job_types = [jt for jt in all_job_types if jt["name"].upper().startswith("C")]
        if not job_types:
            job_types = all_job_types  # Fallback to all if no commercial types found
    else:
        # For residential customers, EXCLUDE commercial job types (those starting with 'C' followed by another letter)
        job_types = [jt for jt in all_job_types if not (jt["name"].upper().startswith("C") and len(jt["name"]) > 1 and jt["name"][1:2].isalpha())]
        if not job_types:
            job_types = all_job_types  # Fallback to all if filtering removed everything

    print(f"[Job Type] Filtered to {len(job_types)} job types for {customer_type} customer")

    # Build formatted list for OpenAI - clearer format with CODE as primary identifier
    formatted_list = ""
    for jt in job_types:
        summary = jt["summary"].strip() if jt["summary"] and jt["summary"].strip() else "General"
        # Format: CODE -> Description
        formatted_list += f"  {jt['name']} -> {summary}\n"

    print(f"[Job Type] Issue: {issue_description}")
    print(f"[Job Type] Customer Type: {customer_type}")
    print(f"[Job Type] Analyzing with OpenAI...")

    # Build customer type note for OpenAI
    customer_type_note = ""
    if is_commercial:
        customer_type_note = "\n\nIMPORTANT: This is a COMMERCIAL customer. Only use commercial job types (starting with C like CP1, CP2, CS1, CS2, CCL, CWH)."
    else:
        customer_type_note = "\n\nIMPORTANT: This is a RESIDENTIAL customer. Do NOT use commercial job types (those starting with C like CS2, CP2). Use residential types like S2, P2, WH1, etc."

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=50,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": """You are an expert plumbing dispatcher. Classify issues into job type CODES.

IMPORTANT: Respond with ONLY the job type CODE (like WH1, S2, Pump1, GAS1, P2 Minor Plumbing), not the description.

Classification rules:
- Water heater not working/no hot water -> WH1
- Water heater leaking -> WH2
- Water heater estimate -> WH3
- Main sewer/basement drain/multiple drains backing up -> S1 Main Line
- Single drain (sink, tub, shower) clog -> S2 Secondary Drain
- Gas smell/gas leak/gas line issues -> GAS1
- Sump pump or sewage pump -> Pump1
- Well pump -> Pump2
- Faucet, toilet, minor leak, frozen pipes -> P2 Minor Plumbing
- Emergency water line break/burst -> P1 Emergency Plumbing
- Remodel estimate -> P3"""
                },
                {
                    "role": "user",
                    "content": f"""Customer issue: "{issue_description}"

Job type codes:
{formatted_list}
Reply with ONLY the code (e.g., WH1, S2, Pump1).{customer_type_note}"""
                }
            ]
        )

        detected_name = response.choices[0].message.content.strip()
        print(f"[Job Type] OpenAI response: {detected_name}")

        # Clean the response - remove quotes, parentheses, extra text
        cleaned_name = re.sub(r'["\']', '', detected_name)
        cleaned_name = re.sub(r'\s*\(.*?\)', '', cleaned_name).strip()
        # Take only first line if multiple
        cleaned_name = cleaned_name.split('\n')[0].strip()
        print(f"[Job Type] Cleaned name: {cleaned_name}")

        # Find matching job type - try exact match first
        for jt in job_types:
            if jt["name"].strip().lower() == cleaned_name.lower():
                result = {
                    "job_type_id": jt["id"],
                    "job_type_name": jt["name"],
                    "priority": jt["priority"],
                    "job_category": get_dispatch_category(jt["name"])
                }
                _job_type_detection_cache[cache_key] = result
                print(f"[Job Type] Detected: {result['job_type_name']} (ID: {result['job_type_id']}) Priority: {result['priority']} Category: {result['job_category']}")
                print("[Job Type] Method: AI exact match")
                return result

        # Try partial match - if response contains the job type code
        for jt in job_types:
            jt_name_lower = jt["name"].strip().lower()
            cleaned_lower = cleaned_name.lower()
            # Check if the job type code is contained in the response or vice versa
            if jt_name_lower in cleaned_lower or cleaned_lower in jt_name_lower:
                result = {
                    "job_type_id": jt["id"],
                    "job_type_name": jt["name"],
                    "priority": jt["priority"],
                    "job_category": get_dispatch_category(jt["name"])
                }
                _job_type_detection_cache[cache_key] = result
                print(f"[Job Type] Detected: {result['job_type_name']} (ID: {result['job_type_id']}) Priority: {result['priority']} Category: {result['job_category']}")
                print("[Job Type] Method: AI partial match")
                return result

        # Try matching by summary keywords as last resort
        cleaned_words = set(cleaned_lower.split())
        for jt in job_types:
            summary = (jt["summary"] or "").lower()
            # Check if key words from response appear in summary
            if any(word in summary for word in cleaned_words if len(word) > 3):
                result = {
                    "job_type_id": jt["id"],
                    "job_type_name": jt["name"],
                    "priority": jt["priority"],
                    "job_category": get_dispatch_category(jt["name"])
                }
                _job_type_detection_cache[cache_key] = result
                print(f"[Job Type] Detected: {result['job_type_name']} (ID: {result['job_type_id']}) Priority: {result['priority']} Category: {result['job_category']}")
                print("[Job Type] Method: AI summary match")
                return result

        # No match found
        print(f"[Job Type] WARNING: No match for '{cleaned_name}', using fallback")
        print(f"[Job Type] Detected: {fallback['job_type_name']} (ID: {fallback['job_type_id']}) Priority: {fallback['priority']}")
        print("[Job Type] Method: fallback")
        return fallback

    except Exception as e:
        print(f"[Job Type] OpenAI error: {e}")
        print(f"[Job Type] Detected: {fallback['job_type_name']} (ID: {fallback['job_type_id']}) Priority: {fallback['priority']}")
        print("[Job Type] Method: fallback (error)")
        return fallback


# Default fallback campaign for Retell calls
RETELL_CAMPAIGN_ID = 1405279506
RETELL_CAMPAIGN_NAME = "Branding - Pittsburgh"
RETELL_BUSINESS_UNIT_ID = 1239
RETELL_BUSINESS_UNIT_NAME = "Western PA"


def get_live_call_campaign(from_number: str, to_number: str):
    """
    Get campaign info for a live call by matching from/to numbers.
    Tries telecom API first, then campaigns API, then uses real fallback values.
    """
    # Clean both numbers
    clean_from = clean_phone(from_number)
    clean_to = clean_phone(to_number)

    print("\n" + "=" * 70)
    print("                    LIVE CALL CAMPAIGN LOOKUP")
    print("=" * 70)
    print(f"  From: {from_number} -> cleaned: {clean_from}")
    print(f"  To:   {to_number} -> cleaned: {clean_to}")
    print("=" * 70)

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    # Method 1: Try telecom API - find call matching to_number
    print("\n[Method 1] Trying Telecom API...")
    print(f"  Looking for call where leadCall.to matches: {clean_to}")
    telecom_url = f"https://api.servicetitan.io/telecom/v2/tenant/{TENANT_ID}/calls"
    params = {
        "pageSize": 5,
        "orderBy": "Id",
        "orderByDirection": "desc",
        "from": clean_from
    }

    try:
        resp = requests.get(telecom_url, headers=headers, params=params)
        print(f"  GET {telecom_url}")
        print(f"  Params: {params}")
        print(f"  Status: {resp.status_code}")

        if resp.status_code == 200:
            calls_data = resp.json().get("data", [])
            print(f"  Found {len(calls_data)} calls from this number")

            # Loop through ALL results to find matching to_number
            for call in calls_data:
                lead_call = call.get("leadCall") or {}
                call_to_raw = lead_call.get("to", "")
                call_to_cleaned = clean_phone(call_to_raw)
                campaign = lead_call.get("campaign")

                print(f"    - Call ID {call.get('id')}: to={call_to_raw} -> cleaned={call_to_cleaned}, campaign={campaign}")

                # Check if this call's to_number matches our target
                if call_to_cleaned == clean_to:
                    print(f"  MATCH FOUND! Call to {call_to_cleaned} matches target {clean_to}")

                    if campaign:
                        # Extract campaign info from leadCall.campaign
                        campaign_id = campaign.get("id")
                        campaign_name = campaign.get("name", "")

                        # Extract business_unit from outer object, default to 1239
                        business_unit = call.get("businessUnit")
                        if business_unit:
                            business_unit_id = business_unit.get("id", RETELL_BUSINESS_UNIT_ID)
                            business_unit_name = business_unit.get("name", RETELL_BUSINESS_UNIT_NAME)
                        else:
                            business_unit_id = RETELL_BUSINESS_UNIT_ID
                            business_unit_name = RETELL_BUSINESS_UNIT_NAME

                        print("\n" + "*" * 70)
                        print("  SUCCESS: Found campaign via Telecom API!")
                        print(f"  Campaign: {campaign_name} (ID: {campaign_id})")
                        print(f"  Business Unit: {business_unit_name} (ID: {business_unit_id})")
                        print("*" * 70 + "\n")

                        return {
                            "method": "telecom_api",
                            "campaign_id": campaign_id,
                            "campaign_name": campaign_name,
                            "business_unit_id": business_unit_id,
                            "business_unit_name": business_unit_name
                        }
                    else:
                        print(f"  Matching call found but no campaign attached")

            print(f"  No call found with to_number matching {clean_to}")
    except Exception as e:
        print(f"  Error: {e}")

    # Method 2: Try campaigns API - search by to_number in campaign phone numbers
    print("\n[Method 2] Trying Campaigns API...")
    print(f"  Looking for to_number: {clean_to}")
    campaigns_url = f"https://api.servicetitan.io/marketing/v2/tenant/{TENANT_ID}/campaigns"
    params = {"pageSize": 100, "active": "true"}

    try:
        resp = requests.get(campaigns_url, headers=headers, params=params)
        print(f"  GET {campaigns_url}")
        print(f"  Status: {resp.status_code}")

        if resp.status_code == 200:
            campaigns_data = resp.json().get("data", [])
            print(f"  Found {len(campaigns_data)} active campaigns")
            print(f"  Searching for match with: {clean_to}")

            for campaign in campaigns_data:
                campaign_phones = campaign.get("campaignPhoneNumbers", [])
                campaign_name_preview = campaign.get("name", "Unknown")

                if campaign_phones:
                    # Clean all phone numbers for this campaign
                    cleaned_phones = []
                    for phone_entry in campaign_phones:
                        if isinstance(phone_entry, str):
                            cleaned = clean_phone(phone_entry)
                        elif isinstance(phone_entry, dict):
                            raw = phone_entry.get("phoneNumber", "") or phone_entry.get("number", "") or str(phone_entry)
                            cleaned = clean_phone(raw)
                        else:
                            cleaned = clean_phone(str(phone_entry))
                        if cleaned:
                            cleaned_phones.append(cleaned)

                    # Print campaign with its phone numbers (only first 3 campaigns with phones)
                    if cleaned_phones:
                        print(f"    - {campaign_name_preview}: {cleaned_phones}")

                    # Check for match
                    if clean_to in cleaned_phones:
                        campaign_id = campaign.get("id")
                        campaign_name = campaign.get("name", "")
                        business_unit = campaign.get("businessUnit")

                        # Handle businessUnit being a string, dict, or None
                        if isinstance(business_unit, dict):
                            business_unit_id = business_unit.get("id", RETELL_BUSINESS_UNIT_ID)
                            business_unit_name = business_unit.get("name", RETELL_BUSINESS_UNIT_NAME)
                        elif isinstance(business_unit, str):
                            # businessUnit is just the name as a string
                            business_unit_id = RETELL_BUSINESS_UNIT_ID
                            business_unit_name = business_unit
                        else:
                            business_unit_id = RETELL_BUSINESS_UNIT_ID
                            business_unit_name = RETELL_BUSINESS_UNIT_NAME

                        print("\n" + "*" * 70)
                        print("  SUCCESS: Found campaign via Campaigns API!")
                        print(f"  Campaign: {campaign_name} (ID: {campaign_id})")
                        print(f"  Business Unit: {business_unit_name} (ID: {business_unit_id})")
                        print(f"  Matched phone: {clean_to}")
                        print("*" * 70 + "\n")

                        return {
                            "method": "campaigns_api",
                            "campaign_id": campaign_id,
                            "campaign_name": campaign_name,
                            "business_unit_id": business_unit_id,
                            "business_unit_name": business_unit_name
                        }

            print(f"  No campaign found with phone number: {clean_to}")
    except Exception as e:
        print(f"  Error: {e}")

    # Method 3: Use real fallback values for Retell number
    print("\n[Method 3] Using real fallback values for Retell number...")
    print("*" * 70)
    print("  FALLBACK: Using confirmed values for +14126596858")
    print(f"  Campaign: {RETELL_CAMPAIGN_NAME} (ID: {RETELL_CAMPAIGN_ID})")
    print(f"  Business Unit: {RETELL_BUSINESS_UNIT_NAME} (ID: {RETELL_BUSINESS_UNIT_ID})")
    print("*" * 70 + "\n")

    return {
        "method": "real_fallback",
        "campaign_id": RETELL_CAMPAIGN_ID,
        "campaign_name": RETELL_CAMPAIGN_NAME,
        "business_unit_id": RETELL_BUSINESS_UNIT_ID,
        "business_unit_name": RETELL_BUSINESS_UNIT_NAME
    }


def get_job_types():
    """
    Fetch all active job types from ServiceTitan.
    """
    print("[ServiceTitan] Fetching job types...")

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/job-types"
    params = {"pageSize": 100, "active": "true"}

    print(f"  GET {url}")
    print(f"  Params: {params}")

    try:
        resp = requests.get(url, headers=headers, params=params)
        print(f"  Status: {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            job_types = data.get("data", [])
            print(f"  Found {len(job_types)} active job types")
            return {"success": True, "job_types": data}
        else:
            print(f"  Error: {resp.text}")
            return {"success": False, "error": resp.text, "status_code": resp.status_code}
    except Exception as e:
        print(f"  Exception: {e}")
        return {"success": False, "error": str(e)}


def get_zones():
    """
    Fetch zones from ServiceTitan - tries multiple API endpoints.
    """
    print("[ServiceTitan] Fetching zones from multiple endpoints...")

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    urls = [
        f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/zones",
        f"https://api.servicetitan.io/dispatch/v2/tenant/{TENANT_ID}/zones",
        f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/zones"
    ]

    results = {}

    for i, url in enumerate(urls, 1):
        print(f"\n[{i}] GET {url}")
        try:
            resp = requests.get(url, headers=headers)
            print(f"    Status: {resp.status_code}")
            print(f"    Response: {resp.text[:500]}..." if len(resp.text) > 500 else f"    Response: {resp.text}")

            results[f"endpoint_{i}"] = {
                "url": url,
                "status_code": resp.status_code,
                "response": resp.json() if resp.status_code == 200 else resp.text
            }
        except Exception as e:
            print(f"    Error: {e}")
            results[f"endpoint_{i}"] = {
                "url": url,
                "error": str(e)
            }

    return results


def get_access_token():
    """
    Get access token from ServiceTitan OAuth endpoint.
    Caches token and reuses it for 900 seconds.
    """
    current_time = time.time()

    # Return cached token if still valid
    if _token_cache["access_token"] and current_time < _token_cache["expires_at"]:
        print("[ServiceTitan] Using cached access token")
        return _token_cache["access_token"]

    # Request new token
    print("[ServiceTitan] Requesting new access token...")

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

    # Cache token for 900 seconds
    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = current_time + 900

    print("[ServiceTitan] Access token obtained successfully")
    return access_token


def test_telecom():
    """
    Test telecom API endpoints to find call data with tracking numbers.
    """
    print(f"\n[ServiceTitan] Testing telecom API endpoints...")

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    url = f"https://api.servicetitan.io/telecom/v2/tenant/{TENANT_ID}/calls"
    params = {"pageSize": 5, "orderBy": "Id", "orderByDirection": "desc"}
    print(f"\n[ServiceTitan] GET {url}")
    print(f"[ServiceTitan] Params: {params}")

    resp = requests.get(url, headers=headers, params=params)
    print(f"[ServiceTitan] Response status: {resp.status_code}")
    print(f"[ServiceTitan] Response body: {resp.text}")

    if resp.status_code == 200:
        return {"success": True, "calls": resp.json()}
    else:
        return {"success": False, "error": resp.text, "status_code": resp.status_code}


def get_call_details(call_id):
    """
    Fetch call details by ID from ServiceTitan.
    """
    print(f"\n[ServiceTitan] Fetching call details for ID: {call_id}")

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/calls/{call_id}"
    print(f"[ServiceTitan] GET {url}")

    resp = requests.get(url, headers=headers)
    print(f"[ServiceTitan] Response status: {resp.status_code}")
    print(f"[ServiceTitan] Response body: {resp.text}")

    if resp.status_code == 200:
        return {"success": True, "call": resp.json()}
    else:
        return {"success": False, "status_code": resp.status_code, "error": resp.text}


def get_latest_call():
    """
    Fetch the latest lead and latest call from ServiceTitan.
    """
    print(f"\n[ServiceTitan] Fetching latest lead and call...")

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    results = {}

    # Get latest lead
    leads_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/leads"
    params = {"pageSize": 1, "orderBy": "createdOn", "orderByDirection": "desc"}
    print(f"\n[ServiceTitan] GET {leads_url}")
    print(f"[ServiceTitan] Params: {params}")

    leads_resp = requests.get(leads_url, headers=headers, params=params)
    print(f"[ServiceTitan] Leads response status: {leads_resp.status_code}")
    print(f"[ServiceTitan] Leads response body: {json.dumps(leads_resp.json() if leads_resp.status_code == 200 else leads_resp.text, indent=2)}")

    if leads_resp.status_code == 200:
        results["latest_lead"] = leads_resp.json()
    else:
        results["latest_lead"] = {"error": leads_resp.text, "status_code": leads_resp.status_code}

    # Get latest call
    calls_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/calls"
    print(f"\n[ServiceTitan] GET {calls_url}")
    print(f"[ServiceTitan] Params: {params}")

    calls_resp = requests.get(calls_url, headers=headers, params=params)
    print(f"[ServiceTitan] Calls response status: {calls_resp.status_code}")
    print(f"[ServiceTitan] Calls response body: {json.dumps(calls_resp.json() if calls_resp.status_code == 200 else calls_resp.text, indent=2)}")

    if calls_resp.status_code == 200:
        results["latest_call"] = calls_resp.json()
    else:
        results["latest_call"] = {"error": calls_resp.text, "status_code": calls_resp.status_code}

    return results


def get_campaign_details(campaign_id):
    """
    Fetch campaign details by ID from ServiceTitan.
    """
    print(f"\n[ServiceTitan] Fetching campaign details for ID: {campaign_id}")

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    url = f"https://api.servicetitan.io/marketing/v2/tenant/{TENANT_ID}/campaigns/{campaign_id}"
    print(f"[ServiceTitan] GET {url}")

    resp = requests.get(url, headers=headers)
    print(f"[ServiceTitan] Response status: {resp.status_code}")
    print(f"[ServiceTitan] Response body: {resp.text}")

    if resp.status_code == 200:
        return {"success": True, "campaign": resp.json()}
    else:
        return {"success": False, "status_code": resp.status_code, "error": resp.text}


def get_campaign_from_call(phone):
    """
    Look up the campaign ID associated with a phone number.
    Tries leads endpoint first, then jobs endpoint as fallback.
    Returns campaign ID if found, otherwise 477 as default.
    """
    # Clean phone number
    clean = phone.replace("+1", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
    clean = ''.join(filter(str.isdigit, clean))
    if len(clean) == 11 and clean.startswith('1'):
        clean = clean[1:]

    print(f"\n[ServiceTitan] Looking up campaign for phone: {phone} -> cleaned: {clean}")

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    # Try leads endpoint first
    print(f"[ServiceTitan] Trying leads endpoint...")
    leads_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/leads"
    leads_resp = requests.get(leads_url, headers=headers, params={"phone": clean, "pageSize": 1})
    print(f"[ServiceTitan] Leads response status: {leads_resp.status_code}")
    print(f"[ServiceTitan] Leads response body: {leads_resp.text}")

    if leads_resp.status_code == 200:
        leads_data = leads_resp.json()
        leads = leads_data.get("data", [])
        if leads and leads[0].get("campaignId"):
            campaign_id = leads[0]["campaignId"]
            print(f"[ServiceTitan] Found campaign from leads: {campaign_id}")
            return campaign_id

    # Try jobs endpoint as fallback
    print(f"[ServiceTitan] Trying jobs endpoint as fallback...")
    jobs_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs"
    jobs_resp = requests.get(jobs_url, headers=headers, params={"phone": clean, "pageSize": 1})
    print(f"[ServiceTitan] Jobs response status: {jobs_resp.status_code}")
    print(f"[ServiceTitan] Jobs response body: {jobs_resp.text}")

    if jobs_resp.status_code == 200:
        jobs_data = jobs_resp.json()
        jobs = jobs_data.get("data", [])
        if jobs and jobs[0].get("campaignId"):
            campaign_id = jobs[0]["campaignId"]
            print(f"[ServiceTitan] Found campaign from jobs: {campaign_id}")
            return campaign_id

    print(f"[ServiceTitan] No campaign found, using default: {CAMPAIGN_ID}")
    return CAMPAIGN_ID


def parse_appointment_time(appointment_time: str) -> dict:
    """
    Parse appointment time string from Sarah into ST-compatible start/end datetimes.
    Returns dict with 'start' and 'end' as ISO 8601 strings in UTC.

    All times are interpreted as Eastern Time and converted to UTC for ServiceTitan.

    Handles:
    - "morning window 8-12" -> 08:00 to 12:00 ET
    - "afternoon window 12-4" -> 12:00 to 16:00 ET
    - "evening window 4-8" -> 16:00 to 20:00 ET
    - "emergency" / "asap" / "immediately" / "as soon as possible" -> now + 2 hours
    - "tomorrow morning window 8-12" -> tomorrow 08:00 to 12:00 ET
    - "friday afternoon window 12-4" -> next friday 12:00 to 16:00 ET
    """
    from zoneinfo import ZoneInfo

    # Eastern timezone (handles EDT/EST automatically)
    eastern = ZoneInfo("America/New_York")
    utc = ZoneInfo("UTC")

    now_utc = datetime.now(utc)
    now_eastern = now_utc.astimezone(eastern)
    text = (appointment_time or "").lower().strip()

    print(f"[Scheduling] Parsing: '{appointment_time}'")
    print(f"[Scheduling] Current time: {now_eastern.strftime('%Y-%m-%d %H:%M %Z')}")

    # Step 1: Determine the base date (in Eastern time)
    base_date = None

    # Check for specific days
    days = {
        "monday": 0, "tuesday": 1, "wednesday": 2,
        "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6
    }
    for day_name, day_num in days.items():
        if day_name in text:
            days_ahead = (day_num - now_eastern.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # next week if today
            base_date = now_eastern + timedelta(days=days_ahead)
            print(f"[Scheduling] Day detected: {day_name} -> {base_date.date()}")
            break

    # Check for tomorrow
    if base_date is None and "tomorrow" in text:
        base_date = now_eastern + timedelta(days=1)
        print(f"[Scheduling] Tomorrow detected -> {base_date.date()}")

    # Check for today
    if base_date is None and "today" in text:
        base_date = now_eastern
        print(f"[Scheduling] Today detected -> {base_date.date()}")

    # Default to next business day if no date found
    if base_date is None:
        # Skip weekends
        base_date = now_eastern + timedelta(days=1)
        while base_date.weekday() >= 5:  # 5=Saturday, 6=Sunday
            base_date += timedelta(days=1)
        print(f"[Scheduling] No date specified, using next business day -> {base_date.date()}")

    # Step 2: Determine the time window
    base_day = base_date.replace(hour=0, minute=0, second=0, microsecond=0)

    # Emergency / ASAP phrases
    emergency_phrases = [
        "emergency", "asap", "immediately", "right away", "now", "urgent",
        "as soon as possible", "soon", "earliest", "next available"
    ]

    # Check if "today" is specified WITHOUT a specific window (treat as ASAP)
    window_keywords = ["morning", "afternoon", "evening", "8-12", "12-4", "4-8", "8am", "12pm", "4pm"]
    today_without_window = "today" in text and not any(w in text for w in window_keywords)

    if any(phrase in text for phrase in emergency_phrases) or today_without_window:
        start_eastern = now_eastern + timedelta(hours=2)
        end_eastern = start_eastern + timedelta(hours=2)
        # Convert to UTC
        start_utc = start_eastern.astimezone(utc)
        end_utc = end_eastern.astimezone(utc)
        local_time_display = f"{start_eastern.strftime('%a %b %d %I:%M %p')} - {end_eastern.strftime('%I:%M %p')} Eastern (ASAP)"
        print(f"[Scheduling] EMERGENCY detected -> {start_eastern.strftime('%Y-%m-%d %H:%M %Z')} to {end_eastern.strftime('%H:%M %Z')}")
        return {
            "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window": "emergency",
            "local_time": local_time_display
        }

    # Morning window 8-12 ET
    if "morning" in text or "8-12" in text or "8 to 12" in text or "8am" in text:
        start_eastern = base_day.replace(hour=8, minute=0, second=0, tzinfo=eastern)
        end_eastern = base_day.replace(hour=12, minute=0, second=0, tzinfo=eastern)

        # If the window has already passed today, move to next day
        if end_eastern <= now_eastern:
            start_eastern += timedelta(days=1)
            end_eastern += timedelta(days=1)
            print(f"[Scheduling] Morning window passed, moving to next day: {start_eastern.date()}")

        # Convert to UTC
        start_utc = start_eastern.astimezone(utc)
        end_utc = end_eastern.astimezone(utc)
        local_time_display = f"{start_eastern.strftime('%a %b %d')} 8:00 AM - 12:00 PM Eastern"
        print(f"[Scheduling] MORNING window -> {start_eastern.strftime('%Y-%m-%d %H:%M %Z')} to {end_eastern.strftime('%H:%M %Z')}")
        return {
            "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window": "morning 8-12",
            "local_time": local_time_display
        }

    # Afternoon window 12-4 ET
    if "afternoon" in text or "12-4" in text or "12 to 4" in text or "12pm" in text:
        start_eastern = base_day.replace(hour=12, minute=0, second=0, tzinfo=eastern)
        end_eastern = base_day.replace(hour=16, minute=0, second=0, tzinfo=eastern)

        # If the window has already passed today, move to next day
        if end_eastern <= now_eastern:
            start_eastern += timedelta(days=1)
            end_eastern += timedelta(days=1)
            print(f"[Scheduling] Afternoon window passed, moving to next day: {start_eastern.date()}")

        # Convert to UTC
        start_utc = start_eastern.astimezone(utc)
        end_utc = end_eastern.astimezone(utc)
        local_time_display = f"{start_eastern.strftime('%a %b %d')} 12:00 PM - 4:00 PM Eastern"
        print(f"[Scheduling] AFTERNOON window -> {start_eastern.strftime('%Y-%m-%d %H:%M %Z')} to {end_eastern.strftime('%H:%M %Z')}")
        return {
            "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window": "afternoon 12-4",
            "local_time": local_time_display
        }

    # Evening window 4-8 ET
    if "evening" in text or "4-8" in text or "4 to 8" in text or "4pm" in text:
        start_eastern = base_day.replace(hour=16, minute=0, second=0, tzinfo=eastern)
        end_eastern = base_day.replace(hour=20, minute=0, second=0, tzinfo=eastern)

        # If the window has already passed today, move to next day
        if end_eastern <= now_eastern:
            start_eastern += timedelta(days=1)
            end_eastern += timedelta(days=1)
            print(f"[Scheduling] Evening window passed, moving to next day: {start_eastern.date()}")

        # Convert to UTC
        start_utc = start_eastern.astimezone(utc)
        end_utc = end_eastern.astimezone(utc)
        local_time_display = f"{start_eastern.strftime('%a %b %d')} 4:00 PM - 8:00 PM Eastern"

        print(f"[Scheduling] EVENING window -> {start_eastern.strftime('%Y-%m-%d %H:%M %Z')} to {end_eastern.strftime('%H:%M %Z')}")
        return {
            "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window": "evening 4-8",
            "local_time": local_time_display
        }

    # Fallback: next business day morning
    print(f"[Scheduling] No window detected, defaulting to morning window")
    start_eastern = base_day.replace(hour=8, minute=0, second=0, tzinfo=eastern)
    end_eastern = base_day.replace(hour=12, minute=0, second=0, tzinfo=eastern)
    # Convert to UTC
    start_utc = start_eastern.astimezone(utc)
    end_utc = end_eastern.astimezone(utc)
    local_time_display = f"{start_eastern.strftime('%a %b %d')} 8:00 AM - 12:00 PM Eastern"
    return {
        "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": "morning 8-12 (default)",
        "local_time": local_time_display
    }


def create_booking(customer_name, address, phone, email, issue_description,
                   appointment_time, appointment_start, appointment_end,
                   customer_type, is_homeowner=None, promotional_emails=None,
                   contact_preference=None, alternate_phone=None,
                   campaign_id=None, business_unit_id=None, is_emergency=None):

    token = get_access_token()

    # Normalize customer_type to "Residential" or "Commercial"
    if customer_type and customer_type.lower() == "commercial":
        customer_type = "Commercial"
    else:
        customer_type = "Residential"

    print(f"[ST] Customer Type: {customer_type}")

    # Parse address with Google geocoding to get lat/lng for dispatch
    from app.services.service_area import parse_address_google
    import os
    google_api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    geocoded = parse_address_google(address, google_api_key)

    # Extract lat/lng for dispatch (may be None if geocoding failed)
    customer_lat = geocoded.get("lat")
    customer_lng = geocoded.get("lng")

    # Use geocoded address or fall back to basic parsing
    if geocoded.get("method") == "google" and geocoded.get("zip"):
        parsed_addr = geocoded
        print(f"[ST] Geocoded address: {geocoded.get('formatted_address')} (lat={customer_lat}, lng={customer_lng})")
    else:
        parsed_addr = parse_address(address)
        print(f"[ST] Using fallback address parsing (no geocode)")

    # Track business unit source for logging
    bu_source = "provided" if business_unit_id else None

    # Check service area cache for zone-based business unit (highest priority)
    # Try phone-based cache first
    if not business_unit_id:
        cached_bu = get_service_area_business_unit(phone)
        if cached_bu:
            business_unit_id = cached_bu["business_unit_id"]
            bu_source = f"zone ({cached_bu.get('zone_name', 'cached')})"
            print(f"[ST] Using cached zone-based BU (by phone): {cached_bu['business_unit_name']} (ID: {business_unit_id})")

    # Try address-based cache if phone-based didn't work
    if not business_unit_id:
        cached_bu = get_service_area_bu_by_address(parsed_addr.get("street", ""), parsed_addr.get("zip", ""))
        if cached_bu:
            business_unit_id = cached_bu["business_unit_id"]
            bu_source = f"zone-address ({cached_bu.get('zone_name', 'cached')})"
            print(f"[ST] Using cached zone-based BU (by address): {cached_bu['business_unit_name']} (ID: {business_unit_id})")

    # Look up campaign using cached to_number from inbound webhook
    if not campaign_id or not business_unit_id:
        to_number = get_live_call_to_number(phone)
        if to_number:
            print(f"[ST] Looking up campaign using to_number: {to_number}")
            campaign_info = get_live_call_campaign(phone, to_number)
            if not campaign_id:
                campaign_id = campaign_info.get("campaign_id")
            if not business_unit_id:
                business_unit_id = campaign_info.get("business_unit_id")
                bu_source = "campaign"
            print(f"[ST] Found campaign: {campaign_info.get('campaign_name')} (ID: {campaign_id})")
        else:
            print(f"[ST] No cached to_number found for {phone}, using fallback lookup")

    # Fall back to historical lookup if still missing
    if not campaign_id:
        campaign_id = get_campaign_from_call(phone)
    if not business_unit_id:
        business_unit_id = BUSINESS_UNIT_ID
        bu_source = "default"

    print(f"[ST] Final campaign_id: {campaign_id}")
    print(f"[ST] Final business_unit_id: {business_unit_id} (source: {bu_source})")

    # Detect job type from issue description using AI (pass customer_type for commercial preference)
    job_type_result = detect_job_type(issue_description, customer_type)
    detected_job_type_id = job_type_result["job_type_id"]
    detected_priority = job_type_result["priority"]
    print(f"[ST] Using job_type_id: {detected_job_type_id}, priority: {detected_priority}")

    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }

    address_obj = {
        "street": parsed_addr["street"],
        "city": parsed_addr["city"],
        "state": parsed_addr["state"],
        "zip": parsed_addr["zip"],
        "country": DEFAULT_COUNTRY
    }

    # Build contacts - only add valid phone/email
    contacts = []
    cleaned_phone = clean_phone(phone)
    if cleaned_phone:
        contacts.append({"type": "Phone", "value": cleaned_phone, "memo": customer_name})
    if email and "@" in email:
        contacts.append({"type": "Email", "value": email, "memo": customer_name})

    # Step 1a - Create Customer
    print("[ST] Step 1a - Creating customer...")
    customer_payload = {
        "name": customer_name,
        "type": customer_type,
        "address": address_obj,
        "contacts": contacts,
        "locations": [
            {
                "name": customer_name,
                "address": address_obj,
                "contacts": contacts
            }
        ]
    }
    print(f"[ST] Customer payload: {json.dumps(customer_payload)}")
    
    r = requests.post(
        f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers",
        headers=headers,
        json=customer_payload
    )
    print(f"[ST] Customer response {r.status_code}: {r.text}")
    
    if r.status_code not in (200, 201):
        return {"status": "error", "failed_step": "1a", "step_name": "Create Customer", 
                "error": r.text, "status_code": r.status_code}
    
    customer_data = r.json()
    customer_id = customer_data["id"]
    location_id = customer_data["locations"][0]["id"]
    print(f"[ST] Customer ID: {customer_id}, Location ID: {location_id}")

    # Step 2 - Create Job
    print("[ST] Step 2 - Creating job...")

    # Parse appointment time - use provided start/end or parse from natural language
    if appointment_start and appointment_end:
        parsed_start = appointment_start
        parsed_end = appointment_end
        scheduling_window = "provided"
        local_time_display = None
        print(f"[ST] Using provided appointment times: {parsed_start} to {parsed_end}")
    else:
        time_result = parse_appointment_time(appointment_time)
        parsed_start = time_result["start"]
        parsed_end = time_result["end"]
        scheduling_window = time_result["window"]
        local_time_display = time_result.get("local_time")
        print(f"[ST] Scheduling window: {scheduling_window}")
        print(f"[ST] Local time: {local_time_display}")
        print(f"[ST] Parsed appointment times: {parsed_start} to {parsed_end}")

    # Build special instructions with local Eastern time for dispatcher clarity
    if local_time_display:
        special_instructions = f"ARRIVAL WINDOW: {local_time_display}"
    elif appointment_time:
        special_instructions = f"{appointment_time} [{scheduling_window}]"
    else:
        special_instructions = scheduling_window

    # Build custom fields for booking questions
    custom_fields = []

    # 1 - "Do you own the home?" - Valid options from ServiceTitan
    if is_homeowner is not None:
        homeowner_str = str(is_homeowner).lower().strip()
        if homeowner_str in ("yes", "true", "1", "y"):
            homeowner_value = "YES: \u201CGreat that is all the information I will need to create your profile. I do have a few additional questions to ask though regarding the issues you are having and for scheduling.\u201D"
        elif customer_type == "Commercial":
            homeowner_value = "NO, it\u2019s a Commercial Property: \u201CGreat, we can definitely help. I do have a few additional questions to ask though regarding the issues you are having and for scheduling.\u201D Make sure that you classify this Profile as Commercial in the profile."
        else:
            homeowner_value = "NO, It\u2019s a Rental: \u201CNo problem we service Rentals as well. I do have a few questions I will need to ask.\u201D Complete LL/Tenant Questions"
        custom_fields.append({
            "typeId": CUSTOM_FIELD_HOMEOWNER,
            "value": homeowner_value
        })
        print(f"[ST] Custom field - Homeowner: {homeowner_value[:50]}...")

    # 2 - "Do we have your permission to send promotional emails?"
    if promotional_emails is not None:
        promo_str = str(promotional_emails).lower().strip()
        if promo_str in ("yes", "true", "1", "y"):
            email_promo_value = "Yes, You can send promotional emails."
        else:
            email_promo_value = "No, Please do not send emails that are not related to this appointment."
        custom_fields.append({
            "typeId": CUSTOM_FIELD_EMAIL_PROMO,
            "value": email_promo_value
        })
        print(f"[ST] Custom field - Email promo: {email_promo_value[:50]}...")

    job_payload = {
        "customerId": customer_id,
        "locationId": location_id,
        "summary": issue_description,
        "jobTypeId": detected_job_type_id,
        "businessUnitId": business_unit_id,
        "campaignId": campaign_id,
        "priority": detected_priority,
        "customFields": custom_fields,
        "appointments": [
            {
                "start": parsed_start,
                "end": parsed_end,
                "specialInstructions": special_instructions
            }
        ]
    }
    print(f"[ST] Job payload: {json.dumps(job_payload)}")
    
    r = requests.post(
        f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs",
        headers=headers,
        json=job_payload
    )
    print(f"[ST] Job response {r.status_code}: {r.text}")
    
    if r.status_code not in (200, 201):
        return {"status": "error", "failed_step": 2, "step_name": "Create Job",
                "error": r.text, "status_code": r.status_code, "customer_id": customer_id}
    
    job_data = r.json()
    job_id = job_data["id"]
    first_appointment_id = job_data.get("firstAppointmentId")
    print(f"[ST] Job ID: {job_id}")
    print(f"[ST] First Appointment ID: {first_appointment_id}")

    # Step 3 - Dispatch Technician
    dispatch_result = None
    try:
        from app.services.dispatch import dispatch_technician

        job_category = job_type_result.get("job_category", "Misc Plumbing")

        # Use provided is_emergency, default to False if not specified
        dispatch_emergency = is_emergency if is_emergency is not None else False

        print(f"[ST] Dispatching technician for category: {job_category}, emergency: {dispatch_emergency}")

        dispatch_result = dispatch_technician(
            job_category=job_category,
            customer_address={
                "lat": customer_lat,
                "lng": customer_lng,
                "zip": parsed_addr.get("zip")
            },
            is_emergency=dispatch_emergency
        )

        if dispatch_result.get("error"):
            print(f"[ST] Dispatch warning: {dispatch_result}")
        else:
            print(f"[ST] Dispatch success: {dispatch_result['tech_name']} "
                  f"(skill={dispatch_result['skill_rating']}, "
                  f"distance={dispatch_result.get('distance_miles')} mi, "
                  f"auto_dispatch={dispatch_result['auto_dispatch']})")

            # Post note with technician assignment for dispatcher
            # (API auto-assign not available without SmartDispatch feature)
            if first_appointment_id:
                try:
                    from app.services.servicetitan_notes import post_job_note

                    tech_name = dispatch_result["tech_name"]
                    skill = dispatch_result["skill_rating"]
                    distance = dispatch_result.get("distance_miles", "N/A")
                    job_category = job_type_result.get("job_category", "Unknown")

                    if dispatch_result.get("auto_dispatch"):
                        # Auto-dispatch: skill 1-3, ready to assign
                        note_text = (
                            f"🚀 ASSIGN TECHNICIAN\n\n"
                            f"Technician: {tech_name}\n"
                            f"Skill Rating: {skill}/5 for {job_category}\n"
                            f"Distance: {distance} miles\n\n"
                            f"This technician is recommended for immediate assignment."
                        )
                        print(f"[ST] Posting auto-dispatch note for tech {tech_name}")
                    else:
                        # Requires approval: skill 4-5
                        note_text = (
                            f"⚠️ DISPATCH PENDING APPROVAL\n\n"
                            f"Recommended Technician: {tech_name}\n"
                            f"Skill Rating: {skill}/5 for {job_category}\n"
                            f"Distance: {distance} miles\n\n"
                            f"This technician has a skill rating of {skill} (4-5 requires manager approval).\n"
                            f"Please review and confirm dispatch or reassign to a more experienced technician."
                        )
                        print(f"[ST] Posting approval-required note for tech {tech_name}")

                    if post_job_note(str(job_id), note_text, pin=True):
                        print(f"[ST] Posted dispatch note to job {job_id}")
                        dispatch_result["note_posted"] = True
                    else:
                        print(f"[ST] Failed to post dispatch note to job {job_id}")
                        dispatch_result["note_posted"] = False

                except Exception as note_err:
                    print(f"[ST] Error posting dispatch note: {note_err}")
                    dispatch_result["note_posted"] = False

    except Exception as e:
        print(f"[ST] Dispatch failed (non-blocking): {e}")
        dispatch_result = {"error": "dispatch_exception", "message": str(e)}

    # # Step 4 - Create Appointment (commented out)
    # print("[ST] Step 3 - Creating appointment...")
    # appointment_payload = {
    #     "jobId": job_id,
    #     "start": "2026-04-15T10:00:00Z",
    #     "end": "2026-04-15T11:00:00Z",
    #     "arrivalWindowStart": "2026-04-15T10:00:00Z",
    #     "arrivalWindowEnd": "2026-04-15T11:00:00Z",
    #     "specialInstructions": appointment_time
    # }
    # print(f"[ST] Appointment payload: {json.dumps(appointment_payload)}")
    
    # r = requests.post(
    #     f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/appointments",
    #     headers=headers,
    #     json=appointment_payload
    # )
    # print(f"[ST] Appointment response {r.status_code}: {r.text}")
    
    if r.status_code not in (200, 201):
        return {"status": "error", "failed_step": 3, "step_name": "Create Appointment",
                "error": r.text, "status_code": r.status_code, "job_id": job_id}

    result = {
        "status": "success",
        "message": f"Your appointment has been booked successfully. Your job number is {job_id}. We will reach out before arrival. Thank you for calling Mr. Rooter.",
        "job_id": job_id,
        "customer_id": customer_id,
        "job_category": job_type_result.get("job_category", "Misc Plumbing")
    }

    # Add dispatch info if available
    if dispatch_result and not dispatch_result.get("error"):
        result["dispatch"] = {
            "tech_id": dispatch_result["tech_id"],
            "tech_name": dispatch_result["tech_name"],
            "skill_rating": dispatch_result["skill_rating"],
            "distance_miles": dispatch_result.get("distance_miles"),
            "auto_dispatch": dispatch_result["auto_dispatch"],
            "requires_approval": dispatch_result["requires_approval"],
            "note_posted": dispatch_result.get("note_posted", False)
        }

    elif dispatch_result:
        result["dispatch"] = dispatch_result  # Contains error info

        # Post note for no_match or dispatch error - needs manual dispatch
        if dispatch_result.get("error"):
            try:
                from app.services.servicetitan_notes import post_job_note

                error_reason = dispatch_result.get("reason", dispatch_result.get("message", "unknown"))
                note_text = (
                    f"⚠️ MANUAL DISPATCH REQUIRED\n\n"
                    f"Auto-dispatch could not find an available technician.\n"
                    f"Reason: {error_reason}\n\n"
                    f"Job Category: {job_type_result.get('job_category', 'Unknown')}\n"
                    f"Customer Zip: {parsed_addr.get('zip', 'Unknown')}\n\n"
                    f"Please manually assign a technician to this job."
                )

                if post_job_note(str(job_id), note_text, pin=True):
                    print(f"[ST] Posted manual dispatch note to job {job_id}")
                    result["dispatch"]["manual_dispatch_note_posted"] = True
                else:
                    print(f"[ST] Failed to post manual dispatch note to job {job_id}")
                    result["dispatch"]["manual_dispatch_note_posted"] = False

            except Exception as e:
                print(f"[ST] Error posting manual dispatch note: {e}")

    return result


def create_lead(call_type: str, summary: str, from_number: str, campaign_id: int, business_unit_id: int):
    """
    Create a lead in ServiceTitan CRM for non-booking calls.
    Used for inquiries, callbacks, and other call outcomes that don't result in a booking.

    Args:
        call_type: Type of call (e.g., "INQUIRY", "CALLBACK", "INFO")
        summary: Description of the call/inquiry
        from_number: Caller's phone number
        campaign_id: Campaign ID to associate with the lead
        business_unit_id: Business unit ID for routing

    Returns:
        lead_id on success, None on failure
    """
    print("\n" + "=" * 70)
    print("                    CREATE LEAD")
    print("=" * 70)
    print(f"  Call Type: {call_type}")
    print(f"  Summary: {summary}")
    print(f"  From: {from_number}")
    print(f"  Campaign ID: {campaign_id}")
    print(f"  Business Unit ID: {business_unit_id}")
    print("=" * 70)

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }

    # Step 1: Clean phone number and look up customer
    cleaned_phone = clean_phone(from_number)
    customer_id = None
    location_id = None
    customer_name = "Unknown Caller"

    if cleaned_phone:
        print(f"\n[Lead] Looking up customer by phone: {cleaned_phone}")
        search_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers"
        search_resp = requests.get(search_url, headers=headers, params={"phone": cleaned_phone})

        if search_resp.status_code == 200:
            customers = search_resp.json().get("data", [])
            if customers:
                customer = customers[0]
                customer_id = customer.get("id")
                customer_name = customer.get("name", "Unknown Caller")
                print(f"[Lead] Found customer: {customer_name} (ID: {customer_id})")

                # Fetch locations for this customer via separate API call
                print(f"[Lead] Fetching locations for customer {customer_id}...")
                locations_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/locations"
                locations_resp = requests.get(locations_url, headers=headers, params={"customerId": customer_id, "pageSize": 1})

                if locations_resp.status_code == 200:
                    locations = locations_resp.json().get("data", [])
                    if locations:
                        location_id = locations[0].get("id")
                        print(f"[Lead] Found location: {location_id}")
                    else:
                        print("[Lead] Customer has no locations")
                else:
                    print(f"[Lead] Locations lookup failed: {locations_resp.status_code}")
            else:
                print("[Lead] No existing customer found, creating new customer...")

                # Create new customer with "Unknown Caller" name
                customer_payload = {
                    "name": "Unknown Caller",
                    "type": "Residential",
                    "contacts": [
                        {"type": "Phone", "value": cleaned_phone, "memo": "Unknown Caller"}
                    ],
                    "locations": [
                        {
                            "name": "Unknown Caller",
                            "contacts": [
                                {"type": "Phone", "value": cleaned_phone, "memo": "Unknown Caller"}
                            ]
                        }
                    ]
                }

                print(f"[Lead] Creating customer: {json.dumps(customer_payload)}")
                create_cust_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers"
                create_cust_resp = requests.post(create_cust_url, headers=headers, json=customer_payload)

                if create_cust_resp.status_code in (200, 201):
                    cust_data = create_cust_resp.json()
                    customer_id = cust_data.get("id")
                    customer_name = cust_data.get("name", "Unknown Caller")
                    # Get location from the created customer
                    created_locations = cust_data.get("locations", [])
                    if created_locations:
                        location_id = created_locations[0].get("id")
                    print(f"[Lead] Created new customer: {customer_name} (ID: {customer_id})")
                else:
                    print(f"[Lead] Failed to create customer: {create_cust_resp.status_code} - {create_cust_resp.text}")
        else:
            print(f"[Lead] Customer lookup failed: {search_resp.status_code}")

    print(f"[Lead] Customer ID: {customer_id}")
    print(f"[Lead] Location ID: {location_id}")

    # Step 2: Use OpenAI to determine priority and follow-up timing
    from datetime import timezone
    priority = "Low"  # ServiceTitan only accepts "High" or "Low"
    follow_up_days = 1
    follow_up_date = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT09:00:00Z")

    try:
        print(f"\n[Lead] Analyzing priority and follow-up with AI...")
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=50,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": """You are a plumbing business operations expert. Given a call type and summary, determine the priority and follow-up timing. Respond with ONLY this exact format, nothing else:
PRIORITY:High
FOLLOWUP_DAYS:1

Priority options: High or Low (use High for urgent/emergency, Low for everything else)
FOLLOWUP_DAYS options: 0 (today), 1 (tomorrow), 2, 3, 5, 7"""
                },
                {
                    "role": "user",
                    "content": f"Call type: {call_type}\nCall summary: {summary}\n\nWhat priority and follow-up days?"
                }
            ]
        )

        ai_response = response.choices[0].message.content.strip()
        print(f"[Lead] AI response: {ai_response}")

        # Parse the response
        for line in ai_response.split("\n"):
            line = line.strip()
            if line.startswith("PRIORITY:"):
                parsed_priority = line.replace("PRIORITY:", "").strip()
                # ServiceTitan only accepts "High" or "Low" - map Medium to Low
                if parsed_priority == "High":
                    priority = "High"
                else:
                    priority = "Low"
            elif line.startswith("FOLLOWUP_DAYS:"):
                try:
                    follow_up_days = int(line.replace("FOLLOWUP_DAYS:", "").strip())
                except ValueError:
                    pass

        # Calculate follow_up_date based on follow_up_days
        follow_up_date = (datetime.now(timezone.utc) + timedelta(days=follow_up_days)).strftime("%Y-%m-%dT09:00:00Z")

        print(f"[Lead] AI Priority: {priority}")
        print(f"[Lead] AI Follow-up: {follow_up_days} days from now ({follow_up_date})")

    except Exception as e:
        print(f"[Lead] AI analysis failed: {e}, using defaults")
        print(f"[Lead] AI Priority: {priority} (default)")
        print(f"[Lead] AI Follow-up: {follow_up_days} days from now ({follow_up_date}) (default)")

    # Step 3: Create lead payload
    lead_payload = {
        "customerId": customer_id,
        "locationId": location_id,
        "summary": f"[{call_type.upper()}] {summary}",
        "callReasonId": 92029507,
        "campaignId": campaign_id,
        "businessUnitId": business_unit_id,
        "status": "Open",
        "priority": priority,
        "followUpDate": follow_up_date
    }

    print(f"\n[Lead] Creating lead...")
    print(f"[Lead] Payload: {json.dumps(lead_payload, indent=2)}")

    # Step 4: POST to leads endpoint
    leads_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/leads"
    resp = requests.post(leads_url, headers=headers, json=lead_payload)

    print(f"[Lead] Response status: {resp.status_code}")
    print(f"[Lead] Response: {resp.text}")

    # If lead creation failed due to missing location, try creating a location first
    if resp.status_code not in (200, 201) and customer_id and not location_id:
        print("[Lead] Lead creation failed due to missing location, searching for address...")

        customer_address = None

        # 1. First try to get cached address from service area check (most recent/accurate)
        cached_address = get_service_area_address(cleaned_phone)
        if cached_address and cached_address.get("street"):
            customer_address = {
                "street": cached_address.get("street", ""),
                "city": cached_address.get("city", ""),
                "state": cached_address.get("state", DEFAULT_STATE),
                "zip": cached_address.get("zip", ""),
                "country": cached_address.get("country", DEFAULT_COUNTRY)
            }
            print(f"[Lead] Using cached address from service area check: {customer_address['street']}, {customer_address['city']}")

        # 2. If no cached address, try to get from customer profile
        if not customer_address:
            try:
                cust_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers/{customer_id}"
                cust_resp = requests.get(cust_url, headers=headers)
                if cust_resp.status_code == 200:
                    cust_data = cust_resp.json()
                    profile_address = cust_data.get("address")
                    if profile_address and profile_address.get("street"):
                        customer_address = profile_address
                        print(f"[Lead] Using customer profile address: {customer_address.get('street', '')}, {customer_address.get('city', '')}")
            except Exception as e:
                print(f"[Lead] Could not fetch customer address: {e}")

        # 3. If still no address, skip lead creation
        if not customer_address or not customer_address.get("street"):
            print(f"[Lead] SKIPPED: No address available for location creation")
            print(f"[Lead] Manual follow-up needed for: {customer_name} ({cleaned_phone})")
            print(f"[Lead] Call summary: {summary[:100]}...")
            return None

        location_payload = {
            "customerId": customer_id,
            "name": customer_name,
            "address": customer_address,
            "contacts": [
                {"type": "Phone", "value": cleaned_phone, "memo": customer_name}
            ] if cleaned_phone else []
        }

        print(f"[Lead] Creating location with address...")
        print(f"[Lead] Location payload: {json.dumps(location_payload)}")
        create_loc_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/locations"
        create_loc_resp = requests.post(create_loc_url, headers=headers, json=location_payload)

        if create_loc_resp.status_code in (200, 201):
            loc_data = create_loc_resp.json()
            location_id = loc_data.get("id")
            print(f"[Lead] Created location: {location_id}")
            print(f"[Lead] Location ID: {location_id}")

            # Retry lead creation with the new location
            lead_payload["locationId"] = location_id
            print(f"[Lead] Retrying lead creation with location...")
            resp = requests.post(leads_url, headers=headers, json=lead_payload)
            print(f"[Lead] Response status: {resp.status_code}")
            print(f"[Lead] Response: {resp.text}")
        else:
            print(f"[Lead] Failed to create location: {create_loc_resp.status_code} - {create_loc_resp.text}")

    if resp.status_code in (200, 201):
        lead_data = resp.json()
        lead_id = lead_data.get("id")

        print("\n" + "*" * 70)
        print(f"[Lead] Created: {call_type} - {summary}")
        print(f"[Lead] ID: {lead_id}")
        print(f"[Lead] Customer: {customer_name}")
        print("*" * 70 + "\n")

        return lead_id
    else:
        print(f"\n[Lead] FAILED to create lead: {resp.text}")
        return None


def lookup_customer_by_phone(phone: str):
    """
    Look up a customer in ServiceTitan CRM by phone number.
    Returns customer info, contacts, and recent job history.
    If multiple customers match, returns the one with most recent job activity.
    """
    print(f"[ServiceTitan] Looking up customer by phone: {phone}")

    # Clean phone number (remove non-digits)
    clean_phone = ''.join(filter(str.isdigit, phone))

    access_token = get_access_token()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "ST-App-Key": APP_KEY
    }

    # Search customers by phone
    url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers"
    response = requests.get(url, headers=headers, params={"phone": clean_phone})

    if response.status_code != 200:
        print(f"[ServiceTitan] Customer lookup failed: {response.status_code}")
        return {"found": False, "error": response.text}

    customers = response.json().get("data", [])

    if not customers:
        print(f"[ServiceTitan] No customer found for phone: {phone}")
        return {"found": False, "message": "No customer found with this phone number"}

    print(f"[ServiceTitan] Found {len(customers)} customer(s) matching phone: {phone}")

    # If multiple customers, find the one with most recent job activity
    if len(customers) > 1:
        customer = None
        most_recent_job_date = None
        jobs_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs"

        for cust in customers:
            cust_id = cust["id"]
            # Get most recent job for this customer
            jobs_resp = requests.get(jobs_url, headers=headers, params={
                "customerId": cust_id,
                "pageSize": 1,
                "orderBy": "createdOn",
                "orderByDirection": "desc"
            })

            if jobs_resp.status_code == 200:
                jobs = jobs_resp.json().get("data", [])
                if jobs:
                    job_date = jobs[0].get("createdOn", "")
                    print(f"[ServiceTitan] Customer {cust['name']} (ID: {cust_id}) - most recent job: {job_date}")
                    if most_recent_job_date is None or job_date > most_recent_job_date:
                        most_recent_job_date = job_date
                        customer = cust
                else:
                    print(f"[ServiceTitan] Customer {cust['name']} (ID: {cust_id}) - no jobs found")
                    # If no customer selected yet and this one has no jobs, keep as fallback
                    if customer is None:
                        customer = cust

        # Fallback to first customer if none had jobs
        if customer is None:
            customer = customers[0]
            print(f"[ServiceTitan] No jobs found for any customer, using first: {customer['name']}")
        else:
            print(f"[ServiceTitan] Selected customer with most recent activity: {customer['name']}")
    else:
        customer = customers[0]

    customer_id = customer["id"]
    print(f"[ServiceTitan] Using customer: {customer['name']} (ID: {customer_id})")

    # Get customer contacts
    contacts_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers/{customer_id}/contacts"
    contacts_resp = requests.get(contacts_url, headers=headers)
    contacts = contacts_resp.json().get("data", []) if contacts_resp.status_code == 200 else []

    # Get recent jobs
    jobs_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs"
    jobs_resp = requests.get(jobs_url, headers=headers, params={"customerId": customer_id, "pageSize": 5})
    jobs = jobs_resp.json().get("data", []) if jobs_resp.status_code == 200 else []

    # Format job history
    job_history = []
    for job in jobs:
        job_history.append({
            "job_number": job.get("jobNumber"),
            "summary": job.get("summary"),
            "status": job.get("jobStatus"),
            "completed_on": job.get("completedOn")
        })

    result = {
        "found": True,
        "customer": {
            "id": customer_id,
            "name": customer.get("name"),
            "type": customer.get("type"),
            "address": customer.get("address"),
            "balance": customer.get("balance"),
            "do_not_service": customer.get("doNotService")
        },
        "contacts": [
            {"type": c.get("type"), "value": c.get("value"), "memo": c.get("memo")}
            for c in contacts
        ],
        "recent_jobs": job_history
    }

    print(f"[ServiceTitan] Customer lookup complete: {customer['name']}")
    return result


def lookup_by_address(address: str):
    """
    Look up a customer in ServiceTitan CRM by street address.
    Returns customer info, contacts, and recent job history.
    """
    print(f"[ServiceTitan] Looking up customer by address: {address}")

    access_token = get_access_token()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "ST-App-Key": APP_KEY
    }

    # Search customers by street address
    url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers"
    response = requests.get(url, headers=headers, params={"street": address})

    if response.status_code != 200:
        print(f"[ServiceTitan] Address lookup failed: {response.status_code}")
        return {"found": False, "error": response.text}

    customers = response.json().get("data", [])

    if not customers:
        print(f"[ServiceTitan] No customer found for address: {address}")
        return {"found": False, "message": "No customer found with this address"}

    customer = customers[0]
    customer_id = customer["id"]
    print(f"[ServiceTitan] Found customer: {customer['name']} (ID: {customer_id})")

    # Get customer contacts
    contacts_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers/{customer_id}/contacts"
    contacts_resp = requests.get(contacts_url, headers=headers)
    contacts = contacts_resp.json().get("data", []) if contacts_resp.status_code == 200 else []

    # Get recent jobs
    jobs_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs"
    jobs_resp = requests.get(jobs_url, headers=headers, params={"customerId": customer_id, "pageSize": 5})
    jobs = jobs_resp.json().get("data", []) if jobs_resp.status_code == 200 else []

    # Format job history
    job_history = []
    for job in jobs:
        job_history.append({
            "job_number": job.get("jobNumber"),
            "summary": job.get("summary"),
            "status": job.get("jobStatus"),
            "completed_on": job.get("completedOn")
        })

    result = {
        "found": True,
        "customer": {
            "id": customer_id,
            "name": customer.get("name"),
            "type": customer.get("type"),
            "address": customer.get("address"),
            "balance": customer.get("balance"),
            "do_not_service": customer.get("doNotService")
        },
        "contacts": [
            {"type": c.get("type"), "value": c.get("value"), "memo": c.get("memo")}
            for c in contacts
        ],
        "recent_jobs": job_history
    }

    print(f"[ServiceTitan] Address lookup complete: {customer['name']}")
    return result


def test_connection():
    """
    Test ServiceTitan connection by fetching employees.
    """
    print("[ServiceTitan] Testing connection...")

    access_token = get_access_token()

    url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/employees"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "ST-App-Key": APP_KEY
    }

    response = requests.get(url, headers=headers)

    print(f"[ServiceTitan] Test connection status: {response.status_code}")

    return {
        "status_code": response.status_code,
        "success": response.status_code == 200,
        "data": response.json() if response.status_code == 200 else response.text
    }


def explore_account():
    """
    Explore ServiceTitan account structure.
    Fetches job types, customers, and appointments to understand the data model.
    """
    import json

    print("[ServiceTitan] Exploring account structure...")

    access_token = get_access_token()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "ST-App-Key": APP_KEY
    }

    results = {}

    # 1. Get job types
    print("\n" + "=" * 60)
    print("1. JOB TYPES")
    print("=" * 60)
    job_types_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/job-types"
    job_types_resp = requests.get(job_types_url, headers=headers)
    print(f"GET {job_types_url}")
    print(f"Status: {job_types_resp.status_code}")
    if job_types_resp.status_code == 200:
        job_types_data = job_types_resp.json()
        print(json.dumps(job_types_data, indent=2))
        results["job_types"] = job_types_data
    else:
        print(f"Error: {job_types_resp.text}")
        results["job_types"] = {"error": job_types_resp.text}

    # 2. Get customers sample
    print("\n" + "=" * 60)
    print("2. CUSTOMERS (sample)")
    print("=" * 60)
    customers_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers"
    customers_resp = requests.get(customers_url, headers=headers, params={"pageSize": 5})
    print(f"GET {customers_url}?pageSize=5")
    print(f"Status: {customers_resp.status_code}")
    if customers_resp.status_code == 200:
        customers_data = customers_resp.json()
        print(json.dumps(customers_data, indent=2))
        results["customers"] = customers_data
    else:
        print(f"Error: {customers_resp.text}")
        results["customers"] = {"error": customers_resp.text}

    # 3. Get appointments sample
    print("\n" + "=" * 60)
    print("3. APPOINTMENTS (sample)")
    print("=" * 60)
    appointments_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/appointments"
    appointments_resp = requests.get(appointments_url, headers=headers, params={"pageSize": 5})
    print(f"GET {appointments_url}?pageSize=5")
    print(f"Status: {appointments_resp.status_code}")
    if appointments_resp.status_code == 200:
        appointments_data = appointments_resp.json()
        print(json.dumps(appointments_data, indent=2))
        results["appointments"] = appointments_data
    else:
        print(f"Error: {appointments_resp.text}")
        results["appointments"] = {"error": appointments_resp.text}

    print("\n" + "=" * 60)
    print("[ServiceTitan] Exploration complete")

    return results


def fetch_account_config():
    """
    Fetch ServiceTitan account configuration.
    Returns job types, business units, sample customers, and sample jobs.
    """
    import json

    print("[ServiceTitan] Fetching account configuration...")

    access_token = get_access_token()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "ST-App-Key": APP_KEY
    }

    results = {}

    # 1. Get job types
    print("\n" + "=" * 70)
    print("1. JOB TYPES")
    print("=" * 70)
    job_types_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/job-types"
    job_types_resp = requests.get(job_types_url, headers=headers, params={"pageSize": 50})
    print(f"GET {job_types_url}?pageSize=50")
    print(f"Status: {job_types_resp.status_code}")
    if job_types_resp.status_code == 200:
        job_types_data = job_types_resp.json()
        print(json.dumps(job_types_data, indent=2))
        results["job_types"] = job_types_data
    else:
        print(f"Error: {job_types_resp.text}")
        results["job_types"] = {"error": job_types_resp.text}

    # 2. Get business units
    print("\n" + "=" * 70)
    print("2. BUSINESS UNITS")
    print("=" * 70)
    business_units_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/business-units"
    business_units_resp = requests.get(business_units_url, headers=headers, params={"pageSize": 50})
    print(f"GET {business_units_url}?pageSize=50")
    print(f"Status: {business_units_resp.status_code}")
    if business_units_resp.status_code == 200:
        business_units_data = business_units_resp.json()
        print(json.dumps(business_units_data, indent=2))
        results["business_units"] = business_units_data
    else:
        print(f"Error: {business_units_resp.text}")
        results["business_units"] = {"error": business_units_resp.text}

    # 3. Get customers sample
    print("\n" + "=" * 70)
    print("3. CUSTOMERS (sample - 2 records)")
    print("=" * 70)
    customers_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers"
    customers_resp = requests.get(customers_url, headers=headers, params={"pageSize": 2})
    print(f"GET {customers_url}?pageSize=2")
    print(f"Status: {customers_resp.status_code}")
    if customers_resp.status_code == 200:
        customers_data = customers_resp.json()
        print(json.dumps(customers_data, indent=2))
        results["customers_sample"] = customers_data
    else:
        print(f"Error: {customers_resp.text}")
        results["customers_sample"] = {"error": customers_resp.text}

    # 4. Get jobs sample
    print("\n" + "=" * 70)
    print("4. JOBS (sample - 2 records)")
    print("=" * 70)
    jobs_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs"
    jobs_resp = requests.get(jobs_url, headers=headers, params={"pageSize": 2})
    print(f"GET {jobs_url}?pageSize=2")
    print(f"Status: {jobs_resp.status_code}")
    if jobs_resp.status_code == 200:
        jobs_data = jobs_resp.json()
        print(json.dumps(jobs_data, indent=2))
        results["jobs_sample"] = jobs_data
    else:
        print(f"Error: {jobs_resp.text}")
        results["jobs_sample"] = {"error": jobs_resp.text}

    print("\n" + "=" * 70)
    print("[ServiceTitan] Account config fetch complete")
    print("=" * 70)

    return results
