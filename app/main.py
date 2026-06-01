import os
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from app.routes import booking
from app.webhooks import retell_webhook
from app.webhooks.retell_webhook import is_lead_already_created, mark_lead_created
from app.services.retell import get_job_id_for_call, get_job_id_for_phone
from app.services.servicetitan_notes import post_job_note
from app.services.servicetitan import test_connection, lookup_customer_by_phone, explore_account, lookup_by_address, fetch_account_config, get_campaign_from_call, get_campaign_details, get_latest_call, get_call_details, test_telecom, get_live_call_campaign, get_zones, get_job_types, get_job_types_from_st, detect_job_type, store_live_call_info, parse_appointment_time, get_business_unit_by_zone, get_business_units_from_st, store_service_area_business_unit, store_service_area_address, store_service_area_bu_by_address, create_lead, get_access_token, TENANT_ID, APP_KEY, clean_phone, create_excavation_jobs, EXCAVATOR_EMAILS_BY_ID, send_excavation_email, get_all_campaigns, detect_campaign_from_referral
from app.services.service_area import check_service_area, get_service_area_zips, preload_service_area_cache
from app.services.email_notify import send_call_summary

load_dotenv()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
print(f"[Config] GOOGLE_MAPS_API_KEY: {'SET (' + str(len(GOOGLE_MAPS_API_KEY)) + ' chars)' if GOOGLE_MAPS_API_KEY else 'NOT SET'}")

# H+ Membership detection via Memberships API
def check_hplus_membership(location_id: int) -> bool:
    """
    Check if a location has active H+ membership.

    Logic:
    1. First check recurring services (fast, covers most H+ members)
    2. If none found, check memberships endpoint (catches new members without services yet)

    Returns True if location has H+ membership.
    """
    import requests as req
    try:
        token = get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "ST-App-Key": APP_KEY
        }

        # Method 1: Check recurring services (fast, most common)
        rs_resp = req.get(
            f"https://api.servicetitan.io/memberships/v2/tenant/{TENANT_ID}/recurring-services",
            headers=headers,
            params={"locationIds": str(location_id), "active": "true"}
        )
        if rs_resp.status_code == 200:
            services = [s for s in rs_resp.json().get("data", []) if s.get("locationId") == location_id]
            if len(services) > 0:
                print(f"[H+ Check] Location {location_id}: H+ Member (has {len(services)} recurring services)")
                return True

        # Method 2: Check memberships directly (for new members without services yet)
        mem_resp = req.get(
            f"https://api.servicetitan.io/memberships/v2/tenant/{TENANT_ID}/memberships",
            headers=headers,
            params={"status": "Active", "pageSize": 200}
        )
        if mem_resp.status_code == 200:
            memberships = mem_resp.json().get("data", [])
            location_memberships = [m for m in memberships if m.get("locationId") == location_id and m.get("status") == "Active"]
            if len(location_memberships) > 0:
                print(f"[H+ Check] Location {location_id}: H+ Member (has {len(location_memberships)} active memberships)")
                return True

        print(f"[H+ Check] Location {location_id}: NOT H+ Member")
        return False

    except Exception as e:
        print(f"[H+ Check] Error checking membership for location {location_id}: {e}")
    return False

app = FastAPI(
    title="ServiceTitan Voice Agent",
    description="Retell AI Integration for ServiceTitan",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(booking.router, tags=["Booking"])
app.include_router(retell_webhook.router, prefix="/webhook", tags=["Webhooks"])


@app.on_event("startup")
async def startup_event():
    """Pre-load caches on startup for fast first request."""
    # Pre-load service area zones
    zip_count = preload_service_area_cache()
    print(f"[Startup] Service area zones pre-loaded: {zip_count} zip codes cached for 24 hours")

    # Pre-load job types
    job_types = get_job_types_from_st()
    print(f"[Startup] Job types pre-loaded: {len(job_types)} types cached for 24 hours")

    # Pre-load business units
    units, _ = get_business_units_from_st()
    print(f"[Startup] Business units pre-loaded: {len(units)} units cached for 24 hours")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.post("/check-business-hours")
async def check_business_hours(request: Request):
    """
    Check business hours and return fee/eligibility info.

    Optional request body:
    - is_hplus_member: bool
    - days_since_last_service: int
    - is_emergency: bool (No A/C, No Heat - always eligible)
    """
    from app.services.business_hours import get_business_hours_info

    # Parse optional request body
    is_hplus_member = False
    days_since_last_service = None
    is_emergency = False

    try:
        data = await request.json()
        args = data.get('args', data)
        is_hplus_member = args.get('is_hplus_member', False)
        days_since_last_service = args.get('days_since_last_service')
        is_emergency = args.get('is_emergency', False)
    except:
        pass  # No body provided, use defaults

    result = get_business_hours_info(
        is_hplus_member=is_hplus_member,
        days_since_last_service=days_since_last_service,
        is_emergency=is_emergency
    )

    # Add transfer destination for emergency calls
    if result["period"] == "standard":
        result["transfer_to"] = "office"
    else:
        result["transfer_to"] = "after_hours_line"

    print(f"[BusinessHours] {result['current_time']} | Period: {result['period']} | Fee: ${result['fee']} | Eligible: {result['eligible']} | Emergency: {is_emergency}")
    return result


@app.post("/detect-campaign")
async def detect_campaign(request: Request):
    """
    Detect the best matching campaign based on customer's referral response.
    Uses AI to match "How did you hear about us?" answers to ServiceTitan campaigns.

    Request body:
    - referral_response: string (customer's answer, e.g., "Google", "a friend", "yard sign")

    Returns:
    - campaign_id: matched campaign ID
    - campaign_name: matched campaign name
    - confidence: high/medium/low
    - reason: explanation of the match
    """
    data = await request.json()
    args = data.get('args', data)
    referral_response = args.get('referral_response') or args.get('referral') or args.get('source') or ""

    if not referral_response:
        return {
            "campaign_id": None,
            "campaign_name": None,
            "confidence": "low",
            "reason": "No referral response provided"
        }

    result = detect_campaign_from_referral(referral_response)
    print(f"[DetectCampaign] '{referral_response}' -> {result['campaign_name']} (ID: {result['campaign_id']})")
    return result


@app.get("/test-campaigns")
async def test_campaigns():
    """
    Fetch all active campaigns from ServiceTitan.
    """
    campaigns = get_all_campaigns(force_refresh=True)
    return {
        "count": len(campaigns),
        "campaigns": campaigns
    }


@app.get("/test-st-connection")
async def test_servicetitan_connection():
    """
    Test ServiceTitan API connection.
    Verifies credentials by fetching employees list.
    """
    result = test_connection()
    return result


@app.get("/lookup-caller/{phone}")
async def lookup_caller(phone: str):
    """
    Look up a caller by phone number in ServiceTitan CRM.
    Returns customer info, contacts, and recent job history.
    """
    result = lookup_customer_by_phone(phone)
    return result


@app.post("/lookup-caller")
async def lookup_caller_post(request: Request):
    """
    Look up a caller by phone number (POST version for Retell AI).
    Accepts phone in request body with optional 'args' nesting.
    """
    data = await request.json()
    args = data.get('args', data)
    phone = args.get('phone') or args.get('caller_phone') or args.get('from_number')

    if not phone:
        return {"found": False, "error": "No phone number provided"}

    result = lookup_customer_by_phone(phone)
    return result


@app.get("/explore-st")
async def explore_servicetitan():
    """
    Explore ServiceTitan account structure.
    Returns job types, sample customers, and sample appointments.
    """
    result = explore_account()
    return result


# Cache for inbound webhook deduplication (from_number -> (timestamp, response))
import time
_inbound_cache = {}
_INBOUND_CACHE_TTL = 30  # seconds

# Cache for post-call webhook deduplication (call_id -> timestamp)
_postcall_processed = {}
_POSTCALL_CACHE_TTL = 300  # 5 minutes - prevent duplicate processing


@app.post("/inbound-webhook")
async def inbound_webhook(request: Request):
    """
    Retell AI webhook - handles BOTH call started AND call ended events.
    Retell sends all events to the same webhook URL.
    """
    data = await request.json()

    # DEBUG: Log all incoming webhook data to understand structure
    event_type = data.get("event", "")
    print(f"\n[Webhook DEBUG] ========================================")
    print(f"[Webhook DEBUG] Event type: '{event_type}'")
    print(f"[Webhook DEBUG] Top-level keys: {list(data.keys())}")
    if "call" in data:
        call_data = data.get("call", {})
        print(f"[Webhook DEBUG] call.call_id: {call_data.get('call_id', 'N/A')}")
        print(f"[Webhook DEBUG] call.call_status: {call_data.get('call_status', 'N/A')}")
        print(f"[Webhook DEBUG] call.end_timestamp: {call_data.get('end_timestamp', 'N/A')}")
        print(f"[Webhook DEBUG] Has transcript: {'transcript' in call_data}")
        print(f"[Webhook DEBUG] Has recording_url: {'recording_url' in call_data}")
    print(f"[Webhook DEBUG] ========================================\n")

    # If this is a call_ended event, process it like post-call webhook
    if event_type == "call_ended" or event_type == "call_analyzed":
        print(f"[Webhook] Received {event_type} event - processing as post-call")
        return await process_post_call(data)

    # Also check if call has ended based on call_status or presence of transcript
    call_data = data.get("call", {})
    call_status = call_data.get("call_status", "")
    has_transcript = bool(call_data.get("transcript"))
    has_recording = bool(call_data.get("recording_url"))

    if call_status == "ended" or (has_transcript and has_recording):
        print(f"[Webhook] Detected ended call (status={call_status}, has_transcript={has_transcript}) - processing as post-call")
        return await process_post_call(data)

    # Retell sends data under "call" or "call_inbound" depending on event type
    call_data = data.get("call", {}) or data.get("call_inbound", {})

    # Get from_number and to_number from Retell payload
    from_number = (
        call_data.get("from_number") or
        call_data.get("caller_number") or
        data.get("from_number") or
        ""
    )
    to_number = (
        call_data.get("to_number") or
        call_data.get("callee_number") or
        data.get("to_number") or
        ""
    )

    # Clean phone number
    cleaned_from_number = from_number.replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
    if cleaned_from_number.startswith("+1"):
        cleaned_from_number = cleaned_from_number[2:]
    elif cleaned_from_number.startswith("+"):
        cleaned_from_number = cleaned_from_number[1:]

    clean_phone = cleaned_from_number[-10:] if len(cleaned_from_number) >= 10 else cleaned_from_number

    # Bill Guntrum detection - excavation department head
    BILL_GUNTRUM_PHONE = "7242571514"
    is_bill_guntrum = clean_phone == BILL_GUNTRUM_PHONE

    # Cache the to_number for this caller (for campaign lookup during booking)
    if from_number and to_number:
        store_live_call_info(from_number, to_number)

    # Build minimal dynamic variables for fast response
    # Customer lookup happens later via lookup_by_address during the call
    dynamic_vars = {
        "to_number": to_number,
        "caller_number": cleaned_from_number,
        "is_bill_guntrum": "true" if is_bill_guntrum else "false"
    }

    print(f"[Inbound] {from_number} | Fast response (no lookup)")

    response = {
        "call_inbound": {
            "dynamic_variables": dynamic_vars
        }
    }

    return response


@app.post("/lookup-by-address")
async def lookup_by_address_endpoint(request: Request):
    """
    Look up a customer by street address in ServiceTitan CRM.
    Returns customer info for Retell AI.
    """
    data = await request.json()
    args = data.get('args', data)
    address = args.get('address') or args.get('street') or ""

    if not address:
        return {"found": False, "message": "No address provided"}

    result = lookup_by_address(address)

    if result.get("found"):
        customer = result.get("customer", {})
        addr = customer.get("address", {})
        recent_jobs = result.get("recent_jobs", [])

        address_str = f"{addr.get('street', '')} {addr.get('city', '')} {addr.get('state', '')}".strip()
        recent_job_str = ""
        if recent_jobs:
            job = recent_jobs[0]
            summary = job.get('summary', 'N/A').replace('\r', '').replace('\n', ' ').strip()
            recent_job_str = f"{summary} - {job.get('status', 'N/A')}"

        print(f"[AddressLookup] Found: {customer.get('name', '')} at {address_str}")

        return {
            "found": True,
            "customer_name": customer.get("name", ""),
            "customer_address": address_str,
            "customer_id": customer.get("id"),
            "recent_job": recent_job_str
        }
    else:
        print(f"[AddressLookup] Not found: {address}")
        return {
            "found": False,
            "message": "No customer found with this address"
        }


@app.get("/st-config")
async def get_servicetitan_config():
    """
    Fetch ServiceTitan account configuration.
    Returns job types, business units, sample customers, and sample jobs.
    """
    result = fetch_account_config()
    return result


@app.get("/test-campaign/{phone}")
async def test_campaign_lookup(phone: str):
    """
    Test campaign lookup by phone number.
    Returns campaign ID associated with the phone number.
    """
    campaign_id = get_campaign_from_call(phone)
    return {
        "phone": phone,
        "campaign_id": campaign_id
    }


@app.get("/campaign/{campaign_id}")
async def get_campaign(campaign_id: int):
    """
    Fetch campaign details by ID from ServiceTitan.
    Returns campaign name, category, and other details.
    """
    result = get_campaign_details(campaign_id)
    return result


@app.get("/latest-call")
async def latest_call():
    """
    Fetch the latest lead and latest call from ServiceTitan.
    Returns full response with campaign, from/to numbers, and all fields.
    """
    result = get_latest_call()
    return result


@app.get("/call-details/{call_id}")
async def call_details(call_id: int):
    """
    Fetch call details by ID from ServiceTitan.
    Returns full call information including campaign, from/to numbers.
    """
    result = get_call_details(call_id)
    return result


@app.get("/test-telecom")
async def test_telecom_endpoint():
    """
    Test telecom API endpoints to find call data with tracking numbers.
    """
    result = test_telecom()
    return result


@app.get("/test-live-call/{from_number}/{to_number}")
async def test_live_call(from_number: str, to_number: str):
    """
    Test live call campaign lookup.
    Returns campaign info found by matching from/to numbers.
    """
    result = get_live_call_campaign(from_number, to_number)
    return {
        "from_number": from_number,
        "to_number": to_number,
        "campaign_info": result
    }


@app.get("/test-zones")
async def test_zones():
    """
    Fetch all zones from ServiceTitan settings API.
    """
    result = get_zones()
    return result


@app.get("/test-job-types")
async def test_job_types():
    """
    Fetch all active job types from ServiceTitan.
    """
    result = get_job_types()
    return result


@app.get("/test-job-type")
async def test_job_type(issue: str):
    """
    Test job type detection for an issue description.
    Uses AI to match issue to best job type.
    """
    result = detect_job_type(issue)
    return {"issue": issue, "detected": result}


@app.post("/check-service-area")
async def check_service_area_endpoint(request: Request):
    """
    Check if an address is within the service area AND lookup existing customer.
    Returns service area status, parsed address, zone info, business unit, AND customer info.
    This combines check_service_area + lookup_by_address into one fast call.
    """
    data = await request.json()
    args = data.get('args', data)
    address = args.get('address') or args.get('street') or ""
    phone = args.get('phone') or args.get('caller_phone') or args.get('from_number') or ""

    print(f"[ServiceArea] Request data keys: {list(data.keys())}")
    print(f"[ServiceArea] Args keys: {list(args.keys()) if isinstance(args, dict) else 'not a dict'}")
    print(f"[ServiceArea] Phone extracted: '{phone}'")

    if not address:
        return {
            "in_service_area": False,
            "error": "No address provided"
        }

    result = check_service_area(address, GOOGLE_MAPS_API_KEY)

    # Cache the business unit for this phone number (for automatic lookup during booking)
    if phone and result.get("business_unit_id"):
        store_service_area_business_unit(
            phone=phone,
            business_unit_id=result["business_unit_id"],
            business_unit_name=result.get("business_unit_name", ""),
            zone_name=result.get("zone_name", "")
        )

    # ALWAYS cache business unit by address (works even without phone)
    if result.get("business_unit_id") and result.get("street") and result.get("zip_code"):
        store_service_area_bu_by_address(
            street=result.get("street", ""),
            zip_code=result.get("zip_code", ""),
            business_unit_id=result["business_unit_id"],
            business_unit_name=result.get("business_unit_name", ""),
            zone_name=result.get("zone_name", "")
        )

    # Cache the parsed address for this phone number (for lead/location creation)
    if phone and result.get("street"):
        store_service_area_address(
            phone=phone,
            street=result.get("street", ""),
            city=result.get("city", ""),
            state=result.get("state", ""),
            zip_code=result.get("zip_code", "")
        )

    # If in service area, also lookup existing customer using normalized street matching
    if result.get("in_service_area"):
        import re
        import requests as req

        # Get normalized street from Google result (e.g., "100 Ross Street")
        search_street = result.get("street", "").upper()
        zip_code = result.get("zip_code", "")

        if search_street and zip_code:
            # Extract street number and name for matching
            street_match = re.match(r'^(\d+)\s+(.+)', search_street)
            if street_match:
                search_number = street_match.group(1)
                search_name = street_match.group(2)
                # Normalize: remove ST/STREET/AVE/AVENUE etc
                search_name_normalized = re.sub(r'\b(STREET|ST|AVENUE|AVE|DRIVE|DR|ROAD|RD|LANE|LN|COURT|CT|PLACE|PL|BOULEVARD|BLVD)\b', '', search_name).strip()

                print(f"[CustomerLookup] Searching for: {search_street} in zip {zip_code}")

                access_token = get_access_token()
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "ST-App-Key": APP_KEY
                }

                url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/locations"
                # Search by street first (more specific)
                resp = req.get(url, headers=headers, params={"street": search_street, "zip": zip_code, "pageSize": 10})

                if resp.status_code == 200:
                    all_locations = resp.json().get("data", [])
                    print(f"[CustomerLookup] Found {len(all_locations)} locations matching street")

                    for loc in all_locations:
                        loc_addr = loc.get("address", {})
                        loc_street = loc_addr.get("street", "").upper()

                        # Extract location street number and name
                        loc_match = re.match(r'^(\d+)\s+(.+)', loc_street)
                        if loc_match:
                            loc_number = loc_match.group(1)
                            loc_name = loc_match.group(2)
                            loc_name_normalized = re.sub(r'\b(STREET|ST|AVENUE|AVE|DRIVE|DR|ROAD|RD|LANE|LN|COURT|CT|PLACE|PL|BOULEVARD|BLVD)\b', '', loc_name).strip()

                            # Match if: exact street number AND street name matches
                            if loc_number == search_number and search_name_normalized in loc_name_normalized:
                                # Found matching location
                                customer_id = loc.get("customerId")
                                location_id = loc.get("id")

                                # Fetch customer name
                                cust_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers/{customer_id}"
                                cust_resp = req.get(cust_url, headers=headers)

                                if cust_resp.status_code == 200:
                                    customer = cust_resp.json()

                                    # Skip inactive customers
                                    if not customer.get("active", True):
                                        print(f"[CustomerLookup] Skipping inactive customer {customer_id}")
                                        continue

                                    customer_name = customer.get("name", "")
                                    formatted_addr = f"{loc_addr.get('street', '')}, {loc_addr.get('city', '')}, {loc_addr.get('state', '')} {loc_addr.get('zip', '')}"

                                    # Check H+ membership via Memberships API (recurring services)
                                    is_hplus_member = check_hplus_membership(location_id)
                                    print(f"[CustomerLookup] Location {location_id} | H+ Member: {is_hplus_member}")

                                    # Get customer contacts (phone and email)
                                    contacts_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/customers/{customer_id}/contacts"
                                    contacts_resp = req.get(contacts_url, headers=headers)
                                    customer_phone = ""
                                    customer_email = ""
                                    if contacts_resp.status_code == 200:
                                        contacts = contacts_resp.json().get("data", [])
                                        for contact in contacts:
                                            if contact.get("type") == "Phone" and not customer_phone:
                                                customer_phone = contact.get("value", "")
                                            elif contact.get("type") == "Email" and not customer_email:
                                                customer_email = contact.get("value", "")

                                    result["found"] = True
                                    result["customer_id"] = customer_id
                                    result["location_id"] = location_id
                                    result["customer_name"] = customer_name
                                    result["customer_phone"] = customer_phone
                                    result["customer_email"] = customer_email
                                    result["formatted_address"] = formatted_addr.upper()
                                    result["is_hplus_member"] = is_hplus_member
                                    print(f"[CustomerLookup] Match: {customer_name} | {customer_phone} | H+: {is_hplus_member}")
                                    break

                    if not result.get("found"):
                        result["found"] = False
                        result["is_hplus_member"] = False
                        print(f"[CustomerLookup] No match found")

    return result


@app.get("/test-service-area/{address:path}")
async def test_service_area(address: str):
    """
    Test service area check for an address.
    """
    result = check_service_area(address, GOOGLE_MAPS_API_KEY)
    return result


@app.get("/test-service-zips")
async def test_service_zips():
    """
    Get all service area zip codes from ServiceTitan zones.
    """
    zips, zip_to_zone = get_service_area_zips()
    return {
        "total_zips": len(zips),
        "zips": sorted(list(zips)),
        "zip_to_zone": zip_to_zone
    }


@app.get("/test-job-fields")
async def test_job_fields():
    """
    Test job-related field endpoints in ServiceTitan.
    Fetches cancel reasons, custom fields, and job-specific custom fields.
    """
    import requests
    from app.services.servicetitan import get_access_token, TENANT_ID, APP_KEY
    import json

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    job_id = 1811441646
    results = {}

    # 1. Job Cancel Reasons
    print("\n" + "=" * 70)
    print("1. JOB CANCEL REASONS")
    print("=" * 70)
    url1 = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/job-cancel-reasons"
    print(f"GET {url1}")
    resp1 = requests.get(url1, headers=headers)
    print(f"Status: {resp1.status_code}")
    print(f"Response: {json.dumps(resp1.json() if resp1.status_code == 200 else resp1.text, indent=2)}")
    results["job_cancel_reasons"] = {
        "url": url1,
        "status": resp1.status_code,
        "response": resp1.json() if resp1.status_code == 200 else resp1.text
    }

    # 2. Custom Fields for Job object type
    print("\n" + "=" * 70)
    print("2. CUSTOM FIELDS (objectType=Job)")
    print("=" * 70)
    url2 = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/custom-fields?objectType=Job"
    print(f"GET {url2}")
    resp2 = requests.get(url2, headers=headers)
    print(f"Status: {resp2.status_code}")
    print(f"Response: {json.dumps(resp2.json() if resp2.status_code == 200 else resp2.text, indent=2)}")
    results["custom_fields_job"] = {
        "url": url2,
        "status": resp2.status_code,
        "response": resp2.json() if resp2.status_code == 200 else resp2.text
    }

    # 3. Custom Fields for specific job
    print("\n" + "=" * 70)
    print(f"3. JOB CUSTOM FIELDS (job_id={job_id})")
    print("=" * 70)
    url3 = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs/{job_id}/custom-fields"
    print(f"GET {url3}")
    resp3 = requests.get(url3, headers=headers)
    print(f"Status: {resp3.status_code}")
    print(f"Response: {json.dumps(resp3.json() if resp3.status_code == 200 else resp3.text, indent=2)}")
    results["job_custom_fields"] = {
        "url": url3,
        "status": resp3.status_code,
        "response": resp3.json() if resp3.status_code == 200 else resp3.text
    }

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)

    return results


@app.get("/test-scheduling")
async def test_scheduling():
    """
    Test appointment time parsing with various inputs.
    Returns parsed results for each test case.
    """
    test_inputs = [
        "morning window 8-12",
        "afternoon window 12-4",
        "evening window 4-8",
        "emergency",
        "tomorrow morning window 8-12",
        "friday evening window 4-8",
        "asap",
    ]

    results = []
    for input_str in test_inputs:
        result = parse_appointment_time(input_str)
        results.append({
            "input": input_str,
            "start": result["start"],
            "end": result["end"],
            "window": result["window"]
        })

    return {
        "test_cases": results
    }


@app.get("/test-business-unit")
async def test_business_unit():
    """
    Test business unit lookup by zone name.
    Returns mapping results for various counties.
    """
    # First, fetch and display all business units
    units, name_to_id = get_business_units_from_st(force_refresh=True)

    # Test cases from the county mapping
    test_zones = [
        "Allegheny County",
        "Erie County",
        "Monongalia County",
        "Belmont County",
        "Mercer County",
        "Fayette County",
        "Lawrence County",
        "Butler County",
        "Westmoreland County",
        "Ohio County",
    ]

    results = []
    for zone in test_zones:
        bu_info = get_business_unit_by_zone(zone)
        results.append({
            "zone": zone,
            "business_unit_id": bu_info.get("business_unit_id"),
            "business_unit_name": bu_info.get("business_unit_name")
        })

    return {
        "business_units_in_st": [{"id": u.get("id"), "name": u.get("name")} for u in units],
        "zone_mappings": results
    }


@app.get("/test-call-reasons")
async def test_call_reasons():
    """
    Test multiple call reasons API endpoints in ServiceTitan.
    Tries CRM leads call-reasons, settings call-reasons, and leads list.
    """
    import requests
    import json
    from app.services.servicetitan import get_access_token, TENANT_ID, APP_KEY

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY
    }

    results = {}

    # 1. CRM Leads Call Reasons
    print("\n" + "=" * 70)
    print("1. CRM LEADS CALL REASONS")
    print("=" * 70)
    url1 = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/leads/call-reasons?pageSize=50"
    print(f"GET {url1}")
    resp1 = requests.get(url1, headers=headers)
    print(f"Status: {resp1.status_code}")
    response1 = resp1.json() if resp1.status_code == 200 else resp1.text
    print(f"Response: {json.dumps(response1, indent=2)[:2000]}")
    results["crm_leads_call_reasons"] = {
        "url": url1,
        "status": resp1.status_code,
        "response": response1
    }

    # 2. Settings Call Reasons
    print("\n" + "=" * 70)
    print("2. SETTINGS CALL REASONS")
    print("=" * 70)
    url2 = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/call-reasons?pageSize=50"
    print(f"GET {url2}")
    resp2 = requests.get(url2, headers=headers)
    print(f"Status: {resp2.status_code}")
    response2 = resp2.json() if resp2.status_code == 200 else resp2.text
    print(f"Response: {json.dumps(response2, indent=2)[:2000]}")
    results["settings_call_reasons"] = {
        "url": url2,
        "status": resp2.status_code,
        "response": response2
    }

    # 3. Leads List (to see lead structure)
    print("\n" + "=" * 70)
    print("3. LEADS LIST (sample structure)")
    print("=" * 70)
    url3 = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/leads?pageSize=3"
    print(f"GET {url3}")
    resp3 = requests.get(url3, headers=headers)
    print(f"Status: {resp3.status_code}")
    response3 = resp3.json() if resp3.status_code == 200 else resp3.text
    print(f"Response: {json.dumps(response3, indent=2)[:2000]}")
    results["leads_sample"] = {
        "url": url3,
        "status": resp3.status_code,
        "response": response3
    }

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)

    return results


@app.get("/test-create-lead")
async def test_create_lead():
    """
    Test lead creation in ServiceTitan CRM.
    Creates a test lead for an inquiry call.
    """
    lead_id = create_lead(
        call_type="INQUIRY",
        summary="Caller asked about water heater pricing, not ready to book",
        from_number="4125550001",
        campaign_id=1410706053,
        business_unit_id=1239
    )

    if lead_id:
        return {
            "success": True,
            "lead_id": lead_id,
            "message": "Test lead created successfully"
        }
    else:
        return {
            "success": False,
            "error": "Failed to create lead"
        }


@app.get("/test-lead-with-address")
async def test_lead_with_address():
    """
    Test lead creation for customer with no location but cached address.
    Simulates the failed JONES, ANN scenario.
    """
    # Step 1: Cache the address (like check-service-area does)
    store_service_area_address(
        phone="7245538700",
        street="408 Walter Street",
        city="Yorkville",
        state="OH",
        zip_code="43971"
    )

    # Step 2: Create lead (customer JONES, ANN has no locations)
    lead_id = create_lead(
        call_type="INQUIRY",
        summary="The caller reported a backed-up drain in their basement. Address is outside service area, transferred to regional specialists.",
        from_number="7245538700",
        campaign_id=1410706053,
        business_unit_id=1239
    )

    if lead_id:
        return {
            "success": True,
            "lead_id": lead_id,
            "message": "Lead created successfully with cached address"
        }
    else:
        return {
            "success": False,
            "error": "Failed to create lead"
        }


async def process_post_call(data: dict):
    """
    Process post-call data - analyzes transcript, attaches recording to job or creates lead.
    Called from both /post-call-webhook and /inbound-webhook (for call_ended events).
    """
    import requests
    from datetime import datetime, timedelta, timezone
    from openai import OpenAI

    # Extract call data from Retell payload
    call_data = data.get("call", {})
    call_id = call_data.get("call_id", "unknown")

    # Check deduplication - prevent processing same call multiple times
    now = time.time()
    if call_id in _postcall_processed:
        cached_time = _postcall_processed[call_id]
        if now - cached_time < _POSTCALL_CACHE_TTL:
            print(f"[PostCall] Duplicate call {call_id}, already processed {int(now - cached_time)}s ago, skipping")
            return {"status": "ok"}

    # Mark as processed
    _postcall_processed[call_id] = now

    # Clean up old cache entries
    expired_keys = [k for k, v in _postcall_processed.items() if now - v > _POSTCALL_CACHE_TTL]
    for k in expired_keys:
        del _postcall_processed[k]

    recording_url = call_data.get("recording_url", "")
    transcript = call_data.get("transcript", "")
    from_number = call_data.get("from_number", "")
    to_number = call_data.get("to_number", "")
    start_timestamp = call_data.get("start_timestamp")
    end_timestamp = call_data.get("end_timestamp")

    # Calculate duration
    duration_seconds = 0
    if start_timestamp and end_timestamp:
        duration_seconds = round((end_timestamp - start_timestamp) / 1000)

    # Extract dynamic variables
    dynamic_variables = call_data.get("retell_llm_dynamic_variables", {})
    collected_variables = call_data.get("collected_dynamic_variables", {})
    customer_name = dynamic_variables.get("customer_name", "Unknown")
    campaign_id = dynamic_variables.get("campaign_id", 1410706053)
    business_unit_id = dynamic_variables.get("business_unit_id", 1239)

    # Clean phone number
    cleaned_from_number = clean_phone(from_number)

    print("\n")
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║           POST CALL WEBHOOK RECEIVED                         ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print(f"║  Call ID:    {call_id:<47} ║")
    print(f"║  From:       {from_number:<47} ║")
    print(f"║  Duration:   {duration_seconds}s{' ':<44}║")
    print(f"║  Transcript: {'Yes' if transcript else 'No':<47} ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    # Skip processing if call hasn't ended (no duration and no transcript)
    if duration_seconds == 0 and not transcript:
        print("[PostCall] Call still in progress (0s duration, no transcript) - skipping")
        # Remove from processed cache so it can be processed later when call ends
        if call_id in _postcall_processed:
            del _postcall_processed[call_id]
        return {"status": "ok"}

    # Default values
    booking_made = "no"
    call_type = "OTHER"
    summary = "Call transcript analysis unavailable"
    action_result = "None"

    # PRE-CHECK: If we have a call_id -> job_id mapping, we KNOW a booking was made
    # This is more reliable than AI analysis
    mapped_job_id = get_job_id_for_call(call_id)
    if mapped_job_id:
        print(f"[PostCall] Found mapping for call -> job {mapped_job_id}")
        booking_made = "yes"  # Override - we know booking was made
    else:
        # FALLBACK: Try phone-based mapping (from_number -> job_id)
        phone_mapped_job_id = get_job_id_for_phone(from_number)
        if phone_mapped_job_id:
            print(f"[PostCall] Found mapping for phone {from_number} -> job {phone_mapped_job_id}")
            mapped_job_id = phone_mapped_job_id
            booking_made = "yes"

    # Check if transcript is empty or too short (no meaningful conversation)
    transcript_text = (transcript or "").strip()
    if not transcript_text or len(transcript_text) < 50:
        # No transcript or very short = caller didn't speak
        call_type = "SILENT"
        summary = "No transcript available - caller did not speak or hung up immediately"
        print(f"[PostCall] No/short transcript - marking as SILENT")

    # Analyze transcript with OpenAI
    elif transcript_text:
        try:
            print("[PostCall] Analyzing transcript with AI...")
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=150,
                temperature=0,
                messages=[
                    {
                        "role": "system",
                        "content": """Analyze this plumbing company call transcript and respond in EXACTLY this format:
BOOKING_MADE:yes or no
CALL_TYPE:BOOKING or INQUIRY or VENDOR or INVOICING or FOLLOWUP or SPAM or SILENT or OTHER
SUMMARY:Brief 2-3 sentence summary of the call

Use SPAM for: sales calls, solicitation, marketing pitches, business listing verification, SEO services, Google verification scams, robocalls, or any unsolicited promotional calls.
Use SILENT for: calls where the caller said nothing, immediate hangups, or no meaningful conversation occurred."""
                    },
                    {
                        "role": "user",
                        "content": f"Transcript:\n{transcript}"
                    }
                ]
            )

            ai_response = response.choices[0].message.content.strip()
            print(f"[PostCall] AI response:\n{ai_response}")

            # Parse the response
            for line in ai_response.split("\n"):
                line = line.strip()
                if line.startswith("BOOKING_MADE:"):
                    # Don't override if we already know from mapping that booking was made
                    if not mapped_job_id:
                        booking_made = line.replace("BOOKING_MADE:", "").strip().lower()
                elif line.startswith("CALL_TYPE:"):
                    call_type = line.replace("CALL_TYPE:", "").strip().upper()
                elif line.startswith("SUMMARY:"):
                    summary = line.replace("SUMMARY:", "").strip()

        except Exception as e:
            print(f"[PostCall] AI analysis failed: {e}")

    # Get ST API headers
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }

    # Build note text
    note_text = f"""=== RETELL AI CALL RECORDING ===

Call ID: {call_id}
From: {from_number}
Duration: {duration_seconds}s
Recording: {recording_url}

=== TRANSCRIPT ===
{transcript}"""

    if booking_made == "yes":
        # FIRST: Check our call_id -> job_id mapping (most reliable)
        job_id = get_job_id_for_call(call_id)

        # SECOND: Check phone-based mapping (from_number -> job_id)
        if not job_id:
            job_id = get_job_id_for_phone(from_number)
            if job_id:
                print(f"[PostCall] Found job from phone mapping: {job_id}")

        if job_id:
            print(f"[PostCall] Found job from call mapping: {job_id}")
            # Add note to job
            success = post_job_note(str(job_id), note_text)
            if success:
                print(f"[PostCall] Booking detected - attached to job {job_id} (from mapping)")
                action_result = f"Job {job_id}"
            else:
                print(f"[PostCall] Failed to add note to job {job_id}")
                action_result = f"Job {job_id} (note failed)"
        else:
            # FALLBACK: Search for recent jobs
            print(f"[PostCall] No mapping found, searching for recent jobs...")

            jobs_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs"
            found_job_id = None

            # First try: search by caller's phone
            if cleaned_from_number:
                params = {"pageSize": 5, "orderBy": "Id", "orderByDirection": "desc", "phone": cleaned_from_number}
                jobs_resp = requests.get(jobs_url, headers=headers, params=params)

                if jobs_resp.status_code == 200:
                    jobs = jobs_resp.json().get("data", [])
                    ten_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=10)

                    for job in jobs:
                        created_on = job.get("createdOn", "")
                        if created_on:
                            try:
                                job_created = datetime.fromisoformat(created_on.replace("Z", "+00:00"))
                                if job_created > ten_minutes_ago:
                                    found_job_id = job.get("id")
                                    print(f"[PostCall] Found recent job by caller phone: {found_job_id}")
                                    break
                            except Exception as e:
                                print(f"[PostCall] Error parsing job date: {e}")

            # Second try: search recent jobs WITHOUT phone filter (for when caller != customer)
            if not found_job_id:
                print(f"[PostCall] No match by caller phone, checking most recent jobs...")
                params = {"pageSize": 10, "orderBy": "Id", "orderByDirection": "desc"}
                jobs_resp = requests.get(jobs_url, headers=headers, params=params)

                if jobs_resp.status_code == 200:
                    jobs = jobs_resp.json().get("data", [])
                    ten_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=10)

                    for job in jobs:
                        created_on = job.get("createdOn", "")
                        if created_on:
                            try:
                                job_created = datetime.fromisoformat(created_on.replace("Z", "+00:00"))
                                if job_created > ten_minutes_ago:
                                    found_job_id = job.get("id")
                                    print(f"[PostCall] Found recent job by time: {found_job_id} (created {created_on})")
                                    break
                            except Exception as e:
                                print(f"[PostCall] Error parsing job date: {e}")

            if found_job_id:
                # Add note to job
                note_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs/{found_job_id}/notes"
                note_resp = requests.post(note_url, headers=headers, json={"text": note_text})

                if note_resp.status_code in (200, 201):
                    print(f"[PostCall] Booking detected - attaching to job {found_job_id}")
                    action_result = f"Job {found_job_id}"
                else:
                    print(f"[PostCall] Failed to add note to job: {note_resp.status_code} - {note_resp.text}")
                    action_result = f"Job {found_job_id} (note failed)"
            else:
                print("[PostCall] No recent job found, creating lead instead")
                booking_made = "no"  # Fall through to lead creation

    if booking_made != "yes":
        # Skip lead creation for spam calls only
        if call_type == "SPAM":
            print(f"[PostCall] Skipping lead creation - {call_type} call")
            action_result = f"Skipped ({call_type})"
        # Check if lead was already created by another webhook
        elif is_lead_already_created(call_id):
            print(f"[PostCall] Lead already created for this call (dedup), skipping")
            action_result = "Lead already created (dedup)"
        else:
            # For SILENT calls, add tag to summary for easy filtering
            lead_summary = summary
            if call_type == "SILENT":
                lead_summary = "[NO RESPONSE] Caller did not speak or hung up immediately"

            # Create lead for non-booking call
            print(f"[PostCall] Non-booking call - creating lead...")

            lead_id = create_lead(
                call_type=call_type,
                summary=lead_summary,
                from_number=cleaned_from_number,
                campaign_id=int(campaign_id),
                business_unit_id=int(business_unit_id)
            )

            if lead_id:
                # Mark as created to prevent duplicates
                mark_lead_created(call_id)
                # Add note to lead with recording and transcript
                note_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/leads/{lead_id}/notes"
                note_resp = requests.post(note_url, headers=headers, json={"text": note_text})

                if note_resp.status_code in (200, 201):
                    print(f"[PostCall] Lead {lead_id} created")
                    action_result = f"Lead {lead_id}"
                else:
                    print(f"[PostCall] Lead {lead_id} created (note failed)")
                    action_result = f"Lead {lead_id} (note failed)"

                # Send email notification for lead
                send_call_summary(
                    call_type=call_type,
                    customer_name="Unknown",
                    phone=cleaned_from_number,
                    issue=summary,
                    lead_id=str(lead_id)
                )
            else:
                print("[PostCall] Lead creation failed")
                action_result = "Lead creation failed"

    # Simple summary log
    print(f"[PostCall] {from_number} | {call_type} | {action_result}")

    return {"status": "ok"}


@app.post("/post-call-webhook")
async def post_call_webhook(request: Request):
    """
    Retell AI post-call webhook endpoint.
    Wrapper that calls process_post_call with the request data.
    """
    data = await request.json()
    return await process_post_call(data)


@app.get("/test-technicians")
async def test_technicians():
    """
    Test endpoint to fetch both employees and technicians from ServiceTitan.
    Returns full details including id, name, roles, skills, custom fields.
    """
    import requests
    import json

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }

    results = {}

    # 1. Fetch Employees
    print("\n" + "=" * 70)
    print("1. EMPLOYEES")
    print("=" * 70)

    employees_url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/employees"
    all_employees = []
    page = 1

    while True:
        params = {"pageSize": 100, "active": "true", "page": page}
        print(f"  GET {employees_url} (page {page})")
        resp = requests.get(employees_url, headers=headers, params=params)
        print(f"  Status: {resp.status_code}")

        if resp.status_code != 200:
            print(f"  Error: {resp.text[:500]}")
            break

        data = resp.json().get("data", [])
        if not data:
            break

        for emp in data:
            employee_info = {
                "id": emp.get("id"),
                "name": emp.get("name"),
                "role": emp.get("role"),
                "roleIds": emp.get("roleIds", []),
                "email": emp.get("email"),
                "phoneNumber": emp.get("phoneNumber"),
                "active": emp.get("active"),
                "customFields": emp.get("customFields", [])
            }
            all_employees.append(employee_info)
            print(f"    - {emp.get('name')} (ID: {emp.get('id')}, Role: {emp.get('role')})")

        if len(data) < 100:
            break
        page += 1

    results["employees"] = {
        "count": len(all_employees),
        "data": all_employees
    }

    # 2. Fetch Technicians
    print("\n" + "=" * 70)
    print("2. TECHNICIANS")
    print("=" * 70)

    technicians_url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/technicians"
    all_technicians = []
    page = 1

    while True:
        params = {"pageSize": 100, "active": "true", "page": page}
        print(f"  GET {technicians_url} (page {page})")
        resp = requests.get(technicians_url, headers=headers, params=params)
        print(f"  Status: {resp.status_code}")

        if resp.status_code != 200:
            print(f"  Error: {resp.text[:500]}")
            break

        data = resp.json().get("data", [])
        if not data:
            break

        for tech in data:
            # Extract custom fields (skills)
            custom_fields = {}
            for cf in tech.get("customFields", []):
                cf_name = cf.get("name", f"typeId_{cf.get('typeId')}")
                custom_fields[cf_name] = cf.get("value")

            tech_info = {
                "id": tech.get("id"),
                "name": tech.get("name"),
                "status": tech.get("status"),
                "active": tech.get("active"),
                "zoneIds": tech.get("zoneIds", []),
                "businessUnitId": tech.get("businessUnitId"),
                "location": tech.get("location"),
                "customFields": custom_fields,
                "rawCustomFields": tech.get("customFields", [])
            }
            all_technicians.append(tech_info)

            # Print summary
            skills_str = ", ".join([f"{k}={v}" for k, v in custom_fields.items() if "Skill" in k])
            print(f"    - {tech.get('name')} (ID: {tech.get('id')}, Status: {tech.get('status')})")
            if skills_str:
                print(f"      Skills: {skills_str}")

        if len(data) < 100:
            break
        page += 1

    results["technicians"] = {
        "count": len(all_technicians),
        "data": all_technicians
    }

    print("\n" + "=" * 70)
    print(f"SUMMARY: {len(all_employees)} employees, {len(all_technicians)} technicians")
    print("=" * 70 + "\n")

    return results


@app.get("/test-excavation")
async def test_excavation(customer_email: str = None):
    """
    Test excavation job creation workflow.
    Creates 3 jobs (JET, Re-evaluate, Final Payment) and sends email notifications.
    Uses existing test customer.

    Query params:
        customer_email: Optional email for customer thank you email (default: none)
    """
    result = create_excavation_jobs(
        customer_id=1811510751,  # Existing test customer
        location_id=1811510759,
        summary="Excavation needed - pipe burst outside",
        campaign_id=1410706053,
        business_unit_id=1239,
        appointment_time="morning window 8-12",
        customer_name="Test Customer",
        customer_email=customer_email,
        customer_phone="4121234567",
        formatted_address="100 Ross Street, Pittsburgh, PA 15219"
    )
    return result


@app.get("/test-email")
async def test_email(to: str = None):
    """
    Test excavation email sending with dummy data.

    Query params:
        to: Optional email address to send to. If not provided, uses EXCAVATION_TEAM_EMAILS.
    """
    # Determine recipients
    from app.services.servicetitan import EXCAVATION_TEAM_EMAILS
    if to:
        recipients = [to]
    else:
        recipients = EXCAVATION_TEAM_EMAILS.copy()

    print(f"\n[Email Test] Attempting to send to {recipients}")

    # Call send_excavation_email with dummy test data
    result = send_excavation_email(
        customer_name="Test Customer",
        formatted_address="100 Ross Street, Pittsburgh, PA 15219",
        phone="4121234567",
        customer_email="test@example.com",
        issue_description="Test excavation - collapsed sewer line",
        appointment_time="morning window 8-12",
        city="Pittsburgh",
        jet_job_id=9999999,
        reeval_job_id=9999998,
        final_job_id=9999997,
        technician_name="840 Christopher Billings",
        technician_distance=6.5,
        technician_status="Idle",
        recipients=recipients
    )

    # Log result
    if result["status"] == "success":
        print(f"[Email Test] Result: success")
    else:
        print(f"[Email Test] Result: failed")
        print(f"[Email Test] Error: {result.get('error')}")

    return result


# Custom field type IDs for technicians
TECH_FIELD_IDS = {
    "Dispatchable": 1812958436,
    "Skill_Sewers_Mainline": 1812962532,
    "Skill_Water_Heaters": 1812948478,
    "Skill_Misc_Plumbing": 1812962533,
    "Skill_Gas_Lines": 1812961639,
    "Skill_Well_Pump": 1812943717,
}

# Employee cache (5 minute TTL) for email/phone lookup
import time as _time
_employee_cache = {"data": {}, "expires_at": 0}
_EMPLOYEE_CACHE_TTL = 300  # 5 minutes


def _get_employees_cached():
    """Fetch employees from ST with 5-minute cache. Returns dict keyed by name."""
    import requests
    current_time = _time.time()

    if _employee_cache["data"] and current_time < _employee_cache["expires_at"]:
        return _employee_cache["data"]

    print("[API] Fetching employees from ServiceTitan...")
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }

    employees_by_name = {}
    page = 1

    while True:
        url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/employees"
        params = {"pageSize": 200, "page": page, "active": "true"}
        resp = requests.get(url, headers=headers, params=params)

        if resp.status_code != 200:
            print(f"[API] Failed to fetch employees: {resp.status_code}")
            break

        data = resp.json().get("data", [])
        if not data:
            break

        for emp in data:
            name = emp.get("name", "").strip()
            if name:
                employees_by_name[name] = {
                    "email": emp.get("email"),
                    "phoneNumber": emp.get("phoneNumber")
                }

        if len(data) < 200:
            break
        page += 1

    _employee_cache["data"] = employees_by_name
    _employee_cache["expires_at"] = current_time + _EMPLOYEE_CACHE_TTL
    print(f"[API] Cached {len(employees_by_name)} employees for 5 minutes")
    if employees_by_name:
        sample = list(employees_by_name.keys())[:3]
        print(f"[API] Sample employee names: {sample}")

    return employees_by_name


@app.get("/api/debug/employees")
async def debug_employees():
    """Debug endpoint to see employee data."""
    employees = _get_employees_cached()
    return {
        "count": len(employees),
        "sample_names": list(employees.keys())[:20],
        "sample_data": {k: employees[k] for k in list(employees.keys())[:5]}
    }


@app.get("/api/debug/technician-raw")
async def debug_technician_raw():
    """Debug endpoint to see raw technician data from ST."""
    import requests
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }
    url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/technicians"
    resp = requests.get(url, headers=headers, params={"pageSize": 2})
    if resp.status_code == 200:
        data = resp.json().get("data", [])
        return {"raw_technicians": data}
    return {"error": resp.status_code, "text": resp.text[:500]}


@app.get("/api/technicians")
async def get_technicians():
    """
    Fetch all technicians from ServiceTitan with email/phone from employees.
    Returns id, name, email, phone, status, zoneIds, businessUnitId, location, and all skill custom fields.
    """
    import requests

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }

    print("\n" + "=" * 70)
    print("FETCHING TECHNICIANS")
    print("=" * 70)

    all_technicians = []
    page = 1

    while True:
        url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/technicians"
        params = {"pageSize": 100, "page": page}
        print(f"  GET {url} (page {page})")
        resp = requests.get(url, headers=headers, params=params)

        if resp.status_code != 200:
            print(f"  Error: {resp.status_code} - {resp.text[:200]}")
            return {"error": f"Failed to fetch technicians: {resp.status_code}"}

        data = resp.json().get("data", [])
        if not data:
            break

        for tech in data:
            # Extract custom fields into readable format
            skills = {}
            raw_custom_fields = {}
            for cf in tech.get("customFields", []):
                cf_name = cf.get("name", "")
                skills[cf_name] = cf.get("value")
                raw_custom_fields[cf_name] = cf.get("typeId")

            tech_info = {
                "id": tech.get("id"),
                "name": tech.get("name", ""),
                "email": tech.get("email"),  # Direct from technician record
                "phone": tech.get("phoneNumber"),  # Direct from technician record
                "status": tech.get("status"),
                "active": tech.get("active"),
                "zoneIds": tech.get("zoneIds", []),
                "businessUnitId": tech.get("businessUnitId"),
                "location": tech.get("location"),
                "skills": skills,
                "rawCustomFieldTypeIds": raw_custom_fields
            }
            all_technicians.append(tech_info)

        if len(data) < 100:
            break
        page += 1

    print(f"  Found {len(all_technicians)} technicians total")

    # Filter to only active + dispatchable technicians
    filtered = [t for t in all_technicians if t["active"] and t["skills"].get("Dispatchable") == "YES"]
    print(f"  Returning {len(filtered)} active dispatchable technicians")
    print("=" * 70 + "\n")

    return {
        "count": len(filtered),
        "technicians": filtered
    }


@app.put("/api/technicians/{technician_id}")
async def update_technician(technician_id: int, request: Request):
    """
    Update a technician's fields in ServiceTitan.

    Accepts JSON body with any of:
    {
        "name": string,
        "email": string,
        "phone": string,
        "business_unit_id": number,
        "dispatchable": "YES" or null,
        "skill_sewers_mainline": "1"-"5" or null,
        "skill_water_heaters": "1"-"5" or null,
        "skill_misc_plumbing": "1"-"5" or null,
        "skill_gas_lines": "1"-"5" or null,
        "skill_well_pump": "1"-"5" or null,
        "is_excavator": "YES" or null
    }
    """
    import requests

    data = await request.json()

    print("\n" + "=" * 70)
    print(f"UPDATING TECHNICIAN {technician_id}")
    print("=" * 70)
    print(f"  Request body: {data}")

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }

    # First fetch current technician to get Is Excavator typeId
    tech_url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/technicians/{technician_id}"
    tech_resp = requests.get(tech_url, headers=headers)

    is_excavator_type_id = None
    if tech_resp.status_code == 200:
        tech_data = tech_resp.json()
        for cf in tech_data.get("customFields", []):
            if cf.get("name") == "Is Excavator":
                is_excavator_type_id = cf.get("typeId")
                break

    # Map request fields to custom field type IDs
    field_mapping = {
        "dispatchable": ("Dispatchable", TECH_FIELD_IDS["Dispatchable"]),
        "skill_sewers_mainline": ("Skill_Sewers_Mainline", TECH_FIELD_IDS["Skill_Sewers_Mainline"]),
        "skill_water_heaters": ("Skill_Water_Heaters", TECH_FIELD_IDS["Skill_Water_Heaters"]),
        "skill_misc_plumbing": ("Skill_Misc_Plumbing", TECH_FIELD_IDS["Skill_Misc_Plumbing"]),
        "skill_gas_lines": ("Skill_Gas_Lines", TECH_FIELD_IDS["Skill_Gas_Lines"]),
        "skill_well_pump": ("Skill_Well_Pump", TECH_FIELD_IDS["Skill_Well_Pump"]),
    }

    # Add Is Excavator if we found its typeId
    if is_excavator_type_id:
        field_mapping["is_excavator"] = ("Is Excavator", is_excavator_type_id)

    # Build custom fields array for update
    custom_fields = []
    updated_fields = []

    for field_key, (field_name, type_id) in field_mapping.items():
        if field_key in data:
            value = data[field_key]
            if value is not None:
                custom_fields.append({
                    "typeId": type_id,
                    "value": str(value)
                })
            else:
                # Send empty string to clear the field
                custom_fields.append({
                    "typeId": type_id,
                    "value": ""
                })
            updated_fields.append(f"{field_name}={value}")
            print(f"  [Custom Field] {field_name} = {value}")

    # Build main payload for technician update
    payload = {}

    # Handle direct technician fields
    if "name" in data:
        payload["name"] = data["name"]
        updated_fields.append(f"name={data['name']}")
        print(f"  [Direct] name = {data['name']}")

    if "email" in data:
        payload["email"] = data["email"] or ""
        updated_fields.append(f"email={data['email']}")
        print(f"  [Direct] email = {data['email']}")

    if "phone" in data:
        payload["phoneNumber"] = data["phone"] or ""
        updated_fields.append(f"phoneNumber={data['phone']}")
        print(f"  [Direct] phoneNumber = {data['phone']}")

    if "business_unit_id" in data:
        payload["businessUnitId"] = data["business_unit_id"]
        updated_fields.append(f"businessUnitId={data['business_unit_id']}")
        print(f"  [Direct] businessUnitId = {data['business_unit_id']}")

    # Add custom fields to payload if any
    if custom_fields:
        payload["customFields"] = custom_fields

    if not payload:
        print("  No fields to update")
        return {"error": "No valid fields provided to update"}

    # PATCH the technician
    print(f"  PATCH {tech_url}")
    print(f"  Payload: {payload}")

    resp = requests.patch(tech_url, headers=headers, json=payload)
    print(f"  Response: {resp.status_code}")

    if resp.status_code in (200, 204):
        print(f"  SUCCESS: Updated {len(updated_fields)} fields")
        for field in updated_fields:
            print(f"    - {field}")
        print("=" * 70 + "\n")

        # Fetch and return updated technician data
        get_resp = requests.get(tech_url, headers=headers)
        if get_resp.status_code == 200:
            tech_data = get_resp.json()
            skills = {}
            for cf in tech_data.get("customFields", []):
                skills[cf.get("name", "")] = cf.get("value")

            # Get email/phone directly from technician record
            tech_name = tech_data.get("name", "")

            return {
                "status": "success",
                "technician_id": technician_id,
                "updated_fields": updated_fields,
                "technician": {
                    "id": tech_data.get("id"),
                    "name": tech_name,
                    "email": tech_data.get("email"),
                    "phone": tech_data.get("phoneNumber"),
                    "status": tech_data.get("status"),
                    "active": tech_data.get("active"),
                    "businessUnitId": tech_data.get("businessUnitId"),
                    "skills": skills
                }
            }
        else:
            return {
                "status": "success",
                "technician_id": technician_id,
                "updated_fields": updated_fields,
                "message": "Updated but could not fetch latest data"
            }
    else:
        print(f"  FAILED: {resp.text[:500]}")
        print("=" * 70 + "\n")
        return {
            "status": "error",
            "technician_id": technician_id,
            "error": resp.text,
            "status_code": resp.status_code
        }


@app.get("/api/excavators")
async def get_excavators():
    """
    Fetch excavators from ServiceTitan with email/phone from employees.
    Excavators are technicians with "Is Excavator" == "YES".
    """
    import requests

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }

    all_technicians = []
    page = 1

    while True:
        url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/technicians"
        params = {"pageSize": 100, "page": page}
        resp = requests.get(url, headers=headers, params=params)

        if resp.status_code != 200:
            return {"error": f"Failed to fetch technicians: {resp.status_code}"}

        data = resp.json().get("data", [])
        if not data:
            break

        for tech in data:
            skills = {}
            for cf in tech.get("customFields", []):
                cf_name = cf.get("name", "")
                skills[cf_name] = cf.get("value")

            tech_info = {
                "id": tech.get("id"),
                "name": tech.get("name", ""),
                "email": tech.get("email"),  # Direct from technician record
                "phone": tech.get("phoneNumber"),  # Direct from technician record
                "status": tech.get("status"),
                "active": tech.get("active"),
                "zoneIds": tech.get("zoneIds", []),
                "businessUnitId": tech.get("businessUnitId"),
                "location": tech.get("location"),
                "skills": skills
            }
            all_technicians.append(tech_info)

        if len(data) < 100:
            break
        page += 1

    # Filter to excavators: active AND "Is Excavator" == "YES"
    excavators = [
        t for t in all_technicians
        if t["active"] and t["skills"].get("Is Excavator") == "YES"
    ]

    print(f"[API] Found {len(excavators)} excavators (Is Excavator=YES)")

    return {
        "count": len(excavators),
        "excavators": excavators
    }


@app.put("/api/excavators/{technician_id}/email")
async def update_excavator_email(technician_id: int, request: Request):
    """
    Update an excavator's email address.
    Redirects to PUT /api/technicians/{id} with email field.

    Note: Email is stored on the Employee record in ServiceTitan, not Technician.
    This endpoint stores email locally for display purposes.
    """
    import json

    data = await request.json()
    new_email = data.get("email")

    print("\n" + "=" * 70)
    print(f"UPDATING EXCAVATOR EMAIL {technician_id}")
    print("=" * 70)
    print(f"  [Email] {technician_id} = {new_email}")

    # Update in-memory dict (email is on Employee record, not Technician, so we store locally)
    if new_email:
        EXCAVATOR_EMAILS_BY_ID[technician_id] = new_email
    elif technician_id in EXCAVATOR_EMAILS_BY_ID:
        del EXCAVATOR_EMAILS_BY_ID[technician_id]

    # Persist to JSON file
    config_path = os.path.join(os.path.dirname(__file__), "excavator_emails.json")
    try:
        with open(config_path, "w") as f:
            json.dump(EXCAVATOR_EMAILS_BY_ID, f, indent=2)
        print(f"  Saved to {config_path}")
    except Exception as e:
        print(f"  Warning: Could not persist to file: {e}")

    print("=" * 70 + "\n")

    return {
        "success": True,
        "technician_id": technician_id,
        "email": new_email
    }


@app.get("/api/technicians/custom-fields")
async def get_technician_custom_fields():
    """
    Debug endpoint: List all unique custom field names across all technicians.
    """
    import requests

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "ST-App-Key": APP_KEY,
        "Content-Type": "application/json"
    }

    all_fields = {}
    page = 1

    while True:
        url = f"https://api.servicetitan.io/settings/v2/tenant/{TENANT_ID}/technicians"
        params = {"pageSize": 100, "page": page}
        resp = requests.get(url, headers=headers, params=params)

        if resp.status_code != 200:
            return {"error": f"Failed to fetch: {resp.status_code}"}

        data = resp.json().get("data", [])
        if not data:
            break

        for tech in data:
            for cf in tech.get("customFields", []):
                name = cf.get("name", "")
                value = cf.get("value")
                if name not in all_fields:
                    all_fields[name] = {"values": set(), "count": 0}
                all_fields[name]["count"] += 1
                if value:
                    all_fields[name]["values"].add(str(value))

        if len(data) < 100:
            break
        page += 1

    # Convert sets to lists for JSON
    result = {
        name: {"count": info["count"], "sample_values": list(info["values"])[:5]}
        for name, info in all_fields.items()
    }

    return {"custom_fields": result}
