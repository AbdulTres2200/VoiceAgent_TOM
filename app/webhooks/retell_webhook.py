import os
import hmac
import hashlib
import json
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException
from dotenv import load_dotenv
from app.services.servicetitan_notes import post_call_note_to_job
from app.services.retell import get_job_id_for_call

load_dotenv()

RETELL_API_KEY = os.getenv("RETELL_API_KEY")

router = APIRouter()

# In-memory store for call data (keyed by call_id)
call_store = {}


def verify_retell_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """
    Verify Retell webhook signature using HMAC SHA-256.
    Tries multiple methods to handle different Retell signature formats.
    """
    try:
        # Parse signature header: v=1776489325982,d=4e...
        parts = {}
        for part in signature.split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                parts[key] = value

        timestamp = parts.get("v", "")
        received_sig = parts.get("d", signature)  # Fallback to full signature

        print(f"[Retell Webhook] Timestamp: {timestamp}")
        print(f"[Retell Webhook] Received sig: {received_sig[:30]}...")

        # Method 1: Just the body (no timestamp)
        sig1 = hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()
        print(f"[Retell Webhook] Method 1 (body only): {sig1[:30]}...")
        if hmac.compare_digest(sig1, received_sig):
            print("[Retell Webhook] Method 1 matched!")
            return True

        # Method 2: timestamp.body
        if timestamp:
            signed_payload2 = f"{timestamp}.".encode('utf-8') + raw_body
            sig2 = hmac.new(secret.encode('utf-8'), signed_payload2, hashlib.sha256).hexdigest()
            print(f"[Retell Webhook] Method 2 (ts.body): {sig2[:30]}...")
            if hmac.compare_digest(sig2, received_sig):
                print("[Retell Webhook] Method 2 matched!")
                return True

            # Method 3: body.timestamp
            signed_payload3 = raw_body + f".{timestamp}".encode('utf-8')
            sig3 = hmac.new(secret.encode('utf-8'), signed_payload3, hashlib.sha256).hexdigest()
            print(f"[Retell Webhook] Method 3 (body.ts): {sig3[:30]}...")
            if hmac.compare_digest(sig3, received_sig):
                print("[Retell Webhook] Method 3 matched!")
                return True

        print("[Retell Webhook] No signature method matched")
        return False

    except Exception as e:
        print(f"[Retell Webhook] Signature verification error: {e}")
        return False


def format_duration(duration_ms: int) -> str:
    """
    Convert duration in milliseconds to human readable format.

    Args:
        duration_ms: Duration in milliseconds

    Returns:
        String in format "X min Y sec"
    """
    if duration_ms is None:
        return "Unknown"

    total_seconds = duration_ms // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60

    return f"{minutes} min {seconds} sec"


def format_timestamp(timestamp_ms: int) -> str:
    """
    Convert Unix timestamp in milliseconds to human readable UTC datetime.

    Args:
        timestamp_ms: Unix timestamp in milliseconds

    Returns:
        Human readable UTC datetime string
    """
    if timestamp_ms is None:
        return "Unknown"

    try:
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "Unknown"


@router.post("/retell")
async def retell_webhook(request: Request):
    """
    Retell AI webhook handler.
    Receives call events and posts notes to ServiceTitan jobs.
    """
    # Get raw body for signature verification AND JSON parsing
    raw_body = await request.body()

    # Verify signature if RETELL_API_KEY is set
    # TODO: Retell webhook signing uses a different key - skipping verification for now
    signature = request.headers.get("x-retell-signature", "")
    if signature:
        print(f"[Retell Webhook] Signature received (verification skipped): {signature[:40]}...")
    else:
        print("[Retell Webhook] No signature header (OK for testing)")

    # Parse JSON from raw_body (don't call request.json() - body already consumed)
    try:
        payload = json.loads(raw_body)
    except Exception as e:
        print(f"[Retell Webhook] Failed to parse JSON: {e}")
        return {"status": "ok"}

    event = payload.get("event")
    # Retell uses "call" key, but we also support "data" for compatibility
    data = payload.get("call") or payload.get("data", {})

    print(f"[Retell Webhook] Received event: {event}")
    print(f"[Retell Webhook] Payload keys: {list(payload.keys())}")

    # Get call_id first
    call_id = data.get("call_id", "Unknown")

    # Extract job_id from metadata (inside call/data object)
    metadata = data.get("metadata", {}) or {}
    job_id = metadata.get("job_id")

    # Also check retell_llm_dynamic_variables for job_id
    if not job_id:
        dynamic_vars = data.get("retell_llm_dynamic_variables", {}) or {}
        job_id = dynamic_vars.get("job_id")

    # Check our local call_id -> job_id mapping
    if not job_id and call_id != "Unknown":
        job_id = get_job_id_for_call(call_id)
        if job_id:
            print(f"[Retell Webhook] Found job_id {job_id} from local mapping for call {call_id}")

    if not job_id:
        print("[Retell Webhook] WARNING: No job_id in metadata or local mapping, skipping note posting")
        return {"status": "ok"}

    # Handle call_ended event
    if event == "call_ended":
        recording_url = data.get("recording_url")
        duration_ms = data.get("duration_ms")
        start_timestamp = data.get("start_timestamp")
        from_number = data.get("from_number")
        to_number = data.get("to_number")

        # Check for duplicate
        if call_id in call_store and call_store[call_id].get("call_ended_processed"):
            print(f"[Retell Webhook] WARNING: Duplicate call_ended event for {call_id}, skipping")
            return {"status": "ok"}

        # Store call data
        if call_id not in call_store:
            call_store[call_id] = {"call_id": call_id, "job_id": job_id, "created_at": datetime.now(timezone.utc).isoformat()}
        call_store[call_id].update({
            "recording_url": recording_url,
            "duration_ms": duration_ms,
            "start_timestamp": start_timestamp,
            "from_number": from_number,
            "to_number": to_number
        })

        # Format values
        duration_str = format_duration(duration_ms)
        datetime_str = format_timestamp(start_timestamp)
        recording_str = recording_url if recording_url else "Not available"
        from_str = from_number if from_number else "Unknown"
        to_str = to_number if to_number else "Unknown"

        # Build note text
        note_text = (
            f"📞 Retell.ai Call Recording\n\n"
            f"Date: {datetime_str}\n"
            f"Caller: {from_str}\n"
            f"Called: {to_str}\n"
            f"Duration: {duration_str}\n"
            f"Recording URL: {recording_str}\n"
            f"Call ID: {call_id}"
        )

        print(f"[Retell Webhook] Posting call_ended note to job {job_id}")
        success = await post_call_note_to_job(job_id, note_text)

        call_store[call_id]["call_ended_note_posted"] = success
        call_store[call_id]["call_ended_processed"] = True

        if not success:
            print(f"[Retell Webhook] Failed to post call_ended note to job {job_id}")

        return {"status": "ok"}

    # Handle call_analyzed event
    if event == "call_analyzed":
        transcript = data.get("transcript")
        call_analysis = data.get("call_analysis", {})
        call_summary = call_analysis.get("call_summary") if call_analysis else None

        # Check for duplicate
        if call_id in call_store and call_store[call_id].get("call_analyzed_processed"):
            print(f"[Retell Webhook] WARNING: Duplicate call_analyzed event for {call_id}, skipping")
            return {"status": "ok"}

        # Store call data
        if call_id not in call_store:
            call_store[call_id] = {"call_id": call_id, "job_id": job_id, "created_at": datetime.now(timezone.utc).isoformat()}
        call_store[call_id].update({
            "transcript": transcript,
            "call_summary": call_summary
        })

        # Format values
        summary_str = call_summary if call_summary else "No summary available"
        transcript_str = transcript if transcript else "No transcript available"

        # Build note text
        note_text = (
            f"📋 Retell.ai Call Transcript & Summary\n\n"
            f"── Summary ──\n"
            f"{summary_str}\n\n"
            f"── Full Transcript ──\n"
            f"{transcript_str}"
        )

        print(f"[Retell Webhook] Posting call_analyzed note to job {job_id}")
        success = await post_call_note_to_job(job_id, note_text)

        call_store[call_id]["call_analyzed_note_posted"] = success
        call_store[call_id]["call_analyzed_processed"] = True

        if not success:
            print(f"[Retell Webhook] Failed to post call_analyzed note to job {job_id}")

        return {"status": "ok"}

    # Unknown event type - return 200 OK
    print(f"[Retell Webhook] Unknown event type: {event}, returning OK")
    return {"status": "ok"}


@router.get("/retell/calls")
async def get_all_calls():
    """Get all stored call data."""
    return {"calls": list(call_store.values()), "total": len(call_store)}


@router.get("/retell/calls/{job_id}")
async def get_calls_by_job(job_id: str):
    """Get all calls for a specific ServiceTitan job."""
    calls = [c for c in call_store.values() if c.get("job_id") == job_id]
    return {"job_id": job_id, "calls": calls, "total": len(calls)}


@router.get("/retell/retry-failed")
async def retry_failed_notes():
    """Retry posting notes that failed."""
    results = {"retried": 0, "succeeded": 0, "failed": 0, "details": []}

    for call_id, call_data in call_store.items():
        job_id = call_data.get("job_id")
        if not job_id:
            continue

        # Retry call_ended note if it failed
        if call_data.get("call_ended_processed") and not call_data.get("call_ended_note_posted"):
            results["retried"] += 1
            from_str = call_data.get("from_number") or "Unknown"
            to_str = call_data.get("to_number") or "Unknown"
            note_text = (
                f"📞 Retell.ai Call Recording\n\n"
                f"Date: {format_timestamp(call_data.get('start_timestamp'))}\n"
                f"Caller: {from_str}\n"
                f"Called: {to_str}\n"
                f"Duration: {format_duration(call_data.get('duration_ms'))}\n"
                f"Recording URL: {call_data.get('recording_url') or 'Not available'}\n"
                f"Call ID: {call_id}"
            )
            success = await post_call_note_to_job(job_id, note_text)
            if success:
                call_store[call_id]["call_ended_note_posted"] = True
                results["succeeded"] += 1
                results["details"].append({"call_id": call_id, "type": "call_ended", "status": "success"})
            else:
                results["failed"] += 1
                results["details"].append({"call_id": call_id, "type": "call_ended", "status": "failed"})

        # Retry call_analyzed note if it failed
        if call_data.get("call_analyzed_processed") and not call_data.get("call_analyzed_note_posted"):
            results["retried"] += 1
            note_text = (
                f"📋 Retell.ai Call Transcript & Summary\n\n"
                f"── Summary ──\n"
                f"{call_data.get('call_summary') or 'No summary available'}\n\n"
                f"── Full Transcript ──\n"
                f"{call_data.get('transcript') or 'No transcript available'}"
            )
            success = await post_call_note_to_job(job_id, note_text)
            if success:
                call_store[call_id]["call_analyzed_note_posted"] = True
                results["succeeded"] += 1
                results["details"].append({"call_id": call_id, "type": "call_analyzed", "status": "success"})
            else:
                results["failed"] += 1
                results["details"].append({"call_id": call_id, "type": "call_analyzed", "status": "failed"})

    return results
