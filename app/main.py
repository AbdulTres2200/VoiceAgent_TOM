import os
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from app.routes import booking
from app.webhooks import retell_webhook
from app.webhooks.retell_webhook import is_lead_already_created, mark_lead_created
from app.services.servicetitan import test_connection, lookup_customer_by_phone, explore_account, lookup_by_address, fetch_account_config, get_campaign_from_call, get_campaign_details, get_latest_call, get_call_details, test_telecom, get_live_call_campaign, get_zones, get_job_types, get_job_types_from_st, detect_job_type, store_live_call_info, parse_appointment_time, get_business_unit_by_zone, get_business_units_from_st, store_service_area_business_unit, store_service_area_address, store_service_area_bu_by_address, create_lead, get_access_token, TENANT_ID, APP_KEY, clean_phone
from app.services.service_area import check_service_area, get_service_area_zips, preload_service_area_cache

load_dotenv()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

app = FastAPI(
    title="ServiceTitan Voice Agent",
    description="Retell AI Integration for ServiceTitan",
    version="1.0.0"
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
    Retell AI inbound call webhook.
    Receives call payload, looks up caller in ServiceTitan CRM,
    and returns dynamic variables for the AI agent.

    Returns: caller_number, campaign_id, campaign_name, business_unit_id, business_unit_name,
             customer_name, customer_address, customer_found, recent_job, to_number
    """
    import asyncio
    import concurrent.futures

    data = await request.json()

    # Debug: log payload structure to identify where phone numbers are
    print(f"[Inbound Webhook] Payload keys: {list(data.keys())}")

    # Retell sends data under "call" or "call_inbound" depending on event type
    call_data = data.get("call", {}) or data.get("call_inbound", {})
    if call_data:
        print(f"[Inbound Webhook] call data keys: {list(call_data.keys())}")

    # Get from_number and to_number from Retell payload - try multiple locations
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

    # Check deduplication cache
    cache_key = f"{from_number}:{to_number}"
    now = time.time()
    if cache_key in _inbound_cache:
        cached_time, cached_response = _inbound_cache[cache_key]
        if now - cached_time < _INBOUND_CACHE_TTL:
            print(f"[Inbound Webhook] Returning cached response for {from_number} (age: {now - cached_time:.1f}s)")
            return cached_response

    # Clean phone number based on country code:
    # - +92 (Pakistan): strip + only -> 923343060393
    # - +1 (US): strip +1 -> 10 digit number
    # - Otherwise: strip + only
    cleaned_from_number = from_number.replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
    if cleaned_from_number.startswith("+1"):
        cleaned_from_number = cleaned_from_number[2:]  # Strip +1
    elif cleaned_from_number.startswith("+"):
        cleaned_from_number = cleaned_from_number[1:]  # Strip + only

    # For ServiceTitan lookup, use the 10-digit version
    clean_phone = cleaned_from_number[-10:] if len(cleaned_from_number) >= 10 else cleaned_from_number

    print(f"[Inbound Webhook] Received call from: {from_number} -> cleaned: {cleaned_from_number}")
    print(f"[Inbound Webhook] To number: {to_number}")

    # Cache the to_number for this caller (so booking can look up campaign later)
    if from_number and to_number:
        store_live_call_info(from_number, to_number)

    # Look up customer in ServiceTitan (with timeout to avoid blocking voice agent)
    # IMPORTANT: Skip lookup if phone is empty to avoid returning wrong customer
    result = {"found": False}
    if clean_phone and len(clean_phone) >= 10:
        print("[Inbound] Timeout set to 8s for customer lookup")
        try:
            loop = asyncio.get_event_loop()
            with concurrent.futures.ThreadPoolExecutor() as executor:
                result = await asyncio.wait_for(
                    loop.run_in_executor(executor, lookup_customer_by_phone, clean_phone),
                    timeout=8.0  # 8 second timeout - allow more time for ST API
                )
        except asyncio.TimeoutError:
            print(f"[Inbound Webhook] Customer lookup timed out after 8s, continuing without customer data")
        except Exception as e:
            print(f"[Inbound Webhook] Customer lookup error: {e}")
    else:
        print(f"[Inbound Webhook] Skipping customer lookup - no valid phone number")

    # Look up campaign info from to_number
    campaign_id = ""
    campaign_name = ""
    business_unit_id = ""
    business_unit_name = ""

    if to_number:
        campaign_info = get_live_call_campaign(from_number, to_number)
        if campaign_info:
            campaign_id = str(campaign_info.get("campaign_id", "")) if campaign_info.get("campaign_id") else ""
            campaign_name = campaign_info.get("campaign_name", "") or ""
            business_unit_id = str(campaign_info.get("business_unit_id", "")) if campaign_info.get("business_unit_id") else ""
            business_unit_name = campaign_info.get("business_unit_name", "") or ""
            print(f"[Inbound] Campaign found: {campaign_name} (ID: {campaign_id}), BU: {business_unit_name} (ID: {business_unit_id})")

    # Build dynamic variables
    dynamic_vars = {
        "to_number": to_number,
        "caller_number": cleaned_from_number
    }

    # Add campaign/business unit info (only if we have values)
    if campaign_id:
        dynamic_vars["campaign_id"] = campaign_id
    if campaign_name:
        dynamic_vars["campaign_name"] = campaign_name
    if business_unit_id:
        dynamic_vars["business_unit_id"] = business_unit_id
    if business_unit_name:
        dynamic_vars["business_unit_name"] = business_unit_name

    if result.get("found"):
        customer = result.get("customer", {})
        address = customer.get("address", {})
        recent_jobs = result.get("recent_jobs", [])

        # Format address
        address_str = f"{address.get('street', '')} {address.get('city', '')}".strip()

        # Format recent job
        recent_job_str = ""
        if recent_jobs:
            job = recent_jobs[0]
            recent_job_str = f"{job.get('summary', 'N/A')} - {job.get('status', 'N/A')}"

        dynamic_vars.update({
            "customer_name": customer.get("name", ""),
            "customer_address": address_str,
            "customer_found": "true",
            "recent_job": recent_job_str
        })
    else:
        dynamic_vars.update({
            "customer_name": "",
            "customer_address": "",
            "customer_found": "false",
            "recent_job": ""
        })

    print(f"[Inbound] Dynamic variables being sent to Retell: {dynamic_vars}")

    response = {
        "call_inbound": {
            "dynamic_variables": dynamic_vars
        }
    }

    # Cache the response for deduplication
    _inbound_cache[cache_key] = (now, response)

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

    # Print formatted log
    print("\n")
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║              ADDRESS LOOKUP REQUEST                          ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print(f"║  Address: {address:<50} ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    if not address:
        print("║  Result: No address provided                                 ║")
        print("╚══════════════════════════════════════════════════════════════╝\n")
        return {"found": False, "message": "No address provided"}

    result = lookup_by_address(address)

    if result.get("found"):
        customer = result.get("customer", {})
        addr = customer.get("address", {})
        recent_jobs = result.get("recent_jobs", [])

        # Format full address
        address_str = f"{addr.get('street', '')} {addr.get('city', '')} {addr.get('state', '')}".strip()

        # Format recent job
        recent_job_str = ""
        if recent_jobs:
            job = recent_jobs[0]
            summary = job.get('summary', 'N/A').replace('\r', '').replace('\n', ' ').strip()
            recent_job_str = f"{summary} - {job.get('status', 'N/A')}"

        print("╔══════════════════════════════════════════════════════════════╗")
        print("║              ADDRESS LOOKUP RESULT                           ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print(f"║  Found: YES                                                  ║")
        print(f"║  Customer: {customer.get('name', ''):<49} ║")
        print(f"║  Address: {address_str:<50} ║")
        print(f"║  ID: {customer.get('id', ''):<55} ║")
        print("╚══════════════════════════════════════════════════════════════╝\n")

        return {
            "found": True,
            "customer_name": customer.get("name", ""),
            "customer_address": address_str,
            "customer_id": customer.get("id"),
            "recent_job": recent_job_str
        }
    else:
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║              ADDRESS LOOKUP RESULT                           ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print(f"║  Found: NO                                                   ║")
        print(f"║  Address searched: {address:<41} ║")
        print("╚══════════════════════════════════════════════════════════════╝\n")

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
    Check if an address is within the service area.
    Returns service area status, parsed address, zone info, and business unit.
    Caches business unit by phone for automatic use during booking.
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


@app.post("/post-call-webhook")
async def post_call_webhook(request: Request):
    """
    Retell AI post-call webhook.
    Analyzes call transcript to determine if booking was made,
    then either attaches recording to job or creates a lead.
    """
    import requests
    from datetime import datetime, timedelta, timezone
    from openai import OpenAI

    data = await request.json()

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

    # Analyze transcript with OpenAI
    if transcript:
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
CALL_TYPE:BOOKING or INQUIRY or VENDOR or INVOICING or FOLLOWUP or OTHER
SUMMARY:Brief 2-3 sentence summary of the call"""
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
        # Find recent job for this caller
        print(f"[PostCall] Booking detected - searching for recent job...")

        jobs_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs"
        params = {"pageSize": 5, "orderBy": "Id", "orderByDirection": "desc"}

        if cleaned_from_number:
            params["phone"] = cleaned_from_number

        jobs_resp = requests.get(jobs_url, headers=headers, params=params)

        if jobs_resp.status_code == 200:
            jobs = jobs_resp.json().get("data", [])
            job_id = None

            # Find job created within last 2 hours
            two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)

            for job in jobs:
                created_on = job.get("createdOn", "")
                if created_on:
                    try:
                        # Parse ISO format datetime
                        job_created = datetime.fromisoformat(created_on.replace("Z", "+00:00"))
                        if job_created > two_hours_ago:
                            job_id = job.get("id")
                            print(f"[PostCall] Found recent job: {job_id}")
                            break
                    except Exception as e:
                        print(f"[PostCall] Error parsing job date: {e}")

            if job_id:
                # Add note to job
                note_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs/{job_id}/notes"
                note_resp = requests.post(note_url, headers=headers, json={"text": note_text})

                if note_resp.status_code in (200, 201):
                    print(f"[PostCall] Booking detected - attaching to job {job_id}")
                    action_result = f"Job {job_id}"
                else:
                    print(f"[PostCall] Failed to add note to job: {note_resp.status_code} - {note_resp.text}")
                    action_result = f"Job {job_id} (note failed)"
            else:
                print("[PostCall] No recent job found, creating lead instead")
                booking_made = "no"  # Fall through to lead creation
        else:
            print(f"[PostCall] Jobs lookup failed: {jobs_resp.status_code}")
            booking_made = "no"  # Fall through to lead creation

    if booking_made != "yes":
        # Check if lead was already created by another webhook
        if is_lead_already_created(call_id):
            print(f"[PostCall] Lead already created for this call (dedup), skipping")
            action_result = "Lead already created (dedup)"
        else:
            # Create lead for non-booking call
            print(f"[PostCall] Non-booking call - creating lead...")

            lead_id = create_lead(
                call_type=call_type,
                summary=summary,
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
                    print(f"[PostCall] Non-booking call - lead {lead_id} created and recording attached")
                    action_result = f"Lead {lead_id}"
                else:
                    print(f"[PostCall] Lead created but note failed: {note_resp.status_code} - {note_resp.text}")
                    action_result = f"Lead {lead_id} (note failed)"
            else:
                print("[PostCall] Failed to create lead")
                action_result = "Lead creation failed"

    # Print final summary
    print("\n")
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║           POST CALL WEBHOOK SUMMARY                          ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print(f"║  Call ID:      {call_id:<45} ║")
    print(f"║  From:         {from_number:<45} ║")
    print(f"║  Duration:     {duration_seconds}s{' ':<43}║")
    print(f"║  Booking Made: {booking_made:<45} ║")
    print(f"║  Call Type:    {call_type:<45} ║")
    print(f"║  Summary:      {summary[:43]:<45} ║")
    print(f"║  Action:       {action_result:<45} ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print("\n")

    return {"status": "ok"}
