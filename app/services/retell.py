import os
import requests
from dotenv import load_dotenv

load_dotenv()

RETELL_API_KEY = os.getenv("RETELL_API_KEY")

# Local store to map call_id to job_id (for webhook lookup)
call_job_mapping = {}


def store_call_job_mapping(call_id: str, job_id: str):
    """Store call_id -> job_id mapping for webhook lookup."""
    if call_id and job_id:
        call_job_mapping[call_id] = str(job_id)
        print(f"[Retell] Stored mapping: {call_id} -> job_id {job_id}")


def get_job_id_for_call(call_id: str) -> str:
    """Get job_id for a call_id from local store."""
    return call_job_mapping.get(call_id)


def update_call_metadata(call_id: str, metadata: dict) -> bool:
    """
    Update Retell call metadata with job_id so it's included in webhooks.

    Args:
        call_id: The Retell call ID
        metadata: Dict of metadata to add (e.g., {"job_id": "12345"})

    Returns:
        True on success, False on failure
    """
    if not call_id or not RETELL_API_KEY:
        print(f"[Retell] Cannot update metadata: call_id={call_id}, API_KEY={'SET' if RETELL_API_KEY else 'NOT SET'}")
        return False

    try:
        # Try the register-call endpoint to update metadata
        url = f"https://api.retellai.com/v2/register-call/{call_id}"
        headers = {
            "Authorization": f"Bearer {RETELL_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {"metadata": metadata}

        print(f"[Retell] Updating call {call_id} metadata: {metadata}")
        print(f"[Retell] URL: {url}")
        response = requests.patch(url, headers=headers, json=payload)

        # If that fails, try the calls endpoint
        if response.status_code == 404:
            print(f"[Retell] register-call failed, trying calls endpoint...")
            url = f"https://api.retellai.com/v2/calls/{call_id}"
            response = requests.patch(url, headers=headers, json=payload)

        print(f"[Retell] Response: {response.status_code} - {response.text[:200]}")

        if response.status_code in (200, 201, 204):
            print(f"[Retell] Successfully updated call metadata")
            return True
        else:
            print(f"[Retell] Failed to update metadata: {response.status_code}")
            return False

    except Exception as e:
        print(f"[Retell] Error updating call metadata: {e}")
        return False
