from fastapi import APIRouter, Request
from datetime import datetime, timedelta
import dateparser
from app.models import BookingResponse
from app.services.servicetitan import create_booking


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
    is_homeowner = args.get('is_homeowner', True)
    contact_preference = args.get('contact_preference', "Phone")

    # Print nicely formatted booking summary to console
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n")
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║              NEW BOOKING REQUEST RECEIVED                    ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print(f"║  Customer Name    : {customer_name:<40} ║")
    print(f"║  Address          : {address:<40} ║")
    print(f"║  Phone            : {phone:<40} ║")
    print(f"║  Alternate Phone  : {str(alternate_phone or 'N/A'):<40} ║")
    print(f"║  Email            : {str(email or 'N/A'):<40} ║")
    print(f"║  Issue Description: {issue_description:<40} ║")
    print(f"║  Appointment Time : {appointment_time:<40} ║")
    print(f"║  Appointment Start: {appointment_start:<40} ║")
    print(f"║  Appointment End  : {appointment_end:<40} ║")
    print(f"║  Customer Type    : {customer_type:<40} ║")
    print(f"║  Is Homeowner     : {str(is_homeowner):<40} ║")
    print(f"║  Contact Pref     : {contact_preference:<40} ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print(f"║  Received at      : {timestamp:<40} ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print("\n")

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
        contact_preference=contact_preference,
        alternate_phone=alternate_phone
    )

    # Create confirmation message for Retell to read back
    if st_result is None:
        st_result = {"status": "error", "message": "Booking function returned no response"}

    if st_result.get("status") == "success":
        success = True
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
