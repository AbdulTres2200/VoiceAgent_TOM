#!/usr/bin/env python3
"""
Test script for Retell webhook with signature verification.
Simulates Retell sending call_ended and call_analyzed events.
"""
import hmac
import hashlib
import json
import requests
import os
from dotenv import load_dotenv

load_dotenv()

RETELL_API_KEY = os.getenv("RETELL_API_KEY")
BASE_URL = "http://localhost:8000"

# Replace with a real ServiceTitan job ID to test note posting
TEST_JOB_ID = "1811441646"


def send_webhook(event_type, data):
    """Send a signed webhook request."""
    payload = {"event": event_type, "data": data}
    body = json.dumps(payload).encode('utf-8')

    # Retell signature format: v=<timestamp>,d=<signature>
    # Signed payload: <timestamp>.<body>
    import time
    timestamp = str(int(time.time() * 1000))
    signed_payload = f"{timestamp}.".encode('utf-8') + body
    sig = hmac.new(RETELL_API_KEY.encode('utf-8'), signed_payload, hashlib.sha256).hexdigest()
    signature = f"v={timestamp},d={sig}"

    print(f"\n{'='*50}")
    print(f"Sending {event_type} webhook...")
    print(f"Payload: {json.dumps(payload, indent=2)}")

    response = requests.post(
        f"{BASE_URL}/webhook/retell",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-retell-signature": signature
        }
    )
    print(f"Response: {response.status_code} - {response.json()}")
    return response


def test_call_ended():
    """Simulate Retell call_ended event."""
    return send_webhook("call_ended", {
        "call_id": "test-call-001",
        "from_number": "+14125551234",
        "to_number": "+14126596858",
        "recording_url": "https://storage.retellai.com/test-recording.mp3",
        "duration_ms": 125000,  # 2 min 5 sec
        "start_timestamp": 1713450000000,
        "metadata": {"job_id": TEST_JOB_ID}
    })


def test_call_analyzed():
    """Simulate Retell call_analyzed event."""
    return send_webhook("call_analyzed", {
        "call_id": "test-call-001",
        "transcript": "Agent: Hello, thank you for calling. How can I help you today?\nCustomer: Hi, I have a leaking water heater in my basement.\nAgent: I'm sorry to hear that. Let me get your information and schedule a technician.",
        "call_analysis": {
            "call_summary": "Customer called about a leaking water heater in their basement. Appointment scheduled for next day morning window."
        },
        "metadata": {"job_id": TEST_JOB_ID}
    })


def test_duplicate_protection():
    """Test that duplicate events are rejected."""
    print(f"\n{'='*50}")
    print("Testing duplicate protection (sending same call_ended again)...")
    return send_webhook("call_ended", {
        "call_id": "test-call-001",  # Same call_id as before
        "from_number": "+14125551234",
        "to_number": "+14126596858",
        "recording_url": "https://storage.retellai.com/test-recording.mp3",
        "duration_ms": 125000,
        "start_timestamp": 1713450000000,
        "metadata": {"job_id": TEST_JOB_ID}
    })


def get_all_calls():
    """Fetch all stored calls."""
    print(f"\n{'='*50}")
    print("Fetching all stored calls...")
    response = requests.get(f"{BASE_URL}/webhook/retell/calls")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response


def get_calls_by_job(job_id):
    """Fetch calls for a specific job."""
    print(f"\n{'='*50}")
    print(f"Fetching calls for job {job_id}...")
    response = requests.get(f"{BASE_URL}/webhook/retell/calls/{job_id}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response


def retry_failed():
    """Retry any failed note posts."""
    print(f"\n{'='*50}")
    print("Retrying failed note posts...")
    response = requests.get(f"{BASE_URL}/webhook/retell/retry-failed")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response


if __name__ == "__main__":
    print("Retell Webhook Test Script")
    print(f"Base URL: {BASE_URL}")
    print(f"RETELL_API_KEY: {'SET' if RETELL_API_KEY else 'NOT SET'}")
    print(f"Test Job ID: {TEST_JOB_ID}")

    if not RETELL_API_KEY:
        print("\nWARNING: RETELL_API_KEY not set in .env file!")
        print("Signature verification will be skipped.\n")

    # Run tests
    test_call_ended()
    test_call_analyzed()
    test_duplicate_protection()
    get_all_calls()
    get_calls_by_job(TEST_JOB_ID)
    retry_failed()

    print(f"\n{'='*50}")
    print("Done! Check your server logs and ServiceTitan job notes.")
