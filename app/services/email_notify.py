"""
Email notification service for Maria AI call summaries.
Sends mobile-friendly email summaries after each call.
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# Email configuration
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp-mail.outlook.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SENDER_EMAIL = os.getenv("SARAH_EMAIL", "")
SENDER_PASSWORD = os.getenv("SARAH_EMAIL_PASSWORD", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "abdulmoiz2200@outlook.com")


def send_call_summary(
    call_type: str,
    customer_name: str,
    phone: str,
    address: str = None,
    email: str = None,
    issue: str = None,
    appointment_time: str = None,
    job_id: str = None,
    lead_id: str = None,
    job_type: str = None,
    dispatch_status: str = None,
    business_unit: str = None,
    is_emergency: bool = False,
    is_excavation: bool = False,
    error: str = None
):
    """
    Send a mobile-friendly email summary of a call.

    Args:
        call_type: BOOKING, INQUIRY, VENDOR, etc.
        customer_name: Customer's name
        phone: Customer's phone number
        address: Service address
        email: Customer's email
        issue: Issue description
        appointment_time: Scheduled time
        job_id: ServiceTitan job ID (if created)
        lead_id: ServiceTitan lead ID (if created)
        job_type: Detected job type
        dispatch_status: Auto-dispatch result
        business_unit: Business unit name
        is_emergency: Emergency flag
        is_excavation: Excavation flag
        error: Error message if something failed
    """
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        print("[Email] Skipping - OUTLOOK_EMAIL or OUTLOOK_PASSWORD not configured")
        return False

    try:
        # Determine status emoji and color
        if error:
            status_emoji = "❌"
            status_text = "FAILED"
        elif job_id:
            status_emoji = "✅"
            status_text = "JOB BOOKED"
        elif lead_id:
            status_emoji = "📋"
            status_text = "LEAD CREATED"
        else:
            status_emoji = "📞"
            status_text = call_type

        # Build subject line
        timestamp = datetime.now().strftime("%I:%M %p")
        subject = f"{status_emoji} {status_text}: {customer_name} - {timestamp}"

        # Build mobile-friendly HTML body
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 10px;">
            <div style="background: {'#dc3545' if error else '#28a745' if job_id else '#ffc107'}; color: white; padding: 15px; border-radius: 8px; margin-bottom: 15px;">
                <h2 style="margin: 0; font-size: 18px;">{status_emoji} {status_text}</h2>
                <p style="margin: 5px 0 0 0; font-size: 14px;">{datetime.now().strftime("%B %d, %Y at %I:%M %p")}</p>
            </div>

            <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin-bottom: 10px;">
                <h3 style="margin: 0 0 10px 0; font-size: 16px; color: #333;">Customer</h3>
                <p style="margin: 5px 0; font-size: 15px;"><strong>{customer_name}</strong></p>
                <p style="margin: 5px 0; font-size: 14px;">📱 {phone}</p>
                {f'<p style="margin: 5px 0; font-size: 14px;">📧 {email}</p>' if email else ''}
                {f'<p style="margin: 5px 0; font-size: 14px;">📍 {address}</p>' if address else ''}
            </div>

            {f'''
            <div style="background: #e3f2fd; padding: 15px; border-radius: 8px; margin-bottom: 10px;">
                <h3 style="margin: 0 0 10px 0; font-size: 16px; color: #333;">Issue</h3>
                <p style="margin: 5px 0; font-size: 14px;">{issue}</p>
                {f'<p style="margin: 5px 0; font-size: 14px;"><strong>Job Type:</strong> {job_type}</p>' if job_type else ''}
            </div>
            ''' if issue else ''}

            {f'''
            <div style="background: #fff3e0; padding: 15px; border-radius: 8px; margin-bottom: 10px;">
                <h3 style="margin: 0 0 10px 0; font-size: 16px; color: #333;">Appointment</h3>
                <p style="margin: 5px 0; font-size: 14px;">🕐 {appointment_time}</p>
                {f'<p style="margin: 5px 0; font-size: 14px;">🏢 {business_unit}</p>' if business_unit else ''}
                {'<p style="margin: 5px 0; font-size: 14px; color: #dc3545;"><strong>🚨 EMERGENCY</strong></p>' if is_emergency else ''}
                {'<p style="margin: 5px 0; font-size: 14px; color: #6f42c1;"><strong>🔧 EXCAVATION</strong></p>' if is_excavation else ''}
            </div>
            ''' if appointment_time else ''}

            {f'''
            <div style="background: #d4edda; padding: 15px; border-radius: 8px; margin-bottom: 10px;">
                <h3 style="margin: 0 0 10px 0; font-size: 16px; color: #333;">Result</h3>
                <p style="margin: 5px 0; font-size: 14px;"><strong>Job ID:</strong> {job_id}</p>
                {f'<p style="margin: 5px 0; font-size: 14px;"><strong>Dispatch:</strong> {dispatch_status}</p>' if dispatch_status else ''}
            </div>
            ''' if job_id else ''}

            {f'''
            <div style="background: #fff3cd; padding: 15px; border-radius: 8px; margin-bottom: 10px;">
                <h3 style="margin: 0 0 10px 0; font-size: 16px; color: #333;">Lead Created</h3>
                <p style="margin: 5px 0; font-size: 14px;"><strong>Lead ID:</strong> {lead_id}</p>
            </div>
            ''' if lead_id else ''}

            {f'''
            <div style="background: #f8d7da; padding: 15px; border-radius: 8px; margin-bottom: 10px;">
                <h3 style="margin: 0 0 10px 0; font-size: 16px; color: #721c24;">Error</h3>
                <p style="margin: 5px 0; font-size: 14px;">{error}</p>
            </div>
            ''' if error else ''}

            <p style="font-size: 12px; color: #666; text-align: center; margin-top: 20px;">
                Maria AI - Hearn Plumbing, Heating & Air
            </p>
        </body>
        </html>
        """

        # Create message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SENDER_EMAIL
        msg["To"] = NOTIFY_EMAIL

        # Plain text fallback
        plain_text = f"""
{status_text}: {customer_name}
{datetime.now().strftime("%B %d, %Y at %I:%M %p")}

Customer: {customer_name}
Phone: {phone}
{f'Email: {email}' if email else ''}
{f'Address: {address}' if address else ''}

{f'Issue: {issue}' if issue else ''}
{f'Job Type: {job_type}' if job_type else ''}
{f'Appointment: {appointment_time}' if appointment_time else ''}
{f'Business Unit: {business_unit}' if business_unit else ''}
{'EMERGENCY' if is_emergency else ''}
{'EXCAVATION' if is_excavation else ''}

{f'Job ID: {job_id}' if job_id else ''}
{f'Lead ID: {lead_id}' if lead_id else ''}
{f'Dispatch: {dispatch_status}' if dispatch_status else ''}
{f'Error: {error}' if error else ''}

- Maria AI
        """

        msg.attach(MIMEText(plain_text, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        # Send email
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, NOTIFY_EMAIL, msg.as_string())

        print(f"[Email] Sent summary to {NOTIFY_EMAIL}")
        return True

    except Exception as e:
        print(f"[Email] Failed to send: {e}")
        return False
