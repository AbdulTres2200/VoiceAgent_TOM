from fastapi import APIRouter, Request
from datetime import datetime, timedelta
import dateparser
from app.models import BookingResponse
from app.services.servicetitan import create_booking
from app.services.retell import update_call_metadata, store_call_job_mapping


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
    issue_description = args.get('issue_description') or "Not provided"
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

    # Get to_number from Retell (passed as dynamic variable from inbound webhook)
    to_number = args.get('to_number') or data.get('to_number')

    # Get zone-based business_unit_id if provided (from check_service_area result)
    zone_business_unit_id = args.get('business_unit_id')
    zone_business_unit_name = args.get('business_unit_name')

    # Campaign and business unit - lookup using to_number if available
    campaign_id = None
    business_unit_id = None

    if to_number:
        from app.services.servicetitan import get_live_call_campaign
        print(f"[Booking] Looking up campaign using to_number: {to_number}")
        campaign_info = get_live_call_campaign(phone, to_number)
        campaign_id = campaign_info.get("campaign_id")
        business_unit_id = campaign_info.get("business_unit_id")
        print(f"[Booking] Found campaign: {campaign_info.get('campaign_name')} (ID: {campaign_id})")

    # Override with zone-based business unit if provided (takes precedence)
    if zone_business_unit_id:
        print(f"[Booking] Using zone-based business unit: {zone_business_unit_name} (ID: {zone_business_unit_id})")
        business_unit_id = int(zone_business_unit_id)

    # Print nicely formatted booking summary to console (wrapped in try-except to never block operation)
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("\n")
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║              NEW BOOKING REQUEST RECEIVED                    ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print(f"║  Customer Name    : {str(customer_name or ''):<40} ║")
        print(f"║  Address          : {str(address or ''):<40} ║")
        print(f"║  Phone            : {str(phone or ''):<40} ║")
        print(f"║  Alternate Phone  : {str(alternate_phone or 'N/A'):<40} ║")
        print(f"║  Email            : {str(email or 'N/A'):<40} ║")
        print(f"║  Issue Description: {str(issue_description or ''):<40} ║")
        print(f"║  Appointment Time : {str(appointment_time or ''):<40} ║")
        print(f"║  Appointment Start: {str(appointment_start or ''):<40} ║")
        print(f"║  Appointment End  : {str(appointment_end or ''):<40} ║")
        print(f"║  Customer Type    : {str(customer_type or ''):<40} ║")
        print(f"║  Is Homeowner     : {str(is_homeowner or ''):<40} ║")
        print(f"║  Promo Emails     : {str(promotional_emails or ''):<40} ║")
        print(f"║  To Number        : {str(to_number or 'Not provided'):<40} ║")
        print(f"║  Campaign ID      : {str(campaign_id or 'Auto'):<40} ║")
        bu_display = f"{business_unit_id} (zone: {zone_business_unit_name})" if zone_business_unit_id else str(business_unit_id or 'Auto')
        print(f"║  Business Unit ID : {bu_display:<40} ║")
        print(f"║  Retell Call ID   : {str(call_id or 'Not provided'):<40} ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print(f"║  Received at      : {timestamp:<40} ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print("\n")
    except Exception as e:
        print(f"[Booking] Warning: Could not print booking summary: {e}")

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
        business_unit_id=business_unit_id
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

        confirmation_message = (
            f"Great! I've booked your appointment for {customer_name} "
            f"at {address} for {appointment_time}. "
            f"We'll contact you at {phone}. "
            f"Thank you for choosing our service!"
        )
    else:
        success = False
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
