"""Test credential rotation and secret hygiene."""

import os
import secrets
from unittest.mock import patch

from auth import _bff_api_key_matches, api_key_matches
from program.settings import settings_manager


def test_credential_rotation_legacy_api_key():
    """Verify revoked key fails and rotated key succeeds."""
    mock_revoked_key = "revoked-test-key-000000000000000"
    mock_rotated_key = secrets.token_hex(16)

    with patch.object(settings_manager.settings, "api_key", mock_rotated_key):
        # Revoked / unauthorized keys MUST fail authentication
        assert api_key_matches(mock_revoked_key) is False
        assert api_key_matches("local-legacy-api-key-32characters") is False
        assert api_key_matches("") is False
        assert api_key_matches(None) is False

        # Rotated key MUST succeed
        assert api_key_matches(mock_rotated_key) is True


def test_credential_rotation_bff_api_key():
    """Verify revoked BFF key fails and rotated BFF key succeeds."""
    mock_revoked_bff = "revoked-test-bff-000000000000000"
    mock_rotated_bff = secrets.token_hex(16)

    with patch.dict(os.environ, {"BFF_API_KEY": mock_rotated_bff}, clear=False):
        # Revoked / unauthorized BFF keys MUST fail
        assert _bff_api_key_matches(mock_revoked_bff) is False
        assert _bff_api_key_matches("") is False
        assert _bff_api_key_matches(None) is False

        # Rotated key MUST succeed
        assert _bff_api_key_matches(mock_rotated_bff) is True
