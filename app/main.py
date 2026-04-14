from fastapi import FastAPI, Request
from app.routes import booking
from app.services.servicetitan import test_connection, lookup_customer_by_phone, explore_account, lookup_by_address, fetch_account_config

app = FastAPI(
    title="ServiceTitan Voice Agent",
    description="Retell AI Integration for ServiceTitan",
    version="1.0.0"
)

# Include routers
app.include_router(booking.router, tags=["Booking"])


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/test-st-connection")
async def test_servicetitan_connection():
    """
    Test ServiceTitan API connection.
    Verifies credentials by fetching employees list.
    """
    result = test_connection()
    return result


@app.get("/lookup-caller/{phone}")
async def lookup_caller(phone: str):
    """
    Look up a caller by phone number in ServiceTitan CRM.
    Returns customer info, contacts, and recent job history.
    """
    result = lookup_customer_by_phone(phone)
    return result


@app.post("/lookup-caller")
async def lookup_caller_post(request: Request):
    """
    Look up a caller by phone number (POST version for Retell AI).
    Accepts phone in request body with optional 'args' nesting.
    """
    data = await request.json()
    args = data.get('args', data)
    phone = args.get('phone') or args.get('caller_phone') or args.get('from_number')

    if not phone:
        return {"found": False, "error": "No phone number provided"}

    result = lookup_customer_by_phone(phone)
    return result


@app.get("/explore-st")
async def explore_servicetitan():
    """
    Explore ServiceTitan account structure.
    Returns job types, sample customers, and sample appointments.
    """
    result = explore_account()
    return result


@app.post("/inbound-webhook")
async def inbound_webhook(request: Request):
    """
    Retell AI inbound call webhook.
    Receives call payload, looks up caller in ServiceTitan CRM,
    and returns dynamic variables for the AI agent.
    """
    data = await request.json()

    # Get from_number from Retell payload
    from_number = data.get("from_number") or data.get("call", {}).get("from_number") or ""

    # Clean phone number: strip +1, dashes, spaces, parentheses
    clean_phone = from_number.replace("+1", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")

    print(f"[Inbound Webhook] Received call from: {from_number} -> cleaned: {clean_phone}")

    # Look up customer in ServiceTitan
    result = lookup_customer_by_phone(clean_phone)

    if result.get("found"):
        customer = result.get("customer", {})
        address = customer.get("address", {})
        recent_jobs = result.get("recent_jobs", [])

        # Format address
        address_str = f"{address.get('street', '')} {address.get('city', '')}".strip()

        # Format recent job
        recent_job_str = ""
        if recent_jobs:
            job = recent_jobs[0]
            recent_job_str = f"{job.get('summary', 'N/A')} - {job.get('status', 'N/A')}"

        return {
            "call_inbound": {
                "dynamic_variables": {
                    "customer_name": customer.get("name", ""),
                    "customer_address": address_str,
                    "customer_found": "true",
                    "recent_job": recent_job_str
                }
            }
        }
    else:
        return {
            "call_inbound": {
                "dynamic_variables": {
                    "customer_name": "",
                    "customer_address": "",
                    "customer_found": "false",
                    "recent_job": ""
                }
            }
        }


@app.post("/lookup-by-address")
async def lookup_by_address_endpoint(request: Request):
    """
    Look up a customer by street address in ServiceTitan CRM.
    Returns customer info for Retell AI.
    """
    data = await request.json()
    args = data.get('args', data)
    address = args.get('address') or args.get('street') or ""

    # Print formatted log
    print("\n")
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║              ADDRESS LOOKUP REQUEST                          ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print(f"║  Address: {address:<50} ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    if not address:
        print("║  Result: No address provided                                 ║")
        print("╚══════════════════════════════════════════════════════════════╝\n")
        return {"found": False, "message": "No address provided"}

    result = lookup_by_address(address)

    if result.get("found"):
        customer = result.get("customer", {})
        addr = customer.get("address", {})
        recent_jobs = result.get("recent_jobs", [])

        # Format full address
        address_str = f"{addr.get('street', '')} {addr.get('city', '')} {addr.get('state', '')}".strip()

        # Format recent job
        recent_job_str = ""
        if recent_jobs:
            job = recent_jobs[0]
            summary = job.get('summary', 'N/A').replace('\r', '').replace('\n', ' ').strip()
            recent_job_str = f"{summary} - {job.get('status', 'N/A')}"

        print("╔══════════════════════════════════════════════════════════════╗")
        print("║              ADDRESS LOOKUP RESULT                           ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print(f"║  Found: YES                                                  ║")
        print(f"║  Customer: {customer.get('name', ''):<49} ║")
        print(f"║  Address: {address_str:<50} ║")
        print(f"║  ID: {customer.get('id', ''):<55} ║")
        print("╚══════════════════════════════════════════════════════════════╝\n")

        return {
            "found": True,
            "customer_name": customer.get("name", ""),
            "customer_address": address_str,
            "customer_id": customer.get("id"),
            "recent_job": recent_job_str
        }
    else:
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║              ADDRESS LOOKUP RESULT                           ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print(f"║  Found: NO                                                   ║")
        print(f"║  Address searched: {address:<41} ║")
        print("╚══════════════════════════════════════════════════════════════╝\n")

        return {
            "found": False,
            "message": "No customer found with this address"
        }


@app.get("/st-config")
async def get_servicetitan_config():
    """
    Fetch ServiceTitan account configuration.
    Returns job types, business units, sample customers, and sample jobs.
    """
    result = fetch_account_config()
    return result
