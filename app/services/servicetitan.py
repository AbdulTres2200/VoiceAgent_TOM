import os
import time
import requests
import usaddress
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from dotenv import load_dotenv
import json

load_dotenv()

# Excavation job type IDs - these require special 3-job workflow
EXCAVATION_JOB_TYPE_IDS = [
    1447569631,  # EXP
    1447586371,  # EXS
    1447570137,  # EXPB
    1447570138,  # EXPL
    1447571975,  # EXR Excavation ReEval
    1447569360,  # EXF FINAL PAYMENT
    1454280716,  # EX2
    1447570767,  # EXCU
    1447570002,  # CE
]

# Excavation-specific job type IDs for the 3-job workflow
EXCAVATION_JET_JOB_TYPE_ID = 1447569500      # JET1
EXCAVATION_REEVAL_JOB_TYPE_ID = 1447571975   # EXR Excavation ReEval
EXCAVATION_FINAL_JOB_TYPE_ID = 1447569360    # EXF FINAL PAYMENT

# Team emails for excavation notifications (list for general notifications)
EXCAVATION_TEAM_EMAILS = [
    "mrr043@gmail.com",  # Tim Boyle
    # Add more team members as needed
]

# Excavator emails by technician ID (for dashboard editing)
# Load from JSON file if exists, otherwise use defaults
def _load_excavator_emails():
    import json
    config_path = os.path.join(os.path.dirname(__file__), "..", "excavator_emails.json")
    try:
        with open(config_path, "r") as f:
            data = json.load(f)
            # Convert string keys to int (JSON doesn't support int keys)
            return {int(k): v for k, v in data.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            1532411211: "mrr043@gmail.com",  # Tim Boyle
        }

EXCAVATOR_EMAILS_BY_ID = _load_excavator_emails()

# Email credentials (loaded from .env)
SARAH_EMAIL = os.getenv("SARAH_EMAIL")
SARAH_EMAIL_PASSWORD = os.getenv("SARAH_EMAIL_PASSWORD")

# Cache for excavation technicians (5 minute TTL - location changes frequently)
_excavator_cache = {"data": None, "expires_at": 0}
_EXCAVATOR_CACHE_TTL = 300  # 5 minutes


def send_excavation_email(
    customer_name: str,
    formatted_address: str,
    phone: str,
    customer_email: str,
    issue_description: str,
    appointment_time: str,
    city: str,
    jet_job_id: int,
    reeval_job_id: int,
    final_job_id: int,
    technician_name: str,
    technician_distance: float,
    technician_status: str,
    recipients: list = None
) -> dict:
    """
    Send excavation job notification email to the team.

    Args:
        recipients: Optional list of email addresses. If None, uses EXCAVATION_TEAM_EMAILS.

    Returns dict with:
        - status: "success" or "failed"
        - email_sent: True/False
        - recipients: list of email addresses
        - error: error message if failed
    """
    if recipients is None:
        recipients = EXCAVATION_TEAM_EMAILS.copy()
    else:
        recipients = list(recipients)  # Ensure it's a list copy

    result = {
        "status": "failed",
        "email_sent": False,
        "recipients": recipients,
        "error": None
    }

    if not SARAH_EMAIL or not SARAH_EMAIL_PASSWORD:
        result["error"] = "Missing email credentials (SARAH_EMAIL or SARAH_EMAIL_PASSWORD)"
        return result

    if not recipients:
        result["error"] = "No recipients configured (EXCAVATION_TEAM_EMAILS is empty)"
        return result

    try:
        subject = f"New Excavation Job Booked - {customer_name} - {city}"
        body = f"""New Excavation Job has been booked through Maria AI.

CUSTOMER DETAILS:
Customer Name: {customer_name}
Address: {formatted_address}
Phone: {phone}
Email: {customer_email}
Issue: {issue_description}
Appointment: {appointment_time}

JOBS CREATED IN SERVICETITAN:
1. JET Job #{jet_job_id}
2. Re-Evaluate Job #{reeval_job_id}
3. Final Payment Job #{final_job_id}

RECOMMENDED EXCAVATOR:
{technician_name} — {technician_distance:.1f} miles away | Status: {technician_status}

Please log into ServiceTitan to view and assign these jobs.

- Maria AI Dispatch System
Hearn Plumbing, Heating & Air
"""

        msg = MIMEMultipart()
        msg['From'] = SARAH_EMAIL
        msg['To'] = ", ".join(recipients)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP('smtp.office365.com', 587) as server:
            server.starttls()
            server.login(SARAH_EMAIL, SARAH_EMAIL_PASSWORD)
            server.sendmail(SARAH_EMAIL, recipients, msg.as_string())

        result["status"] = "success"
        result["email_sent"] = True
        return result

    except Exception as e:
        result["error"] = str(e)
        return result


def haversine(lat1, lon1, lat2, lon2):
    """Calculate distance between two points in miles using haversine formula."""
    from math import radians, sin, cos, sqrt, atan2
    R = 3959  # Earth radius in miles
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return R * c


def find_closest_excavator(job_lat, job_lon):
    """
    Find the closest excavator to a job location.

    Qualification criteria:
    - "Is Excavator" == "YES"
    - active == true
    - Has valid GPS coordinates (latitude and longitude not null)

    Returns single closest excavator dict with id, name, distance, status.
    Prioritizes Idle status, falls back to closest regardless of status.
    """
    from app.services.servicetitan import get_access_token, TENANT_ID, APP_KEY

    current_time = time.time()

    # Check cache
    if _excavator_cache["data"] and current_time < _excavator_cache["expires_at"]:
        all_techs = _excavator_cache["data"]
        print(f"[Excavation] Using cached technician data ({len(all_techs)} techs)")
    else:
        # Fetch fresh data from ServiceTitan
        print("[Excavation] Fetching technicians from ServiceTitan...")
        token = get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "ST-App-Key": APP_KEY,
            "Content-Type": "application/json"
        }

        all_techs = []
        page = 1

        while True:
            url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/technicians"
            params = {"pageSize": 100, "active": "true", "page": page}
            resp = requests.get(url, headers=headers, params=params)

            if resp.status_code != 200:
                print(f"[Excavation] Failed to fetch technicians: {resp.status_code}")
                break

            data = resp.json().get("data", [])
            if not data:
                break

            all_techs.extend(data)
            if len(data) < 100:
                break
            page += 1

        # Cache the data
        _excavator_cache["data"] = all_techs
        _excavator_cache["expires_at"] = current_time + _EXCAVATOR_CACHE_TTL
        print(f"[Excavation] Fetched {len(all_techs)} technicians, cached for 5 minutes")

    # Filter to qualified excavators
    qualified = []
    for tech in all_techs:
        if not tech.get("active"):
            continue

        # Check custom fields for Is_Excavator
        custom_fields = {}
        for cf in tech.get("customFields", []):
            cf_name = cf.get("name", "")
            custom_fields[cf_name] = cf.get("value")

        # Must have "Is Excavator" == "YES"
        if custom_fields.get("Is Excavator") != "YES":
            continue

        # Must have valid GPS coordinates
        location = tech.get("location", {}) or {}
        lat = location.get("latitude")
        lon = location.get("longitude")

        if lat is None or lon is None:
            continue

        qualified.append({
            "id": tech.get("id"),
            "name": tech.get("name"),
            "email": tech.get("email"),  # Get email directly from technician record
            "status": tech.get("status"),
            "lat": lat,
            "lon": lon
        })

    print(f"[Excavation] Found {len(qualified)} excavators (Is Excavator=YES)")

    if not qualified:
        return None

    # Calculate distances and sort
    for tech in qualified:
        tech["distance"] = haversine(job_lat, job_lon, tech["lat"], tech["lon"])

    qualified.sort(key=lambda x: x["distance"])

    # First try to find closest Idle technician
    selected = None
    for tech in qualified:
        if tech["status"] == "Idle":
            selected = tech
            break

    # If no Idle technician, take the closest one regardless of status
    if not selected:
        selected = qualified[0]

    result = {
        "id": selected["id"],
        "name": selected["name"],
        "email": selected.get("email"),
        "distance": selected["distance"],
        "status": selected["status"]
    }

    print(f"[Excavation] Selected: {result['name']} ({result['distance']:.1f} miles) Status: {result['status']} Email: {result['email']}")

    return result


def get_dispatch_category(job_type_name):
    """
    Map ServiceTitan job type names to dispatch categories.
    Returns one of: "Sewers/Mainline", "Water Heaters", "Misc Plumbing", "Gas Lines", "Well Pump",
                    "HVAC Cooling", "HVAC Heating", "HVAC Service"

    Mapping rules:
    - Gas1, Gas2, anything with "GAS" → Gas Lines
    - WH1, WH2, "Water Heater" → Water Heaters
    - Pump1, Pump2, anything with "PUMP" or "WELL" → Well Pump
    - S1, S2, anything with "DRAIN", "SEWER", "MAINLINE" → Sewers/Mainline
    - AC, A/C, COOLING, NO COOL → HVAC Cooling
    - HEAT, FURNACE, BOILER, NO HEAT → HVAC Heating
    - HVAC, MINISPLIT → HVAC Service
    - P1, P2, P3 and everything else → Misc Plumbing
    """
    if not job_type_name:
        return "Misc Plumbing"

    name_upper = job_type_name.upper()

    # HVAC Cooling jobs
    if "AC" in name_upper or "A/C" in name_upper or "COOLING" in name_upper or "NO COOL" in name_upper or "AIR CONDITION" in name_upper:
        return "HVAC Cooling"

    # HVAC Heating jobs
    if "HEAT" in name_upper or "FURNACE" in name_upper or "BOILER" in name_upper or "NO HEAT" in name_upper:
        return "HVAC Heating"

    # General HVAC jobs
    if "HVAC" in name_upper or "MINISPLIT" in name_upper or "MINI SPLIT" in name_upper or "DUCTLESS" in name_upper:
        return "HVAC Service"

    # Gas line jobs - check first to avoid GAS1 matching S1/S2
    if "GAS" in name_upper:
        return "Gas Lines"

    # Water heater jobs
    if "WH" in name_upper or "WATER HEATER" in name_upper:
        return "Water Heaters"

    # Well pump jobs - "PUMP" catches Pump1, Pump2, etc. (won't match P1/P2)
    if "PUMP" in name_upper or "WELL" in name_upper:
        return "Well Pump"

    # Sewer/drain jobs - S1, S2 at start, or drain-related keywords
    if name_upper.startswith("S1") or name_upper.startswith("S2") or \
       "DRAIN" in name_upper or "SEWER" in name_upper or \
       "MAIN LINE" in name_upper or "MAINLINE" in name_upper:
        return "Sewers/Mainline"

    # Default to misc plumbing (covers P1, P2, P3, etc.)
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

# HVAC Job Type to Business Unit mapping
# Based on Tom's HVAC Job Types Classification Reference document
HVAC_JOB_TYPE_BU_MAP = {
    # H+ Maintenance jobs → HVAC Res Maintenance
    "H - A/C H+ Maintenance": "HVAC Res Maintenance",
    "H - A/C H+ Maintenance - Minisplit": "HVAC Res Maintenance",
    "H - Heating H+ Maintenance": "HVAC Res Maintenance",
    "H - Heating H+ Maintenance - Boiler": "HVAC Res Maintenance",
    "H - Heating H+ Maintenance - Oil": "HVAC Res Maintenance",
    "H - HVAC Air Filter Sale": "HVAC Res Maintenance",

    # Service jobs → HVAC Res Service
    "H - A/C Service": "HVAC Res Service",
    "H - A/C Service - Minisplit": "HVAC Res Service",
    "H - Heating Service": "HVAC Res Service",
    "H - Heating Service - Boiler": "HVAC Res Service",
    "H - Heating Service - Oil": "HVAC Res Service",
    "H - HVAC Callback": "HVAC Res Service",
    "H - HVAC Mfr Warranty": "HVAC Res Service",
    "H - No A/C": "HVAC Res Service",
    "H - No A/C - Minisplit": "HVAC Res Service",
    "H - No Heat": "HVAC Res Service",
    "H - No Heat - Boiler": "HVAC Res Service",
    "H - No Heat - Oil": "HVAC Res Service",

    # Sales/Quote jobs → HVAC Res Sales
    "H - HVAC Quote": "HVAC Res Sales",
}


def get_business_unit_for_job_type(job_type_name: str) -> dict:
    """
    Get the appropriate Business Unit for an HVAC job type.
    Returns dict with business_unit_id and business_unit_name, or None if not an HVAC job.
    """
    if not job_type_name:
        return None

    # Check exact match first
    bu_name = HVAC_JOB_TYPE_BU_MAP.get(job_type_name)

    # If no exact match, try partial matching for job types with slight name variations
    if not bu_name:
        job_upper = job_type_name.upper()
        if "H+ MAINTENANCE" in job_upper or "H - A/C H+" in job_upper or "H - HEATING H+" in job_upper:
            bu_name = "HVAC Res Maintenance"
        elif "NO A/C" in job_upper or "NO HEAT" in job_upper or "NO AC" in job_upper:
            bu_name = "HVAC Res Service"
        elif "SERVICE" in job_upper and ("A/C" in job_upper or "AC" in job_upper or "HEATING" in job_upper or "HVAC" in job_upper):
            bu_name = "HVAC Res Service"
        elif "CALLBACK" in job_upper:
            bu_name = "HVAC Res Service"
        elif "QUOTE" in job_upper or "ESTIMATE" in job_upper:
            bu_name = "HVAC Res Sales"

    if not bu_name:
        return None

    # Look up the actual BU ID from ServiceTitan
    units, name_to_id = get_business_units_from_st()
    if bu_name in name_to_id:
        return {
            "business_unit_id": name_to_id[bu_name],
            "business_unit_name": bu_name
        }

    # Try case-insensitive match
    for stored_name, bu_id in name_to_id.items():
        if stored_name.upper() == bu_name.upper():
            return {
                "business_unit_id": bu_id,
                "business_unit_name": stored_name
            }

    print(f"[HVAC BU] Warning: Could not find BU ID for '{bu_name}'")
    return None


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
            # Fall back to default from .env
            return {"business_unit_id": BUSINESS_UNIT_ID, "business_unit_name": "Default"}
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


def detect_job_type(issue_description: str, customer_type: str = "Residential",
                    is_emergency: bool = False, is_excavation: bool = False,
                    appointment_info: str = None):
    """
    Use OpenAI to detect the best job type for a given issue description.
    Returns {job_type_id, job_type_name, priority, job_category}.

    Args:
        issue_description: The customer's issue description
        customer_type: "Residential" or "Commercial" - affects job type selection
        is_emergency: Whether customer indicated this is an emergency
        is_excavation: Whether Sarah flagged this as excavation work
        appointment_info: Appointment window chosen (e.g., "tomorrow afternoon 12-4")
    """
    global _job_type_detection_cache
    import re

    # Normalize customer_type
    is_commercial = customer_type.lower() == "commercial" if customer_type else False

    # Check if issue is HVAC-related for smart fallback
    # IMPORTANT: Check for water heater FIRST to avoid false HVAC match
    issue_lower = (issue_description or "").lower()

    # Water heater issues are PLUMBING, not HVAC
    is_water_heater_issue = 'water heater' in issue_lower or 'hot water' in issue_lower or 'no hot water' in issue_lower

    hvac_keywords = ['ac', 'a/c', 'air condition', 'cooling', 'cool', 'heating',
                     'furnace', 'boiler', 'hvac', 'thermostat', 'minisplit', 'mini split',
                     'ductless', 'hot air', 'cold air', 'not cooling', 'not heating',
                     'no heat', 'no cooling', 'warm air', 'cold house', 'hot house']

    # Only mark as HVAC if NOT a water heater issue
    is_hvac_issue = not is_water_heater_issue and any(kw in issue_lower for kw in hvac_keywords)

    # Default fallback - Tom's Hearn Plumbing/HVAC job types
    # Will be overridden with HVAC type if issue is HVAC-related
    fallback = {
        "job_type_id": 4449187,  # Tom's "P - Plumbing Service" (default)
        "job_type_name": "P - Plumbing Service",
        "priority": "High",
        "job_category": "Plumbing"
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
    print(f"[Job Type] Is Emergency: {is_emergency}")
    print(f"[Job Type] Is Excavation: {is_excavation}")
    print(f"[Job Type] Appointment: {appointment_info or 'Not specified'}")
    print(f"[Job Type] Analyzing with OpenAI...")

    # Build customer type note for OpenAI
    customer_type_note = ""
    if is_commercial:
        customer_type_note = "\n\nIMPORTANT: This is a COMMERCIAL customer. Only use commercial job types (starting with C like CP1, CP2, CS1, CS2, CCL, CWH)."
    else:
        customer_type_note = "\n\nIMPORTANT: This is a RESIDENTIAL customer. Do NOT use commercial job types (those starting with C like CS2, CP2). Use residential types like S2, P2, WH1, etc."

    # Build context note
    context_note = ""
    if is_excavation:
        context_note = "\n\nNOTE: This has been flagged as EXCAVATION work. Use excavation job types (S1 Main Line, EXS, etc.)."
    elif not is_emergency and appointment_info:
        context_note = f"\n\nNOTE: Customer scheduled for {appointment_info} (NOT an emergency). Use appropriate non-emergency job type."

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
                    "content": """You are an expert HVAC and plumbing dispatcher for Hearn Plumbing, Heating & Air. Classify issues into job type names.

IMPORTANT: Respond with ONLY the exact job type name from the list provided. Match the format exactly.

=== HVAC CLASSIFICATION RULES ===

**EMERGENCY - NO COOLING (Priority: High)**

"H - No A/C" - Traditional central A/C with NO cooling:
- Complete loss of cooling, house is hot, urgent situation
- "AC not working at all", "No cool air", "AC is dead"
- "House is 85+ degrees", "Can't cool down"
- Customer needs same-day/emergency appointment

"H - No A/C - Minisplit" - Mini-split/ductless with NO cooling:
- Same as above but for mini-split/ductless systems
- Customer mentions "mini-split", "ductless", "wall unit", Mitsubishi/Daikin/Fujitsu

**EMERGENCY - NO HEATING (Priority: Urgent)**

"H - No Heat" - Gas/propane/electric furnace with NO heat:
- Complete loss of heat, house is cold, pipes may freeze
- "Furnace not working", "No heat at all", "House is freezing"
- NOT a boiler, NOT oil fuel

"H - No Heat - Boiler" - Boiler system (gas/propane/electric) with NO heat:
- Customer mentions "boiler", "radiators", "baseboard heat", "steam heat"
- NOT oil fuel

"H - No Heat - Oil" - ANY oil-fired system with NO heat:
- Oil furnace OR oil boiler - oil fuel takes priority
- Customer mentions "oil heat", "oil furnace", "oil boiler", "oil burner"

**SERVICE CALLS - A/C (Priority: Normal)**

"H - A/C Service" - Traditional A/C issues (still has some cooling):
- AC running but not cooling well, making noise, leaking water
- Short cycling, weak airflow, ice on unit, refrigerant concerns
- Customer wants tune-up but is NOT an H+ member
- NOT a mini-split

"H - A/C Service - Minisplit" - Mini-split issues (still has some cooling):
- Same symptoms as above but for mini-split/ductless
- Customer mentions "mini-split", "ductless", "wall unit"

**SERVICE CALLS - HEATING (Priority: Normal)**

"H - Heating Service" - Gas/propane/electric furnace issues (still has some heat):
- Furnace running but not heating well, making noise
- Short cycling, pilot issues, thermostat problems
- NOT a boiler, NOT oil

"H - Heating Service - Boiler" - Boiler issues (gas/propane/electric, still has some heat):
- Customer mentions "boiler", "radiators", "baseboard", "steam"
- NOT oil fuel

"H - Heating Service - Oil" - ANY oil system issues (still has some heat):
- Oil takes priority over equipment type
- Customer mentions "oil heat", "oil furnace", "oil boiler"

**H+ MEMBER MAINTENANCE (Priority: Low)**

"H - A/C H+ Maintenance" - H+ member routine A/C tune-up:
- Customer confirms H+ membership AND wants maintenance/tune-up
- Traditional central A/C (not mini-split)
- NO symptoms or problems reported

"H - A/C H+ Maintenance - Minisplit" - H+ member mini-split maintenance:
- H+ member wants maintenance on mini-split/ductless

"H - Heating H+ Maintenance" - H+ member furnace tune-up:
- H+ member wants maintenance on gas/propane/electric furnace
- NOT boiler, NOT oil

"H - Heating H+ Maintenance - Boiler" - H+ member boiler maintenance:
- H+ member wants maintenance on gas/propane/electric boiler

"H - Heating H+ Maintenance - Oil" - H+ member oil system maintenance:
- H+ member with oil furnace or oil boiler

**SPECIAL HVAC TYPES**

"H - HVAC Callback" (Priority: High):
- Issue related to work done in last 365 days
- "You were just here", "Same problem as before", "After your tech left..."

"H - HVAC Quote" (Priority: Normal):
- Customer wants quote/estimate for NEW system (not repair)
- "Want to replace my furnace", "Quote for new AC", "System is old, want new one"

=== PLUMBING CLASSIFICATION RULES ===

**DRAIN/SEWER ISSUES**

"P - Sewer Main Line" - ONLY for main sewer issues:
- MULTIPLE drains backing up throughout house
- Basement floor drain with sewage
- Main sewer line affecting whole house

"P - Drain Secondary" - Single fixture drains:
- ONE drain clogged (sink, tub, shower, toilet, washer)
- "Water behind washer" = secondary drain (single fixture)

**WATER HEATER ISSUES**

"P - Water Heater- Leaking!!" - Water heater is LEAKING:
- Water on floor around water heater
- "Water heater is leaking", "puddle under water heater"

"P - Plumbing Service" - Water heater NOT working (no leak):
- No hot water, water heater not producing heat
- "No hot water", "water heater not working"
- Use this for water heater service calls without leaking

"P - Plumbing Install - Water Heater" - New water heater install:
- Customer wants to replace/install new water heater
- "Need a new water heater", "replace water heater"

**OTHER PLUMBING**

"P - Plumbing Emergency Service" - URGENT plumbing:
- Active water leak/burst pipe
- Major water damage occurring NOW

"P - Water Leak-Major" - Major water leak (non-emergency):
- Significant water leak but not emergency level

"P - Plumbing Service" - General plumbing service:
- Faucet, toilet, minor leak, general plumbing issues
- Default for plumbing service calls

=== DECISION PRIORITY ===
1. Check fuel type: OIL always routes to Oil job types
2. Check equipment: Boiler/Mini-split have specific job types
3. Check urgency: No cooling/No heat = emergency types
4. Check membership: H+ member maintenance vs regular service
5. Check if callback: Related to recent work = Callback
6. Check if quote: Wants replacement = Quote

WHEN UNSURE:
- HVAC cooling issue -> "H - A/C Service"
- HVAC heating issue -> "H - Heating Service"
- Water heater issue -> "P - Plumbing Service"
- Plumbing issue -> "P - Plumbing Service\""""
                },
                {
                    "role": "user",
                    "content": f"""Customer issue: "{issue_description}"

Job type codes:
{formatted_list}
Reply with ONLY the code (e.g., WH1, S2, Pump1).{customer_type_note}{context_note}"""
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
                # Add business unit for HVAC job types
                bu_info = get_business_unit_for_job_type(jt["name"])
                if bu_info:
                    result["business_unit_id"] = bu_info["business_unit_id"]
                    result["business_unit_name"] = bu_info["business_unit_name"]
                _job_type_detection_cache[cache_key] = result
                print(f"[Job Type] Detected: {result['job_type_name']} (ID: {result['job_type_id']}) Priority: {result['priority']} Category: {result['job_category']}")
                if bu_info:
                    print(f"[Job Type] Business Unit: {result['business_unit_name']} (ID: {result['business_unit_id']})")
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
                # Add business unit for HVAC job types
                bu_info = get_business_unit_for_job_type(jt["name"])
                if bu_info:
                    result["business_unit_id"] = bu_info["business_unit_id"]
                    result["business_unit_name"] = bu_info["business_unit_name"]
                _job_type_detection_cache[cache_key] = result
                print(f"[Job Type] Detected: {result['job_type_name']} (ID: {result['job_type_id']}) Priority: {result['priority']} Category: {result['job_category']}")
                if bu_info:
                    print(f"[Job Type] Business Unit: {result['business_unit_name']} (ID: {result['business_unit_id']})")
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
                # Add business unit for HVAC job types
                bu_info = get_business_unit_for_job_type(jt["name"])
                if bu_info:
                    result["business_unit_id"] = bu_info["business_unit_id"]
                    result["business_unit_name"] = bu_info["business_unit_name"]
                _job_type_detection_cache[cache_key] = result
                print(f"[Job Type] Detected: {result['job_type_name']} (ID: {result['job_type_id']}) Priority: {result['priority']} Category: {result['job_category']}")
                if bu_info:
                    print(f"[Job Type] Business Unit: {result['business_unit_name']} (ID: {result['business_unit_id']})")
                print("[Job Type] Method: AI summary match")
                return result

        # No match found - try smart fallback based on issue type
        print(f"[Job Type] WARNING: No match for '{cleaned_name}', using smart fallback")

        # WATER HEATER fallback - check first before HVAC
        if is_water_heater_issue:
            # Look for water heater job types in order of preference
            wh_keywords = ['water heater', 'wh']
            for jt in job_types:
                jt_name_lower = jt["name"].lower()
                if any(kw in jt_name_lower for kw in wh_keywords):
                    result = {
                        "job_type_id": jt["id"],
                        "job_type_name": jt["name"],
                        "priority": jt["priority"],
                        "job_category": "Plumbing"
                    }
                    _job_type_detection_cache[cache_key] = result
                    print(f"[Job Type] Detected: {result['job_type_name']} (ID: {result['job_type_id']}) Priority: {result['priority']}")
                    print("[Job Type] Method: smart water heater fallback")
                    return result
            # If no specific water heater type found, use P - Plumbing Service
            for jt in job_types:
                if jt["name"] == "P - Plumbing Service":
                    result = {
                        "job_type_id": jt["id"],
                        "job_type_name": jt["name"],
                        "priority": "High",  # Upgrade to High for no hot water
                        "job_category": "Plumbing"
                    }
                    _job_type_detection_cache[cache_key] = result
                    print(f"[Job Type] Detected: {result['job_type_name']} (ID: {result['job_type_id']}) Priority: High (water heater issue)")
                    print("[Job Type] Method: plumbing service fallback for water heater")
                    return result

        # If HVAC issue, try to find an HVAC job type from the available list
        if is_hvac_issue:
            hvac_type_keywords = ['ac', 'a/c', 'heat', 'hvac', 'air', 'furnace', 'cooling', 'heating']
            for jt in job_types:
                jt_name_lower = jt["name"].lower()
                jt_summary_lower = (jt["summary"] or "").lower()
                if any(kw in jt_name_lower or kw in jt_summary_lower for kw in hvac_type_keywords):
                    result = {
                        "job_type_id": jt["id"],
                        "job_type_name": jt["name"],
                        "priority": jt["priority"],
                        "job_category": "HVAC"
                    }
                    # Add business unit for HVAC job types
                    bu_info = get_business_unit_for_job_type(jt["name"])
                    if bu_info:
                        result["business_unit_id"] = bu_info["business_unit_id"]
                        result["business_unit_name"] = bu_info["business_unit_name"]
                    _job_type_detection_cache[cache_key] = result
                    print(f"[Job Type] Detected: {result['job_type_name']} (ID: {result['job_type_id']}) Priority: {result['priority']}")
                    if bu_info:
                        print(f"[Job Type] Business Unit: {result['business_unit_name']} (ID: {result['business_unit_id']})")
                    print("[Job Type] Method: smart HVAC fallback")
                    return result

        print(f"[Job Type] Detected: {fallback['job_type_name']} (ID: {fallback['job_type_id']}) Priority: {fallback['priority']}")
        print("[Job Type] Method: fallback")
        return fallback

    except Exception as e:
        print(f"[Job Type] OpenAI error: {e}")

        # Try smart fallback on error too
        if is_hvac_issue and 'job_types' in dir():
            for jt in job_types:
                jt_name_lower = jt["name"].lower()
                jt_summary_lower = (jt["summary"] or "").lower()
                if any(kw in jt_name_lower or kw in jt_summary_lower for kw in ['ac', 'heat', 'hvac', 'air', 'furnace']):
                    result = {
                        "job_type_id": jt["id"],
                        "job_type_name": jt["name"],
                        "priority": jt["priority"],
                        "job_category": "HVAC"
                    }
                    # Add business unit for HVAC job types
                    bu_info = get_business_unit_for_job_type(jt["name"])
                    if bu_info:
                        result["business_unit_id"] = bu_info["business_unit_id"]
                        result["business_unit_name"] = bu_info["business_unit_name"]
                    print(f"[Job Type] Detected: {result['job_type_name']} (ID: {result['job_type_id']}) Priority: {result['priority']}")
                    if bu_info:
                        print(f"[Job Type] Business Unit: {result['business_unit_name']} (ID: {result['business_unit_id']})")
                    print("[Job Type] Method: smart HVAC fallback (error recovery)")
                    return result

        print(f"[Job Type] Detected: {fallback['job_type_name']} (ID: {fallback['job_type_id']}) Priority: {fallback['priority']}")
        print("[Job Type] Method: fallback (error)")
        return fallback


# Default fallback campaign for Retell calls
RETELL_CAMPAIGN_ID = 1405279506
RETELL_CAMPAIGN_NAME = "Branding - Pittsburgh"
RETELL_BUSINESS_UNIT_ID = BUSINESS_UNIT_ID  # Use default from .env (405 for Tom)
RETELL_BUSINESS_UNIT_NAME = "PLMG Res Service"  # Tom's default BU name

# Campaign cache (24 hour TTL)
_campaigns_cache = {"data": [], "expires_at": 0}
_CAMPAIGNS_CACHE_TTL = 86400  # 24 hours


def get_all_campaigns(force_refresh: bool = False) -> list:
    """
    Fetch all active campaigns from ServiceTitan.
    Results are cached for 24 hours.
    """
    import time as _time
    current_time = _time.time()

    if not force_refresh and _campaigns_cache["data"] and current_time < _campaigns_cache["expires_at"]:
        return _campaigns_cache["data"]

    print("[Campaigns] Fetching campaigns from ServiceTitan...")
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    all_campaigns = []
    page = 1

    while True:
        url = f"https://api.servicetitan.io/marketing/v2/tenant/{TENANT_ID}/campaigns"
        params = {"pageSize": 100, "active": "true", "page": page}
        resp = requests.get(url, headers=headers, params=params)

        if resp.status_code != 200:
            print(f"[Campaigns] API error: {resp.status_code}")
            break

        data = resp.json().get("data", [])
        if not data:
            break

        for c in data:
            # Handle category - can be None, dict, or other
            category_obj = c.get("category")
            category_name = ""
            if isinstance(category_obj, dict):
                category_name = category_obj.get("name", "")
            elif isinstance(category_obj, str):
                category_name = category_obj

            # Handle businessUnit - can be None, dict, or other
            bu_obj = c.get("businessUnit")
            bu_id = None
            bu_name = ""
            if isinstance(bu_obj, dict):
                bu_id = bu_obj.get("id")
                bu_name = bu_obj.get("name", "")

            campaign_info = {
                "id": c.get("id"),
                "name": c.get("name", ""),
                "category": category_name,
                "business_unit_id": bu_id,
                "business_unit_name": bu_name,
                "active": c.get("active", True)
            }
            all_campaigns.append(campaign_info)

        if len(data) < 100:
            break
        page += 1

    print(f"[Campaigns] Cached {len(all_campaigns)} active campaigns")
    _campaigns_cache["data"] = all_campaigns
    _campaigns_cache["expires_at"] = current_time + _CAMPAIGNS_CACHE_TTL

    return all_campaigns


def detect_campaign_from_referral(referral_response: str) -> dict:
    """
    Use AI to detect the best matching campaign based on customer's referral response.

    Args:
        referral_response: Customer's answer to "How did you hear about us?"

    Returns:
        dict with campaign_id, campaign_name, confidence, and reason
    """
    from openai import OpenAI
    import os

    # Get all available campaigns
    campaigns = get_all_campaigns()

    if not campaigns:
        print("[Campaign Detection] No campaigns available, using default")
        return {
            "campaign_id": RETELL_CAMPAIGN_ID,
            "campaign_name": RETELL_CAMPAIGN_NAME,
            "confidence": "low",
            "reason": "No campaigns available for matching"
        }

    # Build campaign list for AI
    campaign_list = "\n".join([
        f"- ID: {c['id']} | Name: {c['name']} | Category: {c['category']}"
        for c in campaigns
    ])

    # Use OpenAI to match referral to campaign
    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=150,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": f"""You are a campaign matching assistant for a plumbing/HVAC company.

Given a customer's response to "How did you hear about us?", match it to the best campaign from this list:

{campaign_list}

MATCHING RULES:
- "Google", "online", "searched", "internet" → Look for Google/SEO/PPC campaigns
- "Friend", "neighbor", "family", "referral", "someone recommended" → Look for Referral campaigns
- "Yard sign", "sign", "truck", "van", "saw your truck" → Look for Branding/Yard Sign campaigns
- "Facebook", "Instagram", "social media" → Look for Social Media campaigns
- "TV", "television", "commercial" → Look for TV campaigns
- "Radio" → Look for Radio campaigns
- "Mailer", "postcard", "mail", "flyer" → Look for Direct Mail campaigns
- "Repeat customer", "used you before", "came back" → Look for Repeat/Returning Customer campaigns
- "Angie", "Angi", "HomeAdvisor", "Thumbtack" → Look for those specific lead source campaigns

Respond in this EXACT format:
CAMPAIGN_ID: [number]
CAMPAIGN_NAME: [name]
CONFIDENCE: high/medium/low
REASON: [brief explanation]"""
                },
                {
                    "role": "user",
                    "content": f"Customer said: \"{referral_response}\""
                }
            ]
        )

        ai_response = response.choices[0].message.content.strip()
        print(f"[Campaign Detection] AI response: {ai_response}")

        # Parse response
        result = {
            "campaign_id": RETELL_CAMPAIGN_ID,
            "campaign_name": RETELL_CAMPAIGN_NAME,
            "confidence": "low",
            "reason": "Could not parse AI response"
        }

        for line in ai_response.split("\n"):
            line = line.strip()
            if line.startswith("CAMPAIGN_ID:"):
                try:
                    result["campaign_id"] = int(line.replace("CAMPAIGN_ID:", "").strip())
                except:
                    pass
            elif line.startswith("CAMPAIGN_NAME:"):
                result["campaign_name"] = line.replace("CAMPAIGN_NAME:", "").strip()
            elif line.startswith("CONFIDENCE:"):
                result["confidence"] = line.replace("CONFIDENCE:", "").strip().lower()
            elif line.startswith("REASON:"):
                result["reason"] = line.replace("REASON:", "").strip()

        print(f"[Campaign Detection] Matched: {result['campaign_name']} (ID: {result['campaign_id']}) - {result['confidence']}")
        return result

    except Exception as e:
        print(f"[Campaign Detection] Error: {e}")
        return {
            "campaign_id": RETELL_CAMPAIGN_ID,
            "campaign_name": RETELL_CAMPAIGN_NAME,
            "confidence": "low",
            "reason": f"AI detection failed: {str(e)}"
        }


def get_live_call_campaign(from_number: str, to_number: str):
    """
    Get campaign info for a live call by matching from/to numbers.
    Tries telecom API first, then campaigns API, then uses fallback values.
    """
    clean_from = clean_phone(from_number)
    clean_to = clean_phone(to_number)

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    # Method 1: Try telecom API
    telecom_url = f"https://api.servicetitan.io/telecom/v2/tenant/{TENANT_ID}/calls"
    params = {"pageSize": 5, "orderBy": "Id", "orderByDirection": "desc", "from": clean_from}

    try:
        resp = requests.get(telecom_url, headers=headers, params=params)
        if resp.status_code == 200:
            for call in resp.json().get("data", []):
                lead_call = call.get("leadCall") or {}
                if clean_phone(lead_call.get("to", "")) == clean_to:
                    campaign = lead_call.get("campaign")
                    if campaign:
                        business_unit = call.get("businessUnit") or {}
                        return {
                            "method": "telecom_api",
                            "campaign_id": campaign.get("id"),
                            "campaign_name": campaign.get("name", ""),
                            "business_unit_id": business_unit.get("id", RETELL_BUSINESS_UNIT_ID),
                            "business_unit_name": business_unit.get("name", RETELL_BUSINESS_UNIT_NAME)
                        }
    except Exception:
        pass

    # Method 2: Try campaigns API
    campaigns_url = f"https://api.servicetitan.io/marketing/v2/tenant/{TENANT_ID}/campaigns"
    try:
        resp = requests.get(campaigns_url, headers=headers, params={"pageSize": 100, "active": "true"})
        if resp.status_code == 200:
            for campaign in resp.json().get("data", []):
                campaign_phones = campaign.get("campaignPhoneNumbers", [])
                cleaned_phones = []
                for phone_entry in campaign_phones:
                    if isinstance(phone_entry, str):
                        cleaned_phones.append(clean_phone(phone_entry))
                    elif isinstance(phone_entry, dict):
                        raw = phone_entry.get("phoneNumber", "") or phone_entry.get("number", "")
                        cleaned_phones.append(clean_phone(raw))

                if clean_to in cleaned_phones:
                    business_unit = campaign.get("businessUnit")
                    if isinstance(business_unit, dict):
                        bu_id = business_unit.get("id", RETELL_BUSINESS_UNIT_ID)
                        bu_name = business_unit.get("name", RETELL_BUSINESS_UNIT_NAME)
                    elif isinstance(business_unit, str):
                        bu_id = RETELL_BUSINESS_UNIT_ID
                        bu_name = business_unit
                    else:
                        bu_id = RETELL_BUSINESS_UNIT_ID
                        bu_name = RETELL_BUSINESS_UNIT_NAME

                    return {
                        "method": "campaigns_api",
                        "campaign_id": campaign.get("id"),
                        "campaign_name": campaign.get("name", ""),
                        "business_unit_id": bu_id,
                        "business_unit_name": bu_name
                    }
    except Exception:
        pass

    # Fallback
    return {
        "method": "fallback",
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


def create_excavation_jobs(customer_id, location_id, summary, campaign_id, business_unit_id, appointment_time,
                           customer_name=None, customer_email=None, customer_phone=None, formatted_address=None):
    """
    Create 3 jobs for excavation work in ServiceTitan:
    1. JET job (jetting/initial assessment)
    2. Re-evaluate job
    3. Final Payment job

    Also sends email notification to excavation team.

    Returns dict with status and all 3 job IDs.
    """
    print("\n" + "=" * 70)
    print("EXCAVATION JOB CREATION")
    print("=" * 70)

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }

    # Parse appointment time
    time_result = parse_appointment_time(appointment_time)
    start_time = time_result["start"]
    end_time = time_result["end"]
    local_time_display = time_result.get("local_time", appointment_time)

    print(f"[Excavation] Customer ID: {customer_id}")
    print(f"[Excavation] Location ID: {location_id}")
    print(f"[Excavation] Summary: {summary}")
    print(f"[Excavation] Appointment: {local_time_display}")

    job_ids = {}

    # Job 1 - JET
    print("\n[Excavation] Creating Job 1 - JET...")
    jet_payload = {
        "customerId": customer_id,
        "locationId": location_id,
        "summary": f"JET - {summary}",
        "jobTypeId": EXCAVATION_JET_JOB_TYPE_ID,
        "businessUnitId": business_unit_id,
        "campaignId": campaign_id,
        "priority": "Urgent",
        "appointments": [{
            "start": start_time,
            "end": end_time,
            "specialInstructions": f"EXCAVATION JOB 1/3 - JET | {appointment_time}"
        }]
    }

    r = requests.post(
        f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs",
        headers=headers,
        json=jet_payload
    )

    if r.status_code in (200, 201):
        job_ids["jet"] = r.json().get("id")
        print(f"[Excavation] Job 1 (JET) created: {job_ids['jet']}")
    else:
        print(f"[Excavation] Job 1 (JET) FAILED: {r.status_code} - {r.text[:200]}")
        return {"status": "error", "error": f"JET job creation failed: {r.text}"}

    # Job 2 - Re-evaluate
    print("\n[Excavation] Creating Job 2 - Re-evaluate...")
    reeval_payload = {
        "customerId": customer_id,
        "locationId": location_id,
        "summary": f"RE-EVALUATE - {summary}",
        "jobTypeId": EXCAVATION_REEVAL_JOB_TYPE_ID,
        "businessUnitId": business_unit_id,
        "campaignId": campaign_id,
        "priority": "Normal",
        "appointments": [{
            "start": start_time,
            "end": end_time,
            "specialInstructions": f"EXCAVATION JOB 2/3 - RE-EVALUATE | {appointment_time}"
        }]
    }

    r = requests.post(
        f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs",
        headers=headers,
        json=reeval_payload
    )

    if r.status_code in (200, 201):
        job_ids["reeval"] = r.json().get("id")
        print(f"[Excavation] Job 2 (Re-evaluate) created: {job_ids['reeval']}")
    else:
        print(f"[Excavation] Job 2 (Re-evaluate) FAILED: {r.status_code} - {r.text[:200]}")
        return {"status": "error", "error": f"Re-evaluate job creation failed: {r.text}"}

    # Job 3 - Final Payment
    print("\n[Excavation] Creating Job 3 - Final Payment...")
    final_payload = {
        "customerId": customer_id,
        "locationId": location_id,
        "summary": f"FINAL PAYMENT - {summary}",
        "jobTypeId": EXCAVATION_FINAL_JOB_TYPE_ID,
        "businessUnitId": business_unit_id,
        "campaignId": campaign_id,
        "priority": "Low",
        "appointments": [{
            "start": start_time,
            "end": end_time,
            "specialInstructions": f"EXCAVATION JOB 3/3 - FINAL PAYMENT | {appointment_time}"
        }]
    }

    r = requests.post(
        f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs",
        headers=headers,
        json=final_payload
    )

    if r.status_code in (200, 201):
        job_ids["final"] = r.json().get("id")
        print(f"[Excavation] Job 3 (Final Payment) created: {job_ids['final']}")
    else:
        print(f"[Excavation] Job 3 (Final Payment) FAILED: {r.status_code} - {r.text[:200]}")
        return {"status": "error", "error": f"Final Payment job creation failed: {r.text}"}

    # STEP 1: Find closest excavator FIRST (need their email for notification)
    print("\n[Excavation] Finding closest available excavator...")
    closest_excavator = None
    recommendation_posted = False
    email_sent_to = None

    try:
        # Fetch location coordinates
        location_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/locations/{location_id}"
        loc_resp = requests.get(location_url, headers=headers)

        if loc_resp.status_code == 200:
            loc_data = loc_resp.json()
            loc_address = loc_data.get("address", {})
            job_lat = loc_address.get("latitude")
            job_lon = loc_address.get("longitude")

            if job_lat and job_lon:
                print(f"[Excavation] Job location: {job_lat}, {job_lon}")
                closest_excavator = find_closest_excavator(job_lat, job_lon)

                if closest_excavator:
                    # Build recommendation note
                    note_text = f"""=== EXCAVATION TEAM RECOMMENDATION ===
Based on current location and availability:

Recommended Technician:
{closest_excavator['name']} — {closest_excavator['distance']:.1f} miles away | Status: {closest_excavator['status']}

Please assign this technician to this job in ServiceTitan.
Note: Auto-assignment pending ST API access.

Linked Jobs:
- JET: #{job_ids['jet']}
- Re-evaluate: #{job_ids['reeval']}
- Final Payment: #{job_ids['final']}
=== END ==="""

                    # Post note to JET job
                    note_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs/{job_ids['jet']}/notes"
                    note_resp = requests.post(note_url, headers=headers, json={"text": note_text})

                    if note_resp.status_code in (200, 201):
                        recommendation_posted = True
                        print(f"[Excavation] Recommendation note posted to job #{job_ids['jet']}")
                    else:
                        print(f"[Excavation] Failed to post recommendation note: {note_resp.status_code}")
                else:
                    print("[Excavation] No qualified excavators found with valid GPS coordinates")
            else:
                print("[Excavation] Location has no GPS coordinates, skipping excavator search")
        else:
            print(f"[Excavation] Failed to fetch location: {loc_resp.status_code}")

    except Exception as e:
        print(f"[Excavation] Excavator search failed: {e}")

    # Department head info (TODO: Get from Tom)
    DEPT_HEAD_EMAIL = os.getenv("DEPT_HEAD_EMAIL", "")
    DEPT_HEAD_NAME = os.getenv("DEPT_HEAD_NAME", "Hearn Plumbing Team")
    DEPT_HEAD_PHONE = os.getenv("DEPT_HEAD_PHONE", "")

    # Track emails sent
    emails_sent_list = []

    if SARAH_EMAIL and SARAH_EMAIL_PASSWORD:
        # STEP 2A: Send email to closest excavator
        print("\n[Excavation] Sending email notification to excavator...")
        if closest_excavator and closest_excavator.get("email"):
            excavator_email = closest_excavator["email"]
            print(f"[Excavation] Excavator email from ServiceTitan: {excavator_email}")

            try:
                subject = f"New Excavation Job Assigned - {summary[:50]}"
                body = f"""New Excavation Job has been booked through Maria AI.

YOU HAVE BEEN SELECTED as the closest available excavator.

CUSTOMER DETAILS:
Customer: {customer_name or 'N/A'}
Address: {formatted_address or 'N/A'}
Phone: {customer_phone or 'N/A'}
Issue: {summary}
Appointment: {appointment_time}

JOBS CREATED IN SERVICETITAN:
1. JET Job #{job_ids['jet']}
2. Re-Evaluate Job #{job_ids['reeval']}
3. Final Payment Job #{job_ids['final']}

YOUR ASSIGNMENT:
You are {closest_excavator['distance']:.1f} miles away from the job location.
Current Status: {closest_excavator['status']}

Please log into ServiceTitan to view the job details.

- Maria AI Dispatch System
Hearn Plumbing, Heating & Air
"""

                msg = MIMEMultipart()
                msg['From'] = SARAH_EMAIL
                msg['To'] = excavator_email
                msg['Subject'] = subject
                msg.attach(MIMEText(body, 'plain'))

                with smtplib.SMTP('smtp.office365.com', 587) as server:
                    server.starttls()
                    server.login(SARAH_EMAIL, SARAH_EMAIL_PASSWORD)
                    server.sendmail(SARAH_EMAIL, [excavator_email], msg.as_string())
                    emails_sent_list.append({"to": excavator_email, "type": "excavator"})

                print(f"[Excavation] Email sent to excavator: {excavator_email}")

            except Exception as e:
                print(f"[Excavation] Excavator email failed: {e}")
        elif closest_excavator:
            print(f"[Excavation] Excavator {closest_excavator['name']} has no email in ServiceTitan")
        else:
            print("[Excavation] No excavator found - cannot send excavator notification")

        # STEP 2B: Send email to department head (Bill Guntrum)
        print(f"\n[Excavation] Sending email to department head ({DEPT_HEAD_EMAIL})...")
        try:
            subject = f"New Excavation Job Booked - {customer_name or 'Customer'}"
            body = f"""New Excavation Job has been booked through Maria AI.

CUSTOMER DETAILS:
Customer: {customer_name or 'N/A'}
Address: {formatted_address or 'N/A'}
Phone: {customer_phone or 'N/A'}
Email: {customer_email or 'N/A'}
Issue: {summary}
Appointment: {appointment_time}

JOBS CREATED IN SERVICETITAN:
1. JET Job #{job_ids['jet']}
2. Re-Evaluate Job #{job_ids['reeval']}
3. Final Payment Job #{job_ids['final']}

ASSIGNED EXCAVATOR:
{closest_excavator['name'] if closest_excavator else 'Not assigned'}{f" — {closest_excavator['distance']:.1f} miles away | Status: {closest_excavator['status']}" if closest_excavator else ''}

Please log into ServiceTitan to view and manage these jobs.

- Maria AI Dispatch System
Hearn Plumbing, Heating & Air
"""

            msg = MIMEMultipart()
            msg['From'] = SARAH_EMAIL
            msg['To'] = DEPT_HEAD_EMAIL
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))

            with smtplib.SMTP('smtp.office365.com', 587) as server:
                server.starttls()
                server.login(SARAH_EMAIL, SARAH_EMAIL_PASSWORD)
                server.sendmail(SARAH_EMAIL, [DEPT_HEAD_EMAIL], msg.as_string())
                emails_sent_list.append({"to": DEPT_HEAD_EMAIL, "type": "department_head"})

            print(f"[Excavation] Email sent to department head: {DEPT_HEAD_EMAIL}")

        except Exception as e:
            print(f"[Excavation] Department head email failed: {e}")

        # STEP 2C: Send thank you email to customer
        if customer_email and "@" in customer_email:
            print(f"\n[Excavation] Sending thank you email to customer ({customer_email})...")
            try:
                subject = "Thank You for Booking with Hearn Plumbing, Heating & Air"
                body = f"""Dear {customer_name or 'Valued Customer'},

Thank you for booking an excavation job with Hearn Plumbing, Heating & Air!

YOUR APPOINTMENT DETAILS:
Address: {formatted_address or 'N/A'}
Issue: {summary}
Appointment: {appointment_time}

Your job has been scheduled and assigned to one of our experienced excavation specialists.

If you have any questions or concerns about your upcoming appointment, please don't hesitate to contact our Project Manager:

{DEPT_HEAD_NAME}
Phone: {DEPT_HEAD_PHONE}

We look forward to serving you!

Best regards,
Hearn Plumbing, Heating & Air
"""

                msg = MIMEMultipart()
                msg['From'] = SARAH_EMAIL
                msg['To'] = customer_email
                msg['Subject'] = subject
                msg.attach(MIMEText(body, 'plain'))

                with smtplib.SMTP('smtp.office365.com', 587) as server:
                    server.starttls()
                    server.login(SARAH_EMAIL, SARAH_EMAIL_PASSWORD)
                    server.sendmail(SARAH_EMAIL, [customer_email], msg.as_string())
                    emails_sent_list.append({"to": customer_email, "type": "customer"})

                print(f"[Excavation] Thank you email sent to customer: {customer_email}")

            except Exception as e:
                print(f"[Excavation] Customer email failed: {e}")
        else:
            print(f"[Excavation] No customer email provided - skipping thank you email")

    else:
        print("[Excavation] Email not configured (missing SARAH_EMAIL or SARAH_EMAIL_PASSWORD)")

    print("\n" + "=" * 70)
    print("EXCAVATION JOB CREATION COMPLETE")
    print(f"  JET Job: {job_ids['jet']}")
    print(f"  Re-evaluate Job: {job_ids['reeval']}")
    print(f"  Final Payment Job: {job_ids['final']}")
    if closest_excavator:
        print(f"  Recommended excavator: {closest_excavator['name']} ({closest_excavator['distance']:.1f} mi)")
        print(f"  Excavator email: {closest_excavator.get('email') or 'NOT SET'}")
    print(f"  Emails sent: {len(emails_sent_list)}")
    for email_info in emails_sent_list:
        print(f"    - {email_info['type']}: {email_info['to']}")
    print(f"  Recommendation note: {'Posted' if recommendation_posted else 'Not posted'}")
    print("=" * 70 + "\n")

    return {
        "status": "success",
        "jet_job_id": job_ids["jet"],
        "reeval_job_id": job_ids["reeval"],
        "final_payment_job_id": job_ids["final"],
        "emails_sent": len(emails_sent_list),
        "emails_sent_to": emails_sent_list,
        "recommended_excavator": closest_excavator,
        "recommendation_posted": recommendation_posted,
        "message": f"Excavation jobs created: JET #{job_ids['jet']}, Re-eval #{job_ids['reeval']}, Final #{job_ids['final']}"
    }


def create_booking(customer_name, address, phone, email, issue_description,
                   appointment_time, appointment_start, appointment_end,
                   customer_type, is_homeowner=None, promotional_emails=None,
                   contact_preference=None, alternate_phone=None,
                   campaign_id=None, business_unit_id=None, is_emergency=None,
                   is_excavation=None, existing_customer_id=None, existing_location_id=None):

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

    # Normalize is_excavation parameter (can be bool or string)
    is_excavation_bool = False
    if is_excavation is not None:
        if isinstance(is_excavation, bool):
            is_excavation_bool = is_excavation
        elif isinstance(is_excavation, str):
            is_excavation_bool = is_excavation.lower() in ("true", "yes", "1")

    # If is_excavation is explicitly true, skip job type detection
    if is_excavation_bool:
        print(f"[ST] is_excavation=True - skipping job type detection, using excavation workflow")
        job_type_result = {"job_type_id": EXCAVATION_JOB_TYPE_IDS[0], "priority": "Urgent", "job_category": "Excavation"}
        detected_job_type_id = job_type_result["job_type_id"]
        detected_priority = job_type_result["priority"]
    else:
        # Normalize is_emergency for detection
        is_emergency_bool = False
        if is_emergency is not None:
            if isinstance(is_emergency, bool):
                is_emergency_bool = is_emergency
            elif isinstance(is_emergency, str):
                is_emergency_bool = is_emergency.lower() in ("true", "yes", "1")

        # Detect job type from issue description using AI (pass full context)
        job_type_result = detect_job_type(
            issue_description=issue_description,
            customer_type=customer_type,
            is_emergency=is_emergency_bool,
            is_excavation=is_excavation_bool,
            appointment_info=appointment_time
        )
        detected_job_type_id = job_type_result["job_type_id"]
        detected_priority = job_type_result["priority"]

        # HVAC jobs have job-type-specific business units - override zone/campaign BU
        if "business_unit_id" in job_type_result and job_type_result["business_unit_id"]:
            business_unit_id = job_type_result["business_unit_id"]
            bu_source = f"job_type ({job_type_result.get('business_unit_name', 'HVAC')})"
            print(f"[ST] Using job-type-specific BU: {job_type_result['business_unit_name']} (ID: {business_unit_id})")

    print(f"[ST] Using job_type_id: {detected_job_type_id}, priority: {detected_priority}")

    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }

    # Final safeguard: if street is still empty after parsing, extract from original address
    street_value = parsed_addr.get("street", "")
    if not street_value:
        print(f"[ST] WARNING: Parsed street is empty, extracting from original address")
        # Try to extract street from original address (everything before city/state/zip)
        import re
        street_part = address
        for remove in [parsed_addr.get("city", ""), parsed_addr.get("state", ""), parsed_addr.get("zip", "")]:
            if remove:
                street_part = re.sub(re.escape(remove), '', street_part, flags=re.IGNORECASE)
        street_part = re.sub(r'[,\s]+$', '', street_part).strip()
        street_part = re.sub(r'^[,\s]+', '', street_part).strip()
        if street_part:
            street_value = street_part
            print(f"[ST] Extracted street from original: {street_value}")
        else:
            # Last resort: use the full address as street
            street_value = address.split(',')[0].strip() if ',' in address else address
            print(f"[ST] Using first part of address as street: {street_value}")

    address_obj = {
        "street": street_value,
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

    # Track whether we need to create a new customer
    need_create_customer = True
    customer_id = None
    location_id = None

    # Check if existing customer ID was provided (from inbound lookup)
    if existing_customer_id and existing_location_id:
        # Verify the name matches before using existing customer
        # This prevents booking under wrong customer when caller gives different name
        print(f"[ST] Existing customer ID provided: {existing_customer_id}")
        print(f"[ST] Verifying name match with provided name: {customer_name}")

        # Fetch existing customer's name
        existing_cust_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers/{existing_customer_id}"
        existing_cust_resp = requests.get(existing_cust_url, headers=headers)

        use_existing = False
        existing_name = ""
        if existing_cust_resp.status_code == 200:
            existing_cust_data = existing_cust_resp.json()
            existing_name = existing_cust_data.get("name", "")
            print(f"[ST] Existing customer name: {existing_name}")

            # Compare names (case-insensitive, normalize spaces)
            name1 = ' '.join(customer_name.lower().split())
            name2 = ' '.join(existing_name.lower().split())

            # Check if names match (exact, contains, or first name match)
            if name1 == name2:
                use_existing = True
                print(f"[ST] Names match exactly")
            elif name1 in name2 or name2 in name1:
                use_existing = True
                print(f"[ST] Names partially match (one contains the other)")
            elif name1.split() and name2.split() and name1.split()[0] == name2.split()[0]:
                use_existing = True
                print(f"[ST] First names match")
            else:
                # Check for typos (1-2 char difference in similar length names)
                if abs(len(name1) - len(name2)) <= 2:
                    diff_count = sum(1 for a, b in zip(name1, name2) if a != b)
                    if diff_count <= 2:
                        use_existing = True
                        print(f"[ST] Names are similar (minor typo)")

        if use_existing:
            customer_id = existing_customer_id
            location_id = existing_location_id
            need_create_customer = False
            print(f"[ST] Using existing customer from inbound lookup: ID {customer_id}, Location {location_id}")

            # Update customer contacts if new phone/email provided
            if phone or email:
                update_customer_contacts(customer_id, phone, email, customer_name, headers)
        else:
            print(f"[ST] Name mismatch! Provided: '{customer_name}', Existing: '{existing_name}'")
            print(f"[ST] Will create new customer instead of using existing")
            need_create_customer = True
    else:
        # Step 0 - Search for existing customer by address
        print("[ST] Step 0 - Searching for existing customer by address...")
        existing_customer = lookup_customer_by_address(
            street=parsed_addr["street"],
            city=parsed_addr["city"],
            state=parsed_addr["state"],
            zip_code=parsed_addr["zip"]
        )

        if existing_customer.get("found"):
            # Check if name matches before using existing customer
            existing_name = existing_customer.get("customer_name", "")
            print(f"[ST] Found existing customer by address: {existing_name}")
            print(f"[ST] Comparing with provided name: {customer_name}")

            # Compare names (case-insensitive, normalize spaces)
            name1 = ' '.join(customer_name.lower().split())
            name2 = ' '.join(existing_name.lower().split())

            use_existing = False
            if name1 == name2:
                use_existing = True
                print(f"[ST] Names match exactly")
            elif name1 in name2 or name2 in name1:
                use_existing = True
                print(f"[ST] Names partially match")
            elif name1.split() and name2.split() and name1.split()[0] == name2.split()[0]:
                use_existing = True
                print(f"[ST] First names match")
            else:
                # Check for typos
                if abs(len(name1) - len(name2)) <= 2:
                    diff_count = sum(1 for a, b in zip(name1, name2) if a != b)
                    if diff_count <= 2:
                        use_existing = True
                        print(f"[ST] Names are similar (minor typo)")

            if use_existing:
                customer_id = existing_customer["customer_id"]
                location_id = existing_customer["location_id"]
                need_create_customer = False
                print(f"[ST] Using existing customer ID: {customer_id}, Location ID: {location_id}")
                # Update contacts if phone/email provided differs from existing
                if phone or email:
                    update_customer_contacts(customer_id, phone, email, customer_name, headers)
            else:
                print(f"[ST] Name mismatch! Provided: '{customer_name}', Existing: '{existing_name}'")
                print(f"[ST] Will create new customer at this address")
                need_create_customer = True
        else:
            print("[ST] No existing customer found by address")
            need_create_customer = True

    # Create new customer if needed
    if need_create_customer:
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

    # Check if this is an excavation job type - requires special 3-job workflow
    if detected_job_type_id in EXCAVATION_JOB_TYPE_IDS:
        print(f"[ST] Excavation job type detected (ID: {detected_job_type_id}) - using excavation workflow")
        # Build formatted address for email
        formatted_addr = f"{parsed_addr['street']}, {parsed_addr['city']}, {parsed_addr['state']} {parsed_addr['zip']}"

        excavation_result = create_excavation_jobs(
            customer_id=customer_id,
            location_id=location_id,
            summary=issue_description,
            campaign_id=campaign_id,
            business_unit_id=business_unit_id,
            appointment_time=appointment_time,
            customer_name=customer_name,
            customer_email=email,
            customer_phone=phone,
            formatted_address=formatted_addr
        )
        # Add customer info to result
        excavation_result["customer_id"] = customer_id
        excavation_result["location_id"] = location_id
        return excavation_result

    # Step 2 - Create Job (normal non-excavation workflow)
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
        # customFields removed - Tom's account doesn't use job custom fields
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
        "message": f"Your appointment has been booked successfully. Your job number is {job_id}. We will reach out before arrival. Thank you for calling Hearn Plumbing.",
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
                # ServiceTitan requires an address - use placeholder for leads
                placeholder_address = {
                    "street": "Address Unknown",
                    "city": "Pittsburgh",
                    "state": "PA",
                    "zip": "15222",
                    "country": "US"
                }
                customer_payload = {
                    "name": "Unknown Caller",
                    "type": "Residential",
                    "address": placeholder_address,
                    "contacts": [
                        {"type": "Phone", "value": cleaned_phone, "memo": "Unknown Caller"}
                    ],
                    "locations": [
                        {
                            "name": "Unknown Caller",
                            "address": placeholder_address,
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
    # Include phone number in summary so it's always visible, even if customer creation failed
    phone_display = from_number or cleaned_phone or "No phone"
    lead_summary = f"[{call_type.upper()}] {summary}"
    if not customer_id:
        # No customer was created/found - add phone to summary for visibility
        lead_summary = f"[{call_type.upper()}] [Phone: {phone_display}] {summary}"

    lead_payload = {
        "customerId": customer_id,
        "locationId": location_id,
        "summary": lead_summary,
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


def update_customer_contacts(customer_id: int, phone: str, email: str, customer_name: str, headers: dict):
    """
    Update existing customer contacts in ServiceTitan.
    ServiceTitan doesn't support PUT for contacts, so we DELETE the old contact and POST a new one.
    """
    if not phone and not email:
        return

    print(f"[ST] Updating contacts for customer {customer_id}")

    # Get existing contacts
    contacts_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers/{customer_id}/contacts"
    contacts_resp = requests.get(contacts_url, headers=headers)

    first_phone_contact = None
    first_email_contact = None

    if contacts_resp.status_code == 200:
        contacts = contacts_resp.json().get("data", [])
        for contact in contacts:
            if contact.get("type") == "Phone" and not first_phone_contact:
                first_phone_contact = contact
            elif contact.get("type") == "Email" and not first_email_contact:
                first_email_contact = contact

    # Clean provided phone
    cleaned_phone = clean_phone(phone) if phone else ""

    # Update phone contact (DELETE old + POST new)
    if cleaned_phone:
        if first_phone_contact:
            contact_id = first_phone_contact.get("id")
            old_value = first_phone_contact.get("value", "")
            if clean_phone(old_value) != cleaned_phone:
                print(f"[ST] Replacing phone: {old_value} -> {cleaned_phone}")
                # Delete old contact
                delete_url = f"{contacts_url}/{contact_id}"
                delete_resp = requests.delete(delete_url, headers=headers)
                if delete_resp.status_code == 200:
                    print(f"[ST] Deleted old phone contact {contact_id}")
                    # Add new contact
                    new_contact = {
                        "type": "Phone",
                        "value": cleaned_phone,
                        "memo": f"Updated by {customer_name}"
                    }
                    add_resp = requests.post(contacts_url, headers=headers, json=new_contact)
                    if add_resp.status_code in (200, 201):
                        print(f"[ST] Phone contact updated successfully")
                    else:
                        print(f"[ST] Failed to add new phone: {add_resp.status_code}")
                else:
                    print(f"[ST] Failed to delete old phone: {delete_resp.status_code}")
            else:
                print(f"[ST] Phone already matches: {cleaned_phone}")
        else:
            # No existing phone, add new one
            print(f"[ST] No existing phone contact, adding: {cleaned_phone}")
            new_contact = {
                "type": "Phone",
                "value": cleaned_phone,
                "memo": f"Added by {customer_name}"
            }
            add_resp = requests.post(contacts_url, headers=headers, json=new_contact)
            if add_resp.status_code in (200, 201):
                print(f"[ST] Phone contact added successfully")

    # Update email contact (DELETE old + POST new)
    if email and "@" in email:
        if first_email_contact:
            contact_id = first_email_contact.get("id")
            old_value = first_email_contact.get("value", "")
            if old_value.lower() != email.lower():
                print(f"[ST] Replacing email: {old_value} -> {email}")
                # Delete old contact
                delete_url = f"{contacts_url}/{contact_id}"
                delete_resp = requests.delete(delete_url, headers=headers)
                if delete_resp.status_code == 200:
                    print(f"[ST] Deleted old email contact {contact_id}")
                    # Add new contact
                    new_contact = {
                        "type": "Email",
                        "value": email,
                        "memo": f"Updated by {customer_name}"
                    }
                    add_resp = requests.post(contacts_url, headers=headers, json=new_contact)
                    if add_resp.status_code in (200, 201):
                        print(f"[ST] Email contact updated successfully")
                    else:
                        print(f"[ST] Failed to add new email: {add_resp.status_code}")
                else:
                    print(f"[ST] Failed to delete old email: {delete_resp.status_code}")
            else:
                print(f"[ST] Email already matches: {email}")
        else:
            # No existing email, add new one
            print(f"[ST] No existing email contact, adding: {email}")
            new_contact = {
                "type": "Email",
                "value": email,
                "memo": f"Added by {customer_name}"
            }
            add_resp = requests.post(contacts_url, headers=headers, json=new_contact)
            if add_resp.status_code in (200, 201):
                print(f"[ST] Email contact added successfully")


def lookup_customer_by_address(street: str, city: str, state: str, zip_code: str):
    """
    Look up an existing customer in ServiceTitan CRM by address.
    Searches locations to find a matching customer.
    Returns customerId and locationId if found.
    """
    print(f"[ST] Looking up customer by address: {street}, {city}, {state} {zip_code}")

    access_token = get_access_token()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "ST-App-Key": APP_KEY
    }

    # Search locations by address components
    url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/locations"
    params = {
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "pageSize": 5
    }

    response = requests.get(url, headers=headers, params=params)

    if response.status_code != 200:
        print(f"[ST] Location lookup failed: {response.status_code} - {response.text}")
        return {"found": False, "error": response.text}

    locations = response.json().get("data", [])

    if not locations:
        print(f"[ST] No existing customer found by address")
        return {"found": False}

    # Use the first matching location
    location = locations[0]
    customer_id = location.get("customerId")
    location_id = location.get("id")
    customer_name = location.get("name", "Unknown")

    print(f"[ST] Found existing customer by address: {customer_name} (ID: {customer_id}), Location: {location_id}")

    return {
        "found": True,
        "customer_id": customer_id,
        "location_id": location_id,
        "customer_name": customer_name,
        "location_data": location
    }


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
    Uses location search for better matching.
    """
    print(f"[ServiceTitan] Looking up customer by address: {address}")

    access_token = get_access_token()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "ST-App-Key": APP_KEY
    }

    # Parse address to get components
    parsed = parse_address(address)
    street = parsed.get("street", "")
    city = parsed.get("city", "")
    state = parsed.get("state", "")
    zip_code = parsed.get("zip", "")

    # Extract just the street number for flexible matching (e.g., "100" from "100 Ross Street")
    import re
    street_number = ""
    match = re.match(r'^(\d+)', street)
    if match:
        street_number = match.group(1)

    # Search locations by zip + street number (more flexible than exact street match)
    url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/locations"

    # Try with full street first
    response = requests.get(url, headers=headers, params={"street": street, "zip": zip_code, "pageSize": 5})

    locations = []
    if response.status_code == 200:
        locations = response.json().get("data", [])

    if not locations:
        print(f"[ServiceTitan] No customer found for address: {address}")
        return {"found": False, "message": "No customer found with this address"}

    # Get customer from location
    location = locations[0]
    customer_id = location.get("customerId")
    location_address = location.get("address", {})

    # Fetch full customer details
    cust_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers/{customer_id}"
    cust_resp = requests.get(cust_url, headers=headers)

    if cust_resp.status_code != 200:
        print(f"[ServiceTitan] Customer fetch failed: {cust_resp.status_code}")
        return {"found": False, "error": cust_resp.text}

    customer = cust_resp.json()
    customers = [customer]  # For compatibility with code below

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
