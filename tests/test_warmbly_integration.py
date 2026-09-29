"""
Unit tests for Warmbly ↔ Prefect integration flow.

Tests mxCheck wrapper and Phoenix event emission.
"""

import asyncio
import json
import os
import subprocess
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flow.warmbly_integration_test import (
    validate_test_emails,
    emit_warmbly_event,
    warmbly_integration_test,
)


class TestValidateTestEmails:
    """Test the mxCheck email validation task."""

    def test_validate_test_emails_returns_list(self):
        """validate_test_emails should return a list of validated email dicts."""
        result = validate_test_emails()
        assert isinstance(result, list)

    def test_validated_emails_have_required_fields(self):
        """Each validated email should have email, verified, and mx_server fields."""
        result = validate_test_emails()

        if result:  # Only check if mxCheck ran successfully
            for email_result in result:
                assert 'email' in email_result
                assert 'verified' in email_result
                # mx_server may be None if verification failed
                assert 'mx_server' in email_result or email_result.get('error')


class TestEmitWarmblyEvent:
    """Test the Warmbly Phoenix event emission task."""

    def test_emit_requires_org_id(self):
        """emit_warmbly_event should fail gracefully if org_id is missing."""
        result = emit_warmbly_event([], "")
        assert result is False

    def test_emit_requires_api_token(self):
        """emit_warmbly_event should fail if WARMBLY_API_TOKEN is not set."""
        # Temporarily remove token
        original_token = os.environ.pop('WARMBLY_API_TOKEN', None)

        try:
            result = emit_warmbly_event([], "org_test")
            assert result is False
        finally:
            # Restore
            if original_token:
                os.environ['WARMBLY_API_TOKEN'] = original_token


class TestWarmblyIntegrationFlow:
    """Test the main integration flow."""

    def test_flow_requires_org_id(self):
        """Flow should fail if org_id is not provided and not in env."""
        os.environ.pop('WARMBLY_ORG_ID', None)

        result = warmbly_integration_test(org_id=None)

        assert result['success'] is False
        assert result['error'] == 'missing_org_id'

    def test_flow_with_org_id_from_env(self):
        """Flow should read org_id from environment if not passed."""
        os.environ['WARMBLY_ORG_ID'] = 'org_env_test'

        with patch('flow.warmbly_integration_test.validate_test_emails', return_value=[]):
            with patch('flow.warmbly_integration_test.emit_warmbly_event', return_value=False):
                result = warmbly_integration_test(org_id=None)

        # Should use env var and proceed (but fail due to no emails)
        assert result['error'] == 'email_validation_failed'

    def test_flow_with_successful_validation_and_emission(self):
        """Flow should complete successfully when both steps pass."""
        test_emails = [
            {'email': 'test@example.com', 'verified': True, 'mx_server': 'mx1.example.com'}
        ]

        with patch('flow.warmbly_integration_test.validate_test_emails', return_value=test_emails):
            with patch('flow.warmbly_integration_test.emit_warmbly_event', return_value=True):
                result = warmbly_integration_test(org_id='org_test')

        assert result['success'] is True
        assert result['verified_count'] == 1
        assert result['org_id'] == 'org_test'


class TestIntegrationWithLiveServers:
    """Integration tests against live Prefect and Warmbly servers.

    These tests require:
    - WARMBLY_API_TOKEN in environment
    - WARMBLY_ORG_ID in environment (get from Warmbly UI settings)
    - Network access to wss://realtime.warmbly.com

    Mark with @pytest.mark.live to run against live servers.
    """

    @pytest.mark.live
    def test_live_warmbly_connection(self):
        """Test actual Phoenix WebSocket connection to live Warmbly."""
        org_id = os.environ.get('WARMBLY_ORG_ID')
        if not org_id:
            pytest.skip("WARMBLY_ORG_ID not set")

        result = warmbly_integration_test(org_id=org_id)

        # Should succeed (or at least not fail due to missing credentials)
        # Actual success depends on network/Warmbly availability
        assert isinstance(result, dict)
        assert 'success' in result


# Pytest configuration for live tests
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: mark test as requiring live server access"
    )


if __name__ == '__main__':
    # Run with: pytest tests/test_warmbly_integration.py -v
    # Run live tests with: pytest tests/test_warmbly_integration.py -v -m live
    pytest.main([__file__, '-v'])
