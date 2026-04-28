import os
import requests
from dotenv import load_dotenv
from app.services.servicetitan import get_access_token

load_dotenv()

TENANT_ID = os.getenv("TENANT_ID")
APP_KEY = os.getenv("APP_KEY")


def post_job_note(job_id: str, note_text: str, pin: bool = False) -> bool:
    """
    Post a note to a ServiceTitan job (synchronous version).

    Args:
        job_id: The ServiceTitan job ID
        note_text: The text content of the note
        pin: Whether to pin the note (default False)

    Returns:
        True on success, False on failure.
    """
    try:
        print(f"[ServiceTitan Notes] Posting note to job {job_id}")

        token = get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "ST-App-Key": APP_KEY,
            "Content-Type": "application/json"
        }

        url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs/{job_id}/notes"

        payload = {
            "text": note_text,
            "isPinned": pin
        }

        response = requests.post(url, headers=headers, json=payload)

        if response.status_code in (200, 201):
            print(f"[ServiceTitan Notes] Successfully posted note to job {job_id}")
            return True
        else:
            print(f"[ServiceTitan Notes] Failed: {response.status_code} - {response.text[:200]}")
            return False

    except Exception as e:
        print(f"[ServiceTitan Notes] Exception: {e}")
        return False


async def post_call_note_to_job(job_id: str, note_text: str) -> bool:
    """
    Post a note to a ServiceTitan job.

    Args:
        job_id: The ServiceTitan job ID
        note_text: The text content of the note

    Returns:
        True on success, False on failure. Never raises exceptions.
    """
    try:
        print(f"[ServiceTitan Notes] Posting note to job {job_id}")

        token = get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "ST-App-Key": APP_KEY,
            "Content-Type": "application/json"
        }

        url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs/{job_id}/notes"

        payload = {
            "text": note_text,
            "isPinned": False
        }

        print(f"[ServiceTitan Notes] POST {url}")
        print(f"[ServiceTitan Notes] Payload: {payload}")

        response = requests.post(url, headers=headers, json=payload)

        print(f"[ServiceTitan Notes] Response status: {response.status_code}")
        print(f"[ServiceTitan Notes] Response body: {response.text}")

        if response.status_code in (200, 201):
            print(f"[ServiceTitan Notes] Successfully posted note to job {job_id}")
            return True
        else:
            print(f"[ServiceTitan Notes] Failed to post note to job {job_id}: "
                  f"Status {response.status_code}, Response: {response.text}")
            return False

    except Exception as e:
        print(f"[ServiceTitan Notes] Exception posting note to job {job_id}: {e}")
        return False
