"""
Unit tests for the Warmbly inbound webhook HTTP endpoint.

Covers signature validation and the webhook.test challenge/response handshake
Warmbly uses to verify an endpoint before it will deliver real events.
"""

import asyncio
import hashlib
import hmac
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from flow.warmbly_http_endpoint import (
    CHALLENGE_FIELD,
    CHALLENGE_HEADER,
    SIGNATURE_ALGORITHM,
    SIGNATURE_HEADER,
    app,
    detect_challenge,
    route_payload,
)


CHALLENGE = 'whcg_ab804143791fd7323187e2c65975b550cd0aa6b33818f8a2'

TEST_EVENT = {
    "id": "20549cb9-e9b8-4902-bd3a-618c11fe021b",
    "event_type": "webhook.test",
    "organization_id": "00e33e77-6d57-4e2f-a0cd-e0bc66afd774",
    "created_at": "2026-09-30T04:19:45.085088351Z",
    "data": {
        "message": (
            "Echo the challenge value (in the body or the "
            "X-Warmbly-Webhook-Challenge header) to verify this endpoint."
        ),
        CHALLENGE_FIELD: CHALLENGE,
        "endpoint_id": "51f8b950-d2e2-4abb-87b4-c57b3f8bea34",
    },
}

REAL_EVENT = {
    "id": "aa11bb22-cc33-dd44-ee55-ff6677889900",
    "event_type": "contact.created",
    "organization_id": "00e33e77-6d57-4e2f-a0cd-e0bc66afd774",
    "created_at": "2026-09-30T04:30:00.000000000Z",
    "data": {"contact_id": "con_abc123", "email": "owner@example.com"},
}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def no_webhook_secret():
    """Run without a signing secret unless a test sets one."""
    original = os.environ.pop('WARMBLY_WEBHOOK_SECRET', None)
    yield
    if original is not None:
        os.environ['WARMBLY_WEBHOOK_SECRET'] = original


class TestDetectChallenge:
    """detect_challenge pulls the verification value out of a payload."""

    def test_finds_challenge_in_data(self):
        assert detect_challenge(TEST_EVENT) == CHALLENGE

    def test_returns_none_for_real_event(self):
        assert detect_challenge(REAL_EVENT) is None

    def test_tolerates_missing_data(self):
        assert detect_challenge({"event_type": "contact.created"}) is None

    def test_tolerates_null_data(self):
        assert detect_challenge({"event_type": "x", "data": None}) is None

    def test_tolerates_non_dict_data(self):
        assert detect_challenge({"event_type": "x", "data": "nope"}) is None

    def test_empty_challenge_is_not_a_challenge(self):
        assert detect_challenge({"data": {CHALLENGE_FIELD: ""}}) is None


class TestRoutePayload:
    """route_payload dispatches a validated payload to the right handler."""

    def test_challenge_payload_routes_to_echo(self):
        response = asyncio.run(route_payload(TEST_EVENT))

        assert response.body.decode() == CHALLENGE
        assert response.headers[CHALLENGE_HEADER] == CHALLENGE

    def test_real_payload_routes_to_research(self):
        with patch(
            'flow.warmbly_http_endpoint.trigger_research_flow',
            new_callable=AsyncMock,
        ) as mock_trigger:
            mock_trigger.return_value = {"status": "queued"}
            result = asyncio.run(route_payload(REAL_EVENT))

        mock_trigger.assert_called_once_with(REAL_EVENT)
        assert result['status'] == "accepted"
        assert result['routing'] == {"status": "queued"}


class TestChallengeEcho:
    """Warmbly verifies an endpoint by asking it to echo a challenge value."""

    def test_challenge_is_echoed_in_body(self, client):
        """The response body must be exactly the challenge value."""
        response = client.post("/webhooks/warmbly", json=TEST_EVENT)

        assert response.status_code == 200
        assert response.text == CHALLENGE

    def test_challenge_is_echoed_in_header(self, client):
        """The response must also carry X-Warmbly-Webhook-Challenge."""
        response = client.post("/webhooks/warmbly", json=TEST_EVENT)

        assert response.headers[CHALLENGE_HEADER] == CHALLENGE

    def test_challenge_event_does_not_trigger_research(self, client):
        """A verification ping is not a real event; don't run the pipeline."""
        with patch(
            'flow.warmbly_http_endpoint.trigger_research_flow',
            new_callable=AsyncMock,
        ) as mock_trigger:
            client.post("/webhooks/warmbly", json=TEST_EVENT)

        mock_trigger.assert_not_called()

    def test_challenge_echoed_regardless_of_event_type(self, client):
        """Echo any payload carrying data.challenge, not only webhook.test."""
        payload = dict(REAL_EVENT)
        payload['data'] = {CHALLENGE_FIELD: CHALLENGE}

        response = client.post("/webhooks/warmbly", json=payload)

        assert response.text == CHALLENGE


class TestRealEvents:
    """Events without a challenge route to the research pipeline as before."""

    def test_real_event_returns_accepted_envelope(self, client):
        with patch(
            'flow.warmbly_http_endpoint.trigger_research_flow',
            new_callable=AsyncMock,
        ) as mock_trigger:
            mock_trigger.return_value = {"status": "queued"}
            response = client.post("/webhooks/warmbly", json=REAL_EVENT)

        assert response.status_code == 200
        body = response.json()
        assert body['status'] == "accepted"
        assert body['event_type'] == "contact.created"

    def test_real_event_triggers_research(self, client):
        with patch(
            'flow.warmbly_http_endpoint.trigger_research_flow',
            new_callable=AsyncMock,
        ) as mock_trigger:
            mock_trigger.return_value = {"status": "queued"}
            client.post("/webhooks/warmbly", json=REAL_EVENT)

        mock_trigger.assert_called_once()

    def test_no_challenge_header_on_real_event(self, client):
        with patch(
            'flow.warmbly_http_endpoint.trigger_research_flow',
            new_callable=AsyncMock,
        ) as mock_trigger:
            mock_trigger.return_value = {"status": "queued"}
            response = client.post("/webhooks/warmbly", json=REAL_EVENT)

        assert CHALLENGE_HEADER not in response.headers


class TestSignatureValidation:
    """Signature checking must still apply to challenge requests."""

    def test_challenge_rejected_without_valid_signature(self, client):
        os.environ['WARMBLY_WEBHOOK_SECRET'] = 'test_secret'

        response = client.post("/webhooks/warmbly", json=TEST_EVENT)

        assert response.status_code == 401

    def test_challenge_accepted_with_valid_signature(self, client):
        secret = 'test_secret'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = secret
        body = json.dumps(TEST_EVENT).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        response = client.post(
            "/webhooks/warmbly",
            content=body,
            headers={
                'Content-Type': 'application/json',
                SIGNATURE_HEADER: f'{SIGNATURE_ALGORITHM}={sig}',
            },
        )

        assert response.status_code == 200
        assert response.text == CHALLENGE

    def test_malformed_json_rejected(self, client):
        response = client.post(
            "/webhooks/warmbly",
            content=b'not json',
            headers={'Content-Type': 'application/json'},
        )

        assert response.status_code == 400
