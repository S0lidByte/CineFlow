"""Unit tests for declarative state matrix and normalized provider errors."""

from __future__ import annotations

import pytest

from program.contracts.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNetworkError,
    ProviderQuotaExceededError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from program.media.state import States
from program.media.state_matrix import (
    ALLOWED_TRANSITIONS,
    can_transition,
    is_active_state,
    is_retryable_state,
    is_terminal_state,
)


class TestStateMatrix:
    def test_all_states_represented_in_allowed_transitions(self):
        for state in States:
            assert state in ALLOWED_TRANSITIONS, f"Missing {state} in ALLOWED_TRANSITIONS"
            assert isinstance(ALLOWED_TRANSITIONS[state], frozenset)

    def test_can_transition_valid_paths(self):
        assert can_transition(States.Requested, States.Indexed) is True
        assert can_transition(States.Indexed, States.Scraped) is True
        assert can_transition(States.Scraped, States.Downloaded) is True
        assert can_transition(States.Downloaded, States.Completed) is True
        assert can_transition(States.Completed, States.Requested) is True

    def test_can_transition_invalid_paths(self):
        assert can_transition(States.Symlinked, States.Indexed) is False

    def test_state_classifications(self):
        assert is_terminal_state(States.Completed) is True
        assert is_terminal_state(States.Failed) is True
        assert is_terminal_state(States.Paused) is True
        assert is_terminal_state(States.Indexed) is False

        assert is_active_state(States.Requested) is True
        assert is_active_state(States.Scraped) is True
        assert is_active_state(States.Completed) is False

        assert is_retryable_state(States.Failed) is True
        assert is_retryable_state(States.Ongoing) is True
        assert is_retryable_state(States.Requested) is True


class TestProviderErrors:
    def test_provider_error_base(self):
        err = ProviderError("connection reset", provider_name="RealDebrid", is_transient=True)
        assert str(err) == "[RealDebrid] connection reset"
        assert err.is_transient is True

    def test_provider_auth_error(self):
        err = ProviderAuthError("Invalid API key", provider_name="Torbox")
        assert err.is_transient is False
        assert err.status_code == 401
        assert "Torbox" in str(err)

    def test_provider_rate_limit_error(self):
        err = ProviderRateLimitError("Too Many Requests", provider_name="Zilean", retry_after_seconds=30.0)
        assert err.is_transient is True
        assert err.retry_after_seconds == 30.0
        assert err.status_code == 429

    def test_provider_network_and_unavailable_errors(self):
        net_err = ProviderNetworkError("DNS resolution failed", provider_name="Prowlarr")
        assert net_err.is_transient is True

        unavail_err = ProviderUnavailableError("503 Service Unavailable", provider_name="AllDebrid")
        assert unavail_err.is_transient is True
        assert unavail_err.status_code == 503

    def test_provider_quota_exceeded_error(self):
        quota_err = ProviderQuotaExceededError("Account points expired", provider_name="Premiumize")
        assert quota_err.is_transient is False
