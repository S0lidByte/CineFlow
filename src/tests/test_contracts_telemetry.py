"""Unit tests for CineFlow telemetry, correlation context, and secret redaction contracts."""

from __future__ import annotations

import asyncio
import time

import pytest

from program.contracts.events import (
    OperationEvent,
    OperationStatus,
    OperationType,
    create_operation_event,
)
from program.contracts.telemetry import (
    generate_correlation_id,
    get_correlation_id,
    is_sensitive_key,
    redact_sensitive_data,
    redact_text,
    reset_correlation_id,
    set_correlation_id,
)


class TestTelemetryAndCorrelation:
    def test_generate_correlation_id(self):
        cid1 = generate_correlation_id()
        cid2 = generate_correlation_id()
        assert cid1 != cid2
        assert len(cid1) >= 32

    def test_get_and_set_correlation_id(self):
        custom_id = "test-custom-correlation-12345"
        token = set_correlation_id(custom_id)
        try:
            assert get_correlation_id() == custom_id
        finally:
            reset_correlation_id(token)

    @pytest.mark.asyncio
    async def test_correlation_id_async_context_isolation(self):
        async def task_worker(assigned_id: str) -> str:
            token = set_correlation_id(assigned_id)
            await asyncio.sleep(0.01)
            active_id = get_correlation_id()
            reset_correlation_id(token)
            return active_id

        res1, res2 = await asyncio.gather(
            task_worker("task-alpha-1111"),
            task_worker("task-beta-2222"),
        )
        assert res1 == "task-alpha-1111"
        assert res2 == "task-beta-2222"


class TestSecretRedaction:
    def test_is_sensitive_key(self):
        assert is_sensitive_key("api_key") is True
        assert is_sensitive_key("APIKEY") is True
        assert is_sensitive_key("auth_token") is True
        assert is_sensitive_key("secret") is True
        assert is_sensitive_key("client_secret") is True
        assert is_sensitive_key("password") is True
        assert is_sensitive_key("session_token") is True
        assert is_sensitive_key("private_key") is True
        assert is_sensitive_key("movie_title") is False
        assert is_sensitive_key("user_name") is False
        assert is_sensitive_key("status") is False

    def test_redact_text_bearer_and_query_tokens(self):
        raw_bearer = "Authorization: Bearer myverysecrettoken1234567890abcdef"
        redacted_bearer = redact_text(raw_bearer)
        assert "myverysecrettoken1234567890abcdef" not in redacted_bearer
        assert "Bearer [REDACTED]" in redacted_bearer

        raw_url = "https://indexer.example.com/api?apikey=abcdef1234567890abcdef1234567890&query=batman"
        redacted_url = redact_text(raw_url)
        assert "abcdef1234567890abcdef1234567890" not in redacted_url
        assert "apikey=[REDACTED]" in redacted_url
        assert "query=batman" in redacted_url

    def test_redact_sensitive_data_nested_structures(self):
        payload = {
            "api_key": "12345678901234567890123456789012",
            "provider": "RealDebrid",
            "nested": {
                "token": "secret-token-xyz-12345678",
                "normal_field": "visible",
                "items": [
                    {"password": "mypassword123", "id": 101},
                    {"token_name": "allowed_name", "secret_value": "hide_me"},
                ],
            },
            "unrelated_int": 42,
            "tuple_data": ("safe", "Bearer 12345678901234567890"),
        }

        redacted = redact_sensitive_data(payload)

        assert redacted["api_key"] == "[REDACTED]"
        assert redacted["provider"] == "RealDebrid"
        assert redacted["nested"]["token"] == "[REDACTED]"
        assert redacted["nested"]["normal_field"] == "visible"
        assert redacted["nested"]["items"][0]["password"] == "[REDACTED]"
        assert redacted["nested"]["items"][0]["id"] == 101
        assert redacted["nested"]["items"][1]["secret_value"] == "[REDACTED]"
        assert redacted["unrelated_int"] == 42
        assert "12345678901234567890" not in str(redacted["tuple_data"])

    def test_redaction_does_not_leak_data_beyond_depth_limit(self):
        secret = "depth-boundary-token-1234567890"
        payload: dict[str, object] = {"level": {"level": {"level": {"token": secret}}}}

        redacted = redact_sensitive_data(payload, max_depth=3)

        assert secret not in str(redacted)
        assert redacted["level"]["level"]["level"] == "[REDACTED]"

    def test_redaction_terminates_for_cyclic_payloads(self):
        secret = "cycle-token-1234567890"
        payload: dict[str, object] = {"value": "safe"}
        payload["self"] = payload
        payload["nested"] = {"token": secret}

        redacted = redact_sensitive_data(payload)

        assert redacted["value"] == "safe"
        assert redacted["self"] == "[REDACTED]"
        assert redacted["nested"]["token"] == "[REDACTED]"
        assert secret not in str(redacted)


class TestOperationEvents:
    def test_create_and_complete_operation_event(self):
        event = create_operation_event(
            operation_type=OperationType.SCRAPE,
            item_id=99,
            title="Inception (2010)",
            metadata={"indexer": "torrentio", "api_key": "test_dummy_key_value"},
        )
        assert event.operation_type == OperationType.SCRAPE
        assert event.status == OperationStatus.RUNNING
        assert event.item_id == 99
        assert event.title == "Inception (2010)"
        assert event.duration_ms is None

        time.sleep(0.01)
        event.mark_completed(status=OperationStatus.SUCCESS)

        assert event.status == OperationStatus.SUCCESS
        assert event.duration_ms is not None
        assert event.duration_ms >= 0.0

        d = event.to_dict(redact_secrets=True)
        assert d["status"] == "success"
        assert d["operation_type"] == "scrape"
        assert d["metadata"]["api_key"] == "[REDACTED]"
        assert d["metadata"]["indexer"] == "torrentio"
