from fastapi import APIRouter, Request
from datetime import datetime, timedelta
import dateparser
from app.models import BookingResponse
from app.services.servicetitan import create_booking, get_recent_callers
from app.services.retell import update_call_metadata, store_call_job_mapping, store_phone_job_mapping
from app.services.email_notify import send_call_summary


def parse_appointment_time(time_string):
    """
    Convert natural language time to ISO format start/end times.
    Returns tuple of (start_iso, end_iso) or (None, None) if parsing fails.
    """
    import re

    if not time_string:
        return None, None

    print(f"[Parsing] Input: {time_string}")

    lower = time_string.lower().strip()

    # Handle "as soon as possible" / "asap" - default to tomorrow 9am
    if "asap" in lower or "as soon as possible" in lower or "earliest" in lower:
        tomorrow = datetime.utcnow() + timedelta(days=1)
        start = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        print(f"[Parsing] ASAP detected -> {start}")
        return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Preprocess: remove "next" as dateparser doesn't handle "next monday" well
    cleaned = re.sub(r'\bnext\b', '', lower).strip()
    # Also handle "this coming"
    cleaned = re.sub(r'\bthis coming\b', '', cleaned).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)  # normalize spaces

    print(f"[Parsing] Cleaned: {cleaned}")

    # Try to parse natural language
    try:
        parsed = dateparser.parse(
            cleaned,
            settings={
                'PREFER_DATES_FROM': 'future',
                'PREFER_DAY_OF_MONTH': 'first',
                'RETURN_AS_TIMEZONE_AWARE': False
            }
        )
        print(f"[Parsing] Dateparser result: {parsed}")

        if parsed:
            # If no time specified, default to 9am
            if parsed.hour == 0 and parsed.minute == 0:
                parsed = parsed.replace(hour=9)

            start = parsed.replace(minute=0, second=0, microsecond=0)
            end = start + timedelta(hours=1)
            print(f"[Parsing] Final: start={start}, end={end}")
            return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception as e:
        print(f"[Parsing] Error: {e}")

    print(f"[Parsing] Failed to parse: {time_string}")
    return None, None

router = APIRouter()


@router.post("/book-appointment", response_model=BookingResponse)
async def book_appointment(request: Request):
    """
    Book an appointment via Retell AI and create it in ServiceTitan.
    Retell sends arguments nested inside an 'args' key.
    """
    data = await request.json()

    # DEBUG: Log what Retell sends us
    print(f"[Booking DEBUG] Top-level keys: {list(data.keys())}")
    if 'call' in data:
        print(f"[Booking DEBUG] call keys: {list(data.get('call', {}).keys())}")
        print(f"[Booking DEBUG] call.from_number: {data.get('call', {}).get('from_number')}")
        print(f"[Booking DEBUG] call.call_id: {data.get('call', {}).get('call_id')}")

    # Extract call_id from Retell request (needed to update metadata after booking)
    call_id = data.get('call_id') or data.get('call', {}).get('call_id')

    # Extract args from Retell's nested format, fallback to top-level for direct calls
    args = data.get('args', data)

    # Handle missing fields gracefully with default values
    customer_name = args.get('customer_name') or "Not provided"
    address = args.get('address') or "Not provided"
    phone = args.get('phone') or "Not provided"
    alternate_phone = args.get('alternate_phone')
    email = args.get('email')
    issue_description = args.get('issue_description') or args.get('issue') or "Not provided"
    appointment_time = args.get('appointment_time') or ""
    appointment_start = args.get('appointment_start') or ""
    appointment_end = args.get('appointment_end') or ""

    # If start/end not provided, parse from natural language appointment_time
    if not appointment_start or not appointment_end:
        parsed_start, parsed_end = parse_appointment_time(appointment_time)
        appointment_start = appointment_start or parsed_start or ""
        appointment_end = appointment_end or parsed_end or ""
    customer_type = args.get('customer_type') or "Residential"
    # Accept both 'is_homeowner' and 'owns_home' from Retell
    is_homeowner = args.get('is_homeowner') or args.get('owns_home') or "yes"
    promotional_emails = args.get('promotional_emails') or "yes"
    contact_preference = args.get('contact_preference', "Phone")
    # is_emergency defaults to False if not provided
    is_emergency = args.get('is_emergency', False)
    # is_excavation - if true, skip job type detection and use excavation workflow
    is_excavation = args.get('is_excavation', False)

    # Referral source - how customer heard about us (for AI campaign detection)
    referral_source = args.get('referral_source') or args.get('referral') or args.get('how_heard') or ""

    # Existing customer ID from inbound lookup (skip customer creation if provided)
    existing_customer_id = args.get('customer_id') or args.get('existing_customer_id')
    existing_location_id = args.get('location_id') or args.get('existing_location_id')

    # Convert to int if provided as string
    if existing_customer_id:
        try:
            existing_customer_id = int(existing_customer_id)
            print(f"[Booking] Using existing customer ID from Retell: {existing_customer_id}")
        except (ValueError, TypeError):
            existing_customer_id = None
    if existing_location_id:
        try:
            existing_location_id = int(existing_location_id)
            print(f"[Booking] Using existing location ID from Retell: {existing_location_id}")
        except (ValueError, TypeError):
            existing_location_id = None

    # Get to_number from Retell (passed as dynamic variable from inbound webhook)
    to_number = args.get('to_number') or data.get('to_number')

    # Get from_number (caller's phone) for phone-based job mapping
    # This is different from customer's phone - it's the number the call came from
    # Check multiple locations where Retell might pass this
    dynamic_vars = data.get('retell_llm_dynamic_variables', {}) or data.get('dynamic_variables', {})
    from_number = (
        args.get('from_number') or
        args.get('caller_number') or
        data.get('from_number') or
        data.get('caller_number') or
        dynamic_vars.get('caller_number') or
        dynamic_vars.get('from_number') or
        data.get('call', {}).get('from_number')
    )

    # Get zone-based business_unit_id if provided (from check_service_area result)
    zone_business_unit_id = args.get('business_unit_id')
    zone_business_unit_name = args.get('business_unit_name')

    # Campaign and business unit - lookup using to_number if available
    campaign_id = None
    business_unit_id = None

    # PRIORITY 1: Use AI to detect campaign from referral source (if provided)
    if referral_source:
        from app.services.servicetitan import detect_campaign_from_referral
        print(f"[Booking] Detecting campaign from referral: '{referral_source}'")
        campaign_result = detect_campaign_from_referral(referral_source)
        campaign_id = campaign_result.get("campaign_id")
        print(f"[Booking] AI detected campaign: {campaign_result.get('campaign_name')} (ID: {campaign_id}) - {campaign_result.get('confidence')}")

    # PRIORITY 2: Fallback to to_number lookup if no referral source
    if not campaign_id and to_number:
        from app.services.servicetitan import get_live_call_campaign
        print(f"[Booking] Looking up campaign using to_number: {to_number}")
        campaign_info = get_live_call_campaign(phone, to_number)
        campaign_id = campaign_info.get("campaign_id")
        business_unit_id = campaign_info.get("business_unit_id")
        print(f"[Booking] Found campaign: {campaign_info.get('campaign_name')} (ID: {campaign_id})")

    # Override with zone-based business unit if provided by Retell (takes precedence)
    if zone_business_unit_id:
        print(f"[Booking] Using zone-based business unit (from Retell): {zone_business_unit_name} (ID: {zone_business_unit_id})")
        business_unit_id = int(zone_business_unit_id)
    else:
        # Check address-based cache (from check_service_area) - highest priority
        from app.services.servicetitan import get_service_area_bu_by_address, parse_address
        parsed_addr = parse_address(address)
        cached_bu = get_service_area_bu_by_address(parsed_addr.get("street", ""), parsed_addr.get("zip", ""))
        if cached_bu:
            print(f"[Booking] Using zone-based business unit (from address cache): {cached_bu['business_unit_name']} (ID: {cached_bu['business_unit_id']})")
            business_unit_id = cached_bu["business_unit_id"]
            zone_business_unit_id = cached_bu["business_unit_id"]
            zone_business_unit_name = cached_bu["business_unit_name"]

    # Simple booking log
    print(f"[Booking] {customer_name} | {phone} | {address} | {issue_description[:50] if issue_description else 'N/A'}...")
    print(f"[Booking] call_id={call_id} | from_number={from_number}")

    # Create booking in ServiceTitan
    st_result = create_booking(
        customer_name=customer_name,
        address=address,
        phone=phone,
        email=email,
        issue_description=issue_description,
        appointment_time=appointment_time,
        appointment_start=appointment_start,
        appointment_end=appointment_end,
        customer_type=customer_type,
        is_homeowner=is_homeowner,
        promotional_emails=promotional_emails,
        contact_preference=contact_preference,
        alternate_phone=alternate_phone,
        campaign_id=campaign_id,
        business_unit_id=business_unit_id,
        is_emergency=is_emergency,
        is_excavation=is_excavation,
        existing_customer_id=existing_customer_id,
        existing_location_id=existing_location_id
    )

    # Create confirmation message for Retell to read back
    if st_result is None:
        st_result = {"status": "error", "message": "Booking function returned no response"}

    if st_result.get("status") == "success":
        success = True
        job_id = st_result.get("job_id")

        # Store call_id -> job_id mapping locally (for webhook lookup)
        if call_id and job_id:
            store_call_job_mapping(call_id, str(job_id))
            # Also try to update Retell metadata (may fail but that's OK)
            update_call_metadata(call_id, {"job_id": str(job_id)})

        # Store from_number -> job_id mapping (fallback when call_id not available)
        if from_number and job_id:
            store_phone_job_mapping(from_number, str(job_id))
            print(f"[Booking] Stored caller phone mapping: {from_number} -> job {job_id}")

        # Also store customer phone -> job_id mapping (since Retell doesn't pass call metadata)
        if phone and job_id:
            store_phone_job_mapping(phone, str(job_id))
            print(f"[Booking] Stored customer phone mapping: {phone} -> job {job_id}")

        # Store mappings for ALL recent callers (within 15 min) since Retell doesn't pass from_number
        # This ensures the actual caller's phone is mapped even if it differs from customer phone
        recent_callers = get_recent_callers(max_age_minutes=15)
        for caller_phone in recent_callers:
            if caller_phone != phone:  # Don't duplicate customer phone
                store_phone_job_mapping(caller_phone, str(job_id))
                print(f"[Booking] Stored recent caller mapping: {caller_phone} -> job {job_id}")

        # Get dispatch status
        dispatch_info = st_result.get("dispatch", {})
        if dispatch_info.get("auto_dispatch"):
            dispatch_status = "Auto-dispatched"
        elif dispatch_info.get("requires_approval"):
            dispatch_status = "Requires approval"
        else:
            dispatch_status = "Pending"

        # Send email notification
        send_call_summary(
            call_type="BOOKING",
            customer_name=customer_name,
            phone=phone,
            address=address,
            email=email,
            issue=issue_description,
            appointment_time=appointment_time,
            job_id=str(job_id),
            job_type=st_result.get("job_type_name"),
            dispatch_status=dispatch_status,
            business_unit=zone_business_unit_name,
            is_emergency=is_emergency,
            is_excavation=is_excavation
        )

        confirmation_message = (
            f"Great! I've booked your appointment for {customer_name} "
            f"at {address} for {appointment_time}. "
            f"We'll contact you at {phone}. "
            f"Thank you for choosing our service!"
        )
    else:
        success = False
        # Send email notification for failed booking
        send_call_summary(
            call_type="BOOKING",
            customer_name=customer_name,
            phone=phone,
            address=address,
            email=email,
            issue=issue_description,
            appointment_time=appointment_time,
            error=st_result.get("error") or st_result.get("message") or "Booking failed"
        )

        confirmation_message = (
            f"I've received your booking request for {customer_name}. "
            f"Our team will contact you shortly at {phone} to confirm. "
            f"Thank you!"
        )

    return BookingResponse(
        success=success,
        message=confirmation_message,
        booking_details={
            "customer_name": customer_name,
            "address": address,
            "phone": phone,
            "alternate_phone": alternate_phone,
            "email": email,
            "issue_description": issue_description,
            "appointment_time": appointment_time,
            "appointment_start": appointment_start,
            "appointment_end": appointment_end,
            "customer_type": customer_type,
            "is_homeowner": is_homeowner,
            "contact_preference": contact_preference,
            "servicetitan_response": st_result
        }
    )
