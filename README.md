# Sarah Voice Agent

FastAPI backend connecting Retell AI voice agents to ServiceTitan CRM. Automates customer creation, job scheduling, and appointment booking.

## Features

- **Automated Booking Flow** - Creates customer, location, job, and appointment in ServiceTitan
- **Natural Language Dates** - Parses "tomorrow at 10am", "next monday", "asap" into ISO timestamps
- **Smart Address Parsing** - Extracts street, city, state, zip from free-form addresses
- **Phone Validation** - Cleans and validates US phone numbers
- **Real-time Integration** - Direct API integration with ServiceTitan CRM/JPM v2

## Tech Stack

- FastAPI
- ServiceTitan CRM/JPM API v2
- Retell AI
- dateparser
- usaddress

## Installation

```bash
# Clone the repo
git clone https://github.com/AbdulTres2200/sarah_voice_agent.git
cd sarah_voice_agent

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
# Edit .env with your credentials
```

## Environment Variables

```env
# ServiceTitan Credentials
TENANT_ID=your_tenant_id
APP_KEY=your_app_key
CLIENT_ID=your_client_id
CLIENT_SECRET=your_client_secret

# ServiceTitan Job Configuration
JOB_TYPE_ID=your_job_type_id
BUSINESS_UNIT_ID=your_business_unit_id
CAMPAIGN_ID=your_campaign_id
JOB_PRIORITY=Normal
DEFAULT_COUNTRY=US
DEFAULT_ZIP=15201
DEFAULT_STATE=PA
```

## Running the Server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## API Endpoints

### POST /book-appointment

Create a new booking in ServiceTitan.

**Request:**
```json
{
  "customer_name": "John Smith",
  "address": "123 Main Street, Pittsburgh, PA 15213",
  "phone": "4125551234",
  "email": "john@example.com",
  "issue_description": "Clogged drain",
  "appointment_time": "tomorrow at 10am",
  "customer_type": "Residential"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Your appointment has been booked successfully. Your job number is 123456.",
  "booking_details": {
    "customer_name": "John Smith",
    "appointment_start": "2026-04-16T10:00:00Z",
    "appointment_end": "2026-04-16T11:00:00Z",
    "servicetitan_response": { ... }
  }
}
```

## Booking Flow

1. **Create Customer** - POST to ServiceTitan /customers with name, address, contacts
2. **Create Job** - POST to ServiceTitan /jobs with customer_id, location_id, summary
3. **Appointment** - Included in job creation with start/end times

## License

MIT
