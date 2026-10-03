"""
Unit tests for the Warmbly inbound webhook HTTP endpoint.

Covers signature validation and the webhook.test challenge/response handshake
Warmbly uses to verify an endpoint before it will deliver real events.
"""

import asyncio
import hashlib
import time
import hmac
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from flow.warmbly_http_endpoint import (
    CHALLENGE_FIELD,
    CHALLENGE_HEADER,
    SIGNATURE_HEADER,
    REDACTED,
    SIGNATURE_HEADER,
    SIGNATURE_TIMESTAMP_KEY,
    SIGNATURE_VERSION_KEY,
    app,
    build_signed_payload,
    describe_request,
    describe_signature_check,
    detect_challenge,
    parse_signature_header,
    probe_signature_schemes,
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

    def _header(self, secret, body):
        ts = str(int(time.time()))
        signed = build_signed_payload(ts, body)
        digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return f'{SIGNATURE_TIMESTAMP_KEY}={ts},{SIGNATURE_VERSION_KEY}={digest}'

    def test_challenge_rejected_without_valid_signature(self, client):
        os.environ['WARMBLY_WEBHOOK_SECRET'] = 'test_secret'

        response = client.post("/webhooks/warmbly", json=TEST_EVENT)

        assert response.status_code == 401

    def test_challenge_accepted_with_valid_signature(self, client):
        secret = 'test_secret'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = secret
        body = json.dumps(TEST_EVENT).encode()

        response = client.post(
            "/webhooks/warmbly",
            content=body,
            headers={
                'Content-Type': 'application/json',
                SIGNATURE_HEADER: self._header(secret, body),
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


class TestDescribeSignatureCheckBasics:
    """Cases that do not depend on the signing scheme."""

    def test_reports_when_no_secret_configured(self):
        report = describe_signature_check(b'{}', None)

        assert report['secret_configured'] is False
        assert report['matched'] is True

    def test_reports_missing_header(self):
        os.environ['WARMBLY_WEBHOOK_SECRET'] = 'test_secret'

        report = describe_signature_check(b'{}', None)

        assert report['header_present'] is False
        assert report['matched'] is False

    def test_reports_body_length(self):
        os.environ['WARMBLY_WEBHOOK_SECRET'] = 'test_secret'

        report = describe_signature_check(b'12345', 't=1,v1=abc')

        assert report['body_bytes'] == 5


class TestProbeSignatureSchemes:
    """The probe confirms which payload form the remote actually signs."""

    def test_identifies_dot_separated_timestamp_scheme(self):
        secret, ts, body = 'test_secret', '1791037655', b'{"a":1}'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = secret
        digest = hmac.new(
            secret.encode(), f'{ts}.'.encode() + body, hashlib.sha256
        ).hexdigest()

        matches = probe_signature_schemes(body, f't={ts},v1={digest}')

        assert 'hex(t.body)' in matches

    def test_identifies_unseparated_timestamp_scheme(self):
        secret, ts, body = 'test_secret', '1791037655', b'{"a":1}'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = secret
        digest = hmac.new(
            secret.encode(), ts.encode() + body, hashlib.sha256
        ).hexdigest()

        matches = probe_signature_schemes(body, f't={ts},v1={digest}')

        assert 'hex(t+body)' in matches

    def test_identifies_raw_body_scheme(self):
        secret, body = 'test_secret', b'{"a":1}'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = secret
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        matches = probe_signature_schemes(body, f't=1791037655,v1={digest}')

        assert 'hex(body)' in matches

    def test_identifies_base64_digest(self):
        import base64

        secret, ts, body = 'test_secret', '1791037655', b'{"a":1}'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = secret
        raw = hmac.new(
            secret.encode(), f'{ts}.'.encode() + body, hashlib.sha256
        ).digest()
        digest = base64.b64encode(raw).decode()

        matches = probe_signature_schemes(body, f't={ts},v1={digest}')

        assert 'base64(t.body)' in matches

    def test_returns_empty_when_nothing_matches(self):
        os.environ['WARMBLY_WEBHOOK_SECRET'] = 'test_secret'

        matches = probe_signature_schemes(b'{"a":1}', 't=1,v1=deadbeef')

        assert matches == []

    def test_returns_empty_without_a_secret(self):
        assert probe_signature_schemes(b'{"a":1}', 't=1,v1=deadbeef') == []


class TestDescribeRequest:
    """describe_request dumps headers for debugging, redacting credentials."""

    def test_includes_header_names(self):
        report = describe_request({'x-warmbly-signature': 'sha256=abc'}, b'{}')

        assert 'x-warmbly-signature' in report['headers']

    def test_redacts_authorization(self):
        report = describe_request({'authorization': 'Bearer hunter2'}, b'{}')

        assert 'hunter2' not in json.dumps(report)
        assert report['headers']['authorization'] == REDACTED

    def test_redacts_cookies(self):
        report = describe_request({'cookie': 'session=abc123'}, b'{}')

        assert 'abc123' not in json.dumps(report)

    def test_includes_body_preview(self):
        report = describe_request({}, b'{"event_type":"webhook.test"}')

        assert 'webhook.test' in report['body_preview']


class TestParseSignatureHeader:
    """Warmbly sends 't=<unix>,v1=<hex>' (Stripe-style elements)."""

    def test_parses_timestamp_and_digest(self):
        parsed = parse_signature_header('t=1791037655,v1=abc123')

        assert parsed['t'] == ['1791037655']
        assert parsed['v1'] == ['abc123']

    def test_parses_real_warmbly_header(self):
        header = (
            't=1791037655,'
            'v1=eccdf01d65769ad4d2ce3883df84e1b2b1ce9b486b256e914b4c213deade5c85'
        )

        parsed = parse_signature_header(header)

        assert parsed[SIGNATURE_TIMESTAMP_KEY] == ['1791037655']
        assert len(parsed[SIGNATURE_VERSION_KEY][0]) == 64

    def test_collects_multiple_digests_for_key_rotation(self):
        parsed = parse_signature_header('t=1,v1=aaa,v1=bbb')

        assert parsed['v1'] == ['aaa', 'bbb']

    def test_tolerates_whitespace(self):
        parsed = parse_signature_header(' t=1 , v1=abc ')

        assert parsed['t'] == ['1']
        assert parsed['v1'] == ['abc']

    def test_ignores_elements_without_a_value(self):
        parsed = parse_signature_header('t=1,garbage,v1=abc')

        assert 'garbage' not in parsed
        assert parsed['v1'] == ['abc']

    def test_keeps_base64_padding_in_value(self):
        """A value may itself contain '=', so only split on the first one."""
        parsed = parse_signature_header('t=1,v1=YWJjZA==')

        assert parsed['v1'] == ['YWJjZA==']


class TestBuildSignedPayload:
    """Warmbly signs '<timestamp>.<raw body>'."""

    def test_prefixes_timestamp_with_dot_separator(self):
        assert build_signed_payload('123', b'{"a":1}') == b'123.{"a":1}'

    def test_falls_back_to_raw_body_without_a_timestamp(self):
        assert build_signed_payload(None, b'{"a":1}') == b'{"a":1}'


class TestWarmblySignatureValidation:
    """End-to-end signature checking against the real header format."""

    def _sign(self, secret, timestamp, body):
        signed = build_signed_payload(timestamp, body)
        return hmac.new(secret.encode(), signed,
                        hashlib.sha256).hexdigest()

    def test_accepts_correct_timestamped_signature(self):
        secret, ts, body = 'test_secret', '1791037655', b'{"a":1}'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = secret
        digest = self._sign(secret, ts, body)

        report = describe_signature_check(body, f't={ts},v1={digest}')

        assert report['matched'] is True
        assert report['timestamp'] == ts
        assert report['signed_payload_form'] == 't.body'

    def test_rejects_wrong_secret(self):
        ts, body = '1791037655', b'{"a":1}'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = 'the_right_one'
        digest = self._sign('the_wrong_one', ts, body)

        report = describe_signature_check(body, f't={ts},v1={digest}')

        assert report['matched'] is False
        assert 'mismatch' in report['reason']

    def test_accepts_when_any_rotated_digest_matches(self):
        secret, ts, body = 'test_secret', '1791037655', b'{"a":1}'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = secret
        good = self._sign(secret, ts, body)

        report = describe_signature_check(
            body, f't={ts},v1=deadbeef,v1={good}'
        )

        assert report['matched'] is True

    def test_reports_missing_version_element(self):
        os.environ['WARMBLY_WEBHOOK_SECRET'] = 'test_secret'

        report = describe_signature_check(b'{}', 't=1791037655')

        assert report['matched'] is False
        assert SIGNATURE_VERSION_KEY in report['reason']

    def test_never_leaks_the_secret(self):
        secret = 'super_secret_value'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = secret

        report = describe_signature_check(b'{}', 't=1,v1=deadbeef')

        assert secret not in json.dumps(report)


class TestSignatureAge:
    """The timestamp enables replay detection; enforcement is opt-in."""

    def _fresh_header(self, secret, body, age_seconds=0):
        ts = str(int(time.time()) - age_seconds)
        signed = build_signed_payload(ts, body)
        digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return f't={ts},v1={digest}'

    def test_reports_age_in_seconds(self):
        secret, body = 'test_secret', b'{"a":1}'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = secret
        header = self._fresh_header(secret, body, age_seconds=120)

        report = describe_signature_check(body, header)

        assert report['matched'] is True
        assert 110 <= report['timestamp_age_seconds'] <= 130

    def test_old_signature_accepted_when_max_age_unset(self):
        secret, body = 'test_secret', b'{"a":1}'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = secret
        os.environ.pop('WARMBLY_SIGNATURE_MAX_AGE', None)
        header = self._fresh_header(secret, body, age_seconds=99999)

        report = describe_signature_check(body, header)

        assert report['matched'] is True

    def test_old_signature_rejected_when_max_age_set(self):
        secret, body = 'test_secret', b'{"a":1}'
        os.environ['WARMBLY_WEBHOOK_SECRET'] = secret
        header = self._fresh_header(secret, body, age_seconds=600)

        try:
            report = describe_signature_check(
                body, header, max_age_seconds=300
            )
        finally:
            os.environ.pop('WARMBLY_SIGNATURE_MAX_AGE', None)

        assert report['matched'] is False
        assert 'old' in report['reason']

    def test_unparseable_timestamp_reports_no_age(self):
        os.environ['WARMBLY_WEBHOOK_SECRET'] = 'test_secret'

        report = describe_signature_check(b'{}', 't=not-a-number,v1=abc')

        assert report['timestamp_age_seconds'] is None
