"""Test credential rotation and secret hygiene."""

import os
from unittest.mock import patch

from auth import _bff_api_key_matches, api_key_matches
from program.settings import settings_manager


def test_credential_rotation_legacy_api_key():
    """Verify old compromised key fails and rotated key succeeds."""
    old_compromised_key = "12345678901234567890123456789012"
    new_rotated_key = "d5af2b85c976d4ecf822b8374db383b3"

    with patch.object(settings_manager.settings, "api_key", new_rotated_key):
        # Old compromised key MUST fail authentication
        assert api_key_matches(old_compromised_key) is False
        assert api_key_matches("local-legacy-api-key-32characters") is False
        assert api_key_matches("") is False
        assert api_key_matches(None) is False

        # Rotated key MUST succeed
        assert api_key_matches(new_rotated_key) is True


def test_credential_rotation_bff_api_key():
    """Verify old BFF key fails and rotated BFF key succeeds."""
    old_compromised_bff = "local-bff-api-key-32characters"
    new_rotated_bff = "a52e1fff1dabda1098140149d2e48284"

    with patch.dict(os.environ, {"BFF_API_KEY": new_rotated_bff}, clear=False):
        # Old key MUST fail
        assert _bff_api_key_matches(old_compromised_bff) is False
        assert _bff_api_key_matches("") is False
        assert _bff_api_key_matches(None) is False

        # Rotated key MUST succeed
        assert _bff_api_key_matches(new_rotated_bff) is True
