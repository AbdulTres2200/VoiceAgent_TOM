import os
import time
import requests
import usaddress
from dotenv import load_dotenv
import json

load_dotenv()


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
    state = DEFAULT_STATE
    state_match = re.search(r'\b([A-Z]{2})\b', address_string.upper())
    if state_match and state_match.group(1) in VALID_STATES:
        state = state_match.group(1)

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

# ServiceTitan Job Configuration (from your account)
JOB_TYPE_ID = int(os.getenv("JOB_TYPE_ID"))
BUSINESS_UNIT_ID = int(os.getenv("BUSINESS_UNIT_ID"))
CAMPAIGN_ID = int(os.getenv("CAMPAIGN_ID"))
JOB_PRIORITY = os.getenv("JOB_PRIORITY")
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY")
DEFAULT_ZIP = os.getenv("DEFAULT_ZIP")
DEFAULT_STATE = os.getenv("DEFAULT_STATE")

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


def clean_phone(phone_string):
    """
    Clean and validate phone number. Returns digits only if valid, empty string if invalid.
    """
    import re
    if not phone_string:
        return ""

    # Extract only digits
    digits = re.sub(r'\D', '', phone_string)

    # Valid US phone: 10 digits, or 11 starting with 1
    if len(digits) == 10:
        return digits
    elif len(digits) == 11 and digits.startswith('1'):
        return digits[1:]  # Remove leading 1

    return ""


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


def create_booking(customer_name, address, phone, email, issue_description,
                   appointment_time, appointment_start, appointment_end,
                   customer_type, is_homeowner=None, contact_preference=None,
                   alternate_phone=None):
    
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }
    
    # Parse address
    parsed_addr = parse_address(address)

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
    job_payload = {
        "customerId": customer_id,
        "locationId": location_id,
        "summary": issue_description,
        "jobTypeId": JOB_TYPE_ID,
        "businessUnitId": BUSINESS_UNIT_ID,
        "campaignId": CAMPAIGN_ID,
        "priority": JOB_PRIORITY,
        "appointments": [
            {
                "start": appointment_start,
                "end": appointment_end,
                "specialInstructions": appointment_time
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
    print(f"[ST] Job ID: {job_id}")

    # # Step 3 - Create Appointment
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

    return {
        "status": "success",
        "message": f"Your appointment has been booked successfully. Your job number is {job_id}. We will reach out before arrival. Thank you for calling Mr. Rooter.",
        "job_id": job_id,
        "customer_id": customer_id
    }

    


def lookup_customer_by_phone(phone: str):
    """
    Look up a customer in ServiceTitan CRM by phone number.
    Returns customer info, contacts, and recent job history.
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
