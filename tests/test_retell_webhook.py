import hmac
import hashlib
import json
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from app.main import app


client = TestClient(app)


def generate_signature(body: bytes, secret: str) -> str:
    """Generate HMAC SHA-256 signature for test payloads in Retell format."""
    import time
    timestamp = str(int(time.time() * 1000))
    signed_payload = f"{timestamp}.".encode('utf-8') + body
    sig = hmac.new(secret.encode('utf-8'), signed_payload, hashlib.sha256).hexdigest()
    return f"v={timestamp},d={sig}"


class TestRetellWebhook:
    """Tests for the Retell webhook endpoint."""

    @patch('app.webhooks.retell_webhook.RETELL_API_KEY', None)
    @patch('app.webhooks.retell_webhook.post_call_note_to_job', new_callable=AsyncMock)
    def test_call_ended_payload_returns_200(self, mock_post_note):
        """Test that a valid call_ended payload returns HTTP 200."""
        mock_post_note.return_value = True

        payload = {
            "event": "call_ended",
            "data": {
                "call_id": "test-call-123",
                "recording_url": "https://example.com/recording.mp3",
                "duration_ms": 125000,
                "start_timestamp": 1713456789000,
                "metadata": {
                    "job_id": "12345"
                }
            }
        }

        response = client.post(
            "/webhook/retell",
            json=payload
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        mock_post_note.assert_called_once()

        # Verify the note text contains expected content
        call_args = mock_post_note.call_args
        job_id = call_args[0][0]
        note_text = call_args[0][1]

        assert job_id == "12345"
        assert "Retell.ai Call Recording" in note_text
        assert "test-call-123" in note_text
        assert "https://example.com/recording.mp3" in note_text

    @patch('app.webhooks.retell_webhook.RETELL_API_KEY', None)
    @patch('app.webhooks.retell_webhook.post_call_note_to_job', new_callable=AsyncMock)
    def test_call_analyzed_payload_returns_200(self, mock_post_note):
        """Test that a valid call_analyzed payload returns HTTP 200."""
        mock_post_note.return_value = True

        payload = {
            "event": "call_analyzed",
            "data": {
                "call_id": "test-call-456",
                "transcript": "Hello, this is a test transcript.",
                "call_analysis": {
                    "call_summary": "Customer called about plumbing issue."
                },
                "metadata": {
                    "job_id": "67890"
                }
            }
        }

        response = client.post(
            "/webhook/retell",
            json=payload
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        mock_post_note.assert_called_once()

        # Verify the note text contains expected content
        call_args = mock_post_note.call_args
        job_id = call_args[0][0]
        note_text = call_args[0][1]

        assert job_id == "67890"
        assert "Retell.ai Call Transcript & Summary" in note_text
        assert "Customer called about plumbing issue." in note_text
        assert "Hello, this is a test transcript." in note_text

    @patch('app.webhooks.retell_webhook.RETELL_API_KEY', None)
    @patch('app.webhooks.retell_webhook.post_call_note_to_job', new_callable=AsyncMock)
    def test_payload_missing_job_id_returns_200(self, mock_post_note):
        """Test that a payload with missing job_id in metadata returns HTTP 200."""
        payload = {
            "event": "call_ended",
            "data": {
                "call_id": "test-call-789",
                "recording_url": "https://example.com/recording.mp3",
                "duration_ms": 60000,
                "start_timestamp": 1713456789000,
                "metadata": {}
            }
        }

        response = client.post(
            "/webhook/retell",
            json=payload
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        # Should not call post_call_note_to_job when job_id is missing
        mock_post_note.assert_not_called()

    @patch('app.webhooks.retell_webhook.RETELL_API_KEY', None)
    @patch('app.webhooks.retell_webhook.post_call_note_to_job', new_callable=AsyncMock)
    def test_payload_no_metadata_returns_200(self, mock_post_note):
        """Test that a payload with no metadata returns HTTP 200."""
        payload = {
            "event": "call_ended",
            "data": {
                "call_id": "test-call-999",
                "recording_url": "https://example.com/recording.mp3",
                "duration_ms": 60000,
                "start_timestamp": 1713456789000
            }
        }

        response = client.post(
            "/webhook/retell",
            json=payload
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        mock_post_note.assert_not_called()

    @patch('app.webhooks.retell_webhook.RETELL_API_KEY', None)
    @patch('app.webhooks.retell_webhook.post_call_note_to_job', new_callable=AsyncMock)
    def test_unknown_event_returns_200(self, mock_post_note):
        """Test that an unknown event type returns HTTP 200."""
        payload = {
            "event": "call_started",
            "data": {
                "call_id": "test-call-000",
                "metadata": {
                    "job_id": "11111"
                }
            }
        }

        response = client.post(
            "/webhook/retell",
            json=payload
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        mock_post_note.assert_not_called()

    @patch('app.webhooks.retell_webhook.RETELL_API_KEY', 'test-secret-key')
    @patch('app.webhooks.retell_webhook.post_call_note_to_job', new_callable=AsyncMock)
    def test_valid_signature_passes(self, mock_post_note):
        """Test that a valid signature passes verification."""
        mock_post_note.return_value = True

        payload = {
            "event": "call_ended",
            "data": {
                "call_id": "test-call-sig",
                "metadata": {
                    "job_id": "22222"
                }
            }
        }

        body = json.dumps(payload).encode('utf-8')
        signature = generate_signature(body, 'test-secret-key')

        response = client.post(
            "/webhook/retell",
            content=body,
            headers={
                "Content-Type": "application/json",
                "x-retell-signature": signature
            }
        )

        assert response.status_code == 200

    @patch('app.webhooks.retell_webhook.RETELL_API_KEY', 'test-secret-key')
    @patch('app.webhooks.retell_webhook.post_call_note_to_job', new_callable=AsyncMock)
    def test_invalid_signature_returns_200(self, mock_post_note):
        """Test that invalid signature still returns 200 (verification disabled)."""
        mock_post_note.return_value = True
        payload = {
            "event": "call_ended",
            "data": {
                "call_id": "test-call-invalid",
                "metadata": {
                    "job_id": "33333"
                }
            }
        }

        body = json.dumps(payload).encode('utf-8')

        response = client.post(
            "/webhook/retell",
            content=body,
            headers={
                "Content-Type": "application/json",
                "x-retell-signature": "invalid-signature"
            }
        )

        assert response.status_code == 200

    @patch('app.webhooks.retell_webhook.RETELL_API_KEY', 'test-secret-key')
    @patch('app.webhooks.retell_webhook.post_call_note_to_job', new_callable=AsyncMock)
    def test_missing_signature_returns_200(self, mock_post_note):
        """Test that missing signature still returns 200 (verification disabled)."""
        mock_post_note.return_value = True
        payload = {
            "event": "call_ended",
            "data": {
                "call_id": "test-call-missing",
                "metadata": {
                    "job_id": "44444"
                }
            }
        }

        response = client.post(
            "/webhook/retell",
            json=payload
        )

        assert response.status_code == 200

    @patch('app.webhooks.retell_webhook.RETELL_API_KEY', None)
    @patch('app.webhooks.retell_webhook.post_call_note_to_job', new_callable=AsyncMock)
    def test_call_ended_with_null_recording_url(self, mock_post_note):
        """Test call_ended with null recording_url shows 'Not available'."""
        mock_post_note.return_value = True

        payload = {
            "event": "call_ended",
            "data": {
                "call_id": "test-call-null-recording",
                "recording_url": None,
                "duration_ms": 30000,
                "start_timestamp": 1713456789000,
                "metadata": {
                    "job_id": "55555"
                }
            }
        }

        response = client.post(
            "/webhook/retell",
            json=payload
        )

        assert response.status_code == 200
        call_args = mock_post_note.call_args
        note_text = call_args[0][1]
        assert "Recording URL: Not available" in note_text

    @patch('app.webhooks.retell_webhook.RETELL_API_KEY', None)
    @patch('app.webhooks.retell_webhook.post_call_note_to_job', new_callable=AsyncMock)
    def test_call_analyzed_with_null_summary(self, mock_post_note):
        """Test call_analyzed with null call_summary shows 'No summary available'."""
        mock_post_note.return_value = True

        payload = {
            "event": "call_analyzed",
            "data": {
                "call_id": "test-call-null-summary",
                "transcript": "Some transcript text",
                "call_analysis": {
                    "call_summary": None
                },
                "metadata": {
                    "job_id": "66666"
                }
            }
        }

        response = client.post(
            "/webhook/retell",
            json=payload
        )

        assert response.status_code == 200
        call_args = mock_post_note.call_args
        note_text = call_args[0][1]
        assert "No summary available" in note_text

    @patch('app.webhooks.retell_webhook.RETELL_API_KEY', None)
    @patch('app.webhooks.retell_webhook.post_call_note_to_job', new_callable=AsyncMock)
    def test_servicetitan_failure_still_returns_200(self, mock_post_note):
        """Test that ServiceTitan note posting failure still returns HTTP 200."""
        mock_post_note.return_value = False  # Simulate failure

        payload = {
            "event": "call_ended",
            "data": {
                "call_id": "test-call-st-fail",
                "recording_url": "https://example.com/recording.mp3",
                "duration_ms": 60000,
                "start_timestamp": 1713456789000,
                "metadata": {
                    "job_id": "77777"
                }
            }
        }

        response = client.post(
            "/webhook/retell",
            json=payload
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
