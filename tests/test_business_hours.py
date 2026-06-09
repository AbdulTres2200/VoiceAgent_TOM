"""
Unit tests for business hours policy logic.

Tests the check_eligibility and get_business_hours_info functions,
specifically verifying the H+ member-only after-hours emergency policy.
"""

import pytest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.business_hours import (
    check_eligibility,
    get_business_hours_info,
    get_time_period,
    TIMEZONE
)


class TestCheckEligibility:
    """Tests for the check_eligibility function."""

    # ===== CASE 1: H+ Member + Emergency + After-Hours =====
    def test_hplus_member_emergency_after_hours_eligible(self):
        """
        H+ members with emergencies should be eligible for after-hours service.
        They get the after-hours fee ($199) and transfer to after_hours_line.
        """
        result = check_eligibility(
            period="after_hours",
            is_hplus_member=True,
            is_emergency=True
        )

        assert result["eligible"] is True
        assert result["is_emergency"] is True
        assert "H+ Member" in result["reason"]
        assert result.get("use_standard_fee") is None  # Should NOT use standard fee

    def test_hplus_member_emergency_weekend_eligible(self):
        """H+ members with emergencies should be eligible on weekends too."""
        result = check_eligibility(
            period="weekend",
            is_hplus_member=True,
            is_emergency=True
        )

        assert result["eligible"] is True
        assert result["is_emergency"] is True

    # ===== CASE 2: Non-Member + Emergency + After-Hours =====
    def test_non_member_emergency_after_hours_not_eligible(self):
        """
        Non-members with emergencies should NOT be eligible for after-hours.
        They should be offered next business day at standard fee ($99).
        """
        result = check_eligibility(
            period="after_hours",
            is_hplus_member=False,
            is_emergency=True
        )

        assert result["eligible"] is False
        assert result["is_emergency"] is True
        assert result["offer_next_business_day"] is True
        assert result["use_standard_fee"] is True
        assert "H+ members only" in result["reason"]
        assert "$99" in result["weekday_message"]

    def test_non_member_emergency_weekend_not_eligible(self):
        """
        Non-members with emergencies on weekends should NOT be eligible.
        Same policy as after-hours.
        """
        result = check_eligibility(
            period="weekend",
            is_hplus_member=False,
            is_emergency=True
        )

        assert result["eligible"] is False
        assert result["is_emergency"] is True
        assert result["offer_next_business_day"] is True
        assert result["use_standard_fee"] is True

    # ===== CASE 3: Regular Hours (anyone eligible) =====
    def test_non_member_emergency_standard_hours_eligible(self):
        """
        During standard hours, everyone is eligible including non-member emergencies.
        """
        result = check_eligibility(
            period="standard",
            is_hplus_member=False,
            is_emergency=True
        )

        assert result["eligible"] is True
        assert result["is_emergency"] is True
        assert result.get("use_standard_fee") is None

    def test_non_member_non_emergency_standard_hours_eligible(self):
        """During standard hours, all non-emergency customers are eligible."""
        result = check_eligibility(
            period="standard",
            is_hplus_member=False,
            is_emergency=False
        )

        assert result["eligible"] is True

    def test_hplus_member_standard_hours_eligible(self):
        """H+ members during standard hours are eligible."""
        result = check_eligibility(
            period="standard",
            is_hplus_member=True,
            is_emergency=False
        )

        assert result["eligible"] is True

    # ===== CASE 4: Non-Emergency After-Hours =====
    def test_non_member_non_emergency_after_hours_not_eligible(self):
        """
        Non-members without emergencies should not be eligible after-hours.
        They should be offered weekday booking.
        """
        result = check_eligibility(
            period="after_hours",
            is_hplus_member=False,
            is_emergency=False
        )

        assert result["eligible"] is False
        assert result["offer_weekday"] is True
        assert result.get("is_emergency") is None

    def test_hplus_member_non_emergency_after_hours_eligible(self):
        """H+ members should be eligible after-hours even without emergency."""
        result = check_eligibility(
            period="after_hours",
            is_hplus_member=True,
            is_emergency=False
        )

        assert result["eligible"] is True

    # ===== Edge Cases =====
    def test_holiday_non_member_eligible(self):
        """During holidays, all customers are eligible."""
        result = check_eligibility(
            period="holiday",
            is_hplus_member=False,
            is_emergency=False
        )

        assert result["eligible"] is True

    def test_recent_service_non_emergency_after_hours_eligible(self):
        """Customers serviced in last 30 days are eligible for after-hours (non-emergency)."""
        result = check_eligibility(
            period="after_hours",
            is_hplus_member=False,
            days_since_last_service=15,
            is_emergency=False
        )

        assert result["eligible"] is True


class TestGetBusinessHoursInfo:
    """Tests for the full get_business_hours_info function with fee calculations."""

    def _mock_time(self, hour: int, minute: int = 0, weekday: int = 0):
        """Create a mock datetime for testing specific time scenarios."""
        # weekday: 0=Monday, 5=Saturday, 6=Sunday
        # Create a date that falls on the desired weekday
        base_date = datetime(2026, 6, 8, hour, minute, tzinfo=TIMEZONE)  # June 8, 2026 is a Monday
        days_to_add = weekday
        target_date = base_date.replace(day=8 + days_to_add)
        return target_date

    @patch('app.services.business_hours.get_current_time')
    def test_hplus_member_emergency_after_hours_fee_199(self, mock_time):
        """H+ member emergency after-hours should get $199 fee and be eligible."""
        # 10 PM on Monday (after hours)
        mock_time.return_value = self._mock_time(22, 0, 0)

        result = get_business_hours_info(
            is_hplus_member=True,
            is_emergency=True
        )

        assert result["eligible"] is True
        assert result["fee"] == 199
        assert result["period"] == "after_hours"
        assert result["is_emergency"] is True

    @patch('app.services.business_hours.get_current_time')
    def test_non_member_emergency_after_hours_fee_99(self, mock_time):
        """
        Non-member emergency after-hours should get $99 fee (standard rate)
        because they're being booked for next business day.
        """
        # 10 PM on Monday (after hours)
        mock_time.return_value = self._mock_time(22, 0, 0)

        result = get_business_hours_info(
            is_hplus_member=False,
            is_emergency=True
        )

        assert result["eligible"] is False
        assert result["fee"] == 99  # Standard rate, NOT $199
        assert result["period"] == "after_hours"
        assert result["is_emergency"] is True
        assert result["offer_next_business_day"] is True

    @patch('app.services.business_hours.get_current_time')
    def test_non_member_emergency_weekend_fee_99(self, mock_time):
        """
        Non-member emergency on weekend should get $99 fee (standard rate)
        because they're being booked for next business day.
        """
        # 2 PM on Saturday (weekend)
        mock_time.return_value = self._mock_time(14, 0, 5)

        result = get_business_hours_info(
            is_hplus_member=False,
            is_emergency=True
        )

        assert result["eligible"] is False
        assert result["fee"] == 99  # Standard rate, NOT $199
        assert result["period"] == "weekend"
        assert result["is_emergency"] is True
        assert result["offer_next_business_day"] is True

    @patch('app.services.business_hours.get_current_time')
    def test_standard_hours_non_member_emergency_fee_99(self, mock_time):
        """During standard hours, non-member emergency gets $99 and is eligible."""
        # 10 AM on Monday (standard hours)
        mock_time.return_value = self._mock_time(10, 0, 0)

        result = get_business_hours_info(
            is_hplus_member=False,
            is_emergency=True
        )

        assert result["eligible"] is True
        assert result["fee"] == 99
        assert result["period"] == "standard"

    @patch('app.services.business_hours.get_current_time')
    def test_standard_hours_hplus_member_fee_49(self, mock_time):
        """During standard hours, H+ member gets $49 fee."""
        # 10 AM on Monday (standard hours)
        mock_time.return_value = self._mock_time(10, 0, 0)

        result = get_business_hours_info(
            is_hplus_member=True,
            is_emergency=False
        )

        assert result["eligible"] is True
        assert result["fee"] == 49
        assert result["period"] == "standard"

    @patch('app.services.business_hours.get_current_time')
    def test_after_hours_non_emergency_non_member_offers_weekday(self, mock_time):
        """Non-member non-emergency after-hours should offer weekday booking."""
        # 10 PM on Monday (after hours)
        mock_time.return_value = self._mock_time(22, 0, 0)

        result = get_business_hours_info(
            is_hplus_member=False,
            is_emergency=False
        )

        assert result["eligible"] is False
        assert result["offer_weekday"] is True
        assert result.get("offer_next_business_day") is None


class TestEndpointIntegration:
    """
    Integration tests simulating full endpoint behavior.
    Tests that transfer_to is correctly set based on eligibility.
    """

    @patch('app.services.business_hours.get_current_time')
    def test_non_member_emergency_after_hours_no_transfer(self, mock_time):
        """
        Non-member emergency after-hours should have transfer_to=None
        (simulating what main.py does with the result).
        """
        from datetime import datetime
        mock_time.return_value = datetime(2026, 6, 8, 22, 0, tzinfo=TIMEZONE)

        result = get_business_hours_info(
            is_hplus_member=False,
            is_emergency=True
        )

        # Simulate main.py logic
        if not result["eligible"]:
            transfer_to = None
        elif result["period"] == "standard":
            transfer_to = "office"
        else:
            transfer_to = "after_hours_line"

        assert transfer_to is None
        assert result["fee"] == 99

    @patch('app.services.business_hours.get_current_time')
    def test_hplus_member_emergency_after_hours_transfers(self, mock_time):
        """
        H+ member emergency after-hours should transfer to after_hours_line.
        """
        from datetime import datetime
        mock_time.return_value = datetime(2026, 6, 8, 22, 0, tzinfo=TIMEZONE)

        result = get_business_hours_info(
            is_hplus_member=True,
            is_emergency=True
        )

        # Simulate main.py logic
        if not result["eligible"]:
            transfer_to = None
        elif result["period"] == "standard":
            transfer_to = "office"
        else:
            transfer_to = "after_hours_line"

        assert transfer_to == "after_hours_line"
        assert result["fee"] == 199


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
