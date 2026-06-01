# Voice Agent - Hearn Plumbing, Heating & Air

AI-powered voice agent "Maria" for Hearn Plumbing, Heating & Air. Built with Retell.ai and integrated with ServiceTitan CRM.

## Overview

Maria is an intelligent dispatcher that handles inbound calls, collects customer information, books appointments, and creates jobs directly in ServiceTitan. She supports both English and Spanish, handles H+ membership verification, and provides 24/7 availability.

## Features

- **Appointment Booking** - Collects customer info and creates jobs in ServiceTitan
- **Service Area Verification** - Validates addresses against ServiceTitan zones
- **H+ Membership Detection** - Checks and applies member pricing
- **Smart Job Type Detection** - AI-powered classification (HVAC, Plumbing, Water Heater, etc.)
- **Campaign Tracking** - Detects referral source and assigns campaigns
- **Business Hours & Pricing** - Dynamic fee calculation based on time and membership
- **Post-Call Processing** - Attaches transcripts and recordings to jobs

## Tech Stack

- **Voice AI**: Retell.ai
- **Backend**: FastAPI (Python)
- **CRM**: ServiceTitan API
- **AI**: OpenAI GPT-4o-mini (job type detection, campaign matching)
- **Geocoding**: Google Maps API

## Project Structure

```
├── app/
│   ├── main.py                 # FastAPI app & webhooks
│   ├── routes/
│   │   └── booking.py          # Appointment booking endpoint
│   ├── services/
│   │   ├── servicetitan.py     # ServiceTitan API integration
│   │   ├── service_area.py     # Address validation & zones
│   │   ├── business_hours.py   # Hours & pricing logic
│   │   ├── dispatch.py         # Technician dispatch
│   │   ├── retell.py           # Retell call tracking
│   │   └── email_notify.py     # Email notifications
│   └── webhooks/
│       └── retell_webhook.py   # Retell webhook handlers
├── prompts/
│   └── tom_vagent_prompt_v2.txt  # Maria's conversation prompt
├── retell_functions/           # Retell function definitions
└── test_scripts/               # VA testing scenarios
```

## Environment Variables

```env
# ServiceTitan
TENANT_ID=
APP_KEY=
CLIENT_ID=
CLIENT_SECRET=

# OpenAI
OPENAI_API_KEY=

# Retell
RETELL_API_KEY=

# Google Maps
GOOGLE_MAPS_API_KEY=

# Optional - Email notifications
OUTLOOK_EMAIL=
OUTLOOK_PASSWORD=
```

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /inbound-webhook` | Retell call events (started, ended, analyzed) |
| `POST /check-service-area` | Validate address & check membership |
| `POST /check-business-hours` | Get pricing based on time & membership |
| `POST /book-appointment` | Create customer & job in ServiceTitan |

## Deployment

Configured for Railway deployment:

```bash
# Local development
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload

# Production (Railway)
# Uses Procfile: web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Retell Configuration

1. Set webhook URL to `https://your-domain/inbound-webhook`
2. Enable events: `call_started`, `call_ended`, `call_analyzed`
3. Configure functions from `retell_functions/` folder
4. Update agent prompt from `prompts/tom_vagent_prompt_v2.txt`

## License

Private - Hearn Plumbing, Heating & Air
