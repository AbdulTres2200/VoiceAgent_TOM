"""
Business Hours Logic for Hearn Plumbing, Heating & Air

Determines:
- Time period (standard, after_hours, weekend, holiday)
- Applicable service fee
- Available booking windows
- Eligibility for after-hours service
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Tuple, Optional

# Load fee configuration from environment
STANDARD_FEE_HPLUS = int(os.getenv("STANDARD_FEE_HPLUS", "49"))
STANDARD_FEE_REGULAR = int(os.getenv("STANDARD_FEE_REGULAR", "99"))
AFTER_HOURS_FEE = int(os.getenv("AFTER_HOURS_FEE", "199"))
HOLIDAY_FEE = int(os.getenv("HOLIDAY_FEE", "299"))

# Timezone
TIMEZONE = ZoneInfo("America/New_York")

# 2024-2026 US Holidays
HOLIDAYS = {
    # 2024
    "2024-01-01": "New Year's Day",
    "2024-05-27": "Memorial Day",
    "2024-07-04": "Independence Day",
    "2024-09-02": "Labor Day",
    "2024-11-28": "Thanksgiving",
    "2024-12-25": "Christmas",
    # 2025
    "2025-01-01": "New Year's Day",
    "2025-05-26": "Memorial Day",
    "2025-07-04": "Independence Day",
    "2025-09-01": "Labor Day",
    "2025-11-27": "Thanksgiving",
    "2025-12-25": "Christmas",
    # 2026
    "2026-01-01": "New Year's Day",
    "2026-05-25": "Memorial Day",
    "2026-07-04": "Independence Day",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving",
    "2026-12-25": "Christmas",
}


def get_current_time() -> datetime:
    """Get current time in Eastern timezone."""
    return datetime.now(TIMEZONE)


def is_holiday(date: datetime) -> Tuple[bool, Optional[str]]:
    """Check if a date is a holiday."""
    date_str = date.strftime("%Y-%m-%d")
    if date_str in HOLIDAYS:
        return True, HOLIDAYS[date_str]
    return False, None


def is_in_holiday_window(now: datetime) -> Tuple[bool, Optional[str]]:
    """
    Check if current time is within a holiday window.

    Holiday window rules:
    - Holiday on Sat/Sun: Fri 5pm - Mon 7am
    - Holiday on Friday: Fri 7am - Mon 7am
    - Holiday on Monday: Sat 7am - Tue 7am
    - Holiday on Tuesday: Sat 7am - Wed 7am
    - Holiday on Thursday: Thu 7am - Mon 7am
    - Holiday on Wednesday: Custom (treat as Thu 7am - Mon 7am)
    """
    for days_ahead in range(-3, 5):
        check_date = now + timedelta(days=days_ahead)
        is_hol, holiday_name = is_holiday(check_date)

        if is_hol:
            holiday_weekday = check_date.weekday()

            if holiday_weekday == 5:  # Saturday
                window_start = check_date - timedelta(days=1)
                window_start = window_start.replace(hour=17, minute=0, second=0, microsecond=0)
                window_end = check_date + timedelta(days=2)
                window_end = window_end.replace(hour=7, minute=0, second=0, microsecond=0)
            elif holiday_weekday == 6:  # Sunday
                window_start = check_date - timedelta(days=2)
                window_start = window_start.replace(hour=17, minute=0, second=0, microsecond=0)
                window_end = check_date + timedelta(days=1)
                window_end = window_end.replace(hour=7, minute=0, second=0, microsecond=0)
            elif holiday_weekday == 4:  # Friday
                window_start = check_date.replace(hour=7, minute=0, second=0, microsecond=0)
                window_end = check_date + timedelta(days=3)
                window_end = window_end.replace(hour=7, minute=0, second=0, microsecond=0)
            elif holiday_weekday == 0:  # Monday
                window_start = check_date - timedelta(days=2)
                window_start = window_start.replace(hour=7, minute=0, second=0, microsecond=0)
                window_end = check_date + timedelta(days=1)
                window_end = window_end.replace(hour=7, minute=0, second=0, microsecond=0)
            elif holiday_weekday == 1:  # Tuesday
                window_start = check_date - timedelta(days=3)
                window_start = window_start.replace(hour=7, minute=0, second=0, microsecond=0)
                window_end = check_date + timedelta(days=1)
                window_end = window_end.replace(hour=7, minute=0, second=0, microsecond=0)
            elif holiday_weekday == 3:  # Thursday
                window_start = check_date.replace(hour=7, minute=0, second=0, microsecond=0)
                window_end = check_date + timedelta(days=4)
                window_end = window_end.replace(hour=7, minute=0, second=0, microsecond=0)
            elif holiday_weekday == 2:  # Wednesday
                window_start = check_date.replace(hour=7, minute=0, second=0, microsecond=0)
                window_end = check_date + timedelta(days=5)
                window_end = window_end.replace(hour=7, minute=0, second=0, microsecond=0)
            else:
                continue

            window_start = window_start.replace(tzinfo=TIMEZONE)
            window_end = window_end.replace(tzinfo=TIMEZONE)

            if window_start <= now < window_end:
                return True, holiday_name

    return False, None


def get_time_period(now: datetime = None) -> Dict:
    """
    Determine the current time period.

    Returns:
        period: "standard", "after_hours", "weekend", "holiday"
        period_name: Human-readable name
    """
    if now is None:
        now = get_current_time()

    weekday = now.weekday()
    hour = now.hour
    minute = now.minute
    current_minutes = hour * 60 + minute

    # Check holiday first
    in_holiday, holiday_name = is_in_holiday_window(now)
    if in_holiday:
        return {
            "period": "holiday",
            "period_name": f"Holiday Hours ({holiday_name})",
            "holiday_name": holiday_name
        }

    # Weekend: Saturday 7am to Monday 7am
    if weekday == 5 and current_minutes >= 7 * 60:
        return {"period": "weekend", "period_name": "Weekend Hours"}
    elif weekday == 6:
        return {"period": "weekend", "period_name": "Weekend Hours"}
    elif weekday == 0 and current_minutes < 7 * 60:
        return {"period": "weekend", "period_name": "Weekend Hours"}

    # After Hours: Mon-Fri 5pm to 7am
    if weekday < 5:
        if current_minutes >= 17 * 60:
            return {"period": "after_hours", "period_name": "After Hours"}
        elif current_minutes < 7 * 60:
            return {"period": "after_hours", "period_name": "After Hours"}

    # Standard Hours: Mon-Fri 8am-4pm
    if weekday < 5 and 8 * 60 <= current_minutes < 16 * 60:
        return {"period": "standard", "period_name": "Standard Business Hours"}

    # Edge cases (7am-8am, 4pm-5pm)
    return {"period": "after_hours", "period_name": "After Hours"}


def get_service_fee(period: str, is_hplus_member: bool) -> Dict:
    """Calculate service fee based on period and membership."""
    if period == "holiday":
        return {
            "fee": HOLIDAY_FEE,
            "fee_description": f"${HOLIDAY_FEE} Holiday Service Fee"
        }
    elif period in ("after_hours", "weekend"):
        return {
            "fee": AFTER_HOURS_FEE,
            "fee_description": f"${AFTER_HOURS_FEE} After Hours/Weekend Service Fee"
        }
    else:
        if is_hplus_member:
            return {
                "fee": STANDARD_FEE_HPLUS,
                "fee_description": f"${STANDARD_FEE_HPLUS} H+ Member Service Fee"
            }
        else:
            return {
                "fee": STANDARD_FEE_REGULAR,
                "fee_description": f"${STANDARD_FEE_REGULAR} Service Fee"
            }


def get_booking_windows(period: str, now: datetime = None) -> List[Dict]:
    """
    Get available booking windows.

    Standard: 8am-12pm OR 12pm-4pm (4-hour windows)
    After Hours/Weekend/Holiday: 2-hour increments
    """
    if now is None:
        now = get_current_time()

    windows = []

    if period == "standard":
        today = now.date()
        morning_end = datetime(today.year, today.month, today.day, 12, 0, tzinfo=TIMEZONE)
        afternoon_end = datetime(today.year, today.month, today.day, 16, 0, tzinfo=TIMEZONE)

        if now < morning_end:
            windows.append({
                "start": "8:00 AM",
                "end": "12:00 PM",
                "description": "Morning (8am-12pm)"
            })
        if now < afternoon_end:
            windows.append({
                "start": "12:00 PM",
                "end": "4:00 PM",
                "description": "Afternoon (12pm-4pm)"
            })

        if not windows:
            windows.append({"description": "Tomorrow Morning (8am-12pm)"})
            windows.append({"description": "Tomorrow Afternoon (12pm-4pm)"})
    else:
        windows = [{"description": "2-hour arrival windows available"}]

    return windows


def check_eligibility(period: str, is_hplus_member: bool, days_since_last_service: int = None, is_emergency: bool = False) -> Dict:
    """
    Check if customer is eligible for service.

    Emergencies (No A/C, No Heat): ALWAYS eligible - these are urgent situations
    After Hours & Weekends: H+ Members OR serviced in last 30 days OR emergency
    Standard & Holidays: All customers eligible
    """
    # Emergencies always get service - safety first
    if is_emergency:
        return {
            "eligible": True,
            "reason": "Emergency service - eligible regardless of membership status",
            "is_emergency": True
        }

    if period in ("standard", "holiday"):
        return {"eligible": True, "reason": "All customers can book during this time"}

    if is_hplus_member:
        return {"eligible": True, "reason": "H+ Members can book after hours and weekends"}

    if days_since_last_service is not None and days_since_last_service <= 30:
        return {
            "eligible": True,
            "reason": f"Serviced {days_since_last_service} days ago - eligible for after-hours"
        }

    return {
        "eligible": False,
        "reason": "After-hours service is only for H+ Members or customers serviced in the last 30 days",
        "offer_weekday": True,
        "weekday_message": "I can get you scheduled for our first available appointment Monday morning between 8 and 12, or Monday afternoon between 12 and 4. Would one of those work for you?"
    }


def get_business_hours_info(is_hplus_member: bool = False, days_since_last_service: int = None, is_emergency: bool = False) -> Dict:
    """
    Main function - get all business hours information.

    Args:
        is_hplus_member: Whether the customer is an H+ member
        days_since_last_service: Days since last service (None if unknown)
        is_emergency: Whether this is an emergency (No A/C, No Heat, etc.)

    Returns:
        Complete info: period, fee, windows, eligibility
    """
    now = get_current_time()
    period_info = get_time_period(now)
    period = period_info["period"]

    fee_info = get_service_fee(period, is_hplus_member)
    windows = get_booking_windows(period, now)
    eligibility = check_eligibility(period, is_hplus_member, days_since_last_service, is_emergency)

    result = {
        "current_time": now.strftime("%A, %B %d at %I:%M %p"),
        "period": period,
        "period_name": period_info["period_name"],
        "fee": fee_info["fee"],
        "fee_description": fee_info["fee_description"],
        "booking_windows": windows,
        "eligible": eligibility["eligible"],
        "eligibility_reason": eligibility["reason"],
        "is_hplus_member": is_hplus_member,
        "is_emergency": is_emergency
    }

    # Add weekday offer message if not eligible
    if eligibility.get("offer_weekday"):
        result["offer_weekday"] = True
        result["weekday_message"] = eligibility["weekday_message"]

    if period_info.get("holiday_name"):
        result["holiday_name"] = period_info["holiday_name"]

    return result
