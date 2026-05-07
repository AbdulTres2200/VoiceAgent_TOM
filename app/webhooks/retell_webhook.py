import os
import hmac
import hashlib
import json
import requests
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, HTTPException
from dotenv import load_dotenv
from app.services.servicetitan_notes import post_call_note_to_job
from app.services.retell import get_job_id_for_call
from app.services.servicetitan import create_lead, get_access_token, TENANT_ID, APP_KEY, clean_phone

load_dotenv()

RETELL_API_KEY = os.getenv("RETELL_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

router = APIRouter()

# In-memory store for call data (keyed by call_id)
call_store = {}

# Shared deduplication cache for lead creation (call_id -> timestamp)
# Prevents both /webhook/retell and /post-call-webhook from creating duplicate leads
import time
_lead_created_cache = {}
_LEAD_CACHE_TTL = 300  # 5 minutes


def is_lead_already_created(call_id: str) -> bool:
    """Check if a lead was already created for this call."""
    now = time.time()
    if call_id in _lead_created_cache:
        if now - _lead_created_cache[call_id] < _LEAD_CACHE_TTL:
            return True
    return False


def mark_lead_created(call_id: str):
    """Mark that a lead was created for this call."""
    _lead_created_cache[call_id] = time.time()
    # Clean up old entries
    expired = [k for k, v in _lead_created_cache.items() if time.time() - v > _LEAD_CACHE_TTL]
    for k in expired:
        del _lead_created_cache[k]


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

    print("\n")
    print("╔══════════════════════════════════════════════════════════════╗")
    print(f"║  RETELL WEBHOOK: {event:<43} ║")
    print("╠══════════════════════════════════════════════════════════════╣")

    # Get call_id first
    call_id = data.get("call_id", "Unknown")
    print(f"║  Call ID: {call_id:<50} ║")

    # Handle call_started and call_inbound - just acknowledge
    if event in ("call_started", "call_inbound"):
        print(f"║  Action: Acknowledged, no processing needed                   ║")
        print("╚══════════════════════════════════════════════════════════════╝\n")
        return {"status": "ok"}

    # Extract job_id from metadata (inside call/data object)
    metadata = data.get("metadata", {}) or {}
    job_id = metadata.get("job_id")

    # Also check retell_llm_dynamic_variables for job_id
    dynamic_vars = data.get("retell_llm_dynamic_variables", {}) or {}
    if not job_id:
        job_id = dynamic_vars.get("job_id")

    # Check our local call_id -> job_id mapping
    if not job_id and call_id != "Unknown":
        job_id = get_job_id_for_call(call_id)
        if job_id:
            print(f"║  Found job_id from local mapping: {job_id:<26} ║")

    # Get dynamic variables for lead creation
    campaign_id = dynamic_vars.get("campaign_id", 1410706053)
    business_unit_id = dynamic_vars.get("business_unit_id", 1239)
    from_number = data.get("from_number", "")
    to_number = data.get("to_number", "")

    # Handle call_ended event - store recording info, wait for call_analyzed
    if event == "call_ended":
        recording_url = data.get("recording_url")
        duration_ms = data.get("duration_ms")
        start_timestamp = data.get("start_timestamp")
        from_number = data.get("from_number") or from_number
        to_number = data.get("to_number") or to_number

        duration_seconds = (duration_ms // 1000) if duration_ms else 0
        print(f"║  From: {from_number:<53} ║")
        print(f"║  Duration: {duration_seconds}s{' ':<48}║")

        # Check for duplicate
        if call_id in call_store and call_store[call_id].get("call_ended_processed"):
            print(f"║  Action: Duplicate event, skipping                           ║")
            print("╚══════════════════════════════════════════════════════════════╝\n")
            return {"status": "ok"}

        # Store call data for later use
        if call_id not in call_store:
            call_store[call_id] = {"call_id": call_id, "job_id": job_id, "created_at": datetime.now(timezone.utc).isoformat()}
        call_store[call_id].update({
            "recording_url": recording_url,
            "duration_ms": duration_ms,
            "start_timestamp": start_timestamp,
            "from_number": from_number,
            "to_number": to_number,
            "campaign_id": campaign_id,
            "business_unit_id": business_unit_id
        })
        call_store[call_id]["call_ended_processed"] = True

        # If we have a job_id, post recording note to job
        if job_id:
            duration_str = format_duration(duration_ms)
            datetime_str = format_timestamp(start_timestamp)
            note_text = (
                f"📞 Retell.ai Call Recording\n\n"
                f"Date: {datetime_str}\n"
                f"Caller: {from_number or 'Unknown'}\n"
                f"Called: {to_number or 'Unknown'}\n"
                f"Duration: {duration_str}\n"
                f"Recording URL: {recording_url or 'Not available'}\n"
                f"Call ID: {call_id}"
            )
            success = await post_call_note_to_job(job_id, note_text)
            call_store[call_id]["call_ended_note_posted"] = success
            print(f"║  Action: Posted recording to job {job_id:<26} ║")
        else:
            print(f"║  Action: Stored recording, waiting for transcript            ║")

        print("╚══════════════════════════════════════════════════════════════╝\n")
        return {"status": "ok"}

    # Handle call_analyzed event - analyze transcript and create lead if needed
    if event == "call_analyzed":
        transcript = data.get("transcript", "")
        call_analysis = data.get("call_analysis", {}) or {}
        call_summary = call_analysis.get("call_summary", "")
        recording_url = data.get("recording_url") or ""
        duration_ms = data.get("duration_ms") or 0
        from_number = data.get("from_number") or from_number
        to_number = data.get("to_number") or to_number

        # Get stored data from call_ended event
        stored_data = call_store.get(call_id, {})
        if not recording_url:
            recording_url = stored_data.get("recording_url", "")
        if not duration_ms:
            duration_ms = stored_data.get("duration_ms", 0)
        if not from_number:
            from_number = stored_data.get("from_number", "")
        if not campaign_id or campaign_id == 1410706053:
            campaign_id = stored_data.get("campaign_id", 1410706053)
        if not business_unit_id or business_unit_id == 1239:
            business_unit_id = stored_data.get("business_unit_id", 1239)

        # Also check stored data from call_ended event for job_id
        if not job_id:
            job_id = stored_data.get("job_id")
            if job_id:
                print(f"║  Found job_id from stored call data: {job_id:<24} ║")

        duration_seconds = (duration_ms // 1000) if duration_ms else 0
        print(f"║  From: {from_number:<53} ║")
        print(f"║  Duration: {duration_seconds}s{' ':<48}║")
        print(f"║  Has transcript: {'Yes' if transcript else 'No':<43} ║")

        # Check for duplicate
        if call_id in call_store and call_store[call_id].get("call_analyzed_processed"):
            print(f"║  Action: Duplicate event, skipping                           ║")
            print("╚══════════════════════════════════════════════════════════════╝\n")
            return {"status": "ok"}

        # Store call data
        if call_id not in call_store:
            call_store[call_id] = {"call_id": call_id, "job_id": job_id, "created_at": datetime.now(timezone.utc).isoformat()}
        call_store[call_id].update({
            "transcript": transcript,
            "call_summary": call_summary,
            "call_analyzed_processed": True
        })

        # If we have a job_id, post transcript to job and we're done
        if job_id:
            note_text = (
                f"📋 Retell.ai Call Transcript & Summary\n\n"
                f"── Summary ──\n"
                f"{call_summary or 'No summary available'}\n\n"
                f"── Full Transcript ──\n"
                f"{transcript or 'No transcript available'}"
            )
            success = await post_call_note_to_job(job_id, note_text)
            call_store[call_id]["call_analyzed_note_posted"] = success
            print(f"║  Action: Posted transcript to job {job_id:<25} ║")
            print("╚══════════════════════════════════════════════════════════════╝\n")
            return {"status": "ok"}

        # No job_id - analyze transcript to determine if booking was made
        print(f"║  No job_id - analyzing transcript...                         ║")

        booking_made = "no"
        call_type = "OTHER"
        summary = call_summary or "Call transcript analysis unavailable"

        if transcript:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=OPENAI_API_KEY)

                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    max_tokens=150,
                    temperature=0,
                    messages=[
                        {
                            "role": "system",
                            "content": """Analyze this plumbing company call transcript and respond in EXACTLY this format:
BOOKING_MADE:yes or no
CALL_TYPE:BOOKING or INQUIRY or VENDOR or INVOICING or FOLLOWUP or SPAM or SILENT or OTHER
SUMMARY:Brief 2-3 sentence summary of the call

Use SPAM for: sales calls, solicitation, marketing pitches, business listing verification, SEO services, Google verification scams, robocalls, or any unsolicited promotional calls.
Use SILENT for: calls where the caller said nothing, immediate hangups, or no meaningful conversation occurred."""
                        },
                        {
                            "role": "user",
                            "content": f"Transcript:\n{transcript}"
                        }
                    ]
                )

                ai_response = response.choices[0].message.content.strip()

                # Parse the response
                for line in ai_response.split("\n"):
                    line = line.strip()
                    if line.startswith("BOOKING_MADE:"):
                        booking_made = line.replace("BOOKING_MADE:", "").strip().lower()
                    elif line.startswith("CALL_TYPE:"):
                        call_type = line.replace("CALL_TYPE:", "").strip().upper()
                    elif line.startswith("SUMMARY:"):
                        summary = line.replace("SUMMARY:", "").strip()

                print(f"║  AI Analysis: booking={booking_made}, type={call_type:<18} ║")

            except Exception as e:
                print(f"║  AI analysis failed: {str(e)[:38]:<39} ║")

        # Build note text for recording/transcript
        cleaned_from = clean_phone(from_number)
        note_text = f"""=== RETELL AI CALL RECORDING ===

Call ID: {call_id}
From: {from_number}
Duration: {duration_seconds}s
Recording: {recording_url}

=== TRANSCRIPT ===
{transcript}"""

        # Get ST API headers
        token = get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "ST-App-Key": APP_KEY,
            "Content-Type": "application/json"
        }

        action_result = "None"

        if booking_made == "yes":
            # Final check: try mapping one more time (in case booking completed after initial check)
            if not job_id and call_id != "Unknown":
                job_id = get_job_id_for_call(call_id)
                if job_id:
                    print(f"║  Found job_id from mapping (retry): {job_id:<24} ║")
                    # Post transcript to the job
                    note_text = (
                        f"📋 Retell.ai Call Transcript & Summary\n\n"
                        f"── Summary ──\n"
                        f"{call_summary or 'No summary available'}\n\n"
                        f"── Full Transcript ──\n"
                        f"{transcript or 'No transcript available'}"
                    )
                    success = await post_call_note_to_job(job_id, note_text)
                    call_store[call_id]["job_id"] = job_id
                    call_store[call_id]["call_analyzed_note_posted"] = success
                    action_result = f"Job {job_id}"
                    print(f"║  Action: Posted transcript to job {job_id:<25} ║")
                    print(f"╠══════════════════════════════════════════════════════════════╣")
                    print(f"║  RESULT: {action_result:<51} ║")
                    print("╚══════════════════════════════════════════════════════════════╝\n")
                    return {"status": "ok"}

            # Fallback: Search for recent job by phone (less reliable for shared numbers)
            jobs_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs"
            params = {"pageSize": 5, "orderBy": "Id", "orderByDirection": "desc"}
            if cleaned_from:
                params["phone"] = cleaned_from
                print(f"║  Fallback: Searching jobs by phone {cleaned_from:<24} ║")

            jobs_resp = requests.get(jobs_url, headers=headers, params=params)
            found_job_id = None

            if jobs_resp.status_code == 200:
                jobs = jobs_resp.json().get("data", [])
                two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)

                for job in jobs:
                    created_on = job.get("createdOn", "")
                    if created_on:
                        try:
                            job_created = datetime.fromisoformat(created_on.replace("Z", "+00:00"))
                            if job_created > two_hours_ago:
                                found_job_id = job.get("id")
                                break
                        except:
                            pass

            if found_job_id:
                # Add note to job
                note_url = f"https://api.servicetitan.io/jpm/v2/tenant/{TENANT_ID}/jobs/{found_job_id}/notes"
                note_resp = requests.post(note_url, headers=headers, json={"text": note_text})
                action_result = f"Job {found_job_id}"
                print(f"║  Action: Attached recording to job {found_job_id:<24} ║")
            else:
                # No job found, create lead instead
                booking_made = "no"

        if booking_made != "yes":
            # Skip lead creation for spam calls only
            if call_type == "SPAM":
                action_result = f"Skipped ({call_type})"
                print(f"║  Action: Skipped lead - {call_type:<36} ║")
            # Check if lead was already created by another webhook
            elif is_lead_already_created(call_id):
                action_result = "Lead already created (dedup)"
                print(f"║  Action: Lead already created, skipping (dedup)              ║")
            else:
                # For SILENT calls, add tag to summary for easy filtering
                lead_summary = summary
                if call_type == "SILENT":
                    lead_summary = "[NO RESPONSE] Caller did not speak or hung up immediately"

                # Create lead for non-booking call
                lead_id = create_lead(
                    call_type=call_type,
                    summary=lead_summary,
                    from_number=cleaned_from,
                    campaign_id=int(campaign_id),
                    business_unit_id=int(business_unit_id)
                )

                if lead_id:
                    # Mark as created to prevent duplicates
                    mark_lead_created(call_id)
                    # Add note to lead
                    note_url = f"https://api.servicetitan.io/crm/v2/tenant/{TENANT_ID}/leads/{lead_id}/notes"
                    note_resp = requests.post(note_url, headers=headers, json={"text": note_text})
                    action_result = f"Lead {lead_id}"
                    print(f"║  Action: Created lead {lead_id} with recording{' ':<17}║")
                else:
                    action_result = "Lead creation failed"
                    print(f"║  Action: Failed to create lead                               ║")

        print(f"╠══════════════════════════════════════════════════════════════╣")
        print(f"║  RESULT: {action_result:<51} ║")
        print("╚══════════════════════════════════════════════════════════════╝\n")

        return {"status": "ok"}

    # Unknown event type - just acknowledge
    print(f"║  Unknown event type: {event:<39} ║")
    print(f"║  Action: Acknowledged                                        ║")
    print("╚══════════════════════════════════════════════════════════════╝\n")
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
