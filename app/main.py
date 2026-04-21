import os
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from app.routes import booking
from app.webhooks import retell_webhook
from app.services.servicetitan import test_connection, lookup_customer_by_phone, explore_account, lookup_by_address, fetch_account_config, get_campaign_from_call, get_campaign_details, get_latest_call, get_call_details, test_telecom, get_live_call_campaign, get_zones, get_job_types, get_job_types_from_st, detect_job_type, store_live_call_info, parse_appointment_time, get_business_unit_by_zone, get_business_units_from_st, store_service_area_business_unit
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


@app.post("/inbound-webhook")
async def inbound_webhook(request: Request):
    """
    Retell AI inbound call webhook.
    Receives call payload, looks up caller in ServiceTitan CRM,
    and returns dynamic variables for the AI agent.

    OPTIMIZED:
    - Removed campaign lookup - done during booking instead
    - Added 30s deduplication cache to avoid duplicate lookups
    """
    import asyncio
    import concurrent.futures

    data = await request.json()

    # Get from_number and to_number from Retell payload - nested under call_inbound
    call_inbound = data.get("call_inbound", {})
    from_number = (
        call_inbound.get("from_number") or
        data.get("from_number") or
        ""
    )
    to_number = (
        call_inbound.get("to_number") or
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

    # Clean phone number: strip +1, dashes, spaces, parentheses
    clean_phone = from_number.replace("+1", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")

    print(f"[Inbound Webhook] Received call from: {from_number} -> cleaned: {clean_phone}")
    print(f"[Inbound Webhook] To number: {to_number}")

    # Cache the to_number for this caller (so booking can look up campaign later)
    store_live_call_info(from_number, to_number)

    # Look up customer in ServiceTitan (with timeout to avoid blocking voice agent)
    print("[Inbound] Timeout set to 8s for customer lookup")
    result = {"found": False}
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

    # Build dynamic variables - to_number is passed for campaign lookup during booking
    dynamic_vars = {
        "to_number": to_number
    }

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
